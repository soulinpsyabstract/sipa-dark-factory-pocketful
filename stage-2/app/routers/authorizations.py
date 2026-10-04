"""Authorization endpoints (stage-2 spec section "API").

Split out from ``operations.py`` so the Stage-1 transaction router stays
readable, and registered separately in ``main.py``.

Everything here validates by hand rather than leaning on pydantic constraints:
the spec pins ``422 validation_failed`` for a bad ``amount``/``note``/
``visibility``, while a pydantic failure would surface as the shared
``validation_error`` code that the accepted Stage-1 contract requires.
"""

from __future__ import annotations

import hashlib
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from ..dependencies import get_current_user, store
from ..errors import AppError, not_found, validation_error
from ..schemas import AuthorizationIn
from ..store import (
    AUTHORIZATION_STATUSES,
    AUTHORIZATION_VISIBILITIES,
    MAX_AUTHORIZATION_AMOUNT,
    MAX_AUTHORIZATION_NOTE_CHARS,
    MIN_AUTHORIZATION_AMOUNT,
    is_valid_handle,
)

router = APIRouter()

CurrentUser = Annotated[str, Depends(get_current_user)]

#: Pagination defaults shared with ``GET /requests``. The default limit is large
#: enough that an unfiltered Stage-1 style request is never silently truncated.
DEFAULT_PAGE_LIMIT = 1000
MAX_PAGE_LIMIT = 1000


async def raw_body(request: Request) -> bytes:
    """The raw request body bytes, for the D6 idempotency fingerprint.

    Starlette caches the body, so FastAPI can still bind the declared model.
    Fingerprinting the *raw* bytes is what makes ``{}`` and
    ``{"amount": 2000}`` different requests even though they mean the same
    capture, which is exactly what the spec requires.
    """
    return await request.body()


RawBody = Annotated[bytes, Depends(raw_body)]


def fingerprint(method: str, path: str, raw: bytes) -> str:
    """D6: hash the method, path and *raw* body bytes.

    An absent body is normalised to ``{}`` so that a no-body write stays
    stable across retries that send no body at all.
    """
    body = raw if raw and raw.strip() else b"{}"
    digest = hashlib.sha256()
    digest.update(method.encode("utf-8"))
    digest.update(b"\n")
    digest.update(path.encode("utf-8"))
    digest.update(b"\n")
    digest.update(body)
    return digest.hexdigest()


def required_idempotency_key(request: Request) -> str:
    """``Idempotency-Key`` is required on the authorization write paths."""
    for header in ("idempotency-key", "x-idempotency-key"):
        raw = request.headers.get(header)
        if raw and raw.strip():
            return raw.strip()
    raise validation_error(
        "an Idempotency-Key header is required on this endpoint",
        code="validation_failed",
        header="Idempotency-Key",
    )


def validate_amount(value: Any) -> int:
    """``amount`` must be an integer within the spec's bounds."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise validation_error(
            "amount must be an integer number of minor units",
            code="validation_failed",
            field="amount",
        )
    if value < MIN_AUTHORIZATION_AMOUNT or value > MAX_AUTHORIZATION_AMOUNT:
        raise validation_error(
            f"amount must be between {MIN_AUTHORIZATION_AMOUNT} and {MAX_AUTHORIZATION_AMOUNT}",
            code="validation_failed",
            field="amount",
        )
    return value


def validate_note(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise validation_error(
            "note must be a string", code="validation_failed", field="note"
        )
    if len(value) > MAX_AUTHORIZATION_NOTE_CHARS:
        raise validation_error(
            f"note must be at most {MAX_AUTHORIZATION_NOTE_CHARS} characters",
            code="validation_failed",
            field="note",
        )
    return value


def validate_visibility(value: Any) -> str:
    """Defaults to ``public``, matching ``POST /payments``."""
    if value is None:
        return "public"
    if value not in AUTHORIZATION_VISIBILITIES:
        raise validation_error(
            "visibility must be one of: " + ", ".join(AUTHORIZATION_VISIBILITIES),
            code="validation_failed",
            field="visibility",
        )
    return value


def resolve_recipient(handle: Any, me: str) -> str:
    """Self-payment is a 422; an unknown handle is a 404."""
    if not is_valid_handle(handle):
        raise validation_error(
            f"handle {handle!r} is not a valid handle",
            code="validation_failed",
            field="to_handle",
        )
    if handle == me:
        raise validation_error(
            "cannot authorize a payment to yourself",
            code="self_payment",
            handle=handle,
        )
    if handle not in store.users:
        raise not_found(f"no such user: {handle!r}", code="not_found", handle=handle)
    return handle


@router.get("/authorizations")
def list_authorizations(
    me: CurrentUser,
    direction: str = Query(default="all"),
    status: str = Query(default="all"),
    limit: int = Query(default=DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
    offset: int = Query(default=0, ge=0),
) -> JSONResponse:
    """Authorizations where the caller is the payer or the receiver, and no others.

    ``direction`` is ``outgoing`` (caller is the payer), ``incoming`` (caller is
    the receiver) or absent for both. ``status`` is one of the four statuses, a
    comma-separated list, or ``all``. Newest first by ``created_at``.

    An authorization that the clock has passed matches ``expired``, never
    ``open``: the lazy sweep runs before this handler reads anything.
    """
    wanted_direction = direction.strip().lower()
    if wanted_direction not in ("incoming", "outgoing", "all"):
        raise validation_error(
            "direction must be one of: incoming, outgoing, all",
            code="invalid_direction",
            direction=direction,
        )

    wanted_status = [s.strip().lower() for s in status.split(",") if s.strip()] or ["all"]
    if "all" in wanted_status:
        wanted_status = list(AUTHORIZATION_STATUSES)
    for value in wanted_status:
        if value not in AUTHORIZATION_STATUSES:
            raise validation_error(
                f"status must be one of: {', '.join(AUTHORIZATION_STATUSES)}, all",
                code="invalid_status",
                status=value,
            )

    def work() -> dict[str, Any]:
        matched: list[dict[str, Any]] = []
        for record in store.authorizations.values():
            if record.get("from") != me and record.get("to") != me:
                continue
            if record.get("status") not in wanted_status:
                continue
            if wanted_direction == "outgoing" and record.get("from") != me:
                continue
            if wanted_direction == "incoming" and record.get("to") != me:
                continue
            matched.append(record)

        matched.sort(key=lambda r: (r.get("created_at") or "", r.get("id") or ""), reverse=True)

        total = len(matched)
        page = matched[offset : offset + limit]
        items = [
            {
                **store.authorization_public(record),
                "direction": "outgoing" if record.get("from") == me else "incoming",
            }
            for record in page
        ]
        return {
            "status": "ok",
            "authorizations": items,
            "items": items,
            "count": len(items),
            "total": total,
            "has_more": offset + len(items) < total,
            "limit": limit,
            "offset": offset,
            "direction": wanted_direction,
            "status_filter": wanted_status,
        }

    return JSONResponse(status_code=200, content=store.transaction(work))


@router.get("/authorizations/{authorization_id}")
def get_authorization(authorization_id: str, me: CurrentUser) -> JSONResponse:
    """One authorization, scoped to the caller.

    A record that does not exist and a record the caller is not a party to are
    both ``404 not_found``, so this cannot be used to probe which
    authorization ids exist.
    """
    record = store.read(
        lambda: store.authorizations.get(authorization_id)
    )
    if record is None:
        raise not_found(
            f"no such authorization: {authorization_id!r}",
            code="not_found",
            authorization_id=authorization_id,
        )
    if record.get("from") != me and record.get("to") != me:
        raise not_found(
            f"no such authorization: {authorization_id!r}",
            code="not_found",
            authorization_id=authorization_id,
        )

    funds = store.read(lambda: store._funds(record.get("from")))
    return JSONResponse(
        status_code=200,
        content={
            "status": "ok",
            **store.authorization_public(record),
            "currency": store.currency,
            "direction": "outgoing" if record.get("from") == me else "incoming",
            "from_funds": funds,
        },
    )


@router.post("/authorizations")
def create_authorization(
    body: AuthorizationIn, request: Request, raw: RawBody, me: CurrentUser
) -> JSONResponse:
    """Reserve funds without moving them.

    The caller is the payer. 201 on success. An open authorization is **not** a
    feed item, so nothing is written to the activity feed here.
    """
    key = required_idempotency_key(request)
    print_id = fingerprint("POST", "/authorizations", raw)

    # Validation happens before the transaction so a rejected request cannot
    # take (and immediately release) the lock for no reason.
    amount = validate_amount(body.amount)
    note = validate_note(body.note)
    visibility = validate_visibility(body.visibility)
    recipient = resolve_recipient(body.to, me)

    def work() -> tuple[int, dict[str, Any]]:
        replay = store._idem_replay(me, key, print_id)
        if replay is not None:
            return replay

        available = store._available_amount(me)
        if available < amount:
            # 409, per the spec's table for this endpoint. Stage-1 debit paths
            # keep 422; Core approved that split.
            raise AppError(
                409,
                "insufficient_funds",
                f"insufficient funds: {me} has {available} available minor units, needs {amount}",
                handle=me,
                available=available,
                required=amount,
                total=store.balances.get(me, 0),
                held=store._held_amount(me),
            )

        record = store._create_authorization(
            sender=me,
            recipient=recipient,
            amount=amount,
            note=note,
            visibility=visibility,
        )
        funds = store._funds(me)
        result = (
            201,
            {
                "status": "ok",
                **store.authorization_public(record),
                "currency": store.currency,
                "from_funds": funds,
                "available": funds["available"],
                "held": funds["held"],
                "total": funds["total"],
                "balance": funds["balance"],
            },
        )
        store._idem_remember(me, key, print_id, result[0], result[1])
        return result

    status, payload = store.transaction(work)
    return JSONResponse(status_code=status, content=payload)
