"""Caller attribution helpers for Model Router.

Two jobs, both content-free:

* ``client_hint`` records *which software* sent a call (the product token of the
  User-Agent, e.g. "python-httpx" or "openai"). It is stored beside each telemetry row so the
  Control Center can show who the untagged traffic is. It never becomes a caller label by
  itself: guessing silently would merge unrelated callers into one name.
* ``label_for`` turns a hint into a caller label only when the operator has said so, via an
  optional ``client_labels`` object in router_config.json, e.g.
  ``{"client_labels": {"claude-code": "claude", "openai": "chatgpt"}}`` (substring match).

An explicit ``X-Router-Client`` header is the third route: a client that can set one header
can name itself with no config at all.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

_SAFE = re.compile(r"[^a-z0-9._-]+")
MAX_LEN = 40


def sanitize_label(value: Any) -> str | None:
    """Lower-case, strip anything but [a-z0-9._-], cap the length. Headers are attacker-
    controlled text that ends up on a dashboard, so only a boring alphabet survives."""
    if not isinstance(value, str):
        return None
    cleaned = _SAFE.sub("-", value.strip().lower()).strip("-.")[:MAX_LEN]
    return cleaned or None


def client_hint(headers: Mapping[str, str]) -> str | None:
    """Product token of the User-Agent ("OpenAI/Python 1.40" -> "openai"), nothing else."""
    agent = headers.get("user-agent")
    if not isinstance(agent, str) or not agent.strip():
        return None
    return sanitize_label(re.split(r"[\s/;(]", agent.strip(), maxsplit=1)[0])


def label_for(hint: str | None, labels: Any) -> str | None:
    """Operator-defined mapping from a client hint to a caller label (substring match,
    longest key first so "openai-python" beats "openai")."""
    if not hint or not isinstance(labels, dict):
        return None
    for key in sorted((k for k in labels if isinstance(k, str) and k), key=len, reverse=True):
        if key.lower() in hint:
            return sanitize_label(labels[key])
    return None
