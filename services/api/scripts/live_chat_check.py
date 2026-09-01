"""Live end-to-end chat check: real HTTP route, real Gemini, real SSE.

Uses a stub token verifier so no Firebase project is needed; everything else —
routing, ownership, store, provider, streaming — is the real code path.

    uv run python scripts/live_chat_check.py
"""

from __future__ import annotations

import json
import sys

from fastapi.testclient import TestClient

from mcpforge.auth.identity import AuthError, VerifiedIdentity
from mcpforge.config import get_settings
from mcpforge.main import create_app
from mcpforge.store.memory import InMemoryStore

DEV_TOKEN = "dev"  # noqa: S105 — a stub token for a local script, not a credential


class DevVerifier:
    """Stands in for Firebase so this script needs no identity project.

    Everything else in the path is the real code: routing, ownership, store,
    provider, streaming.
    """

    async def verify(self, raw_token: str) -> VerifiedIdentity:
        if raw_token != DEV_TOKEN:
            raise AuthError("bad token")
        return VerifiedIdentity(subject="uid-dev", issuer="live-check")


def main() -> int:
    settings = get_settings()
    if not settings.gemini_configured:
        print("Gemini is not configured — nothing to check.")
        return 2

    app = create_app(settings, token_verifier=DevVerifier(), store=InMemoryStore())
    headers = {"Authorization": "Bearer dev"}

    with TestClient(app) as client:
        project = client.post("/api/projects", json={"name": "hotel"}, headers=headers).json()
        print(
            f"project  : {project['id']}  access={project['access_mode']}  "
            f"demo={project['is_demo']}"
        )

        session = client.post(f"/api/projects/{project['id']}/sessions", headers=headers).json()
        print(f"session  : {session['id']}  state={session['state']}")

        print("\n--- streaming from the real API ---")
        response = client.post(
            f"/api/sessions/{session['id']}/chat",
            json={
                "message": (
                    "I want to make my hotel booking site WebMCP compatible. "
                    "What do you need from me first?"
                )
            },
            headers=headers,
        )
        if response.status_code != 200:
            print(f"FAILED {response.status_code}: {response.text}")
            return 1

        text = ""
        for block in response.text.strip().split("\n\n"):
            lines = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
            name = lines.get("event")
            if name == "activity":
                print(f"[activity] {json.loads(lines['data'])['label']}")
            elif name == "delta":
                chunk = json.loads(lines["data"])["text"]
                text += chunk
                print(chunk, end="", flush=True)
            elif name == "error":
                print(f"\n[error] {lines['data']}")
                return 1

        turns = client.get(f"/api/sessions/{session['id']}/turns", headers=headers).json()
        print(f"\n\nturns persisted: {[t['role'] for t in turns]}")

        stranger = client.get("/api/projects", headers={"Authorization": "Bearer nope"})
        print(f"bad token      : {stranger.status_code} (expected 401)")

    print("\nLive chat path works end to end.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
