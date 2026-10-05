"""System + test-harness endpoints (spec section 3, "System & Testing")."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Request
from fastapi.responses import HTMLResponse, JSONResponse

from ..dependencies import store
from ..schemas import FixtureIn
from ..store import now_iso

router = APIRouter()


@router.get("/health")
def health() -> dict[str, str]:
    """Liveness probe. Deliberately does not take the lock."""
    return {"status": "ok"}


@router.get("/")
def index(request: Request) -> Any:
    """JSON service index by default; the wallet screen for ``Accept: text/html``.

    The JSON branch below is byte-for-byte what it has always been. The HTML
    branch is additive and renders signed-out rather than 401-ing, because a
    browser following a plain link sends no bearer token and a 401 here would
    read as a broken negotiation.
    """
    from ..ui import render_home, render_signed_out, wants_html

    if wants_html(request.headers.get("accept")):
        from .ui import new_nonce, ui_handle

        handle = ui_handle(request)
        if handle is None:
            return HTMLResponse(render_signed_out())

        from ..dependencies import _user_public

        def project() -> tuple[dict, list[dict], dict]:
            public = _user_public(
                store.users[handle],
                store.balances,
                store._funds(handle),
                store.currency,
                store.minor_units,
            )
            rows = [dict(a) for a in store.activity if a["handle"] == handle]
            rows.sort(key=lambda a: a["id"], reverse=True)
            # SPEC: activity-item-{id} carries data-visibility. /activity must
            # not gain a field to do this, so the screens read the private
            # per-payment map the store keeps for exactly this purpose. Same
            # store.read, so the render still sees one consistent snapshot.
            vis: dict[str, str] = {}
            for row in rows:
                pid = row.get("related_id") or row.get("id")
                if pid:
                    vis[pid] = str(store.payment_visibility.get(pid) or "public")
            return public, rows, vis

        user, rows, visibility = store.read(project)
        q = request.query_params
        return HTMLResponse(
            render_home(
                user=user,
                activity=rows,
                visibility=visibility,
                nonce=q.get("pay_nonce") or new_nonce(),
                pay_values={
                    # Flash names carry the outcome of a POST; the bare field
                    # names carry wallet-refresh, a plain GET that must keep
                    # whatever the pay form held. Either way the form comes
                    # back filled.
                    "to": q.get("pay_to") or q.get("to", ""),
                    "amount": q.get("pay_amount") or q.get("amount", ""),
                    "note": q.get("pay_note") or q.get("note", ""),
                    "visibility": q.get("pay_visibility")
                    or q.get("visibility", "private"),
                },
                request_values={
                    "to": q.get("req_to", ""),
                    "amount": q.get("req_amount", ""),
                    "note": q.get("req_note", ""),
                },
                pay_error=q.get("pay_error", ""),
                pay_uncertain=q.get("pay_uncertain", ""),
                request_error=q.get("request_error", ""),
            )
        )

    return {
        "service": "pocketful-stage-2",
        "status": "ok",
        "endpoints": [
            "GET  /health",
            "POST /_test/reset",
            "GET  /_test/export",
            "POST /_test/import",
            "POST /auth/signup",
            "POST /auth/login",
            "GET  /me",
            "POST /payments",
            "POST /requests",
            "POST /requests/{id}/pay",
            "POST /requests/{id}/decline",
            "POST /requests/{id}/cancel",
            "GET  /requests",
            "POST /splits",
            "GET  /activity",
            "POST /settlements",
            "POST /authorizations",
        ],
    }


@router.post("/_test/reset")
def reset(payload: FixtureIn | None = None) -> JSONResponse:
    """Wipe all state and seed it from ``payload``.

    Body: ``{"users": [{"handle","email","display_name","password","balance",
    "is_operator"}], "balances": {handle: int}, "seeded_total": int}``.

    ``seeded_total`` is optional - it defaults to the sum of the seeded
    balances. If it *is* supplied it must equal that sum, otherwise the
    request is rejected 422 rather than installing a state in which
    invariant I1 is already false.
    """
    fixture: dict[str, Any] = payload.model_dump(exclude_none=True) if payload else {}

    def work() -> dict[str, Any]:
        store._install_fixture(fixture)
        snapshot = store._export()
        return {
            "status": "ok",
            "action": "reset",
            "seeded_total": snapshot["seeded_total"],
            "balance_sum": snapshot["balance_sum"],
            "handles": sorted(store.users),
            "balances": snapshot["balances"],
            "users": snapshot["users"],
            "invariants": snapshot["invariants"],
            "reset_at": now_iso(),
        }

    return JSONResponse(status_code=200, content=store.transaction(work))


@router.get("/_test/export")
def export() -> JSONResponse:
    """Full state dump, including the live invariant report."""

    def work() -> dict[str, Any]:
        snapshot = store._export()
        return {
            "status": "ok",
            **snapshot,
            "exported_at": now_iso(),
        }

    return JSONResponse(status_code=200, content=store.transaction(work))


@router.post("/_test/import")
def import_state(payload: Any = Body(default=None)) -> JSONResponse:
    """Replace state with a payload produced by ``/_test/export``.

    Also accepts a bare fixture (``{"users": [...], "seeded_total": n}``), in
    which case requests/activity/idempotency are cleared exactly as on reset.
    """
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        return JSONResponse(
            status_code=422,
            content={
                "detail": "import payload must be a JSON object",
                "code": "validation_error",
                "error": {
                    "code": "validation_error",
                    "message": "import payload must be a JSON object",
                },
            },
        )

    def work() -> dict[str, Any]:
        store._import(payload)
        snapshot = store._export()
        return {
            "status": "ok",
            "action": "import",
            "seeded_total": snapshot["seeded_total"],
            "balance_sum": snapshot["balance_sum"],
            "handles": sorted(store.users),
            "balances": snapshot["balances"],
            "invariants": snapshot["invariants"],
            "imported_at": now_iso(),
        }

    return JSONResponse(status_code=200, content=store.transaction(work))
