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
import json
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from ..dependencies import _user_public, get_current_user, store
from ..errors import AppError, not_found, validation_error
from ..schemas import AuthorizationIn
from ..ui import render_authorizations, wants_html
from ..store import (
    AUTHORIZATION_STATUSES,
    AUTHORIZATION_VISIBILITIES,
    MAX_AUTHORIZATION_AMOUNT,
    MAX_AUTHORIZATION_NOTE_CHARS,
    MAX_LIST_LIMIT,
    MIN_AUTHORIZATION_AMOUNT,
    authorization_remaining,
    is_valid_handle,
    now_iso,
    parse_iso,
)

router = APIRouter()

CurrentUser = Annotated[str, Depends(get_current_user)]

#: ``limit``/``offset`` behave identically on ``GET /requests`` and
#: ``GET /authorizations``. ``MAX_LIST_LIMIT`` is shared with operations.py.


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
    """``Idempotency-Key`` is required on create and capture.

    Not on void: that path is naturally idempotent because voiding an
    already-voided authorization returns ``200`` with the current state.
    """
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


def parse_capture_body(raw: bytes) -> dict[str, Any]:
    """Decode a capture body into ``{"amount": int|None, "final": bool}``.

    Parsed by hand rather than through a pydantic model so a bad ``amount``
    surfaces as the spec's ``422 validation_failed`` instead of the shared
    ``validation_error``, and so ``{}`` fingerprints as a distinct request from
    ``{"amount": n}`` (D6) while still meaning the same capture.
    """
    if not raw or not raw.strip():
        return {"amount": None, "final": True}
    try:
        decoded = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise validation_error(
            f"request body must be valid JSON: {exc}", code="validation_failed", field="body"
        ) from exc
    if not isinstance(decoded, dict):
        raise validation_error(
            "capture body must be a JSON object", code="validation_failed", field="body"
        )

    unknown = set(decoded) - {"amount", "final"}
    if unknown:
        raise validation_error(
            "unknown capture field(s): " + ", ".join(sorted(unknown)),
            code="validation_failed",
            field="body",
        )

    amount = decoded.get("amount")
    # bool is an int subclass; an explicit bool amount is never a valid amount.
    if amount is not None and (isinstance(amount, bool) or not isinstance(amount, int)):
        raise validation_error(
            "amount must be an integer number of minor units",
            code="validation_failed",
            field="amount",
        )

    final = decoded.get("final", True)
    if not isinstance(final, bool):
        raise validation_error(
            "final must be a boolean", code="validation_failed", field="final"
        )

    return {"amount": amount, "final": final}


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
    request: Request,
    me: CurrentUser,
    direction: str = Query(default="all"),
    status: str = Query(default="all"),
    limit: int | None = Query(default=None, ge=1, le=MAX_LIST_LIMIT),
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
        page = matched[offset:] if limit is None else matched[offset : offset + limit]
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

    payload = store.transaction(work)
    if wants_html(request.headers.get("accept")):
        user = store.read(
            lambda: _user_public(
                store.users[me],
                store.balances,
                store._funds(me),
                store.currency,
                store.minor_units,
            )
        )
        return HTMLResponse(render_authorizations(user=user, items=payload["items"]))
    return JSONResponse(status_code=200, content=payload)


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


@router.post("/authorizations/{authorization_id}/capture")
def capture_authorization(
    authorization_id: str, request: Request, raw: RawBody, me: CurrentUser
) -> JSONResponse:
    """Move held funds to the receiver and return the created payment.

    Only the receiver captures. ``amount`` defaults to the uncaptured
    remainder. ``final`` defaults to ``true``: a final capture closes the
    authorization and releases the uncaptured remainder in the same step, while
    ``final: false`` keeps the remainder held for further captures.
    """
    key = required_idempotency_key(request)
    print_id = fingerprint("POST", f"/authorizations/{authorization_id}/capture", raw)

    body = parse_capture_body(raw)
    final = body["final"]

    def work() -> tuple[int, dict[str, Any]]:
        replay = store._idem_replay(me, key, print_id)
        if replay is not None:
            return replay

        record = store.authorizations.get(authorization_id)
        if record is None:
            raise not_found(
                f"no such authorization: {authorization_id!r}",
                code="not_found",
                authorization_id=authorization_id,
            )
        if record.get("to") != me:
            # 403 for the payer and for bystanders alike, per the spec.
            raise AppError(
                403,
                "forbidden",
                "only the receiver of an authorization may capture it",
                id=authorization_id,
                handle=me,
            )

        now = datetime.now(timezone.utc)
        expires_at = parse_iso(record.get("expires_at"))
        overdue = expires_at is not None and expires_at <= now
        status_now = record.get("status")
        # ``expired`` is reported as its own code whether the clock got there on
        # this request or on an earlier lazy sweep, so the answer does not depend
        # on request history. Only captured/voided are "not open".
        if status_now == "expired" or (status_now == "open" and overdue):
            store._expire_authorizations(now)
            raise AppError(
                409,
                "authorization_expired",
                f"authorization {authorization_id!r} expired at {record.get('expires_at')}",
                id=authorization_id,
                expires_at=record.get("expires_at"),
            )
        if status_now != "open":
            raise AppError(
                409,
                "authorization_not_open",
                f"authorization {authorization_id!r} is {status_now!r}, only an 'open' one can be captured",
                id=authorization_id,
                status=status_now,
            )
        store._expire_authorizations(now)

        remaining = authorization_remaining(record)
        amount = body["amount"] if body["amount"] is not None else remaining
        if isinstance(amount, bool) or not isinstance(amount, int) or amount < 1:
            raise validation_error(
                "capture amount must be an integer of at least 1 minor unit",
                code="validation_failed",
                id=authorization_id,
                amount=body["amount"],
            )
        if amount > remaining:
            raise validation_error(
                f"capture of {amount} exceeds the {remaining} still uncaptured",
                code="capture_exceeds_authorization",
                id=authorization_id,
                amount=amount,
                remaining=remaining,
            )

        payer, receiver = record["from"], record["to"]
        store._transfer_from_hold(payer, receiver, amount)

        captured = int(record.get("captured_amount") or 0) + amount
        left = remaining - amount
        closed = bool(final) or left == 0
        moment = now_iso()
        record["captured_amount"] = captured
        record["updated_at"] = moment
        if closed:
            record["status"] = "captured"
            record["closed_at"] = moment
        # Derived after the status settles: a closed record reads zero even
        # though the released remainder was never captured.
        record["remaining_amount"] = authorization_remaining(record)

        payment_id = store._next_id("payment", "pay")
        record["payment_id"] = payment_id
        record.setdefault("payment_ids", []).append(payment_id)

        memo = record.get("note")
        store._add_activity(
            owner=payer,
            type="payment",
            actor=receiver,
            direction="out",
            amount=amount,
            counterparty=receiver,
            related_id=payment_id,
            memo=memo,
        )
        store._add_activity(
            owner=receiver,
            type="payment",
            actor=payer,
            direction="in",
            amount=amount,
            counterparty=payer,
            related_id=payment_id,
            memo=memo,
        )

        result = (
            201,
            {
                "status": "ok",
                "payment_id": payment_id,
                "id": payment_id,
                "from": payer,
                "to": receiver,
                "amount": amount,
                "note": memo,
                "created_at": moment,
                "authorization_id": authorization_id,
                "request_id": None,
                "authorization": store.authorization_public(record),
                "balances": {payer: store.balances[payer], receiver: store.balances[receiver]},
                "balance_sum": sum(store.balances.values()),
                "seeded_total": store.seeded_total,
            },
        )
        store._idem_remember(me, key, print_id, result[0], result[1])
        return result

    status, payload = store.transaction(work)
    return JSONResponse(status_code=status, content=payload)


@router.post("/authorizations/{authorization_id}/void")
def void_authorization(authorization_id: str, me: CurrentUser) -> JSONResponse:
    """Release an open authorization's hold. Only the payer may void.

    No ``Idempotency-Key`` (like decline and cancel): the operation is naturally
    idempotent, because voiding an already-voided authorization is ``200`` with
    the current state rather than an error.

    Voiding a partially captured authorization is allowed: it releases only the
    uncaptured remainder and preserves every capture record. A ``captured`` or
    ``expired`` authorization is ``409 authorization_not_open``.
    """
    def work() -> tuple[int, dict[str, Any]]:
        record = store.authorizations.get(authorization_id)
        if record is None:
            raise not_found(
                f"no such authorization: {authorization_id!r}",
                code="not_found",
                authorization_id=authorization_id,
            )
        if record.get("from") != me:
            # 403 for the receiver and for bystanders alike, per the spec.
            raise AppError(
                403,
                "forbidden",
                "only the payer of an authorization may void it",
                id=authorization_id,
                handle=me,
            )

        moment = now_iso()

        # Already voided: 200 with the current state, no second transition.
        if record.get("status") == "voided":
            return 200, _void_response(record, store._funds(me), already=True)

        # Sweep before the open check so a clock-expired authorization reads as
        # expired and is refused, per the spec's table.
        store._expire_authorizations()
        if record.get("status") != "open":
            raise AppError(
                409,
                "authorization_not_open",
                f"authorization {authorization_id!r} is {record.get('status')!r}, only an 'open' one can be voided",
                id=authorization_id,
                status=record.get("status"),
            )

        record["status"] = "voided"
        record["closed_at"] = moment
        record["updated_at"] = moment
        # Releases the remainder; capture records and captured_amount are kept.
        record["remaining_amount"] = authorization_remaining(record)
        return 200, _void_response(record, store._funds(me), already=False)

    status, payload = store.transaction(work)
    return JSONResponse(status_code=status, content=payload)


def _void_response(
    record: dict[str, Any], funds: dict[str, int], already: bool
) -> dict[str, Any]:
    return {
        "status": "ok",
        **store.authorization_public(record),
        "already_voided": already,
        "currency": store.currency,
        "from_funds": funds,
        "available": funds["available"],
        "held": funds["held"],
        "total": funds["total"],
        "balance": funds["balance"],
    }


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
