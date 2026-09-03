"""Run the API over real HTTP for the live end-to-end check.

Development only. The token verifier here accepts any token beginning `uid-`
as that user, so this must never be pointed at anything real — it exists so
`apps/web/tests/live-e2e.test.ts` can drive the actual WebMCP tools against an
actual server, which no other test in the repository does.

    uv run --directory services/api python scripts/live_api.py
"""

import uvicorn

from mcpforge.auth.identity import AuthError, VerifiedIdentity
from mcpforge.config import Settings
from mcpforge.gemini.fake import FakeGeminiProvider
from mcpforge.main import create_app
from mcpforge.store.memory import InMemoryStore


class TokenIsUid:
    async def verify(self, raw_token: str) -> VerifiedIdentity:
        if not raw_token.startswith("uid-"):
            raise AuthError("bad token")
        return VerifiedIdentity(subject=raw_token, issuer="live-test")


app = create_app(
    Settings(),
    token_verifier=TokenIsUid(),
    store=InMemoryStore(),
    gemini=FakeGeminiProvider([]),
)

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8099, log_level="warning")
