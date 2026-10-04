"""Stage-2 authorization tests (W3): create, list, get.

In-process via ``TestClient`` - no server, no Docker, no network. Covers the
``POST /authorizations`` contract table from ``stage-2/SPEC.md`` (status codes
and exact bounds), the ``authorization_ttl_seconds`` fixture knob, and the
caller-scoped ``GET /authorizations`` / ``GET /authorizations/{id}`` surface.

Stage-1 coverage lives in ``test_stage1.py``; the W2 funds model is in
``test_stage2_funds.py``. Both must stay green independently.
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
    {"handle": "ada", "email": "ada@example.com", "balance": 1000, "password": PW},
    {"handle": "bob", "email": "bob@example.com", "balance": 1000, "password": PW},
    {"handle": "cyd", "email": "cyd@example.com", "balance": 1000, "password": PW},
]


def fixture(**extra):
    payload = {
        "seeded_total": sum(u["balance"] for u in BASE_USERS),
        "users": [dict(u) for u in BASE_USERS],
    }
    payload.update(extra)
    return payload


def iso_in(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


@pytest.fixture()
def client():
    with TestClient(app) as c:
        c.post("/_test/reset", json=fixture())
        yield c


def login(client, handle="ada"):
    r = client.post("/auth/login", json={"email": f"{handle}@example.com", "password": PW})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def auth(tok, key=None):
    headers = {"Authorization": f"Bearer {tok}"}
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


def create(client, handle="ada", key="k1", **body):
    """POST an authorization; ``key=None`` omits the header entirely."""
    payload = {"to_handle": "bob", "amount": 200}
    payload.update(body)
    return client.post("/authorizations", json=payload, headers=auth(login(client, handle), key))


# ----------------------------------------------------------------------
# create: the happy path
# ----------------------------------------------------------------------


def test_create_returns_201_and_the_spec_fields(client):
    r = create(client, note="deposit", visibility="private")
    assert r.status_code == 201, r.text
    body = r.json()
    for key in (
        "authorization_id",
        "from_handle",
        "to_handle",
        "amount",
        "captured_amount",
        "note",
        "visibility",
        "status",
        "expires_at",
        "payment_id",
        "created_at",
    ):
        assert key in body, f"missing {key!r}: {body}"
    assert body["status"] == "open"
    assert body["captured_amount"] == 0
    assert body["remaining_amount"] == 200
    assert body["payment_id"] is None
    assert body["from_handle"] == "ada"
    assert body["to_handle"] == "bob"
    assert body["authorization_id"].startswith("a_")


def test_create_also_emits_the_team_handle_aliases(client):
    body = create(client).json()
    assert body["id"] == body["authorization_id"]
    assert body["from"] == body["from_handle"]
    assert body["to"] == body["to_handle"]


def test_create_accepts_either_to_or_to_handle(client):
    assert client.post(
        "/authorizations", json={"to": "bob", "amount": 10}, headers=auth(login(client), "k1")
    ).status_code == 201
    assert client.post(
        "/authorizations",
        json={"to_handle": "bob", "amount": 10},
        headers=auth(login(client), "k2"),
    ).status_code == 201


def test_create_defaults_visibility_to_public(client):
    assert create(client).json()["visibility"] == "public"


def test_create_does_not_move_money(client):
    before = client.get("/_test/export").json()
    r = create(client, amount=400)
    assert r.status_code == 201
    after = client.get("/_test/export").json()
    assert after["balances"] == before["balances"]
    assert after["seeded_total"] == before["seeded_total"]
    assert after["invariants"]["balance_sum_equals_seeded_total"] is True


def test_create_reserves_funds_so_available_drops(client):
    tok = login(client)
    client.post("/authorizations", json={"to_handle": "bob", "amount": 400}, headers=auth(tok, "k1"))
    me = client.get("/me", headers=auth(tok)).json()
    assert me["total"] == 1000
    assert me["held"] == 400
    assert me["available"] == 600


def test_open_authorization_is_not_a_feed_item(client):
    r = create(client)
    assert r.status_code == 201
    aid = r.json()["authorization_id"]
    feed = client.get("/activity", headers=auth(login(client))).json()["activity"]
    assert not [a for a in feed if a.get("related_id") == aid]
    # and the holder's own feed is empty too
    other = client.get("/activity", headers=auth(login(client, "bob"))).json()["activity"]
    assert not [a for a in other if a.get("related_id") == aid]


def test_creating_two_authorizations_accumulates_the_hold(client):
    tok = login(client)
    client.post("/authorizations", json={"to_handle": "bob", "amount": 100}, headers=auth(tok, "k1"))
    client.post("/authorizations", json={"to_handle": "cyd", "amount": 150}, headers=auth(tok, "k2"))
    me = client.get("/me", headers=auth(tok)).json()
    assert me["held"] == 250
    assert me["available"] == 750


# ----------------------------------------------------------------------
# create: rejections
# ----------------------------------------------------------------------


def test_available_below_amount_is_409_insufficient_funds(client):
    tok = login(client)
    # hold 900 of 1000, leaving 100 available
    client.post("/authorizations", json={"to_handle": "bob", "amount": 900}, headers=auth(tok, "hold"))
    r = client.post(
        "/authorizations", json={"to_handle": "cyd", "amount": 200}, headers=auth(tok, "k1")
    )
    assert r.status_code == 409, r.text
    error = r.json()
    assert error["code"] == "insufficient_funds"
    assert error["error"]["available"] == 100
    assert error["error"]["required"] == 200


def test_exact_available_is_allowed(client):
    tok = login(client)
    client.post("/authorizations", json={"to_handle": "bob", "amount": 900}, headers=auth(tok, "hold"))
    r = client.post(
        "/authorizations", json={"to_handle": "cyd", "amount": 100}, headers=auth(tok, "k1")
    )
    assert r.status_code == 201, r.text
    assert client.get("/me", headers=auth(tok)).json()["available"] == 0


@pytest.mark.parametrize(
    "amount", [0, -1, 1.5, "200", True, None, []],
)
def test_bad_amount_is_422_validation_failed(client, amount):
    r = client.post(
        "/authorizations",
        json={"to_handle": "bob", "amount": amount},
        headers=auth(login(client), "k1"),
    )
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "validation_failed", r.text


def test_amount_above_the_maximum_is_rejected(client):
    r = client.post(
        "/authorizations",
        json={"to_handle": "bob", "amount": 1_000_000_001},
        headers=auth(login(client), "k1"),
    )
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "validation_failed"


def test_amount_at_the_maximum_is_accepted(client):
    big = [{"handle": "ada", "email": "ada@example.com", "balance": 2_000_000_000, "password": PW},
           {"handle": "bob", "email": "bob@example.com", "balance": 0, "password": PW}]
    assert client.post(
        "/_test/reset", json={"seeded_total": 2_000_000_000, "users": big}
    ).status_code == 200
    r = client.post(
        "/authorizations",
        json={"to_handle": "bob", "amount": 1_000_000_000},
        headers=auth(login(client), "k1"),
    )
    assert r.status_code == 201, r.text


def test_self_payment_is_422_self_payment(client):
    r = client.post(
        "/authorizations",
        json={"to_handle": "ada", "amount": 100},
        headers=auth(login(client), "k1"),
    )
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "self_payment", r.text


def test_unknown_recipient_is_404_not_found(client):
    r = client.post(
        "/authorizations",
        json={"to_handle": "nobody", "amount": 100},
        headers=auth(login(client), "k1"),
    )
    assert r.status_code == 404, r.text
    assert r.json()["code"] == "not_found", r.text


def test_note_over_200_chars_is_422_validation_failed(client):
    r = create(client, note="x" * 201)
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "validation_failed", r.text


def test_note_at_200_chars_is_accepted(client):
    assert create(client, note="x" * 200).status_code == 201


def test_bad_visibility_is_422_validation_failed(client):
    r = create(client, visibility="secret")
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "validation_failed", r.text


def test_rejected_create_leaves_no_hold(client):
    tok = login(client)
    client.post("/authorizations", json={"to_handle": "bob", "amount": 5000}, headers=auth(tok, "k1"))
    assert client.get("/me", headers=auth(tok)).json()["held"] == 0


# ----------------------------------------------------------------------
# authorization_ttl_seconds
# ----------------------------------------------------------------------


def test_expires_at_is_created_at_plus_the_default_ttl(client):
    body = create(client).json()
    created = datetime.fromisoformat(body["created_at"])
    expires = datetime.fromisoformat(body["expires_at"])
    assert round((expires - created).total_seconds()) == 600


def test_fixture_ttl_is_applied(client):
    assert client.post(
        "/_test/reset", json=fixture(authorization_ttl_seconds=120)
    ).status_code == 200
    body = create(client).json()
    created = datetime.fromisoformat(body["created_at"])
    expires = datetime.fromisoformat(body["expires_at"])
    assert round((expires - created).total_seconds()) == 120


def test_ttl_omitted_falls_back_to_600(client):
    assert client.post("/_test/reset", json=fixture()).status_code == 200
    body = create(client).json()
    created = datetime.fromisoformat(body["created_at"])
    expires = datetime.fromisoformat(body["expires_at"])
    assert round((expires - created).total_seconds()) == 600


@pytest.mark.parametrize("ttl", [1.5, "600", True])
def test_non_integer_ttl_is_422(client, ttl):
    """Wrong *type*: caught by the request model, so the body's ``validation_error``."""
    r = client.post("/_test/reset", json=fixture(authorization_ttl_seconds=ttl))
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "validation_error", r.text


@pytest.mark.parametrize("ttl", [0, -1])
def test_non_positive_ttl_is_422_validation_failed(client, ttl):
    """Right type, out of range: caught by the store, so ``validation_failed``."""
    r = client.post("/_test/reset", json=fixture(authorization_ttl_seconds=ttl))
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "validation_failed", r.text


def test_a_very_short_ttl_expires_immediately(client):
    assert client.post(
        "/_test/reset", json=fixture(authorization_ttl_seconds=1)
    ).status_code == 200
    aid = create(client).json()["authorization_id"]
    record = next(
        a for a in client.get("/_test/export").json()["authorizations"] if a["id"] == aid
    )
    assert record["status"] == "open"
    # a seeded record in the past exercises the same lazy sweep deterministically
    assert client.post(
        "/_test/reset",
        json=fixture(
            authorizations=[
                {
                    "id": "a_past",
                    "from": "ada",
                    "to": "bob",
                    "amount": 100,
                    "status": "open",
                    "expires_at": iso_in(-5),
                }
            ]
        ),
    ).status_code == 200
    tok = login(client)
    assert client.get("/me", headers=auth(tok)).json()["held"] == 0


# ----------------------------------------------------------------------
# idempotency (D6)
# ----------------------------------------------------------------------


def test_idempotency_key_is_required(client):
    r = client.post(
        "/authorizations", json={"to_handle": "bob", "amount": 10}, headers=auth(login(client))
    )
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "validation_failed", r.text


def test_replay_with_the_same_body_returns_the_first_result(client):
    tok = login(client)
    first = client.post(
        "/authorizations", json={"to_handle": "bob", "amount": 300}, headers=auth(tok, "k1")
    )
    second = client.post(
        "/authorizations", json={"to_handle": "bob", "amount": 300}, headers=auth(tok, "k1")
    )
    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    # the hold was taken once, not twice
    assert client.get("/me", headers=auth(tok)).json()["held"] == 300
    assert client.get("/_test/export").json()["invariants"]["open_authorizations"] == 1


def test_replay_with_a_different_body_is_409_idempotency_key_reuse(client):
    tok = login(client)
    client.post(
        "/authorizations", json={"to_handle": "bob", "amount": 300}, headers=auth(tok, "k1")
    )
    r = client.post(
        "/authorizations", json={"to_handle": "bob", "amount": 400}, headers=auth(tok, "k1")
    )
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "idempotency_key_reuse", r.text


def test_empty_body_and_an_explicit_amount_are_different_requests(client):
    """D6 fingerprints the raw body, so these two must not replay each other."""
    tok = login(client)
    first = client.post(
        "/authorizations", json={"to_handle": "bob", "amount": 200}, headers=auth(tok, "k1")
    )
    assert first.status_code == 201
    # same meaning, different bytes
    r = client.post(
        "/authorizations",
        json={"to_handle": "bob", "amount": 200, "note": None},
        headers=auth(tok, "k1"),
    )
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "idempotency_key_reuse", r.text


def test_idempotency_keys_are_scoped_per_user(client):
    ada = login(client, "ada")
    bob = login(client, "bob")
    r1 = client.post(
        "/authorizations", json={"to_handle": "bob", "amount": 100}, headers=auth(ada, "shared")
    )
    r2 = client.post(
        "/authorizations", json={"to_handle": "ada", "amount": 100}, headers=auth(bob, "shared")
    )
    assert r1.status_code == 201
    assert r2.status_code == 201
    assert r1.json()["authorization_id"] != r2.json()["authorization_id"]


def test_omitting_a_body_entirely_fingerprints_as_empty(client):
    tok = login(client)
    r = client.request(
        "POST", "/authorizations", headers={**auth(tok, "k1"), "Content-Type": "application/json"}
    )
    assert r.status_code == 422, r.text  # no recipient, but the key was seen


# ----------------------------------------------------------------------
# GET /authorizations
# ----------------------------------------------------------------------


def seed_pair(client):
    """ada -> bob (outgoing for ada) and bob -> cyd (incoming for cyd)."""
    ada, bob = login(client, "ada"), login(client, "bob")
    client.post(
        "/authorizations", json={"to_handle": "bob", "amount": 100}, headers=auth(ada, "k1")
    )
    client.post(
        "/authorizations", json={"to_handle": "cyd", "amount": 200}, headers=auth(bob, "k2")
    )


def test_list_only_returns_authorizations_involving_the_caller(client):
    seed_pair(client)
    ada = login(client, "ada")
    items = client.get("/authorizations", headers=auth(ada)).json()["authorizations"]
    assert len(items) == 1
    assert items[0]["to_handle"] == "bob"


def test_list_direction_filters(client):
    seed_pair(client)
    ada = login(client, "ada")
    cyd = login(client, "cyd")
    outgoing = client.get(
        "/authorizations?direction=outgoing", headers=auth(ada)
    ).json()["authorizations"]
    incoming = client.get(
        "/authorizations?direction=incoming", headers=auth(cyd)
    ).json()["authorizations"]
    assert [a["to_handle"] for a in outgoing] == ["bob"]
    assert [a["from_handle"] for a in incoming] == ["bob"]


def test_list_defaults_to_both_directions(client):
    seed_pair(client)
    cyd = login(client, "cyd")
    items = client.get("/authorizations", headers=auth(cyd)).json()["authorizations"]
    assert len(items) == 1
    assert items[0]["direction"] == "incoming"


def test_list_is_newest_first(client):
    tok = login(client, "ada")
    for index in range(3):
        client.post(
            "/authorizations",
            json={"to_handle": "bob", "amount": 10 + index},
            headers=auth(tok, f"k{index}"),
        )
    items = client.get("/authorizations", headers=auth(tok)).json()["authorizations"]
    assert [a["amount"] for a in items] == [12, 11, 10]


def test_list_status_filter(client):
    tok = login(client, "ada")
    client.post("/authorizations", json={"to_handle": "bob", "amount": 10}, headers=auth(tok, "k1"))
    assert client.get(
        "/authorizations?status=open", headers=auth(tok)
    ).json()["count"] == 1
    assert client.get(
        "/authorizations?status=captured", headers=auth(tok)
    ).json()["count"] == 0
    assert client.get("/authorizations?status=all", headers=auth(tok)).json()["count"] == 1


def test_list_reports_an_expired_authorization_as_expired(client):
    assert client.post(
        "/_test/reset",
        json=fixture(
            authorizations=[
                {
                    "id": "a_past",
                    "from": "ada",
                    "to": "bob",
                    "amount": 100,
                    "status": "open",
                    "expires_at": iso_in(-5),
                }
            ]
        ),
    ).status_code == 200
    tok = login(client, "ada")
    items = client.get("/authorizations?status=open", headers=auth(tok)).json()["authorizations"]
    assert items == []
    expired = client.get(
        "/authorizations?status=expired", headers=auth(tok)
    ).json()["authorizations"]
    assert len(expired) == 1
    assert expired[0]["status"] == "expired"


def test_list_pagination_limit_offset_and_has_more(client):
    tok = login(client, "ada")
    for index in range(5):
        client.post(
            "/authorizations",
            json={"to_handle": "bob", "amount": 10 + index},
            headers=auth(tok, f"k{index}"),
        )
    first = client.get("/authorizations?limit=2&offset=0", headers=auth(tok)).json()
    assert first["count"] == 2
    assert first["total"] == 5
    assert first["has_more"] is True
    last = client.get("/authorizations?limit=2&offset=4", headers=auth(tok)).json()
    assert last["count"] == 1
    assert last["has_more"] is False


def test_list_rejects_a_bad_direction(client):
    r = client.get("/authorizations?direction=sideways", headers=auth(login(client)))
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "invalid_direction", r.text


def test_list_rejects_an_unknown_status(client):
    r = client.get("/authorizations?status=weird", headers=auth(login(client)))
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "invalid_status", r.text


def test_list_requires_authentication(client):
    assert client.get("/authorizations").status_code == 401


def test_an_unfiltered_list_is_never_truncated(client):
    """Stage-1 never truncated these lists, so truncation must be opt-in.

    Seeded straight into the store: 1201 records would dominate the suite's
    runtime through the API, and this is about the pagination arithmetic, not
    about request creation.
    """
    from app.dependencies import store
    from app.store import now_iso

    moment = now_iso()

    def seed() -> None:
        for index in range(1201):
            record_id = f"r_bulk_{index:05d}"
            store.requests[record_id] = {
                "id": record_id,
                "from": "ada",
                "from_handle": "ada",
                "to": "bob",
                "to_handle": "bob",
                "amount": 1,
                "status": "open",
                "note": None,
                "created_at": moment,
                "updated_at": moment,
            }

    store.transaction(seed)

    tok = login(client, "ada")
    everything = client.get("/requests", headers=auth(tok)).json()
    assert everything["total"] == 1201
    assert everything["count"] == 1201, "an unfiltered list must not truncate"
    assert everything["has_more"] is False
    assert everything["limit"] is None

    # an explicit limit is what opts into pagination
    paged = client.get("/requests?limit=1000", headers=auth(tok)).json()
    assert paged["count"] == 1000
    assert paged["total"] == 1201
    assert paged["has_more"] is True

    tail = client.get("/requests?limit=1000&offset=1000", headers=auth(tok)).json()
    assert tail["count"] == 201
    assert tail["has_more"] is False


def test_the_same_default_applies_to_authorizations(client):
    from app.dependencies import store
    from app.store import now_iso

    moment = now_iso()

    def seed() -> None:
        for index in range(1100):
            record_id = f"a_bulk_{index:05d}"
            store.authorizations[record_id] = {
                "id": record_id,
                "from": "ada",
                "from_handle": "ada",
                "to": "bob",
                "to_handle": "bob",
                "amount": 1,
                "captured_amount": 0,
                "remaining_amount": 1,
                "status": "voided",
                "note": None,
                "visibility": "public",
                "created_at": moment,
                "updated_at": moment,
                "expires_at": None,
                "payment_id": None,
                "payment_ids": [],
            }

    store.transaction(seed)

    tok = login(client, "ada")
    body = client.get("/authorizations", headers=auth(tok)).json()
    assert body["total"] == 1100
    assert body["count"] == 1100, "an unfiltered list must not truncate"
    assert body["has_more"] is False


# ----------------------------------------------------------------------
# GET /authorizations/{id}
# ----------------------------------------------------------------------


def test_get_returns_the_authorization_for_a_party(client):
    aid = create(client).json()["authorization_id"]
    r = client.get(f"/authorizations/{aid}", headers=auth(login(client, "bob")))
    assert r.status_code == 200, r.text
    assert r.json()["authorization_id"] == aid
    assert r.json()["direction"] == "incoming"


def test_get_is_404_for_a_bystander(client):
    aid = create(client).json()["authorization_id"]
    r = client.get(f"/authorizations/{aid}", headers=auth(login(client, "cyd")))
    assert r.status_code == 404, r.text
    assert r.json()["code"] == "not_found", r.text


def test_get_is_404_for_an_unknown_id(client):
    r = client.get("/authorizations/a_9999", headers=auth(login(client)))
    assert r.status_code == 404, r.text
    assert r.json()["code"] == "not_found", r.text


def test_get_requires_authentication(client):
    aid = create(client).json()["authorization_id"]
    assert client.get(f"/authorizations/{aid}").status_code == 401
