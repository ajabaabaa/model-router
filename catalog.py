"""OpenRouter model catalog for the Control Center's model picker.

The public /models listing needs no API key, so nothing secret is sent. Results are cached for
ten minutes; when OpenRouter cannot be reached a stale copy is served and flagged, and the editor
degrades to "unverified" instead of blocking changes.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import httpx

URL = "https://openrouter.ai/api/v1/models"
TTL_SECONDS = 600
_lock = threading.Lock()
_cache: dict[str, Any] = {"at": 0.0, "models": None, "error": None}


def _fetch() -> list[dict[str, Any]]:
    response = httpx.get(URL, timeout=8.0, headers={"Accept": "application/json"})
    response.raise_for_status()
    data = response.json().get("data")
    if not isinstance(data, list):
        raise ValueError("unexpected catalog shape")
    return data


def _per_million(value: Any) -> float | None:
    try:
        number = float(value) * 1_000_000
    except (TypeError, ValueError):
        return None
    return round(number, 4) if number >= 0 else None  # negative = variable-priced router


def normalise(raw: dict[str, Any]) -> dict[str, Any] | None:
    model_id = raw.get("id")
    if not isinstance(model_id, str) or not model_id:
        return None
    pricing = raw.get("pricing") if isinstance(raw.get("pricing"), dict) else {}
    price_in, price_out = _per_million(pricing.get("prompt")), _per_million(pricing.get("completion"))
    params = raw.get("supported_parameters") if isinstance(raw.get("supported_parameters"), list) else []
    arch = raw.get("architecture") if isinstance(raw.get("architecture"), dict) else {}
    return {
        "id": model_id,
        "name": str(raw.get("name") or model_id)[:80],
        "context": raw.get("context_length") if isinstance(raw.get("context_length"), int) else None,
        "in": price_in, "out": price_out,
        "free": model_id.endswith(":free") or (price_in == 0 and price_out == 0),
        "tools": "tools" in params,
        "modalities": [m for m in (arch.get("input_modalities") or []) if isinstance(m, str)][:6],
    }


def load(force: bool = False) -> tuple[list[dict[str, Any]] | None, dict[str, Any]]:
    """Models plus status: {ok, stale, age_s, error}. Never raises."""
    with _lock:
        now = time.time()
        fresh = _cache["models"] is not None and now - _cache["at"] < TTL_SECONDS and not force
        if not fresh:
            try:
                models = [m for m in map(normalise, _fetch()) if m]
                _cache.update(at=now, models=models, error=None)
            except Exception as exc:  # network, TLS, bad JSON: keep serving whatever we had
                _cache["error"] = exc.__class__.__name__
        models = _cache["models"]
        return models, {"ok": models is not None, "stale": _cache["error"] is not None and models is not None,
                        "age_s": int(now - _cache["at"]) if models is not None else None,
                        "error": _cache["error"], "count": len(models or [])}


def search(models: list[dict[str, Any]], q: str = "", free: bool = False, tools: bool = False,
           limit: int = 40) -> list[dict[str, Any]]:
    terms = [t for t in q.lower().split() if t]
    out = []
    for m in models:
        if free and not m["free"]:
            continue
        if tools and not m["tools"]:
            continue
        hay = f"{m['id']} {m['name']}".lower()
        if all(t in hay for t in terms):
            out.append(m)
    # Cheapest first among matches keeps the picker useful for a cost-minimising router.
    out.sort(key=lambda m: ((m["in"] if m["in"] is not None else 1e9) + (m["out"] if m["out"] is not None else 1e9), m["id"]))
    return out[:max(1, min(limit, 100))]


def lookup(models: list[dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    return {m["id"]: m for m in models or []}
