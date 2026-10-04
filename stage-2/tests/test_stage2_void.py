"""Stage-2 authorization void tests (W5).

Covers ``POST /authorizations/{id}/void`` from ``stage-2/SPEC.md``: payer-only,
naturally idempotent, releases the remainder while preserving captures, and
refuses captured or expired records. In-process via ``TestClient``.
"""

from __future__ import annotations

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


def authorize(client, amount, key="k1", **extra):
    r = client.post(
        "/authorizations",
        json={"to_handle": "bob", "amount": amount, **extra},
        headers=auth(login(client, "ada"), key),
    )
    assert r.status_code == 201, r.text
    return r.json()


def void(client, aid, handle="ada", key=None):
    return client.post(
        f"/authorizations/{aid}/void", headers=auth(login(client, handle), key)
    )


def capture(client, aid, key="c1", handle="bob", **body):
    return client.post(
        f"/authorizations/{aid}/capture",
        json=body or None,
        headers=auth(login(client, handle), key),
    )


# ----------------------------------------------------------------------
# the plain case
# ----------------------------------------------------------------------


def test_void_releases_the_whole_hold(client):
    aid = authorize(client, 800)["authorization_id"]
    assert funds(client, "ada") == (2000, 800, 1200)

    r = void(client, aid)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "voided"
    assert body["remaining_amount"] == 0
    assert body["closed_at"] is not None
    assert body["captured_amount"] == 0
    assert funds(client, "ada") == (2000, 0, 2000)


def test_void_moves_no_money_to_the_receiver(client):
    aid = authorize(client, 800)["authorization_id"]
    void(client, aid)
    assert funds(client, "bob") == (0, 0, 0)


def test_void_returns_the_authorization(client):
    aid = authorize(client, 800, note="rent")["authorization_id"]
    body = void(client, aid).json()
    for key in ("authorization_id", "from_handle", "to_handle", "amount", "status",
                "captured_amount", "remaining_amount", "closed_at"):
        assert key in body, f"missing {key!r}: {body}"
    assert body["authorization_id"] == aid
    assert body["note"] == "rent"


# ----------------------------------------------------------------------
# idempotent by state, no key
# ----------------------------------------------------------------------


def test_voiding_twice_is_200_with_the_current_state(client):
    aid = authorize(client, 800)["authorization_id"]
    void(client, aid)

    second = void(client, aid)
    assert second.status_code == 200, second.text
    assert second.json()["status"] == "voided"
    assert second.json()["already_voided"] is True


def test_voiding_twice_does_not_release_twice(client):
    aid = authorize(client, 800)["authorization_id"]
    void(client, aid)
    void(client, aid)
    assert funds(client, "ada") == (2000, 0, 2000)


def test_void_needs_no_idempotency_key_and_ignores_one(client):
    aid = authorize(client, 300)["authorization_id"]
    assert client.post(
        f"/authorizations/{aid}/void", headers=auth(login(client, "ada"))
    ).status_code == 200

    other = authorize(client, 300, key="k2")["authorization_id"]
    assert void(client, other, key="ignored").status_code == 200


def test_void_requires_authentication(client):
    aid = authorize(client, 300)["authorization_id"]
    assert client.post(f"/authorizations/{aid}/void").status_code == 401


# ----------------------------------------------------------------------
# who may void
# ----------------------------------------------------------------------


def test_the_receiver_cannot_void(client):
    aid = authorize(client, 500)["authorization_id"]
    r = void(client, aid, handle="bob")
    assert r.status_code == 403, r.text
    assert r.json()["code"] == "forbidden", r.text


def test_a_bystander_cannot_void(client):
    aid = authorize(client, 500)["authorization_id"]
    r = void(client, aid, handle="cyd")
    assert r.status_code == 403, r.text
    assert r.json()["code"] == "forbidden", r.text


def test_a_forbidden_void_releases_nothing(client):
    aid = authorize(client, 500)["authorization_id"]
    void(client, aid, handle="cyd")
    assert funds(client, "ada") == (2000, 500, 1500)


# ----------------------------------------------------------------------
# refusals
# ----------------------------------------------------------------------


def test_a_fully_captured_authorization_cannot_be_voided(client):
    aid = authorize(client, 400)["authorization_id"]
    capture(client, aid, amount=400)
    r = void(client, aid)
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "authorization_not_open", r.text


def test_an_expired_authorization_cannot_be_voided(client):
    r = client.post(
        "/_test/reset",
        json=fixture(
            authorizations=[
                {
                    "id": "a_old",
                    "from": "ada",
                    "to": "bob",
                    "amount": 100,
                    "status": "open",
                    "expires_at": "2020-01-01T00:00:00+00:00",
                }
            ]
        ),
    )
    assert r.status_code == 200, r.text
    body = void(client, "a_old")
    assert body.status_code == 409, body.text
    assert body.json()["code"] == "authorization_not_open", body.text
    # expiry already released the hold
    assert funds(client, "ada") == (2000, 0, 2000)


def test_voiding_an_unknown_authorization_is_404(client):
    r = void(client, "a_nope")
    assert r.status_code == 404, r.text
    assert r.json()["code"] == "not_found", r.text


# ----------------------------------------------------------------------
# partially captured
# ----------------------------------------------------------------------


def test_void_after_a_partial_capture_releases_only_the_remainder(client):
    aid = authorize(client, 800)["authorization_id"]
    first = capture(client, aid, amount=300, final=False).json()
    assert funds(client, "ada") == (1700, 500, 1200)

    body = void(client, aid).json()
    assert body["status"] == "voided"
    assert body["captured_amount"] == 300, "captures must be preserved"
    assert body["payment_ids"] == [first["payment_id"]]
    assert body["payment_id"] == first["payment_id"]
    assert body["remaining_amount"] == 0
    # 300 already spent stays spent; only the held 500 comes back
    assert funds(client, "ada") == (1700, 0, 1700)
    assert funds(client, "bob") == (300, 0, 300)


def test_capture_after_void_is_409(client):
    aid = authorize(client, 300)["authorization_id"]
    void(client, aid)
    r = capture(client, aid)
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "authorization_not_open", r.text


# ----------------------------------------------------------------------
# listing and the feed
# ----------------------------------------------------------------------


def test_a_voided_authorization_is_listed_as_voided(client):
    aid = authorize(client, 300)["authorization_id"]
    void(client, aid)
    items = client.get("/authorizations", headers=auth(login(client, "ada"))).json()["authorizations"]
    assert len(items) == 1
    assert items[0]["status"] == "voided"
    assert items[0]["remaining_amount"] == 0


def test_a_voided_authorization_releases_its_hold_for_spending(client):
    aid = authorize(client, 900)["authorization_id"]
    void(client, aid)
    r = client.post(
        "/payments", json={"to": "cyd", "amount": 1900}, headers=auth(login(client, "ada"), "p1")
    )
    assert r.status_code == 200, r.text


def test_void_writes_no_activity(client):
    """Void releases a hold; it moves no money, so it is not a feed item."""
    aid = authorize(client, 300)["authorization_id"]
    void(client, aid)
    for handle in ("ada", "bob", "cyd"):
        feed = client.get("/activity", headers=auth(login(client, handle))).json()["activity"]
        assert feed == [], f"{handle} feed should be empty, got {feed}"


def test_void_preserves_every_invariant(client):
    aid = authorize(client, 800)["authorization_id"]
    capture(client, aid, amount=300, final=False)
    void(client, aid)
    inv = client.get("/_test/export").json()["invariants"]
    assert [k for k, v in inv.items() if v is False] == []
    assert inv["no_negative_available"] is True