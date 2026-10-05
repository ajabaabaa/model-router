"""Safe writer for router_config.json — the only way the Control Center changes routing.

app.py re-reads the config on every request, so a successful write is live on the next call.
That makes a bad write immediately harmful, so every write here:

  * refuses if the file changed since the caller last read it (hash check, HTTP 409),
  * validates the result before touching disk,
  * keeps a timestamped backup of the previous file and prunes old backups,
  * writes atomically (temp file + replace), and
  * appends one audit line (who/what/when, never a credential or any content).

The router stores only the *name* of the environment variable holding the API key, never the
key, and the audit log records changed keys, not values it was not asked to log.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "router_config.json"
BACKUP_DIR = ROOT / "router_config.backups"
AUDIT_PATH = ROOT / "control_audit.jsonl"
KEEP_BACKUPS = 60

_lock = threading.Lock()


class ConfigError(Exception):
    """Raised with an HTTP-ish status so the API layer can answer precisely."""

    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def file_hash(path: Path | None = None) -> str | None:
    try:
        return hashlib.sha256((path or CONFIG_PATH).read_bytes()).hexdigest()[:16]
    except FileNotFoundError:
        return None


def read() -> tuple[dict[str, Any], str | None]:
    """The parsed config plus the hash a later write must present."""
    path = CONFIG_PATH
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError("router_config.json is missing", 404)
    try:
        value = json.loads(raw)
    except ValueError:
        raise ConfigError("router_config.json is not valid JSON; fix it by hand first", 409)
    if not isinstance(value, dict):
        raise ConfigError("router_config.json must be a JSON object", 409)
    return value, hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _is_loopback(url: str) -> bool:
    from urllib.parse import urlparse
    try:
        return (urlparse(url).hostname or "").lower() in ("127.0.0.1", "localhost", "::1")
    except ValueError:
        return False


def validate(config: dict[str, Any]) -> None:
    """Structural checks the router itself relies on. Anything stricter belongs to the caller."""
    tiers = config.get("tiers")
    if not isinstance(tiers, dict) or not tiers:
        raise ConfigError("config needs at least one tier")
    for name, tier in tiers.items():
        if not isinstance(name, str) or not name or "#" in name or "/" in name or " " in name:
            raise ConfigError(f"tier name {name!r} must be non-empty with no '#', '/' or spaces")
        if not isinstance(tier, dict):
            raise ConfigError(f"tier {name!r} must be an object")
        if tier.get("provider") not in ("openrouter", "ollama"):
            raise ConfigError(f"tier {name!r}: provider must be 'openrouter' or 'ollama'")
        if not isinstance(tier.get("primary"), str) or not tier["primary"].strip():
            raise ConfigError(f"tier {name!r} needs a primary model")
        fallbacks = tier.get("fallbacks", [])
        if not isinstance(fallbacks, list) or not all(isinstance(m, str) and m.strip() for m in fallbacks):
            raise ConfigError(f"tier {name!r}: fallbacks must be a list of model ids")
        chain = [tier["primary"], *fallbacks]
        if len(set(chain)) != len(chain):
            raise ConfigError(f"tier {name!r}: a model appears twice in its chain")
        policy = tier.get("provider_policy")
        if policy is not None and not isinstance(policy, dict):
            raise ConfigError(f"tier {name!r}: provider_policy must be an object")
        owner = tier.get("agent")
        if owner is not None and owner != name:
            raise ConfigError(f"tier {name!r}: 'agent' must equal the route's own name")
        if "reasoning_effort" in tier and tier["reasoning_effort"] not in (None, "none", "minimal", "low", "medium", "high"):
            raise ConfigError(f"tier {name!r}: reasoning_effort must be null, none, minimal, low, medium or high")
        floor = tier.get("min_max_tokens")
        if floor is not None and (not isinstance(floor, int) or isinstance(floor, bool) or floor < 1):
            raise ConfigError(f"tier {name!r}: min_max_tokens must be a positive integer")
    fallback_route = config.get("default_route")
    if fallback_route is not None and fallback_route not in tiers:
        raise ConfigError("default_route must name an existing route")
    local = config.get("ollama")
    if local is not None:
        base = local.get("base_url") if isinstance(local, dict) else None
        if not isinstance(local, dict) or (base is not None and not _is_loopback(str(base))):
            raise ConfigError("the 'ollama' block must be an object whose base_url is a loopback address")
    upstream = config.get("openrouter")
    if not isinstance(upstream, dict) or not upstream.get("base_url"):
        raise ConfigError("config needs an 'openrouter' block with a base_url")


def _prune() -> None:
    backups = sorted(BACKUP_DIR.glob("router_config.*.json"))
    for old in backups[:-KEEP_BACKUPS]:
        try:
            old.unlink()
        except OSError:
            pass


def _audit(entry: dict[str, Any]) -> None:
    try:
        with AUDIT_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass  # auditing must never block a legitimate change, but the backup still exists


def changed_paths(before: Any, after: Any, prefix: str = "") -> list[str]:
    """Dotted paths whose value differs. Values are deliberately not returned."""
    if isinstance(before, dict) and isinstance(after, dict):
        out: list[str] = []
        for key in sorted(set(before) | set(after)):
            out += changed_paths(before.get(key), after.get(key), f"{prefix}{key}.")
        return out
    return [] if before == after else [prefix.rstrip(".")]


def update(mutator: Callable[[dict[str, Any]], None], *, base_hash: str | None, action: str,
           actor: str = "control-center") -> dict[str, Any]:
    """Apply ``mutator`` to a copy of the config and persist it safely.

    ``base_hash`` must equal the current file hash (what the page last saw); a mismatch means
    someone edited the file by hand in between, and overwriting it would silently lose that edit.
    """
    with _lock:
        current, current_hash = read()
        if base_hash is None or base_hash != current_hash:
            raise ConfigError("router_config.json changed since you loaded this page; reload and retry", 409)
        updated = json.loads(json.dumps(current))
        mutator(updated)
        validate(updated)
        paths = changed_paths(current, updated)
        if not paths:
            return {"changed": False, "hash": current_hash, "paths": []}
        BACKUP_DIR.mkdir(exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = BACKUP_DIR / f"router_config.{stamp}.json"
        backup.write_bytes(CONFIG_PATH.read_bytes())
        fd, tmp = tempfile.mkstemp(dir=str(ROOT), prefix=".router_config.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps(updated, ensure_ascii=False, indent=2) + "\n")
            os.replace(tmp, CONFIG_PATH)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        new_hash = file_hash()
        _audit({"at": _now(), "actor": actor, "action": action, "paths": paths,
                "backup": backup.name, "hash_before": current_hash, "hash_after": new_hash})
        _prune()
        return {"changed": True, "hash": new_hash, "paths": paths, "backup": backup.name}


def list_backups(limit: int = 20) -> list[dict[str, Any]]:
    if not BACKUP_DIR.exists():
        return []
    files = sorted(BACKUP_DIR.glob("router_config.*.json"), reverse=True)[:limit]
    return [{"name": f.name, "bytes": f.stat().st_size} for f in files]


def restore(name: str, *, base_hash: str | None, actor: str = "control-center") -> dict[str, Any]:
    """Roll back to a named backup. The current file is itself backed up first."""
    if "/" in name or "\\" in name or not name.startswith("router_config.") or not name.endswith(".json"):
        raise ConfigError("not a backup name", 422)
    source = BACKUP_DIR / name
    if not source.exists():
        raise ConfigError("backup not found", 404)
    try:
        snapshot = json.loads(source.read_text(encoding="utf-8"))
    except ValueError:
        raise ConfigError("that backup is not valid JSON", 409)

    def replace_all(cfg: dict[str, Any]) -> None:
        cfg.clear()
        cfg.update(snapshot)

    return update(replace_all, base_hash=base_hash, action=f"restore:{name}", actor=actor)


def read_audit(limit: int = 50) -> list[dict[str, Any]]:
    try:
        lines = AUDIT_PATH.read_text(encoding="utf-8").splitlines()[-limit:]
    except FileNotFoundError:
        return []
    out = []
    for line in reversed(lines):
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out
