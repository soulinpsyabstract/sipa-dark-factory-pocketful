"""D6 idempotency fingerprints the *raw request bytes*, on all seven write paths.

Every write path that accepts an ``Idempotency-Key`` must treat two requests as
the same only when the bytes are the same. The request schemas are
``extra="ignore"``, so hashing the validated model instead of the body silently
discards unrecognised fields: a client that changes an unknown field gets a
replay of the earlier response where the spec owes it a divergence error.

That is the W5 defect class - fingerprinting a normalised view of the request
rather than the request - and it is invisible to a test that only varies fields
the schema knows about.
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
    {"handle": "bob", "email": "bob@example.com", "balance": 500, "password": PW},
    {"handle": "op", "email": "op@example.com", "balance": 1000, "password": PW,
     "is_operator": True},
]


@pytest.fixture()
def client():
    with TestClient(app) as c:
        c.post("/_test/reset", json={"seeded_total": 3500,
                                     "users": [dict(u) for u in USERS]})
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


# ----------------------------------------------------------------------
# One case per write path: a body that differs ONLY by a field the schema
# ignores must not replay.
# ----------------------------------------------------------------------


def test_payments_divergence_is_seen_when_only_an_unknown_field_changes(client):
    tok = login(client, "ada")
    body = {"to": "bob", "amount": 10}

    first = client.post("/payments", json=body, headers=auth(tok, "k1"))
    assert first.status_code == 200, first.text

    # Same key, one extra field the schema does not know about.
    second = client.post(
        "/payments", json={**body, "surprise": "x"}, headers=auth(tok, "k1")
    )
    assert second.status_code == 409, second.text
    assert second.json()["code"] == "idempotency_key_reuse", second.text

    # And the money moved exactly once.
    assert client.get("/me", headers=auth(tok)).json()["balance"] == 1990


def test_requests_divergence_is_seen_when_only_an_unknown_field_changes(client):
    tok = login(client, "ada")
    body = {"to": "bob", "amount": 10}

    assert client.post("/requests", json=body, headers=auth(tok, "k1")).status_code == 200
    second = client.post(
        "/requests", json={**body, "surprise": "x"}, headers=auth(tok, "k1")
    )
    assert second.status_code == 409, second.text
    assert second.json()["code"] == "idempotency_key_reuse", second.text


def test_pay_request_divergence_is_seen_when_a_body_is_supplied_at_all(client):
    """A no-body write must not replay a request that did carry a body."""
    ada = login(client, "ada")
    bob = login(client, "bob")
    rid = client.post(
        "/requests", json={"to": "bob", "amount": 50}, headers=auth(ada, "r1")
    ).json()["id"]

    # First call sends no body at all.
    first = client.post(f"/requests/{rid}/pay", headers=auth(bob, "k1"))
    assert first.status_code == 200, first.text

    # Replay with no body is the same request.
    again = client.post(f"/requests/{rid}/pay", headers=auth(bob, "k1"))
    assert again.status_code == 200, again.text

    # A body, even an ignored one, is a different request.
    body = client.post(f"/requests/{rid}/pay", json={"x": 1}, headers=auth(bob, "k1"))
    assert body.status_code == 409, body.text


def test_splits_divergence_is_seen_when_only_an_unknown_field_changes(client):
    tok = login(client, "ada")
    body = {"amount": 30, "participants": ["bob", "op"]}

    assert client.post("/splits", json=body, headers=auth(tok, "k1")).status_code == 200
    second = client.post(
        "/splits", json={**body, "surprise": "x"}, headers=auth(tok, "k1")
    )
    assert second.status_code == 409, second.text
    assert second.json()["code"] == "idempotency_key_reuse", second.text


def test_settlements_divergence_is_seen_when_only_an_unknown_field_changes(client):
    tok = login(client, "op")
    body = {"entries": [{"from": "op", "to": "ada", "amount": 40}]}

    assert client.post("/settlements", json=body, headers=auth(tok, "k1")).status_code == 200
    second = client.post(
        "/settlements", json={**body, "surprise": "x"}, headers=auth(tok, "k1")
    )
    assert second.status_code == 409, second.text
    assert second.json()["code"] == "idempotency_key_reuse", second.text


def test_create_authorization_divergence_is_seen_when_only_an_unknown_field_changes(client):
    """Regression guard for the path that already hashed raw bytes."""
    tok = login(client, "ada")
    body = {"to": "bob", "amount": 100}

    assert client.post("/authorizations", json=body, headers=auth(tok, "k1")).status_code == 201
    second = client.post(
        "/authorizations", json={**body, "surprise": "x"}, headers=auth(tok, "k1")
    )
    assert second.status_code == 409, second.text


def test_capture_divergence_is_seen_when_only_an_unknown_field_changes(client):
    """This path is stricter still: unknown fields are refused outright.

    Capture does not merely fingerprint them differently, it rejects the request
    with 422 ``validation_failed`` before idempotency is consulted. Asserted so
    the stronger guarantee is pinned rather than assumed.
    """
    tok = login(client, "ada")
    bob = login(client, "bob")
    aid = client.post(
        "/authorizations", json={"to": "bob", "amount": 100}, headers=auth(tok, "a1")
    ).json()["id"]

    body = {"amount": 40, "final": False}
    assert client.post(
        f"/authorizations/{aid}/capture", json=body, headers=auth(bob, "k1")
    ).status_code == 201
    second = client.post(
        f"/authorizations/{aid}/capture", json={**body, "surprise": "x"},
        headers=auth(bob, "k1")
    )
    assert second.status_code == 422, second.text
    assert second.json()["code"] == "validation_failed", second.text

    # And a genuinely different known field is still a 409 divergence.
    third = client.post(
        f"/authorizations/{aid}/capture", json={"amount": 41, "final": False},
        headers=auth(bob, "k1")
    )
    assert third.status_code == 409, third.text
    assert third.json()["code"] == "idempotency_key_reuse", third.text


# ----------------------------------------------------------------------
# The other half: an identical replay must still replay, not 409.
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "path,body,handle",
    [
        ("/payments", {"to": "bob", "amount": 10}, "ada"),
        ("/requests", {"to": "bob", "amount": 10}, "ada"),
        ("/splits", {"amount": 30, "participants": ["bob", "op"]}, "ada"),
    ],
)
def test_an_identical_replay_still_replays(client, path, body, handle):
    """Raw-byte hashing must not break the ordinary replay path."""
    tok = login(client, handle)
    first = client.post(path, json=body, headers=auth(tok, "k1"))
    assert first.status_code in (200, 201), first.text
    second = client.post(path, json=body, headers=auth(tok, "k1"))
    assert second.status_code == first.status_code, second.text
    assert second.json() == first.json(), "an identical replay changed the response"


def test_keys_remain_scoped_per_user_across_paths(client):
    """Two users may use the same key without colliding."""
    ada = login(client, "ada")
    bob = login(client, "bob")
    body = {"to": "bob", "amount": 10}

    r1 = client.post("/payments", json=body, headers=auth(ada, "shared"))
    assert r1.status_code == 200, r1.text
    r2 = client.post("/payments", json=body, headers=auth(bob, "shared"))
    # bob is the recipient of his own request, so he gets a self-payment 422
    # rather than ada's replay - proof the key did not leak across users.
    assert r2.status_code == 422, r2.text
    assert r2.json()["code"] != "idempotency_key_reuse", r2.text


def test_a_blank_body_is_rejected_rather_than_treated_as_absent(client):
    """A body-carrying write must not accept whitespace as "no body".

    The fingerprint normalises a blank body to ``{}`` so a genuine no-body retry
    replays, but on a path where the body is required, whitespace fails JSON
    decoding first and is refused. Pinned so the two rules cannot be confused.
    """
    tok = login(client, "ada")
    first = client.post("/payments", json={"to": "bob", "amount": 10},
                        headers=auth(tok, "k1"))
    assert first.status_code == 200, first.text

    blank = client.post("/payments", content=b"   ",
                        headers={**auth(tok, "k1"), "Content-Type": "application/json"})
    assert blank.status_code == 422, blank.text

    # The original request is untouched by the refused attempt.
    assert client.get("/me", headers=auth(tok)).json()["balance"] == 1990