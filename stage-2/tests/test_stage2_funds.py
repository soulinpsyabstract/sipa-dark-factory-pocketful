"""Stage-2 funds model tests (W2).

Covers the ``total`` / ``held`` / ``available`` triple, seeded-authorization
validation on reset, lazy expiry on read and write, and the rule that every
debit path is evaluated against *available* rather than raw balance.

In-process via ``TestClient`` - no server, no Docker, no network. Stage-1
coverage lives in ``test_stage1.py`` and must stay green independently.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.main import app  # noqa: E402

PW = "correct-horse-battery"

BASE_USERS = [
    {"handle": "alice", "email": "alice@example.com", "balance": 1000, "password": PW},
    {"handle": "bob", "email": "bob@example.com", "balance": 1000, "password": PW},
    {"handle": "carol", "email": "carol@example.com", "balance": 1000, "password": PW},
    {"handle": "op", "email": "op@example.com", "balance": 1000, "password": PW, "is_operator": True},
]


def fixture(*, _expect_rejected: bool = False, **extra):
    """Build a reset payload, refusing to construct an over-holding fixture.

    A reset that over-holds is *correctly* rejected with 422, which leaves the
    previous world in place. If such a fixture were used by a test that expects
    it to be accepted, the test would silently exercise the wrong state. So the
    over-draw is caught here, at construction, and only the tests that assert
    the rejection opt out via ``_expect_rejected=True``.
    """
    payload = {"seeded_total": 4000, "users": [dict(u) for u in BASE_USERS]}
    payload.update(extra)

    if not _expect_rejected:
        balances = {u["handle"]: u["balance"] for u in BASE_USERS}
        holds: dict[str, int] = {}
        for record in payload.get("authorizations") or []:
            if record.get("status", "open") != "open":
                continue
            amount = record.get("amount")
            if not isinstance(amount, int) or amount <= 0:
                continue
            sender = record.get("from")
            if sender in balances:
                holds[sender] = holds.get(sender, 0) + amount
        for handle, held in holds.items():
            assert held <= balances[handle], (
                f"test fixture over-holds {handle}: {held} > {balances[handle]}. "
                "Pass _expect_rejected=True only if the test asserts the 422."
            )
    return payload


def iso_in(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def auth_seed(auth_id: str, sender: str, amount: int, **over):
    """A seeded open authorization with an absolute expiry."""
    record = {
        "id": auth_id,
        "from": sender,
        "to": "bob",
        "amount": amount,
        "status": "open",
        "expires_at": iso_in(600),
    }
    record.update(over)
    return record


@pytest.fixture()
def client():
    with TestClient(app) as c:
        c.post("/_test/reset", json=fixture())
        yield c


def login(client, handle="alice"):
    email = "op@example.com" if handle == "op" else f"{handle}@example.com"
    r = client.post("/auth/login", json={"email": email, "password": PW})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def auth(tok, key=None):
    headers = {"Authorization": f"Bearer {tok}"}
    if key:
        headers["Idempotency-Key"] = key
    return headers


def me(client, handle="alice"):
    tok = login(client, handle)
    r = client.get("/me", headers=auth(tok))
    assert r.status_code == 200, r.text
    return r.json()


def seed(client, **extra):
    """Reset with a fixture, asserting it was accepted.

    Without this assertion a fixture that is (correctly) rejected as
    ``validation_failed`` leaves the previous state in place, and the test then
    silently exercises the wrong world.
    """
    r = client.post("/_test/reset", json=fixture(**extra))
    assert r.status_code == 200, r.text
    return r


# ----------------------------------------------------------------------
# the funds triple on /me
# ----------------------------------------------------------------------


def test_me_exposes_total_held_available_with_balance_equal_total(client):
    body = me(client)
    for key in ("balance", "total", "held", "available"):
        assert key in body, f"/me is missing {key!r}: {body}"
    assert body["balance"] == body["total"]
    assert body["held"] == 0
    assert body["available"] == body["total"]


def test_me_funds_triple_also_nested_under_user(client):
    body = me(client)
    assert body["user"]["balance"] == body["user"]["total"]
    assert body["user"]["available"] == body["user"]["total"] - body["user"]["held"]


def test_seeded_open_hold_reduces_available_not_balance(client):
    r = client.post(
        "/_test/reset", json=fixture(authorizations=[auth_seed("auth_1", "alice", 400)])
    )
    assert r.status_code == 200, r.text
    body = me(client)
    assert body["total"] == 1000
    assert body["held"] == 400
    assert body["available"] == 600
    assert body["balance"] == body["total"]


def test_holds_are_scoped_to_their_owner(client):
    client.post(
        "/_test/reset",
        json=fixture(authorizations=[auth_seed("auth_1", "alice", 400)]),
    )
    assert me(client, "alice")["held"] == 400
    assert me(client, "carol")["held"] == 0
    assert me(client, "carol")["available"] == me(client, "carol")["total"]


def test_multiple_open_holds_sum(client):
    client.post(
        "/_test/reset",
        json=fixture(
            authorizations=[
                auth_seed("auth_1", "alice", 100),
                auth_seed("auth_2", "alice", 250),
            ]
        ),
    )
    body = me(client)
    assert body["held"] == 350
    assert body["available"] == 650


# ----------------------------------------------------------------------
# seeded-authorization validation on reset
# ----------------------------------------------------------------------


def test_over_hold_fixture_is_rejected_422_validation_failed(client):
    r = client.post(
        "/_test/reset",
        json=fixture(_expect_rejected=True, authorizations=[auth_seed("auth_1", "alice", 5000)]),
    )
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "validation_failed", r.text


def test_rejected_fixture_changes_nothing(client):
    before = client.get("/_test/export").json()
    tok = login(client, "alice")
    r = client.post(
        "/_test/reset",
        json=fixture(_expect_rejected=True, authorizations=[auth_seed("auth_1", "alice", 5000)]),
    )
    assert r.status_code == 422
    after = client.get("/_test/export").json()
    assert after["balances"] == before["balances"]
    assert after["seeded_total"] == before["seeded_total"]
    # the live session and the seeded holds are untouched
    assert me(client, "alice")["held"] == 0
    assert client.get("/me", headers=auth(tok)).status_code == 200


def test_over_hold_is_validated_against_the_new_balances_not_the_old_ones(client):
    """Regression: seeded holds must be checked against the incoming fixture.

    Import validates before installing, so reading the *outgoing* balances while
    parsing would let an over-hold through whenever the new fixture shrinks a
    balance below the hold.
    """
    # world A: alice has 5000, comfortably enough for a 3000 hold
    rich = {"handle": "alice", "email": "alice@example.com", "password": PW}
    poor_bob = {"handle": "bob", "email": "bob@example.com", "balance": 0, "password": PW}
    assert client.post(
        "/_test/reset",
        json={"seeded_total": 5000, "users": [dict(rich, balance=5000), poor_bob]},
    ).status_code == 200
    # world B: alice drops to 1000 but still seeds a 3000 hold -> over-hold
    r = client.post(
        "/_test/reset",
        json={
            "seeded_total": 1000,
            "users": [dict(rich, balance=1000), poor_bob],
            "authorizations": [auth_seed("auth_big", "alice", 3000)],
        },
    )
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "validation_failed", r.text
    # world A is intact - the rejected import changed nothing
    assert me(client, "alice")["total"] == 5000
    assert me(client, "alice")["held"] == 0


def test_hold_exactly_equal_to_balance_is_allowed(client):
    r = client.post(
        "/_test/reset", json=fixture(authorizations=[auth_seed("auth_1", "alice", 1000)])
    )
    assert r.status_code == 200, r.text
    body = me(client)
    assert body["held"] == 1000
    assert body["available"] == 0


def test_two_holds_that_individually_fit_but_together_do_not_are_rejected(client):
    r = client.post(
        "/_test/reset",
        json=fixture(
            _expect_rejected=True,
            authorizations=[
                auth_seed("auth_1", "alice", 600),
                auth_seed("auth_2", "alice", 600),
            ],
        ),
    )
    assert r.status_code == 422
    assert r.json()["code"] == "validation_failed"


def test_seeded_authorization_for_unknown_user_is_rejected(client):
    r = client.post(
        "/_test/reset", json=fixture(authorizations=[auth_seed("auth_1", "nobody", 10)])
    )
    assert r.status_code == 422
    assert me(client, "alice")["held"] == 0


def test_seeded_authorization_with_bad_status_is_rejected(client):
    r = client.post(
        "/_test/reset",
        json=fixture(authorizations=[auth_seed("auth_1", "alice", 10, status="weird")]),
    )
    assert r.status_code == 422
    assert me(client, "alice")["held"] == 0


def test_seeded_authorization_with_non_positive_amount_is_rejected(client):
    r = client.post(
        "/_test/reset",
        json=fixture(authorizations=[auth_seed("auth_1", "alice", 0)]),
    )
    assert r.status_code == 422
    assert me(client, "alice")["held"] == 0


def test_terminal_seeded_authorization_holds_nothing(client):
    for status in ("captured", "voided", "expired"):
        r = client.post(
            "/_test/reset",
            json=fixture(authorizations=[auth_seed("auth_1", "alice", 900, status=status)]),
        )
        assert r.status_code == 200, r.text
        body = me(client)
        assert body["held"] == 0, f"{status} authorization must not hold funds"
        assert body["available"] == 1000


def test_over_hold_is_allowed_once_the_authorization_is_terminal(client):
    """A terminal authorization is not a hold, so it cannot over-hold."""
    r = client.post(
        "/_test/reset",
        json=fixture(
            authorizations=[auth_seed("auth_1", "alice", 9000, status="captured")]
        ),
    )
    assert r.status_code == 200, r.text


# ----------------------------------------------------------------------
# lazy expiry
# ----------------------------------------------------------------------


def test_expired_seeded_authorization_does_not_hold(client):
    r = client.post(
        "/_test/reset",
        json=fixture(authorizations=[auth_seed("auth_1", "alice", 900, expires_at=iso_in(-60))]),
    )
    assert r.status_code == 200, r.text
    body = me(client)
    assert body["held"] == 0
    assert body["available"] == 1000


def test_expiry_is_applied_lazily_on_a_read(client):
    """An overdue open authorization flips to expired the moment it is observed."""
    client.post(
        "/_test/reset",
        json=fixture(authorizations=[auth_seed("auth_1", "alice", 900, expires_at=iso_in(-60))]),
    )
    export = client.get("/_test/export").json()
    record = next(a for a in export["authorizations"] if a["id"] == "auth_1")
    assert record["status"] == "expired"
    assert record.get("closed_at")


def test_expiry_is_applied_on_a_write_too(client):
    client.post(
        "/_test/reset",
        json=fixture(authorizations=[auth_seed("auth_1", "alice", 900, expires_at=iso_in(-60))]),
    )
    tok = login(client, "carol")
    r = client.post("/payments", json={"to": "bob", "amount": 100}, headers=auth(tok))
    assert r.status_code == 200, r.text
    record = next(
        a for a in client.get("/_test/export").json()["authorizations"] if a["id"] == "auth_1"
    )
    assert record["status"] == "expired"


def test_unexpired_authorization_stays_open(client):
    client.post(
        "/_test/reset",
        json=fixture(authorizations=[auth_seed("auth_1", "alice", 900, expires_at=iso_in(600))]),
    )
    record = next(
        a for a in client.get("/_test/export").json()["authorizations"] if a["id"] == "auth_1"
    )
    assert record["status"] == "open"


def test_expiry_creates_no_activity_entry(client):
    before = len(client.get("/_test/export").json()["activity"])
    client.post(
        "/_test/reset",
        json=fixture(authorizations=[auth_seed("auth_1", "alice", 900, expires_at=iso_in(-60))]),
    )
    after = client.get("/_test/export").json()["activity"]
    assert len(after) == before
    assert not [a for a in after if a.get("related_id") == "auth_1"]


def test_available_is_released_after_expiry(client):
    client.post(
        "/_test/reset",
        json=fixture(authorizations=[auth_seed("auth_1", "alice", 900, expires_at=iso_in(-60))]),
    )
    tok = login(client, "alice")
    r = client.post("/payments", json={"to": "bob", "amount": 1000}, headers=auth(tok))
    assert r.status_code == 200, r.text


# ----------------------------------------------------------------------
# every debit path is evaluated against available
# ----------------------------------------------------------------------


def test_payment_cannot_spend_held_funds(client):
    client.post(
        "/_test/reset", json=fixture(authorizations=[auth_seed("auth_1", "alice", 900)])
    )
    tok = login(client, "alice")
    r = client.post("/payments", json={"to": "bob", "amount": 500}, headers=auth(tok))
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "insufficient_funds"
    assert r.json()["error"]["available"] == 100
    # no money moved
    assert client.get("/_test/export").json()["balances"]["alice"] == 1000


def test_payment_can_spend_exactly_the_available_amount(client):
    client.post(
        "/_test/reset", json=fixture(authorizations=[auth_seed("auth_1", "alice", 900)])
    )
    tok = login(client, "alice")
    r = client.post("/payments", json={"to": "bob", "amount": 100}, headers=auth(tok))
    assert r.status_code == 200, r.text
    body = me(client)
    assert body["total"] == 900
    assert body["held"] == 900
    assert body["available"] == 0


def test_request_pay_cannot_spend_held_funds(client):
    # bob holds 900 of his 1000, so only 100 is spendable and the 500 request
    # cannot be paid. The hold must be a single one: two 900 holds against a
    # 1000 balance would be an invalid fixture and the reset would be rejected.
    seed(client, authorizations=[auth_seed("auth_1", "bob", 900)])
    creator = login(client, "alice")
    r = client.post("/requests", json={"to": "bob", "amount": 500}, headers=auth(creator))
    assert r.status_code == 200, r.text
    request_id = r.json()["id"]

    payer = login(client, "bob")
    r = client.post(f"/requests/{request_id}/pay", headers=auth(payer))
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "insufficient_funds"
    # the request is still payable and no money moved
    assert client.get("/_test/export").json()["balances"]["bob"] == 1000
    assert next(
        q for q in client.get("/_test/export").json()["requests"] if q["id"] == request_id
    )["status"] == "open"


def test_request_pay_can_spend_the_available_amount(client):
    seed(client, authorizations=[auth_seed("auth_1", "bob", 900)])
    creator = login(client, "alice")
    r = client.post("/requests", json={"to": "bob", "amount": 100}, headers=auth(creator))
    request_id = r.json()["id"]
    payer = login(client, "bob")
    r = client.post(f"/requests/{request_id}/pay", headers=auth(payer))
    assert r.status_code == 200, r.text
    body = me(client, "bob")
    assert body["total"] == 900
    assert body["held"] == 900
    assert body["available"] == 0


def test_split_cannot_spend_held_funds(client):
    client.post(
        "/_test/reset", json=fixture(authorizations=[auth_seed("auth_1", "alice", 900)])
    )
    tok = login(client, "alice")
    r = client.post(
        "/splits", json={"amount": 500, "participants": ["bob", "carol"]}, headers=auth(tok)
    )
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "insufficient_funds"
    balances = client.get("/_test/export").json()["balances"]
    assert balances["bob"] == 1000
    assert balances["carol"] == 1000


def test_split_failure_on_a_hold_leaves_no_partial_movement(client):
    """The dry run must agree with _transfer, or the batch would half-apply."""
    seed(
        client,
        authorizations=[
            auth_seed("auth_1", "bob", 900),
            auth_seed("auth_2", "carol", 900, to="alice"),
        ],
    )
    tok = login(client, "alice")
    r = client.post(
        "/splits", json={"amount": 1000, "participants": ["bob", "carol"]}, headers=auth(tok)
    )
    assert r.status_code == 422, r.text
    balances = client.get("/_test/export").json()["balances"]
    assert balances == {"alice": 1000, "bob": 1000, "carol": 1000, "op": 1000}


def test_split_self_check_reports_available_not_balance(client):
    """The caller's own-coverage guard must report what it actually tested.

    Regression guard: this guard compared against ``available`` but used to raise
    with the raw ``balance``, so under an open hold the error body claimed more
    available than the check had seen.
    """
    seed(client, authorizations=[auth_seed("auth_1", "alice", 900)])
    tok = login(client, "alice")
    # total 1000, held 900 -> available 100. Charging 200 cannot be covered.
    r = client.post(
        "/splits", json={"amount": 200, "participants": ["bob"]}, headers=auth(tok)
    )
    assert r.status_code == 422, r.text
    error = r.json()["error"]
    assert error["code"] == "insufficient_funds"
    assert error["available"] == 100, r.text
    assert error["required"] == 200, r.text


def test_split_participant_error_reports_available(client):
    # bob's balance is 1000, so a 950 hold leaves available 50 - less than his
    # 100 share. A 900 hold would leave exactly 100 and the split would
    # legitimately succeed, which is what this test must not assert.
    seed(client, authorizations=[auth_seed("auth_1", "bob", 950)])
    tok = login(client, "alice")
    r = client.post(
        "/splits", json={"amount": 200, "participants": ["bob", "carol"]}, headers=auth(tok)
    )
    assert r.status_code == 422, r.text
    error = r.json()["error"]
    assert error["handle"] == "bob"
    assert error["available"] == 50, r.text


def test_settlement_cannot_spend_held_funds(client):
    client.post(
        "/_test/reset", json=fixture(authorizations=[auth_seed("auth_1", "alice", 900)])
    )
    tok = login(client, "op")
    r = client.post(
        "/settlements",
        json={"entries": [{"from": "alice", "to": "bob", "amount": 500}]},
        headers=auth(tok),
    )
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "insufficient_funds"
    assert client.get("/_test/export").json()["balances"]["alice"] == 1000


def test_settlement_available_check_is_cumulative(client):
    """Two debits from one holder must not each pass on the same free balance."""
    client.post(
        "/_test/reset", json=fixture(authorizations=[auth_seed("auth_1", "alice", 900)])
    )
    tok = login(client, "op")
    r = client.post(
        "/settlements",
        json={
            "entries": [
                {"from": "alice", "to": "bob", "amount": 60},
                {"from": "alice", "to": "carol", "amount": 60},
            ]
        },
        headers=auth(tok),
    )
    assert r.status_code == 422, r.text
    assert client.get("/_test/export").json()["balances"]["alice"] == 1000


# ----------------------------------------------------------------------
# invariant reporting
# ----------------------------------------------------------------------


def test_export_reports_the_funds_invariant(client):
    client.post(
        "/_test/reset", json=fixture(authorizations=[auth_seed("auth_1", "alice", 400)])
    )
    invariants = client.get("/_test/export").json()["invariants"]
    assert invariants["no_negative_available"] is True
    assert invariants["holds_within_balance"] is True
    assert invariants["over_held_users"] == []
    assert invariants["open_authorizations"] == 1


def test_holds_never_break_money_conservation(client):
    client.post(
        "/_test/reset", json=fixture(authorizations=[auth_seed("auth_1", "alice", 400)])
    )
    invariants = client.get("/_test/export").json()["invariants"]
    assert invariants["sum_of_balances"] == invariants["seeded_total"] == 4000
    assert invariants["balance_sum_equals_seeded_total"] is True
    assert invariants["no_negative_balances"] is True


def test_available_never_negative_across_a_hold_and_a_payment(client):
    client.post(
        "/_test/reset", json=fixture(authorizations=[auth_seed("auth_1", "alice", 400)])
    )
    tok = login(client, "alice")
    r = client.post("/payments", json={"to": "bob", "amount": 600}, headers=auth(tok))
    assert r.status_code == 200, r.text
    body = me(client)
    assert body["available"] >= 0
    assert body["held"] <= body["total"]
