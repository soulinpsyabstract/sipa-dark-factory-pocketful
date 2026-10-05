"""Shared FastAPI dependencies: bearer-token authentication."""

from __future__ import annotations

import os
from typing import Annotated

from fastapi import Header, Request

from .errors import unauthorized
from .store import Store, now_iso

store = Store()

#: Name of the browser session cookie carrying the bearer token for the D2 UI.
SESSION_COOKIE = "pocketful_session"


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


def get_ui_user(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    x_auth_token: Annotated[str | None, Header()] = None,
) -> str:
    """Bearer header, or the browser session cookie for the three UI routes.

    A browser following a link sends no ``Authorization`` header, so the D2
    screens carry the same bearer token in a ``SameSite=Lax`` session cookie.
    Deliberately scoped: only the read-only ``/``, ``/requests`` and
    ``/authorizations`` screens use this, so the JSON API's write endpoints stay
    header-only and cannot be driven by an ambient cookie.

    An explicit bearer header wins over the cookie: a supplied credential is a
    deliberate choice, while the cookie is ambient state that may belong to
    whoever last used the browser. A plain navigation sends no header, so the
    cookie still carries the D2 screens.
    """
    if authorization or x_auth_token:
        return get_current_user(request, authorization, x_auth_token)
    cookie = request.cookies.get(SESSION_COOKIE)
    if cookie:
        handle = store.read(lambda: store.tokens.get(cookie))
        if handle is not None and store.read(lambda: handle in store.users):
            return handle
    return get_current_user(request, authorization, x_auth_token)


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


def _user_public(
    user: dict,
    balances: dict[str, int],
    funds: dict[str, int] | None = None,
    currency: str | None = None,
    minor_units: int | None = None,
) -> dict[str, Any]:
    """The single public projection of a user. **Lock-free by contract.**

    ``threading.Lock`` is NOT reentrant, so this function must never take the
    lock. Handlers call it both from inside ``store.transaction(...)`` (where
    the lock is already held) and from read-only paths, so there is only one
    projection and it is deliberately the lock-free one.

    This is the ONLY user projection in the codebase. A lock-taking sibling was
    deliberately deleted: it had no call sites, and shipping an unused
    lock-taking helper next to the lock-free one is precisely the shape that
    invites the next self-deadlock - someone calls it from inside a ``work()``
    and the whole authenticated surface hangs again.

    ``funds`` is the store's already-computed ``{total, held, available}``
    triple for this handle. It is passed in rather than looked up here so the
    function stays lock-free; callers holding the lock use ``store._funds(...)``.

    ``tests/test_stage1.py::test_public_projection_helpers_take_no_lock`` and
    ``test_no_nested_lock_acquisition_in_app_package`` enforce this statically.
    """
    handle = user["handle"]
    balance = balances.get(handle, 0)
    held = int(funds["held"]) if funds else 0
    return {
        "handle": handle,
        "email": user["email"],
        "display_name": user["display_name"],
        "is_operator": bool(user.get("is_operator", False)),
        "balance": balance,
        # stage-2 funds triple: total is the balance, held is reserved by open
        # authorizations, available is what can actually be spent.
        "total": balance,
        "held": held,
        "available": balance - held,
        "created_at": user.get("created_at") or "",
        # stage-2 display metadata, carried through from the fixture.
        "currency": currency or "EUR",
        "minor_units": 2 if minor_units is None else minor_units,
    }
