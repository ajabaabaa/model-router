"""Installed Ollama models, for the Control Center's local-model picker.

Asked of the local Ollama server only (loopback is enforced, so nothing leaves this machine).
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlparse

import httpx

DEFAULT_BASE = "http://127.0.0.1:11434"
TTL_SECONDS = 15
EMBED_FAMILIES = {"bert", "nomic-bert", "xlm-roberta"}
_cache: dict[str, Any] = {"at": 0.0, "base": None, "models": None, "error": None}


def is_loopback(url: str) -> bool:
    try:
        return (urlparse(url).hostname or "").lower() in ("127.0.0.1", "localhost", "::1")
    except ValueError:
        return False


def native_base(config: dict[str, Any]) -> str:
    """The OpenAI-compatible base (".../v1") and Ollama's native API share a host."""
    block = config.get("ollama") if isinstance(config.get("ollama"), dict) else {}
    base = str(block.get("base_url") or DEFAULT_BASE).rstrip("/")
    return base[:-3] if base.endswith("/v1") else base


def normalise(raw: dict[str, Any]) -> dict[str, Any] | None:
    name = raw.get("name") or raw.get("model")
    if not isinstance(name, str) or not name:
        return None
    details = raw.get("details") if isinstance(raw.get("details"), dict) else {}
    family = str(details.get("family") or "")
    size = raw.get("size") if isinstance(raw.get("size"), int) else None
    embedding = family.lower() in EMBED_FAMILIES or "embed" in name.lower() or "bge-" in name.lower()
    return {"id": name, "family": family[:40], "params": str(details.get("parameter_size") or "")[:20],
            "quant": str(details.get("quantization_level") or "")[:20],
            "size_gb": round(size / 1e9, 1) if size else None, "embedding": embedding}


def load(config: dict[str, Any], force: bool = False) -> tuple[list[dict[str, Any]] | None, dict[str, Any]]:
    """Models plus status {ok, error, base}. Never raises."""
    base = native_base(config)
    if not is_loopback(base):
        return None, {"ok": False, "error": "base_url must be a loopback address", "base": base}
    now = time.time()
    if not force and _cache["models"] is not None and _cache["base"] == base and now - _cache["at"] < TTL_SECONDS:
        return _cache["models"], {"ok": True, "error": None, "base": base}
    try:
        response = httpx.get(base + "/api/tags", timeout=3.0)
        response.raise_for_status()
        models = [m for m in map(normalise, response.json().get("models") or []) if m]
        _cache.update(at=now, base=base, models=models, error=None)
        return models, {"ok": True, "error": None, "base": base}
    except Exception as exc:  # Ollama not running, wrong port, bad JSON
        _cache["error"] = exc.__class__.__name__
        return None, {"ok": False, "error": f"Ollama not reachable ({exc.__class__.__name__})", "base": base}
