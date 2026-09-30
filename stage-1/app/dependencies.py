"""Shared FastAPI dependencies: bearer-token authentication."""

from __future__ import annotations

import os
from typing import Annotated

from fastapi import Header, Request

from .errors import unauthorized
from .store import Store, now_iso

store = Store()


def operator_emails() -> set[str]:
    raw = os.environ.get("POCKETFUL_OPERATOR_EMAILS", "")
    return {part.strip().lower() for part in raw.split(",") if part.strip()}


def is_operator_email(email: str) -> bool:
    return email.strip().lower() in operator_emails()


def issue_token(handle: str) -> str:
    """Mint a bearer token for ``handle``. Caller must hold the store lock."""
    from .security import new_token

    token = new_token()
    store.tokens[token] = handle
    return token


def get_current_user(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    x_auth_token: Annotated[str | None, Header()] = None,
) -> str:
    """Resolve the bearer token to a handle, or raise 401."""
    token: str | None = None
    if authorization:
        parts = authorization.split(None, 1)
        if len(parts) == 2 and parts[0].lower() in ("bearer", "token"):
            token = parts[1].strip()
        else:
            token = authorization.strip()
    if not token and x_auth_token:
        token = x_auth_token.strip()

    if not token:
        raise unauthorized("missing bearer token", code="missing_token")

    handle = store.read(lambda: store.tokens.get(token))
    if handle is None:
        raise unauthorized("invalid or expired token", code="invalid_token")
    if store.read(lambda: handle not in store.users):
        raise unauthorized("invalid or expired token", code="invalid_token")
    return handle


def require_operator(handle: str) -> dict:
    user = store.read(lambda: store.users.get(handle))
    if user is None:
        raise unauthorized("unknown user", code="invalid_token")
    if not user.get("is_operator"):
        from .errors import forbidden

        raise forbidden(
            "settlements are operator-only",
            code="operator_required",
            handle=handle,
        )
    return user


def _user_public(user: dict, balances: dict[str, int]) -> dict[str, Any]:
    """Lock-free projection of a user.

    ``threading.Lock`` is NOT reentrant, so this must never take the lock:
    it is called both from inside ``store.transaction(...)`` and, via
    ``user_public``, from read-only paths.
    """
    return {
        "handle": user["handle"],
        "email": user["email"],
        "display_name": user["display_name"],
        "is_operator": bool(user.get("is_operator", False)),
        "balance": balances.get(user["handle"], 0),
        "created_at": user.get("created_at") or "",
    }


def user_public(user: dict) -> dict[str, Any]:
    """Public projection, taking the lock. Never call while holding it."""
    return store.read(lambda: _user_public(user, store.balances))
