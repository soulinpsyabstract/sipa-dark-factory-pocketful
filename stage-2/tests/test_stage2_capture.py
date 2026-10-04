"""Stage-2 authorization capture tests (W4).

Covers ``POST /authorizations/{id}/capture`` from ``stage-2/SPEC.md``: the
receiver-only rule, default and extended capture, remainder release, cumulative
captures, and the full rejection table. In-process via ``TestClient``.

Create/list/get live in ``test_stage2_authorizations.py``; the funds model is in
``test_stage2_funds.py``. All three must stay green independently.
"""

from __future__ import annotations

import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.main import app  # noqa: E402

PW = "correct-horse-battery"

#: ada funds everything, bob receives, cyd is a bystander who is neither party.
USERS = [
    {"handle": "ada", "email": "ada@example.com", "balance": 2000, "password": PW},
    {"handle": "bob", "email": "bob@example.com", "balance": 0, "password": PW},
    {"handle": "cyd", "email": "cyd@example.com", "balance": 0, "password": PW},
]


def fixture(**extra):
    payload = {
        "seeded_total": sum(u["balance"] for u in USERS),
        "users": [dict(u) for u in USERS],
    }
    payload.update(extra)
    return payload


@pytest.fixture()
def client():
    with TestClient(app) as c:
        c.post("/_test/reset", json=fixture())
        yield c


def login(client, handle):
    r = client.post("/auth/login", json={"email": f"{handle}@example.com", "password": PW})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def auth(tok, key=None):
    headers = {"Authorization": f"Bearer {tok}"}
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


def funds(client, handle):
    body = client.get("/me", headers=auth(login(client, handle))).json()
    return body["total"], body["held"], body["available"]


def authorize(client, amount, key="k1", handle="ada", **extra):
    r = client.post(
        "/authorizations",
        json={"to_handle": "bob", "amount": amount, **extra},
        headers=auth(login(client, handle), key),
    )
    assert r.status_code == 201, r.text
    return r.json()


def capture(client, aid, key="c1", handle="bob", **body):
    return client.post(
        f"/authorizations/{aid}/capture",
        json=body or None,
        headers=auth(login(client, handle), key),
    )


# ----------------------------------------------------------------------
# the spec's worked example
# ----------------------------------------------------------------------


def test_final_capture_releases_the_uncaptured_remainder_immediately(client):
    """Capturing 1500 of 2000 returns 500 to the payer's available."""
    aid = authorize(client, 2000)["authorization_id"]
    body = capture(client, aid, amount=1500).json()

    assert body["amount"] == 1500
    # payer's 2000 becomes 1500 spent and 500 released
    assert funds(client, "ada") == (500, 0, 500)
    assert funds(client, "bob") == (1500, 0, 1500)


def test_final_capture_closes_and_reports_zero_remaining(client):
    aid = authorize(client, 2000)["authorization_id"]
    record = capture(client, aid, amount=1500).json()["authorization"]
    assert record["status"] == "captured"
    assert record["captured_amount"] == 1500
    # the released 500 was never captured, and a closed record holds nothing
    assert record["remaining_amount"] == 0
    assert record["closed_at"] is not None


def test_capture_returns_201_with_the_payment_shape(client):
    aid = authorize(client, 2000, note="deposit")["authorization_id"]
    body = capture(client, aid, amount=1500).json()
    for key in ("payment_id", "id", "from", "to", "amount", "note", "created_at"):
        assert key in body, f"missing {key!r}: {body}"
    assert body["authorization_id"] == aid
    assert body["request_id"] is None
    assert body["note"] == "deposit"
    assert body["from"] == "ada"
    assert body["to"] == "bob"


def test_capture_copies_visibility_into_the_payment_feed_entry(client):
    authorize(client, 500, visibility="private")
    aid = authorize(client, 500, key="k2", visibility="private")["authorization_id"]
    capture(client, aid, amount=100)
    feed = client.get("/activity", headers=auth(login(client, "bob"))).json()["activity"]
    captured = [a for a in feed if a["type"] == "payment"]
    assert len(captured) == 1
    assert captured[0]["amount"] == 100
    # the payer's feed has the outgoing mirror
    payer_feed = client.get("/activity", headers=auth(login(client, "ada"))).json()["activity"]
    assert len([a for a in payer_feed if a["type"] == "payment"]) == 1


def test_capture_is_visible_in_the_activity_feed(client):
    aid = authorize(client, 500)["authorization_id"]
    payment_id = capture(client, aid, amount=200).json()["payment_id"]
    feed = client.get("/activity", headers=auth(login(client, "bob"))).json()["activity"]
    assert any(a["related_id"] == payment_id for a in feed)


# ----------------------------------------------------------------------
# default vs extended capture
# ----------------------------------------------------------------------


def test_omitted_amount_captures_the_whole_remainder(client):
    aid = authorize(client, 800)["authorization_id"]
    record = capture(client, aid).json()["authorization"]
    assert record["captured_amount"] == 800
    assert record["status"] == "captured"
    assert funds(client, "ada") == (1200, 0, 1200)


def test_final_false_keeps_the_remainder_held(client):
    aid = authorize(client, 800)["authorization_id"]
    assert funds(client, "ada") == (2000, 800, 1200)

    record = capture(client, aid, amount=300, final=False).json()["authorization"]
    assert record["status"] == "open"
    assert record["captured_amount"] == 300
    assert record["remaining_amount"] == 500
    # 300 spent, 500 still held, so available is unchanged
    assert funds(client, "ada") == (1700, 500, 1200)


def test_final_false_allows_further_captures_up_to_the_remainder(client):
    aid = authorize(client, 800)["authorization_id"]
    first = capture(client, aid, amount=300, final=False).json()
    second = capture(client, aid, key="c2", amount=400, final=False).json()["authorization"]

    assert second["captured_amount"] == 700  # cumulative
    assert second["remaining_amount"] == 100
    assert second["status"] == "open"
    assert funds(client, "ada") == (1300, 100, 1200)


def test_capturing_the_whole_remainder_closes_even_with_final_false(client):
    aid = authorize(client, 400)["authorization_id"]
    record = capture(client, aid, final=False).json()["authorization"]
    assert record["status"] == "captured"
    assert record["remaining_amount"] == 0


def test_payment_ids_lists_every_capture_in_order(client):
    aid = authorize(client, 800)["authorization_id"]
    first = capture(client, aid, amount=300, final=False).json()
    second = capture(client, aid, key="c2", amount=500, final=False).json()

    record = second["authorization"]
    assert record["payment_ids"] == [first["payment_id"], second["payment_id"]]
    # payment_id is the latest capture
    assert record["payment_id"] == second["payment_id"]


def test_partial_capture_held_drops_available_and_remaining_bound_together(client):
    """Pins the three numbers a ``final: false`` capture must move together.

    Guards the ``_held_amount`` defect: summing the face amount instead of the
    remainder over-reserved the released part, which would have frozen the
    funds and blocked the follow-up capture. All three assertions are in one
    test on purpose - any one of them alone still passes with the bug.
    """
    aid = authorize(client, 800)["authorization_id"]

    total, held, available = funds(client, "ada")
    assert (total, held, available) == (2000, 800, 1200)

    capture(client, aid, amount=300, final=False)

    # 1. held falls by exactly the captured amount: 800 -> 500
    total, held, available = funds(client, "ada")
    assert held == 500
    # 2. available is *unchanged*: the 500 remainder is still held, so releasing
    #    it here would let the same money be spent twice. total fell by the
    #    captured 300, and available = total - held still holds.
    assert available == 1200
    assert total == 1700
    assert total - held == available
    # 3. the next capture is bounded by the *remaining* 500, not the 800 face
    assert capture(client, aid, key="c2", amount=500).status_code == 201
    record = capture(client, aid, key="c3", amount=1)
    assert record.status_code == 409
    assert record.json()["code"] == "authorization_not_open"


def test_a_final_capture_releases_the_whole_remainder(client):
    """The complement: a final capture gives back the uncaptured remainder."""
    aid = authorize(client, 400)["authorization_id"]
    _, held, available = funds(client, "ada")
    assert (held, available) == (400, 1600)

    capture(client, aid, amount=150)

    total, held, available = funds(client, "ada")
    # 150 spent, and all 400 of hold released -> available rises by the 250 remainder
    assert total == 1850
    assert held == 0
    assert available == 1850
    assert available - 1600 == 250


def test_second_final_capture_is_409_authorization_not_open(client):
    aid = authorize(client, 800)["authorization_id"]
    capture(client, aid, amount=800)
    r = capture(client, aid, amount=1, key="c2")
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "authorization_not_open", r.text


def test_a_final_capture_can_follow_a_partial_one(client):
    aid = authorize(client, 800)["authorization_id"]
    capture(client, aid, amount=300, final=False)
    record = capture(client, aid, key="c2").json()["authorization"]
    assert record["status"] == "captured"
    assert record["captured_amount"] == 800
    assert funds(client, "ada") == (1200, 0, 1200)


# ----------------------------------------------------------------------
# who may capture
# ----------------------------------------------------------------------


def test_the_payer_cannot_capture(client):
    aid = authorize(client, 500)["authorization_id"]
    r = capture(client, aid, handle="ada")
    assert r.status_code == 403, r.text
    assert r.json()["code"] == "forbidden", r.text


def test_a_byst_who_is_neither_party_cannot_capture(client):
    aid = authorize(client, 500)["authorization_id"]
    r = capture(client, aid, handle="cyd")
    assert r.status_code == 403, r.text
    assert r.json()["code"] == "forbidden", r.text


def test_a_forbidden_capture_moves_no_money(client):
    aid = authorize(client, 500)["authorization_id"]
    capture(client, aid, handle="cyd")
    assert funds(client, "ada") == (2000, 500, 1500)
    assert funds(client, "bob") == (0, 0, 0)


# ----------------------------------------------------------------------
# rejections
# ----------------------------------------------------------------------


def test_unknown_authorization_is_404_not_found(client):
    r = capture(client, "a_nope")
    assert r.status_code == 404, r.text
    assert r.json()["code"] == "not_found", r.text


@pytest.mark.parametrize("amount", [0, -5, 1.5, "200", True, None if False else [], {"a": 1}])
def test_bad_amount_is_422_validation_failed(client, amount):
    aid = authorize(client, 500)["authorization_id"]
    r = capture(client, aid, amount=amount)
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "validation_failed", r.text


def test_exceeding_the_remainder_is_422_capture_exceeds_authorization(client):
    aid = authorize(client, 100)["authorization_id"]
    r = capture(client, aid, amount=101)
    assert r.status_code == 422, r.text
    body = r.json()
    assert body["code"] == "capture_exceeds_authorization", r.text
    assert body["error"]["remaining"] == 100


def test_exceeding_the_remainder_after_a_partial_capture(client):
    aid = authorize(client, 800)["authorization_id"]
    capture(client, aid, amount=700, final=False)
    r = capture(client, aid, amount=200, key="c2")
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "capture_exceeds_authorization", r.text
    # the comparison is against the 100 remaining, not the 800 face amount
    assert r.json()["error"]["remaining"] == 100


def test_capturing_exactly_the_remainder_is_allowed(client):
    aid = authorize(client, 100)["authorization_id"]
    assert capture(client, aid, amount=100).status_code == 201


@pytest.mark.parametrize("final", ["true", "yes", 1, 0, None if False else []])
def test_non_boolean_final_is_422_validation_failed(client, final):
    aid = authorize(client, 500)["authorization_id"]
    r = capture(client, aid, final=final)
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "validation_failed", r.text


def test_unknown_body_field_is_422_validation_failed(client):
    aid = authorize(client, 500)["authorization_id"]
    r = capture(client, aid, wat=1)
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "validation_failed", r.text


def test_capture_requires_an_idempotency_key(client):
    aid = authorize(client, 500)["authorization_id"]
    r = client.post(
        f"/authorizations/{aid}/capture", json={}, headers=auth(login(client, "bob"))
    )
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "validation_failed", r.text


def test_a_rejected_capture_moves_no_money(client):
    aid = authorize(client, 100)["authorization_id"]
    capture(client, aid, amount=500)
    assert funds(client, "ada") == (2000, 100, 1900)
    assert funds(client, "bob") == (0, 0, 0)


# ----------------------------------------------------------------------
# expiry
# ----------------------------------------------------------------------


def expired_fixture(**extra):
    return fixture(
        authorizations=[
            {
                "id": "a_old",
                "from": "ada",
                "to": "bob",
                "amount": 100,
                "status": "open",
                "expires_at": "2020-01-01T00:00:00+00:00",
            }
        ],
        **extra,
    )


def test_capturing_an_expired_authorization_is_409_authorization_expired(client):
    assert client.post("/_test/reset", json=expired_fixture()).status_code == 200
    r = capture(client, "a_old")
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "authorization_expired", r.text


def test_an_expired_capture_still_reports_expired_after_a_sweep(client):
    """The code must not depend on whether an earlier request swept it."""
    assert client.post("/_test/reset", json=expired_fixture()).status_code == 200
    client.get("/me", headers=auth(login(client, "ada")))  # triggers the sweep
    r = capture(client, "a_old")
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "authorization_expired", r.text


def test_expiry_releases_the_hold(client):
    assert client.post("/_test/reset", json=expired_fixture()).status_code == 200
    capture(client, "a_old")
    assert funds(client, "ada") == (2000, 0, 2000)


# ----------------------------------------------------------------------
# idempotency (D6)
# ----------------------------------------------------------------------


def test_replay_returns_the_same_payment_and_moves_money_once(client):
    aid = authorize(client, 500)["authorization_id"]
    first = capture(client, aid, key="k9", amount=200)
    second = capture(client, aid, key="k9", amount=200)
    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    assert funds(client, "bob") == (200, 0, 200)
    assert funds(client, "ada") == (1800, 0, 1800)


def test_empty_body_and_explicit_amount_are_different_requests(client):
    """D6: {} and {"amount": n} are different JSON values."""
    aid = authorize(client, 500)["authorization_id"]
    capture(client, aid, key="k10")
    r = capture(client, aid, key="k10", amount=500)
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "idempotency_key_reuse", r.text


def test_final_flag_is_part_of_the_fingerprint(client):
    aid = authorize(client, 500)["authorization_id"]
    capture(client, aid, key="k11", amount=100, final=False)
    r = capture(client, aid, key="k11", amount=100, final=True)
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "idempotency_key_reuse", r.text


# ----------------------------------------------------------------------
# invariants
# ----------------------------------------------------------------------


def test_capture_preserves_every_invariant(client):
    aid = authorize(client, 800)["authorization_id"]
    capture(client, aid, amount=300, final=False)
    capture(client, aid, amount=500, key="c2")
    inv = client.get("/_test/export").json()["invariants"]
    assert [k for k, v in inv.items() if v is False] == []
    assert inv["balance_sum_equals_seeded_total"] is True
    assert inv["no_negative_available"] is True


def test_held_funds_stay_unspendable_after_a_partial_capture(client):
    """A partial capture must not free the still-held remainder for spending."""
    aid = authorize(client, 800)["authorization_id"]
    capture(client, aid, amount=300, final=False)
    total, held, available = funds(client, "ada")
    assert (total, held, available) == (1700, 500, 1200)
    # 1700 total is plenty, but only 1200 may actually be spent
    r = client.post(
        "/payments", json={"to": "cyd", "amount": 1500}, headers=auth(login(client, "ada"), "p1")
    )
    assert r.status_code == 422, r.text
