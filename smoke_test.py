import os

import httpx


def main() -> int:
    base = os.environ.get("ROUTER_URL", "http://127.0.0.1:6060")
    for tier in ("fast", "balanced", "deep"):
        response = httpx.post(
            base + "/v1/chat/completions",
            json={"model": tier, "messages": [{"role": "user", "content": "Reply with OK."}]},
            timeout=180,
        )
        response.raise_for_status()
        assert response.json().get("choices")
        print(f"PASS {tier}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
