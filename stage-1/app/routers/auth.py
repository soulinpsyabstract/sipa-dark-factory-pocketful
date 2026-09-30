"""Authentication endpoints (spec section 3, "Authentication")."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from ..dependencies import get_current_user, is_operator_email, issue_token, store, user_public
from ..errors import AppError, conflict, unauthorized, validation_error
from ..schemas import LoginIn, SignupIn
from ..security import hash_password, verify_password
from ..store import HANDLE_RE, derive_handle, is_valid_handle, now_iso

router = APIRouter()

EMAIL_RE = r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$"


def normalise_email(email: str) -> str:
    email = email.strip().lower()
    import re

    if re.fullmatch(EMAIL_RE, email) is None:
        raise validation_error(f"invalid email address: {email!r}", code="invalid_email")
    return email


@router.post("/auth/signup")
def signup(body: SignupIn) -> JSONResponse:
    """Create a user.

    The handle is derived from the email local part: lower-cased, stripped of
    every character outside ``[a-z0-9_]``, truncated to 20, then validated
    against ``^[a-z0-9_]{1,20}$``. A local part that sanitises to nothing
    (``"!!!@x.com"``) fails the regex -> 422 ``invalid_handle``.

    A client may pass an explicit ``handle``; it is validated against the same
    regex (422 if it does not match) and, when valid, becomes the handle.
    """
    email = normalise_email(body.email)

    if body.handle is not None:
        if not is_valid_handle(body.handle):
            raise validation_error(
                f"handle {body.handle!r} does not match {HANDLE_RE}", code="invalid_handle"
            )
        handle = body.handle
    else:
        handle = derive_handle(email)
        if not is_valid_handle(handle):
            raise validation_error(
                f"email {email!r} does not derive a handle matching {HANDLE_RE}",
                code="invalid_handle",
                derived_handle=handle,
            )

    def work() -> dict[str, Any]:
        if handle in store.users:
            raise conflict(f"handle {handle!r} is already taken", code="handle_taken", handle=handle)
        for existing in store.users.values():
            if existing["email"] == email:
                raise conflict(f"email {email!r} is already registered", code="email_taken", handle=existing["handle"])

        store.users[handle] = {
            "handle": handle,
            "email": email,
            "display_name": body.display_name.strip() or handle,
            "password_hash": hash_password(body.password),
            "is_operator": is_operator_email(email),
            "created_at": now_iso(),
        }
        # A brand new user starts at zero; this keeps I1 true because it does
        # not change the sum, and I2 true because 0 is not negative.
        store.balances.setdefault(handle, 0)
        store._check_invariants()
        public = user_public(store.users[handle])
        return {"status": "ok", "handle": handle, "user": public, **public}

    return JSONResponse(status_code=200, content=store.transaction(work))


@router.post("/auth/login")
def login(body: LoginIn) -> JSONResponse:
    """Exchange email + password for a bearer token."""
    email = body.email.strip().lower()

    def work() -> dict[str, Any]:
        user = next((u for u in store.users.values() if u["email"] == email), None)
        # Same 401 + message for "no such user" and "wrong password" so the
        # endpoint cannot be used to enumerate registered addresses.
        if user is None or not verify_password(body.password, user["password_hash"]):
            raise unauthorized("invalid email or password", code="invalid_credentials")
        token = issue_token(user["handle"])
        public = user_public(user)
        return {
            "status": "ok",
            "access_token": token,
            "token": token,
            "token_type": "bearer",
            "handle": user["handle"],
            "user": public,
            **public,
        }

    return JSONResponse(status_code=200, content=store.transaction(work))


@router.get("/me")
def me(handle: Annotated[str, Depends(get_current_user)]) -> JSONResponse:
    """Current authenticated user. 401 without a valid bearer token."""
    public = store.read(lambda: user_public(store.users[handle]))
    return JSONResponse(status_code=200, content={"status": "ok", "handle": handle, "user": public, **public})
