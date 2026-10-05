"""Server-rendered form endpoints for the D2 screens.

These are thin: every write goes through the same route function the JSON API
uses, so there is exactly one implementation of each business rule. The only
thing this module adds is turning browser form posts into an
``Idempotency-Key`` and a canonical raw body, and turning the result back into a
redirect plus a flash cookie.

The no-double-send rule falls out of the key derivation. Each rendered form
carries a nonce; the key is ``sha256(nonce + canonical body)``. So:

  * resubmitting the *same* form untouched -> same key, same fingerprint -> replay
  * changing any field                      -> different key -> a new payment
  * a fresh page load                       -> new nonce -> a new payment

which is what SPEC.md asks for, and it still lets a person deliberately pay the
same person the same amount twice, because the second time the nonce differs.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from starlette.requests import Request as StarletteRequest

from .. import ui
from ..dependencies import _user_public, get_current_user, store
from ..errors import AppError
from .authorizations import (
    capture_authorization,
    create_authorization,
    void_authorization,
)
from .operations import (
    cancel_request,
    create_payment,
    create_request,
    create_split,
    decline_request,
    split_shares,
    pay_request,
)

router = APIRouter()

SESSION = "pocketful_session"
FLASH = "pocketful_flash"


# ----------------------------------------------------------------------
# session + flash, both cookie-based because a browser sends no bearer header
# ----------------------------------------------------------------------


def ui_token(request: Request) -> str | None:
    return request.cookies.get(SESSION) or None


def ui_handle(request: Request) -> str | None:
    """Resolve the caller for the UI, or None when there is no live session.

    An explicit bearer header beats the cookie, matching
    ``dependencies.get_ui_user``: a supplied credential is deliberate, the
    cookie is whatever the last visitor left behind. A plain navigation sends
    no header, so the cookie is what carries the screens.

    A token whose user was removed resolves to None, so a stale cookie cannot
    keep acting on behalf of somebody the store no longer knows.
    """
    auth = request.headers.get("authorization") or request.headers.get("x-auth-token")
    token = None
    if auth:
        parts = auth.split(None, 1)
        if len(parts) == 2 and parts[0].lower() in ("bearer", "token"):
            token = parts[1].strip()
    if token is None:
        token = request.cookies.get(SESSION)
    if not token:
        return None

    def resolve() -> str | None:
        handle = store.tokens.get(token)
        if handle is None or handle not in store.users:
            return None
        return handle

    return store.read(resolve)


def _error_text(exc: AppError) -> str:
    """The human-readable reason, for the flash and the alert boxes.

    ``AppError`` carries the same sentence under ``detail`` and
    ``error.message``; read it from one place so a screen never shows the
    generic fallback just because a key moved.
    """
    body = exc.to_dict()
    nested = body.get("error") or {}
    return str(nested.get("message") or body.get("detail") or body.get("code") or "")


def _require_ui_user(request: Request) -> str:
    handle = ui_handle(request)
    if handle is None:
        raise AppError(401, "sign in to continue", code="missing_token")
    return handle


def _flash_url(target: str, flash: dict[str, Any]) -> str:
    """Carry flash state in the query string for redirects we fully control.

    Deliberately short-lived and read once. Values are escaped on render, so a
    tampered query cannot inject markup.
    """
    keep = {k: v for k, v in flash.items() if v not in (None, "")}
    if not keep:
        return target
    return f"{target}?{urlencode(keep)}"


def _redirect(target: str, flash: dict[str, Any], *, set_cookie: bool = False) -> RedirectResponse:
    r = RedirectResponse(_flash_url(target, flash), status_code=303)
    if set_cookie:
        r.delete_cookie(SESSION, path="/")
    return r


def new_nonce() -> str:
    return secrets.token_urlsafe(12)


def _idempotency_key(nonce: str, payload: dict[str, Any]) -> str:
    """Content-addressed key. Same form + same nonce replays; anything else does not."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{nonce}\x1f{canonical}".encode()).hexdigest()


def _shim(request: Request, key: str) -> StarletteRequest:
    """A Request carrying an Idempotency-Key, for calling the JSON route directly."""
    headers = list(request.scope.get("headers", []))
    headers.append((b"idempotency-key", key.encode()))
    scope = dict(request.scope)
    scope["headers"] = headers
    return StarletteRequest(scope, receive=request.receive)


def _minor_units() -> int:
    return store.read(lambda: store.minor_units)


def _form_amount(raw: str) -> tuple[int | None, str]:
    try:
        return ui.parse_amount(raw, _minor_units()), ""
    except ValueError as exc:
        return None, str(exc)


# ----------------------------------------------------------------------
# auth
# ----------------------------------------------------------------------


@router.post("/ui/login")
def ui_login(
    request: Request,
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
):
    from ..schemas import LoginIn
    from .auth import login

    try:
        response = login(LoginIn(email=email, password=password))
    except AppError as exc:
        message = _error_text(exc) or "sign in failed"
        return _html(ui.render_login(message, email))
    token = json.loads(response.body).get("access_token")
    if not token:
        return _html(ui.render_login("sign in failed", email))
    r = RedirectResponse("/", status_code=303)
    r.set_cookie(SESSION, token, httponly=True, samesite="lax", path="/")
    return r


@router.post("/ui/signup")
def ui_signup(
    request: Request,
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
    display_name: Annotated[str, Form()] = "",
):
    from ..schemas import SignupIn
    from .auth import signup

    try:
        response = signup(
            SignupIn(email=email, password=password, display_name=display_name or None)
        )
    except AppError as exc:
        message = _error_text(exc) or "sign up failed"
        return _html(ui.render_signup(message, email, display_name))
    token = json.loads(response.body).get("access_token")
    if token:
        r = RedirectResponse("/", status_code=303)
        r.set_cookie(SESSION, token, httponly=True, samesite="lax", path="/")
        return r
    # POST /auth/signup deliberately mints no token (Stage-1 logs in
    # separately), so there is no session to hand the browser yet. Sending the
    # new account straight to the login form is the honest outcome.
    return RedirectResponse("/login", status_code=303)


def _html(body: str) -> Any:
    from fastapi.responses import HTMLResponse

    return HTMLResponse(body, status_code=200)


@router.post("/ui/logout")
def ui_logout(request: Request):
    token = ui_token(request)
    if token:
        store.transaction(lambda: store.tokens.pop(token, None))
    return _redirect("/", {"signed_out": "1"}, set_cookie=True)


# ----------------------------------------------------------------------
# wallet forms
# ----------------------------------------------------------------------

#: Failures that mean "we do not know whether this committed", as opposed to a
#: deliberate refusal. Deliberately narrow: a bug in a handler raises something
#: else and must surface as a test failure rather than be reported to the user
#: as a lost payment.
_UNCERTAIN_OUTCOME = (ConnectionError, TimeoutError, OSError)


@router.post("/ui/pay")
def ui_pay(
    request: Request,
    to: Annotated[str, Form()] = "",
    amount: Annotated[str, Form()] = "",
    note: Annotated[str, Form()] = "",
    visibility: Annotated[str, Form()] = "private",
    nonce: Annotated[str, Form()] = "",
):
    me = _require_ui_user(request)
    minor, error = _form_amount(amount)
    kept = {"pay_to": to, "pay_amount": amount, "pay_note": note,
            "pay_visibility": visibility}
    if error:
        return _redirect("/", {**kept, "pay_error": error})
    payload = {"to": to, "amount": minor, "note": note or None,
               "visibility": visibility}
    key = _idempotency_key(nonce, payload)
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    try:
        response = create_payment(
            body=_payment_body(payload),
            request=_shim(request, key),
            raw=raw,
            me=me,
        )
    except AppError as exc:
        return _redirect("/", {**kept, "pay_error": _error_text(exc)})
    except _UNCERTAIN_OUTCOME as exc:  # pragma: no cover - see the note
        # SPEC: an unknown outcome is NOT a confirmed rejection. A dropped
        # connection or a timeout may have committed before the answer went
        # missing, so say pay-uncertain and leave the form retryable. The key
        # comes from the nonce and the canonical body, so an unchanged retry
        # replays the original write instead of sending a second payment.
        # The nonce comes back in the redirect so the re-rendered form derives
        # the SAME key on retry, which is what makes the retry safe. Reusing a
        # nonce cannot smuggle a second payment: the key mixes the nonce with
        # the canonical body, so a changed field yields a different key anyway.
        return _redirect(
            "/",
            {
                **kept,
                "pay_uncertain": "we could not confirm this payment; "
                "submit the same details again to retry safely",
                "pay_nonce": nonce,
            },
        )
    # POST /payments ignores an extra visibility field, and /activity must not
    # grow one, so the choice the payer made lives in the store's private map
    # for the feed's data-visibility attribute.
    payment_id = None
    try:
        # A Starlette Response has no .json(); it carries raw bytes.
        decoded = json.loads(response.body)
        if isinstance(decoded, dict):
            payment_id = decoded.get("payment_id")
    except (ValueError, TypeError, AttributeError):  # unreadable body, not fatal
        payment_id = None
    if payment_id:
        store.payment_visibility[payment_id] = str(visibility or "public")
    return _redirect("/", {**kept, "pay_ok": "1"})


@router.post("/ui/request")
def ui_request(
    request: Request,
    to: Annotated[str, Form()] = "",
    amount: Annotated[str, Form()] = "",
    note: Annotated[str, Form()] = "",
    nonce: Annotated[str, Form()] = "",
):
    me = _require_ui_user(request)
    minor, error = _form_amount(amount)
    kept = {"req_to": to, "req_amount": amount, "req_note": note}
    if error:
        return _redirect("/", {**kept, "request_error": error})
    payload = {"to": to, "amount": minor, "note": note or None}
    key = _idempotency_key(nonce, payload)
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    try:
        create_request(
            body=_request_body(payload),
            request=_shim(request, key),
            raw=raw,
            me=me,
        )
    except AppError as exc:
        return _redirect(
            "/", {**kept, "request_error": _error_text(exc)}
        )
    return _redirect("/", {**kept, "request_ok": "1"})


def _payment_body(payload: dict[str, Any]):
    from ..schemas import PaymentIn

    return PaymentIn(**payload)


def _request_body(payload: dict[str, Any]):
    from ..schemas import RequestIn

    return RequestIn(**payload)


def _authorization_body(payload: dict[str, Any]):
    from ..schemas import AuthorizationIn

    return AuthorizationIn(**payload)


# ----------------------------------------------------------------------
# request actions
# ----------------------------------------------------------------------


@router.post("/ui/requests/{request_id}/pay")
def ui_pay_request(request: Request, request_id: str, nonce: Annotated[str, Form()] = ""):
    me = _require_ui_user(request)
    key = _idempotency_key(nonce, {"a": "pay", "id": request_id})
    raw = b"{}"
    try:
        pay_request(request_id, _shim(request, key), raw, me)
    except AppError as exc:
        return _redirect("/requests", {"request_error": _error_text(exc)})
    return _redirect("/requests", {})


@router.post("/ui/requests/{request_id}/decline")
def ui_decline_request(request: Request, request_id: str, nonce: Annotated[str, Form()] = ""):
    me = _require_ui_user(request)
    try:
        decline_request(request_id, me)
    except AppError as exc:
        return _redirect("/requests", {"request_error": _error_text(exc)})
    return _redirect("/requests", {})


@router.post("/ui/requests/{request_id}/cancel")
def ui_cancel_request(request: Request, request_id: str, nonce: Annotated[str, Form()] = ""):
    me = _require_ui_user(request)
    try:
        cancel_request(request_id, me)
    except AppError as exc:
        return _redirect("/requests", {"request_error": _error_text(exc)})
    return _redirect("/requests", {})


# ----------------------------------------------------------------------
# authorizations
# ----------------------------------------------------------------------


@router.post("/ui/authorizations")
def ui_create_authorization(
    request: Request,
    to: Annotated[str, Form()] = "",
    amount: Annotated[str, Form()] = "",
    note: Annotated[str, Form()] = "",
    visibility: Annotated[str, Form()] = "private",
    nonce: Annotated[str, Form()] = "",
):
    me = _require_ui_user(request)
    minor, error = _form_amount(amount)
    kept = {"authz_to": to, "authz_amount": amount, "authz_note": note}
    if error:
        return _redirect("/authorizations", {**kept, "authorize_error": error})
    payload = {"to": to, "amount": minor, "note": note or None,
               "visibility": visibility}
    key = _idempotency_key(nonce, payload)
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    try:
        create_authorization(
            body=_authorization_body(payload),
            request=_shim(request, key),
            raw=raw,
            me=me,
        )
    except AppError as exc:
        return _redirect(
            "/authorizations",
            {**kept, "authorize_error": _error_text(exc)},
        )
    return _redirect("/authorizations", {**kept, "authz_ok": "1"})


@router.post("/ui/authorizations/{authorization_id}/capture")
def ui_capture(
    request: Request,
    authorization_id: str,
    amount: Annotated[str, Form()] = "",
    nonce: Annotated[str, Form()] = "",
):
    me = _require_ui_user(request)
    remaining = store.read(
        lambda: store.authorizations.get(authorization_id, {}).get("remaining_amount")
    )
    minor, error = _form_amount(amount)
    if error:
        return _redirect("/authorizations", {"authorization_error": error})
    # A capture of the whole remainder closes the authorization and releases
    # nothing further; a smaller one keeps the rest held.
    final = minor == remaining
    payload = {"amount": minor, "final": final}
    key = _idempotency_key(nonce, {"id": authorization_id, **payload})
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    try:
        capture_authorization(authorization_id, _shim(request, key), raw, me)
    except AppError as exc:
        return _redirect(
            "/authorizations",
            {"authorization_error": _error_text(exc)},
        )
    return _redirect("/authorizations", {})


@router.post("/ui/authorizations/{authorization_id}/void")
def ui_void(request: Request, authorization_id: str, nonce: Annotated[str, Form()] = ""):
    me = _require_ui_user(request)
    # void needs no Idempotency-Key: voiding an already-voided authorization is
    # 200 with current state, so it is naturally idempotent.
    try:
        void_authorization(authorization_id, me)
    except AppError as exc:
        return _redirect(
            "/authorizations",
            {"authorization_error": _error_text(exc)},
        )
    return _redirect("/authorizations", {})


# ----------------------------------------------------------------------
# signup, login and split - the routes the spec's table lists alongside the
# three API-backed screens. These are browser-only: they have no JSON
# counterpart to negotiate against, so they serve HTML unconditionally rather
# than pretending to have a JSON branch.
# ----------------------------------------------------------------------


def _user_or_none(handle: str | None) -> dict | None:
    if not handle:
        return None
    user = store.read(lambda: store.users.get(handle))
    if user is None:
        return None
    return store.read(
        lambda: _user_public(
            user,
            store.balances,
            store._funds(handle),
            store.currency,
            store.minor_units,
        )
    )


@router.get("/signup")
def page_signup(request: Request):
    q = request.query_params
    return _html(
        ui.render_signup(
            q.get("auth_error", ""), q.get("email", ""), q.get("display_name", "")
        )
    )


@router.get("/login")
def page_login(request: Request):
    q = request.query_params
    return _html(ui.render_login(q.get("auth_error", ""), q.get("email", "")))


@router.get("/split")
def page_split(request: Request):
    from ..dependencies import get_ui_user

    try:
        handle = get_ui_user(
            request,
            request.headers.get("authorization"),
            request.headers.get("x-auth-token"),
        )
    except AppError:
        return _html(ui.render_login("sign in to split a bill"))
    user = _user_or_none(handle)
    if user is None:
        return _html(ui.render_login("sign in to split a bill"))

    q = request.query_params
    amount_raw = q.get("amount", "")
    handles_raw = q.get("handles", "")
    note = q.get("note", "")
    shares = None
    error = q.get("split_error", "")
    if amount_raw.strip() and handles_raw.strip():
        handles = [h.strip() for h in handles_raw.split(",") if h.strip()]
        minor, parse_error = _form_amount(amount_raw)
        if parse_error:
            error = parse_error
        elif not handles:
            error = "name at least one handle, comma separated"
        elif len(set(handles)) != len(handles):
            error = "handles must be unique"
        else:
            try:
                # Same function POST /splits uses, so the preview cannot lie
                # about what the write will do.
                shares = split_shares(int(minor), handles)
            except AppError as exc:
                error = _error_text(exc)
    return _html(
        ui.render_split(
            user,
            amount=amount_raw,
            handles=handles_raw,
            note=note,
            shares=shares,
            split_error=error,
            nonce=new_nonce(),
        )
    )


@router.post("/ui/split")
def ui_split(
    request: Request,
    amount: Annotated[str, Form()] = "",
    handles: Annotated[str, Form()] = "",
    note: Annotated[str, Form()] = "",
    nonce: Annotated[str, Form()] = "",
):
    from ..schemas import SplitIn

    me = _require_ui_user(request)
    participants = [h.strip() for h in handles.split(",") if h.strip()]
    kept = {"amount": amount, "handles": handles, "note": note}
    minor, error = _form_amount(amount)
    if error:
        return _redirect("/split", {**kept, "split_error": error})
    if not participants:
        return _redirect(
            "/split", {**kept, "split_error": "name at least one handle, comma separated"}
        )
    payload = {"amount": minor, "participants": participants, "note": note or None}
    key = _idempotency_key(nonce, payload)
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    try:
        create_split(
            body=SplitIn(amount=minor, participants=participants, note=note or None),
            request=_shim(request, key),
            raw=raw,
            me=me,
        )
    except AppError as exc:
        return _redirect("/split", {**kept, "split_error": _error_text(exc)})
    return RedirectResponse("/", status_code=303)
