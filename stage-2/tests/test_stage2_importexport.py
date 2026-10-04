"""Stage-2 import/export tests (W6).

Covers ``GET /_test/export`` and ``POST /_test/import`` against the spec's
"Existing clients after an upgrade" section: a stage-2 service must accept an
export produced by the stage-1 service, a browser signed in before the upgrade
must still be signed in afterwards, pending requests stay payable, a payment
whose response was lost stays retryable with the same body and key, and a
rejected import must leave the world untouched.

In-process via ``TestClient``.
"""

from __future__ import annotations

import copy
import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.main import app  # noqa: E402

PW = "correct-horse-battery"

USERS = [
    {"handle": "ada", "email": "ada@example.com", "balance": 2000, "password": PW},
    {"handle": "bob", "email": "bob@example.com", "balance": 0, "password": PW},
    {"handle": "cyd", "email": "cyd@example.com", "balance": 0, "password": PW},
]

# The shape stage-1 emits: no authorizations, no tokens, no stage-2 knobs.
STAGE1_EXPORT = {
    "status": "ok",
    "seeded_total": 2000,
    "users": [dict(u) for u in USERS],
    "balances": {"ada": 2000, "bob": 0, "cyd": 0},
    "requests": [],
    "activity": [],
    "idempotency": [],
    "exported_at": "2026-01-01T00:00:00+00:00",
}


@pytest.fixture()
def client():
    with TestClient(app) as c:
        c.post("/_test/reset", json={"seeded_total": 2000, "users": [dict(u) for u in USERS]})
        yield c


def login(client, handle):
    r = client.post("/auth/login", json={"email": f"{handle}@example.com", "password": PW})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def auth(token, key=None):
    headers = {"Authorization": f"Bearer {token}"}
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


def wallet(client, handle):
    body = client.get("/me", headers=auth(login(client, handle))).json()
    return body["total"], body["held"], body["available"]


def export(client):
    return client.get("/_test/export").json()


def do_import(client, payload):
    return client.post("/_test/import", json=payload)


# ----------------------------------------------------------------------
# stage-1 compatibility
# ----------------------------------------------------------------------


def test_a_stage1_export_imports(client):
    r = do_import(client, STAGE1_EXPORT)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ok"


def test_a_stage1_export_leaves_no_holds_behind(client):
    do_import(client, STAGE1_EXPORT)
    assert wallet(client, "ada") == (2000, 0, 2000)


def test_a_bare_fixture_import_still_clears_stage2_state(client):
    token = login(client, "ada")
    client.post(
        "/authorizations",
        json={"to_handle": "bob", "amount": 500},
        headers=auth(token, "k1"),
    )
    assert wallet(client, "ada")[1] == 500

    do_import(client, {"users": [dict(u) for u in USERS], "seeded_total": 2000})
    assert wallet(client, "ada") == (2000, 0, 2000)


# ----------------------------------------------------------------------
# sessions survive the upgrade (spec: signed in before, still signed in after)
# ----------------------------------------------------------------------


def test_a_signed_in_browser_stays_signed_in_across_import(client):
    token = login(client, "ada")
    assert client.get("/me", headers=auth(token)).status_code == 200

    assert do_import(client, export(client)).status_code == 200
    assert client.get("/me", headers=auth(token)).status_code == 200


def test_every_session_survives_not_just_one(client):
    ada, bob = login(client, "ada"), login(client, "bob")
    do_import(client, export(client))
    assert client.get("/me", headers=auth(ada)).status_code == 200
    assert client.get("/me", headers=auth(bob)).status_code == 200


def test_a_token_naming_an_absent_user_is_refused(client):
    payload = export(client)
    payload["tokens"].append({"token": "ghost-token", "handle": "nobody"})
    r = do_import(client, payload)
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "unknown_user", r.text


# ----------------------------------------------------------------------
# holds survive the upgrade
# ----------------------------------------------------------------------


def test_an_open_hold_survives_export_and_import(client):
    client.post(
        "/authorizations",
        json={"to_handle": "bob", "amount": 800},
        headers=auth(login(client, "ada"), "k1"),
    )
    assert wallet(client, "ada") == (2000, 800, 1200)

    do_import(client, export(client))
    assert wallet(client, "ada") == (2000, 800, 1200)


def test_a_restored_hold_still_blocks_spending(client):
    """The point of restoring the hold: the money must not become spendable."""
    client.post(
        "/authorizations",
        json={"to_handle": "bob", "amount": 1800},
        headers=auth(login(client, "ada"), "k1"),
    )
    do_import(client, export(client))

    r = client.post(
        "/payments", json={"to": "cyd", "amount": 500}, headers=auth(login(client, "ada"), "p1")
    )
    # 422, not 409: stage-1's accepted contract reports insufficient funds on
    # POST /payments as 422 (verify_stage1.py pins this), and stage 2 must not
    # change it. Only the *evaluation* moved, from total to available.
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "insufficient_funds", r.text


def test_a_capture_still_works_after_import(client):
    aid = client.post(
        "/authorizations",
        json={"to_handle": "bob", "amount": 800},
        headers=auth(login(client, "ada"), "k1"),
    ).json()["authorization_id"]

    do_import(client, export(client))

    r = client.post(
        f"/authorizations/{aid}/capture", json=None, headers=auth(login(client, "bob"), "c1")
    )
    assert r.status_code == 201, r.text
    assert wallet(client, "ada") == (1200, 0, 1200)
    assert wallet(client, "bob") == (800, 0, 800)


def test_capture_history_survives(client):
    aid = client.post(
        "/authorizations",
        json={"to_handle": "bob", "amount": 800},
        headers=auth(login(client, "ada"), "k1"),
    ).json()["authorization_id"]
    client.post(
        f"/authorizations/{aid}/capture",
        json={"amount": 300, "final": False},
        headers=auth(login(client, "bob"), "c1"),
    )

    do_import(client, export(client))

    body = client.get(f"/authorizations/{aid}", headers=auth(login(client, "ada"))).json()
    assert body["status"] == "open", body
    assert body["captured_amount"] == 300
    assert body["remaining_amount"] == 500


def test_a_voided_authorization_stays_voided(client):
    aid = client.post(
        "/authorizations",
        json={"to_handle": "bob", "amount": 500},
        headers=auth(login(client, "ada"), "k1"),
    ).json()["authorization_id"]
    client.post(f"/authorizations/{aid}/void", headers=auth(login(client, "ada")))

    do_import(client, export(client))

    body = client.get(f"/authorizations/{aid}", headers=auth(login(client, "ada"))).json()
    assert body["status"] == "voided"
    assert wallet(client, "ada") == (2000, 0, 2000)


def test_an_authorization_with_an_unknown_status_is_refused(client):
    payload = export(client)
    payload["authorizations"] = [
        {
            "id": "a_x",
            "from": "ada",
            "to": "bob",
            "amount": 10,
            "status": "teleported",
        }
    ]
    r = do_import(client, payload)
    assert r.status_code == 422, r.text


def test_an_authorization_naming_an_absent_user_is_refused(client):
    payload = export(client)
    payload["authorizations"] = [
        {"id": "a_x", "from": "ada", "to": "nobody", "amount": 10, "status": "open"}
    ]
    r = do_import(client, payload)
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "unknown_user", r.text


# ----------------------------------------------------------------------
# pending requests and lost payment responses (spec, upgrade section)
# ----------------------------------------------------------------------


def test_a_pending_request_is_still_payable_after_import(client):
    """The `to` party pays, so bob needs funds before he can settle it."""
    ada = login(client, "ada")
    rid = client.post(
        "/requests", json={"to": "bob", "amount": 250}, headers=auth(ada, "r1")
    ).json()["id"]
    client.post("/payments", json={"to": "bob", "amount": 500}, headers=auth(ada, "seed1"))

    do_import(client, export(client))

    r = client.post(f"/requests/{rid}/pay", headers=auth(login(client, "bob"), "pay1"))
    assert r.status_code == 200, r.text
    assert wallet(client, "ada") == (1750, 0, 1750)
    assert wallet(client, "bob") == (250, 0, 250)


def test_a_lost_payment_response_is_retryable_after_import(client):
    """Same body and key, and the money moves exactly once."""
    ada = login(client, "ada")
    body = {"to": "bob", "amount": 300}
    first = client.post("/payments", json=body, headers=auth(ada, "p1"))
    assert first.status_code == 200, first.text
    payment_id = first.json()["payment_id"]

    do_import(client, export(client))

    retry = client.post("/payments", json=body, headers=auth(ada, "p1"))
    assert retry.status_code == 200, retry.text
    assert retry.json()["payment_id"] == payment_id
    # once only
    assert wallet(client, "ada") == (1700, 0, 1700)
    assert wallet(client, "bob") == (300, 0, 300)


def test_a_retry_after_import_still_rejects_a_changed_body(client):
    ada = login(client, "ada")
    client.post("/payments", json={"to": "bob", "amount": 300}, headers=auth(ada, "p1"))
    do_import(client, export(client))

    r = client.post("/payments", json={"to": "bob", "amount": 301}, headers=auth(ada, "p1"))
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "idempotency_key_reuse", r.text


def test_a_restored_authorization_replay_does_not_move_money_twice(client):
    aid = client.post(
        "/authorizations",
        json={"to_handle": "bob", "amount": 400},
        headers=auth(login(client, "ada"), "k1"),
    ).json()["authorization_id"]

    first = client.post(
        f"/authorizations/{aid}/capture", json=None, headers=auth(login(client, "bob"), "c1")
    )
    assert first.status_code == 201, first.text

    do_import(client, export(client))

    replay = client.post(
        f"/authorizations/{aid}/capture", json=None, headers=auth(login(client, "bob"), "c1")
    )
    assert replay.status_code == first.status_code, replay.text
    assert replay.json()["payment_id"] == first.json()["payment_id"]
    assert wallet(client, "bob") == (400, 0, 400)


# ----------------------------------------------------------------------
# round trip
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "field",
    ["balances", "requests", "authorizations", "activity", "idempotency", "currency", "minor_units"],
)
def test_export_import_export_round_trips_each_field(client, field):
    client.post(
        "/authorizations",
        json={"to_handle": "bob", "amount": 700},
        headers=auth(login(client, "ada"), "k1"),
    )
    client.post(
        "/payments", json={"to": "bob", "amount": 100}, headers=auth(login(client, "ada"), "p1")
    )

    first = export(client)
    do_import(client, first)
    second = export(client)
    assert second[field] == first[field], f"{field} did not round-trip"


def test_tokens_round_trip(client):
    login(client, "ada")
    first = export(client)
    do_import(client, first)
    assert export(client)["tokens"] == first["tokens"]


def test_invariants_hold_after_import(client):
    client.post(
        "/authorizations",
        json={"to_handle": "bob", "amount": 700},
        headers=auth(login(client, "ada"), "k1"),
    )
    do_import(client, export(client))
    inv = client.get("/_test/export").json()["invariants"]
    assert [k for k, v in inv.items() if v is False] == []


# ----------------------------------------------------------------------
# a rejected import must change nothing
# ----------------------------------------------------------------------


def _busy_world(client):
    """A world with something in every bucket, so a partial write is visible."""
    ada = login(client, "ada")
    client.post(
        "/authorizations",
        json={"to_handle": "bob", "amount": 700},
        headers=auth(ada, "k1"),
    )
    client.post("/payments", json={"to": "bob", "amount": 100}, headers=auth(ada, "p1"))
    client.post("/requests", json={"to": "bob", "amount": 50}, headers=auth(ada, "r1"))
    return ada


def _state(client):
    """Snapshot for "a rejected import changes nothing".

    The buckets that carry authority - ``authorizations`` and ``tokens`` - are
    compared by value, not by length. A count would pass while a hold or a live
    session was silently rewritten, which is precisely the W5 defect shape:
    import dropped every authorization, so a count could stay right while the
    hold itself vanished.
    """
    snap = export(client)
    return {
        "balances": snap["balances"],
        "requests": snap["requests"],
        "authorizations": snap["authorizations"],
        "activity": snap["activity"],
        "idempotency": snap["idempotency"],
        "tokens": snap["tokens"],
    }


@pytest.mark.parametrize(
    "poison",
    [
        pytest.param(lambda p: p["requests"].append({"id": "r_x", "from": "ada", "to": "ghost", "amount": 1, "status": "open"}), id="request-unknown-user"),
        pytest.param(lambda p: p["requests"].append({"id": "r_x", "from": "ada", "to": "bob", "amount": 1, "status": "levitating"}), id="request-unknown-status"),
        pytest.param(lambda p: p["activity"].append({"id": "a_x", "handle": "ghost", "type": "payment"}), id="activity-unknown-user"),
        pytest.param(lambda p: p["idempotency"].append({"user": "ghost", "key": "k", "status_code": 200, "body": {}}), id="idempotency-unknown-user"),
        pytest.param(lambda p: p["authorizations"].append({"id": "a_x", "from": "ada", "to": "ghost", "amount": 1, "status": "open"}), id="authorization-unknown-user"),
        pytest.param(lambda p: p["authorizations"].append({"id": "a_x", "from": "ada", "to": "bob", "amount": 1, "status": "teleported"}), id="authorization-unknown-status"),
        pytest.param(lambda p: p["tokens"].append({"token": "t_x", "handle": "ghost"}), id="token-unknown-user"),
        pytest.param(lambda p: p.__setitem__("requests", "not-a-list"), id="requests-not-a-list"),
        pytest.param(lambda p: p.__setitem__("authorizations", {"nope": 1}), id="authorizations-not-a-list"),
        pytest.param(lambda p: p.__setitem__("tokens", 7), id="tokens-not-a-list"),
    ],
)
def test_a_rejected_import_changes_nothing(client, poison):
    """A 422 must never leave a half-replaced world behind."""
    token = _busy_world(client)
    before = _state(client)

    payload = export(client)
    # Move the money somewhere else first, so a partial fixture install that
    # lands before validation fails is unmistakable.
    payload["balances"] = {"ada": 1111, "bob": 889, "cyd": 0}
    poison(payload)

    r = do_import(client, payload)
    assert r.status_code == 422, r.text
    assert _state(client) == before


def test_a_rejected_import_leaves_the_session_working(client):
    token = _busy_world(client)
    payload = export(client)
    payload["authorizations"].append(
        {"id": "a_x", "from": "ada", "to": "ghost", "amount": 1, "status": "open"}
    )
    assert do_import(client, payload).status_code == 422
    assert client.get("/me", headers=auth(token)).status_code == 200


def test_a_rejected_import_leaves_the_world_usable(client):
    """After a rejected import the store must still pass its own invariants."""
    _busy_world(client)
    payload = export(client)
    payload["tokens"].append({"token": "t_x", "handle": "ghost"})
    assert do_import(client, payload).status_code == 422

    r = client.post(
        "/payments", json={"to": "cyd", "amount": 10}, headers=auth(login(client, "ada"), "p9")
    )
    assert r.status_code == 200, r.text


def test_a_non_object_payload_is_refused(client):
    _busy_world(client)
    before = _state(client)
    assert do_import(client, [1, 2, 3]).status_code == 422
    assert _state(client) == before


def test_a_payload_with_nothing_recognisable_is_refused(client):
    _busy_world(client)
    before = _state(client)
    r = do_import(client, {"unrelated": "thing"})
    assert r.status_code == 422, r.text
    assert _state(client) == before


# ----------------------------------------------------------------------
# Core's additions: the two properties that would have caught W5
# ----------------------------------------------------------------------


def _stable(snap):
    """Export minus the wall-clock stamp, so two snapshots can be compared."""
    return {k: v for k, v in snap.items() if k != "exported_at"}


def test_a_rejected_import_leaves_tokens_and_authorizations_byte_identical(client):
    """A 422 must not touch the buckets that carry authority.

    Asserted by value rather than by count: a hold or a session can be rewritten
    without changing how many there are.
    """
    _busy_world(client)
    before = _state(client)
    assert before["authorizations"], "needs a populated world to be meaningful"
    assert before["tokens"], "needs a live session to be meaningful"

    payload = export(client)
    # Move the money first, so a partial write that lands before validation
    # fails would be unmistakable in the balances too.
    payload["balances"] = {"ada": 1111, "bob": 889, "cyd": 0}
    payload["tokens"].append({"token": "t_x", "handle": "ghost"})

    assert do_import(client, payload).status_code == 422

    after = _state(client)
    assert after["tokens"] == before["tokens"], "tokens were rewritten by a rejected import"
    assert after["authorizations"] == before["authorizations"], (
        "authorizations were rewritten by a rejected import"
    )
    assert after == before


def test_export_import_export_reaches_a_fixed_point(client):
    """export -> import -> export must be idempotent.

    If an import silently drops a field, the second export differs from the
    first and every subsequent cycle drifts further. This is the property that
    makes hold or token loss impossible to miss.
    """
    _busy_world(client)

    first = export(client)
    assert do_import(client, first).status_code == 200
    second = export(client)
    assert _stable(second) == _stable(first), "first round trip lost information"

    assert do_import(client, second).status_code == 200
    third = export(client)
    assert _stable(third) == _stable(second), "import is not idempotent"


def test_importing_the_same_export_twice_never_releases_a_hold(client):
    """The exact shape of the W5 defect, pinned as a regression.

    In W5 an import wiped every open authorization, so one import turned
    ``held 800`` into ``held 0`` and handed the money back as spendable. Repeat
    imports must be inert, not cumulative.
    """
    client.post(
        "/authorizations",
        json={"to_handle": "bob", "amount": 800},
        headers=auth(login(client, "ada"), "k1"),
    )
    assert wallet(client, "ada") == (2000, 800, 1200)

    for _ in range(3):
        assert do_import(client, export(client)).status_code == 200
        assert wallet(client, "ada") == (2000, 800, 1200), "a hold was released by import"

    # And the restored hold still blocks spending, which is the whole point.
    r = client.post(
        "/payments", json={"to": "cyd", "amount": 1300}, headers=auth(login(client, "ada"), "p1")
    )
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "insufficient_funds", r.text


# ----------------------------------------------------------------------
# session scope on import: preserve survivors, drop the deprovisioned
# ----------------------------------------------------------------------


def test_a_live_session_for_a_user_the_import_removes_is_dropped(client):
    """The security leg: an import must not leave a removed user signed in.

    A token that was already live in the daemon, for a handle the imported
    payload does not contain, must stop working. If it survives, an import that
    deprovisions a user leaves them authenticated - an authentication bypass.

    Seeded from a full stage-2 export so the payload's own ``tokens`` list is
    the authority, which is the stricter of the two shapes.
    """
    ada = login(client, "ada")
    bob = login(client, "bob")
    assert client.get("/me", headers=auth(ada)).status_code == 200

    payload = export(client)
    payload["users"] = [u for u in payload["users"] if u.get("handle") != "ada"]
    payload["balances"] = {"bob": 0}
    payload["seeded_total"] = 0
    payload["tokens"] = [t for t in payload["tokens"] if t.get("handle") != "ada"]
    payload["authorizations"] = []

    assert do_import(client, payload).status_code == 200

    assert client.get("/me", headers=auth(ada)).status_code == 401, (
        "a deprovisioned user's session survived the import - auth bypass"
    )
    assert client.get("/me", headers=auth(bob)).status_code == 200, (
        "a surviving user's session was dropped needlessly"
    )


def test_a_live_session_for_a_removed_user_is_dropped_without_a_token_key(client):
    """Same rule when the payload carries no ``tokens`` key at all.

    A bare fixture has no session list to restore. It also takes the reset
    path, which Core's ruling deliberately left token-free
    (``test_reset_still_wipes_tokens``), so no preservation is claimed here for
    surviving handles either - the point of this test is only the security
    property: a handle the import removes must not stay signed in.

    Preservation for surviving handles is asserted where it does apply, in
    ``test_a_stage1_shaped_export_preserves_live_sessions`` - a stage-1 export
    takes the full-export path despite carrying no ``tokens``.
    """
    ada = login(client, "ada")
    login(client, "bob")

    payload = {
        "users": [{"handle": "bob", "email": "bob@example.com", "balance": 500, "password": PW}],
        "balances": {"bob": 500},
        "seeded_total": 500,
    }
    assert do_import(client, payload).status_code == 200

    assert client.get("/me", headers=auth(ada)).status_code == 401, (
        "a deprovisioned user's session survived an import with no tokens key"
    )


def test_a_stage1_shaped_export_preserves_live_sessions(client):
    """Core's D3 ruling: a stage-1 export has no ``tokens`` field.

    Its format simply cannot express sessions, so wiping every live session
    would break the very requirement SPEC.md line 151 states - a browser signed
    in before the upgrade must stay signed in afterwards. Sessions for surviving
    handles are therefore preserved.
    """
    ada = login(client, "ada")
    bob = login(client, "bob")

    payload = dict(STAGE1_EXPORT)
    assert "tokens" not in payload
    assert do_import(client, payload).status_code == 200

    assert client.get("/me", headers=auth(ada)).status_code == 200, (
        "a stage-1 export wiped a live session; SPEC.md line 151 forbids that"
    )
    assert client.get("/me", headers=auth(bob)).status_code == 200


def test_an_explicitly_empty_token_list_is_authoritative(client):
    """``"tokens": []`` means "nobody was signed in", not "keep what is live".

    Key presence, not truthiness, decides. If emptiness were treated as absence
    then a full export taken with nobody signed in would silently re-admit every
    session that happened to be live at import time.
    """
    ada = login(client, "ada")
    payload = export(client)
    payload["tokens"] = []
    assert do_import(client, payload).status_code == 200
    assert client.get("/me", headers=auth(ada)).status_code == 401


def test_a_rejected_import_leaves_live_sessions_working(client):
    """Rollback must restore the session table, not just the balances."""
    ada = login(client, "ada")
    bob = login(client, "bob")

    payload = export(client)
    payload["tokens"].append({"token": "t_x", "handle": "ghost"})
    assert do_import(client, payload).status_code == 422

    assert client.get("/me", headers=auth(ada)).status_code == 200
    assert client.get("/me", headers=auth(bob)).status_code == 200
