"""Transactions & operations (spec section 3, "Transactions & Operations").

Every handler here is a plain ``def`` (not ``async def``). Starlette runs those
on a worker thread pool, which is what makes the store's ``threading.Lock()``
do real work under the concurrency hammer.
"""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from ..dependencies import _user_public, get_current_user, require_operator, store
from ..errors import AppError, conflict, forbidden, not_found, validation_error
from ..schemas import PaymentIn, RequestIn, SettlementIn, SplitIn
from ..store import MAX_LIST_LIMIT, REQUEST_STATUSES, is_valid_handle, now_iso
from ..ui import render_requests, wants_html

router = APIRouter()

CurrentUser = Annotated[str, Depends(get_current_user)]


async def raw_body(request: Request) -> bytes:
    """The raw request body bytes, for the D6 idempotency fingerprint.

    Starlette caches the body, so FastAPI can still bind the declared model.
    Fingerprinting the *raw* bytes rather than the validated model is what the
    spec requires: the request schemas are ``extra="ignore"``, so hashing the
    model would drop unrecognised fields and treat two genuinely different
    bodies as the same request - a silent replay where a 409 is owed.
    """
    return await request.body()


RawBody = Annotated[bytes, Depends(raw_body)]


def _fingerprint(method: str, path: str, raw: bytes) -> str:
    """D6: hash the method, path and *raw* body bytes.

    An absent or blank body is normalised to ``{}`` so a no-body write stays
    stable across retries that send no body at all. Mirrors
    ``authorizations.fingerprint`` so all seven write paths agree.
    """
    body = raw if raw and raw.strip() else b"{}"
    digest = hashlib.sha256()
    digest.update(method.encode("utf-8"))
    digest.update(b"\n")
    digest.update(path.encode("utf-8"))
    digest.update(b"\n")
    digest.update(body)
    return digest.hexdigest()


def _idempotency_key(request: Request) -> str | None:
    for header in ("idempotency-key", "x-idempotency-key"):
        raw = request.headers.get(header)
        if raw and raw.strip():
            return raw.strip()
    return None


def _check_recipient(handle: str, me: str) -> str:
    if not is_valid_handle(handle):
        raise validation_error(f"handle {handle!r} is not a valid handle", code="invalid_handle")
    if handle == me:
        raise validation_error(
            "cannot target yourself", code="self_transfer_not_allowed", handle=handle
        )
    return handle


# ======================================================================
# W4 - payments
# ======================================================================


@router.post("/payments")
def create_payment(
    body: PaymentIn, request: Request, raw: RawBody, me: CurrentUser
) -> JSONResponse:
    """Move ``amount`` minor units from the caller to ``to``.

    422 on: non-integer / non-positive amount, unknown recipient (404), self
    payment, insufficient funds. A rejected transfer never changes a balance -
    the whole operation is validated before a single unit moves.
    """
    key = _idempotency_key(request)
    fingerprint = _fingerprint("POST", "/payments", raw)

    def work() -> tuple[int, dict[str, Any]]:
        replay = store._idem_replay(me, key, fingerprint)
        if replay is not None:
            return replay

        to = _check_recipient(body.to, me)
        store._require_user(to)
        store._transfer(me, to, body.amount)

        payment_id = store._next_id("payment", "pay")
        store._add_activity(
            owner=me,
            type="payment",
            actor=me,
            direction="out",
            amount=body.amount,
            counterparty=to,
            related_id=payment_id,
            memo=body.message,
        )
        store._add_activity(
            owner=to,
            type="payment",
            actor=me,
            direction="in",
            amount=body.amount,
            counterparty=me,
            related_id=payment_id,
            memo=body.message,
        )
        result = (
            200,
            {
                "status": "ok",
                "payment_id": payment_id,
                "id": payment_id,
                "from": me,
                "to": to,
                "amount": body.amount,
                "note": body.message,
                "created_at": now_iso(),
                # stage-2 (D4): additive and null here, so a payment made
                # without an authorization is distinguishable from a capture.
                # Existing Stage-1 keys above are untouched.
                "authorization_id": None,
                "request_id": None,
                "balances": {me: store.balances[me], to: store.balances[to]},
                "balance_sum": sum(store.balances.values()),
                "seeded_total": store.seeded_total,
            },
        )
        store._idem_remember(me, key, fingerprint, result[0], result[1])
        return result

    status, payload = store.transaction(work)
    return JSONResponse(status_code=status, content=payload)


# ======================================================================
# W5 - requests
# ======================================================================


@router.post("/requests")
def create_request(body: RequestIn, request: Request, raw: RawBody, me: CurrentUser) -> JSONResponse:
    """Ask ``to`` for money. Creates no balance change - funds move on /pay."""
    key = _idempotency_key(request)
    fingerprint = _fingerprint("POST", "/requests", raw)

    def work() -> tuple[int, dict[str, Any]]:
        replay = store._idem_replay(me, key, fingerprint)
        if replay is not None:
            return replay

        to = _check_recipient(body.to, me)
        store._require_user(to)

        request_id = store._next_id("request", "req")
        record = {
            "id": request_id,
            "from": me,
            "from_handle": me,
            "to": to,
            "to_handle": to,
            "amount": body.amount,
            "status": "open",
            "note": body.message,
            "created_at": now_iso(),
            "updated_at": now_iso(),
        }
        store.requests[request_id] = record
        store._add_activity(
            owner=me,
            type="request_created",
            actor=me,
            direction="out",
            amount=body.amount,
            counterparty=to,
            related_id=request_id,
            memo=body.message,
        )
        store._add_activity(
            owner=to,
            type="request_created",
            actor=me,
            direction="in",
            amount=body.amount,
            counterparty=me,
            related_id=request_id,
            memo=body.message,
        )
        result = (200, {"status": "ok", **record})
        store._idem_remember(me, key, fingerprint, result[0], result[1])
        return result

    status, payload = store.transaction(work)
    return JSONResponse(status_code=status, content=payload)


def _load_request(request_id: str) -> dict[str, Any]:
    record = store.requests.get(request_id)
    if record is None:
        raise not_found(f"no such request: {request_id!r}", code="unknown_request", id=request_id)
    return record


@router.post("/requests/{request_id}/pay")
def pay_request(request_id: str, request: Request, raw: RawBody, me: CurrentUser) -> JSONResponse:
    """Recipient fulfils the request; funds move recipient -> creator."""
    key = _idempotency_key(request)
    fingerprint = _fingerprint("POST", f"/requests/{request_id}/pay", raw)

    def work() -> tuple[int, dict[str, Any]]:
        replay = store._idem_replay(me, key, fingerprint)
        if replay is not None:
            return replay

        record = _load_request(request_id)
        if me != record["to"]:
            raise forbidden(
                "only the requested user can pay this request",
                code="not_request_recipient",
                id=request_id,
                handle=me,
            )
        if record["status"] != "open":
            raise conflict(
                f"request {request_id!r} is {record['status']!r}, only an 'open' request can be paid",
                code="invalid_state",
                id=request_id,
                status=record["status"],
            )

        creator, payer = record["from"], record["to"]
        store._transfer(payer, creator, record["amount"])
        record["status"] = "paid"
        record["updated_at"] = now_iso()
        record["paid_at"] = record["updated_at"]
        record["paid_by"] = payer

        store._add_activity(
            owner=payer,
            type="request_paid",
            actor=payer,
            direction="out",
            amount=record["amount"],
            counterparty=creator,
            related_id=request_id,
            memo=record.get("note"),
        )
        store._add_activity(
            owner=creator,
            type="request_paid",
            actor=payer,
            direction="in",
            amount=record["amount"],
            counterparty=payer,
            related_id=request_id,
            memo=record.get("note"),
        )
        result = (
            200,
            {
                "status": "ok",
                **record,
                "balances": {creator: store.balances[creator], payer: store.balances[payer]},
                "balance_sum": sum(store.balances.values()),
                "seeded_total": store.seeded_total,
            },
        )
        store._idem_remember(me, key, fingerprint, result[0], result[1])
        return result

    status, payload = store.transaction(work)
    return JSONResponse(status_code=status, content=payload)


@router.post("/requests/{request_id}/decline")
def decline_request(request_id: str, me: CurrentUser) -> JSONResponse:
    """Recipient declines. Terminal: no further transition is legal."""
    def work() -> dict[str, Any]:
        record = _load_request(request_id)
        if me != record["to"]:
            raise forbidden(
                "only the requested user can decline this request",
                code="not_request_recipient",
                id=request_id,
                handle=me,
            )
        if record["status"] != "open":
            raise conflict(
                f"request {request_id!r} is {record['status']!r}, only an 'open' request can be declined",
                code="invalid_state",
                id=request_id,
                status=record["status"],
            )
        record["status"] = "declined"
        record["updated_at"] = now_iso()

        store._add_activity(
            owner=record["to"],
            type="request_declined",
            actor=me,
            direction="none",
            amount=record["amount"],
            counterparty=record["from"],
            related_id=request_id,
            memo=record.get("note"),
        )
        store._add_activity(
            owner=record["from"],
            type="request_declined",
            actor=me,
            direction="none",
            amount=record["amount"],
            counterparty=record["to"],
            related_id=request_id,
            memo=record.get("note"),
        )
        return {"status": "ok", **record}

    return JSONResponse(status_code=200, content=store.transaction(work))


@router.post("/requests/{request_id}/cancel")
def cancel_request(request_id: str, me: CurrentUser) -> JSONResponse:
    """Creator withdraws their own request. Open only."""
    def work() -> dict[str, Any]:
        record = _load_request(request_id)
        if me != record["from"]:
            raise forbidden(
                "only the creator can cancel this request",
                code="not_request_creator",
                id=request_id,
                handle=me,
            )
        if record["status"] != "open":
            raise conflict(
                f"request {request_id!r} is {record['status']!r}, only an 'open' request can be cancelled",
                code="invalid_state",
                id=request_id,
                status=record["status"],
            )
        record["status"] = "cancelled"
        record["updated_at"] = now_iso()

        store._add_activity(
            owner=me,
            type="request_cancelled",
            actor=me,
            direction="none",
            amount=record["amount"],
            counterparty=record["to"],
            related_id=request_id,
            memo=record.get("note"),
        )
        store._add_activity(
            owner=record["to"],
            type="request_cancelled",
            actor=me,
            direction="none",
            amount=record["amount"],
            counterparty=record["from"],
            related_id=request_id,
            memo=record.get("note"),
        )
        return {"status": "ok", **record}

    return JSONResponse(status_code=200, content=store.transaction(work))


@router.get("/requests")
def list_requests(
    request: Request,
    me: CurrentUser,
    direction: str = Query(default="all"),
    status: str = Query(default="all"),
    limit: int | None = Query(default=None, ge=1, le=MAX_LIST_LIMIT),
    offset: int = Query(default=0, ge=0),
) -> JSONResponse:
    """List the caller's requests.

    ``direction``: ``incoming`` (I am the recipient), ``outgoing`` (I created
    it), or ``all`` (default). ``status``: one of ``open``/``paid``/
    ``declined``/``cancelled``, a comma-separated list, or ``all`` (default).
    Anything else -> 422.

    ``limit``/``offset``/``has_more`` are additive and mirror
    ``GET /authorizations``. ``limit`` defaults to *unbounded* on purpose: the
    accepted Stage-1 contract never truncated this list, so truncation has to be
    something a caller opts into. Passing ``limit`` paginates and sets
    ``has_more``.
    """
    direction = direction.strip().lower()
    if direction not in ("incoming", "outgoing", "all"):
        raise validation_error(
            "direction must be one of: incoming, outgoing, all",
            code="invalid_direction",
            direction=direction,
        )

    wanted = [s.strip().lower() for s in status.split(",") if s.strip()] or ["all"]
    if "all" in wanted:
        wanted = list(REQUEST_STATUSES)
    for s in wanted:
        if s not in REQUEST_STATUSES:
            raise validation_error(
                f"status must be one of: {', '.join(REQUEST_STATUSES)}, all",
                code="invalid_status",
                status=s,
            )

    def work() -> dict[str, Any]:
        items = []
        for record in store.requests.values():
            if direction == "incoming" and record["to"] != me:
                continue
            if direction == "outgoing" and record["from"] != me:
                continue
            if record["status"] not in wanted:
                continue
            items.append(
                {
                    **record,
                    "direction": "outgoing" if record["from"] == me else "incoming",
                }
            )
        items.sort(key=lambda r: r["id"], reverse=True)
        total = len(items)
        # No limit means no truncation, matching Stage-1.
        items = items[offset:] if limit is None else items[offset : offset + limit]
        return {
            "status": "ok",
            "requests": items,
            "items": items,
            "count": len(items),
            "total": total,
            "has_more": offset + len(items) < total,
            "limit": limit,
            "offset": offset,
            "direction": direction,
            "status_filter": wanted,
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
        return HTMLResponse(render_requests(user=user, items=payload["items"]))
    return JSONResponse(status_code=200, content=payload)


# ======================================================================
# W6 - splits
# ======================================================================


@router.post("/splits")
def create_split(body: SplitIn, request: Request, raw: RawBody, me: CurrentUser) -> JSONResponse:
    """Split ``amount`` equally across an ordered participant list.

    Division: ``base, remainder = divmod(amount, len(participants))`` and the
    first ``remainder`` participants in the list get ``base + 1``. So 100/3 ->
    [34, 33, 33] and 10/4 -> [3, 3, 2, 2].

    The caller is the payer: every participant other than the caller is
    debited their share and the caller is credited the total. If the caller is
    also in the list their own share is not moved. 422 if any debit would take
    a balance below zero, or if the payer cannot cover the total charged.
    """
    key = _idempotency_key(request)
    fingerprint = _fingerprint("POST", "/splits", raw)

    def work() -> tuple[int, dict[str, Any]]:
        replay = store._idem_replay(me, key, fingerprint)
        if replay is not None:
            return replay

        participants = body.participants
        for handle in participants:
            if not is_valid_handle(handle):
                raise validation_error(
                    f"handle {handle!r} is not a valid handle", code="invalid_handle"
                )
            store._require_user(handle)
        if len(set(participants)) != len(participants):
            raise validation_error(
                "participants must be unique", code="duplicate_participants"
            )

        count = len(participants)
        base, remainder = divmod(body.amount, count)
        parts = [
            {"handle": handle, "amount": base + (1 if index < remainder else 0)}
            for index, handle in enumerate(participants)
        ]

        # Validate every debit before moving a single unit. Debits are measured
        # against *available* (balance minus holds), so funds reserved by an open
        # authorization cannot be spent, and this dry run agrees with what
        # store._transfer will actually do - otherwise a hold could turn the
        # second transfer into a mid-loop failure and leave a partial split.
        charged = 0
        for part in parts:
            if part["handle"] == me:
                continue
            available = store._available_amount(part["handle"])
            if available < part["amount"]:
                from ..errors import insufficient_funds

                raise insufficient_funds(part["handle"], available, part["amount"])
            charged += part["amount"]
        if store._available_amount(me) < charged:
            from ..errors import insufficient_funds

            # Report the same figure the guard tested, so the error body can
            # never claim more available than the check actually saw.
            raise insufficient_funds(me, store._available_amount(me), charged)

        for part in parts:
            if part["handle"] == me:
                continue
            store._transfer(part["handle"], me, part["amount"])

        split_id = store._next_id("split", "split")
        for part in parts:
            if part["handle"] == me:
                continue
            store._add_activity(
                owner=part["handle"],
                type="split",
                actor=me,
                direction="out",
                amount=part["amount"],
                counterparty=me,
                related_id=split_id,
                memo=body.message,
            )
        store._add_activity(
            owner=me,
            type="split",
            actor=me,
            direction="in",
            amount=charged,
            counterparty=None,
            related_id=split_id,
            memo=body.message,
            parts=parts,
        )

        result = (
            200,
            {
                "status": "ok",
                "split_id": split_id,
                "id": split_id,
                "amount": body.amount,
                "payer": me,
                "participants": participants,
                "parts": parts,
                "shares": [p["amount"] for p in parts],
                "total_charged": charged,
                "note": body.message,
                "created_at": now_iso(),
                "balance_sum": sum(store.balances.values()),
                "seeded_total": store.seeded_total,
            },
        )
        store._idem_remember(me, key, fingerprint, result[0], result[1])
        return result

    status, payload = store.transaction(work)
    return JSONResponse(status_code=status, content=payload)


# ======================================================================
# W8 - activity
# ======================================================================


@router.get("/activity")
def activity(
    me: CurrentUser,
    limit: int = Query(default=100, ge=1, le=1000),
    type: str | None = Query(default=None),
) -> JSONResponse:
    """The caller's activity feed, newest first."""
    wanted_type = type.strip().lower() if type else None

    def work() -> dict[str, Any]:
        items = [dict(a) for a in store.activity if a["handle"] == me]
        if wanted_type:
            items = [a for a in items if a["type"] == wanted_type]
        items.sort(key=lambda a: a["id"], reverse=True)
        items = items[:limit]
        return {
            "status": "ok",
            "activity": items,
            "items": items,
            "count": len(items),
            "handle": me,
            "balance": store.balances.get(me, 0),
        }

    return JSONResponse(status_code=200, content=store.transaction(work))


# ======================================================================
# W8 - settlements (operator only)
# ======================================================================


@router.post("/settlements")
def create_settlement(body: SettlementIn, request: Request, raw: RawBody, me: CurrentUser) -> JSONResponse:
    """Operator-only settlement batch. All-or-nothing.

    403 for any authenticated non-operator. Each transfer is validated up
    front, so a batch that fails part-way leaves every balance untouched.
    """
    require_operator(me)

    key = _idempotency_key(request)
    fingerprint = _fingerprint("POST", "/settlements", raw)

    def work() -> tuple[int, dict[str, Any]]:
        replay = store._idem_replay(me, key, fingerprint)
        if replay is not None:
            return replay

        transfers: list[dict[str, Any]] = []
        for entry in body.entries:
            sender, receiver = entry.from_, entry.to
            if sender == receiver:
                raise validation_error(
                    "settlement entry cannot have the same sender and receiver",
                    code="self_transfer_not_allowed",
                )
            for handle in (sender, receiver):
                if not is_valid_handle(handle):
                    raise validation_error(
                        f"handle {handle!r} is not a valid handle", code="invalid_handle"
                    )
                store._require_user(handle)
            transfers.append({"from": sender, "to": receiver, "amount": entry.amount})

        # Dry run: balances are a plain dict, so snapshot, verify, then commit.
        # Debits are checked against *available* so a hold makes the whole batch
        # fail up front instead of part-way through, which would break atomicity.
        projected = dict(store.balances)
        held = {h: store._held_amount(h) for h in set(projected) | set(store.users)}
        for transfer in transfers:
            free = projected.get(transfer["from"], 0) - held.get(transfer["from"], 0)
            if free < transfer["amount"]:
                from ..errors import insufficient_funds

                raise insufficient_funds(transfer["from"], free, transfer["amount"])
            projected[transfer["from"]] -= transfer["amount"]
            projected[transfer["to"]] = projected.get(transfer["to"], 0) + transfer["amount"]

        for transfer in transfers:
            store._transfer(transfer["from"], transfer["to"], transfer["amount"])

        settlement_id = store._next_id("settlement", "set")
        for transfer in transfers:
            store._add_activity(
                owner=transfer["from"],
                type="settlement",
                actor=me,
                direction="out",
                amount=transfer["amount"],
                counterparty=transfer["to"],
                related_id=settlement_id,
                memo=body.message,
            )
            store._add_activity(
                owner=transfer["to"],
                type="settlement",
                actor=me,
                direction="in",
                amount=transfer["amount"],
                counterparty=transfer["from"],
                related_id=settlement_id,
                memo=body.message,
            )

        result = (
            200,
            {
                "status": "ok",
                "settlement_id": settlement_id,
                "id": settlement_id,
                "operator": me,
                "transfers": transfers,
                "entries": transfers,
                "count": len(transfers),
                "total_amount": sum(t["amount"] for t in transfers),
                "created_at": now_iso(),
                "balance_sum": sum(store.balances.values()),
                "seeded_total": store.seeded_total,
            },
        )
        store._idem_remember(me, key, fingerprint, result[0], result[1])
        return result

    status, payload = store.transaction(work)
    return JSONResponse(status_code=status, content=payload)
