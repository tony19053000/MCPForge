"""Request dependencies — authentication enforced server-side on every route.

An identity is never taken from a request body or a header the client controls
beyond the bearer token itself, which is verified here.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status

from mcpforge.auth.identity import (
    AuthError,
    AuthNotConfiguredError,
    TokenVerifier,
    VerifiedIdentity,
)


def get_verifier(request: Request) -> TokenVerifier:
    verifier: TokenVerifier = request.app.state.token_verifier
    return verifier


async def current_identity(
    verifier: Annotated[TokenVerifier, Depends(get_verifier)],
    authorization: Annotated[str | None, Header()] = None,
) -> VerifiedIdentity:
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )

    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization header must be 'Bearer <token>'",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        return await verifier.verify(token)
    except AuthNotConfiguredError as exc:
        # Honest distinction: the deployment is unconfigured, the token is not bad.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=exc.reason
        ) from exc
    except AuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=exc.reason,
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


CurrentIdentity = Annotated[VerifiedIdentity, Depends(current_identity)]
