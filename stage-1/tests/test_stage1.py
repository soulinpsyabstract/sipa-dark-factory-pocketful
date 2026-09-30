"""In-process test suite for Pocketful stage 1.

Runs the FastAPI app through ``TestClient`` - no server, no Docker, no network.
``verify_stage1.py`` is the counterpart that drives a live container over HTTP.

    python -m pytest tests -v
"""

from __future__ import annotations

import os
import sys
import threading

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.main import app  # noqa: E402
from app.store import Store  # noqa: E402

PW = "correct-horse-battery"

FIXTURE = {
    "seeded_total": 100000,
    "users": [
        {"handle": "alice", "email": "alice@example.com", "balance": 60000, "password": PW},
        {"handle": "bob", "email": "bob@example.com", "balance": 30000, "password": PW},
        {"handle": "carol", "email": "carol@example.com", "balance": 10000, "password": PW},
        {"handle": "op", "email": "op@example.com", "balance": 0, "password": PW, "is_operator": True},
    ],
}

AMOUNT_REJECTED = [0, -5, 1.5, "100", True, None, []]


@pytest.fixture()
def client():
    with TestClient(app) as c:
        c.post("/_test/reset", json=FIXTURE)
        yield c


def token(client, handle="alice"):
    email = "op@example.com" if handle == "op" else f"{handle}@example.com"
    r = client.post("/auth/login", json={"email": email, "password": PW})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def auth(tok, key=None):
    h = {"Authorization": f"Bearer {tok}"}
    if key:
        h["Idempotency-Key"] = key
    return h


def inv(client):
    return client.get("/_test/export").json()


def assert_invariants(client):
    snap = inv(client)
    assert snap["invariants"]["balance_sum_equals_seeded_total"], snap["invariants"]
    assert snap["invariants"]["no_negative_balances"], snap["invariants"]
    assert snap["invariants"]["all_balances_owned_by_users"], snap["invariants"]
    assert snap["invariants"]["no_plaintext_passwords"], snap["invariants"]
    return snap


# ---------------------------------------------------------------- W1


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_app_binds_port_env():
    # main.main() reads $PORT; check the default is 8080 per spec section 1.
    import importlib

    os.environ.pop("PORT", None)
    src = open(os.path.join(os.path.dirname(app.__file__), "..", "app", "main.py"), encoding="utf-8").read()
    assert 'os.environ.get("PORT", "8080")' in src


# ---------------------------------------------------------------- W2


def test_reset_seeds_and_sums(client):
    snap = inv(client)
    assert snap["seeded_total"] == 100000
    assert sum(snap["balances"].values()) == 100000
    assert snap["balance_sum"] == 100000
    assert_invariants(client)


def test_export_import_round_trip(client):
    client.post("/payments", json={"to": "bob", "amount": 500}, headers=auth(token(client)))
    before = inv(client)
    r = client.post("/_test/import", json=before)
    assert r.status_code == 200
    after = inv(client)
    for field in ("seeded_total", "balances", "requests", "activity"):
        assert before[field] == after[field], field


def test_fixture_total_mismatch_rejected(client):
    r = client.post("/_test/reset", json={"users": [{"handle": "x", "balance": 7}], "seeded_total": 999})
    assert r.status_code == 422
    assert inv(client)["seeded_total"] == 100000  # previous state untouched


def test_concurrent_reset_import_never_corrupts(client):
    errors = []
    codes = []

    def worker(i):
        local = TestClient(app)
        try:
            for step in range(6):
                if (step + i) % 2 == 0:
                    r = local.post("/_test/reset", json=FIXTURE)
                else:
                    r = local.post("/_test/import", json=local.get("/_test/export").json())
                codes.append(r.status_code)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    assert all(c < 500 for c in codes), [c for c in codes if c >= 500]
    snap = assert_invariants(client)
    assert snap["balance_sum"] == snap["seeded_total"]


def test_store_uses_a_threading_lock():
    import threading as _t

    store = Store()
    assert isinstance(store.lock, type(_t.Lock()))


# ---------------------------------------------------------------- W3


def test_signup_derives_handle(client):
    r = client.post(
        "/auth/signup",
        json={"email": "Dana.Lopez+work@example.com", "password": PW, "display_name": "Dana"},
    )
    assert r.status_code == 200
    assert r.json()["handle"] == "danalopezwork"


@pytest.mark.parametrize("email", ["!!!@example.com", "   @example.com", "a" * 40 + "@example.com"])
def test_signup_rejects_unconformable_handle(client, email):
    r = client.post("/auth/signup", json={"email": email, "password": PW, "display_name": "X"})
    assert r.status_code == 422


def test_signup_rejects_bad_explicit_handle(client):
    r = client.post(
        "/auth/signup",
        json={"email": "zed@example.com", "password": PW, "display_name": "Z", "handle": "BAD-HANDLE!"},
    )
    assert r.status_code == 422


def test_signup_rejects_duplicate(client):
    r = client.post("/auth/signup", json={"email": "alice@example.com", "password": PW, "display_name": "A"})
    assert 400 <= r.status_code < 500


def test_login_wrong_password_and_me_unauth(client):
    assert client.post("/auth/login", json={"email": "alice@example.com", "password": "nope"}).status_code == 401
    assert client.post("/auth/login", json={"email": "ghost@example.com", "password": PW}).status_code == 401
    assert client.get("/me").status_code == 401
    assert client.get("/me", headers=auth("garbage")).status_code == 401


def test_me_round_trip(client):
    r = client.get("/me", headers=auth(token(client)))
    assert r.status_code == 200
    assert r.json()["handle"] == "alice"
    assert r.json()["user"]["handle"] == "alice"


def test_no_plaintext_password_in_store(client):
    client.post("/auth/signup", json={"email": "zoe@example.com", "password": PW, "display_name": "Z"})
    dump = client.get("/_test/export").text
    assert PW not in dump
    assert all(u["password_hash"].startswith("$2") for u in inv(client)["users"])


# ---------------------------------------------------------------- W4


def test_payment_moves_exact_amount(client):
    before = inv(client)["balances"]
    r = client.post("/payments", json={"to": "bob", "amount": 2500}, headers=auth(token(client)))
    assert r.status_code == 200
    after = inv(client)["balances"]
    assert after["alice"] == before["alice"] - 2500
    assert after["bob"] == before["bob"] + 2500
    assert_invariants(client)


def test_insufficient_funds_no_mutation(client):
    snap = inv(client)["balances"]
    r = client.post("/payments", json={"to": "bob", "amount": 10**9}, headers=auth(token(client)))
    assert r.status_code == 422
    assert inv(client)["balances"] == snap
    assert_invariants(client)


@pytest.mark.parametrize("amount", AMOUNT_REJECTED)
def test_non_integer_or_non_positive_amounts_rejected(client, amount):
    r = client.post("/payments", json={"to": "bob", "amount": amount}, headers=auth(token(client)))
    assert r.status_code == 422, amount


def test_payment_unknown_recipient_and_self(client):
    t = token(client)
    assert client.post("/payments", json={"to": "ghost", "amount": 1}, headers=auth(t)).status_code == 404
    assert client.post("/payments", json={"to": "alice", "amount": 1}, headers=auth(t)).status_code == 422
    assert client.post("/payments", json={"to": "bob", "amount": 1}).status_code == 401


# ---------------------------------------------------------------- W5


def test_request_lifecycle_and_guards(client):
    a, b, c = (token(client, h) for h in ("alice", "bob", "carol"))
    r = client.post("/requests", json={"to": "bob", "amount": 5000}, headers=auth(a))
    assert r.status_code == 200
    rid = r.json()["id"]
    assert r.json()["status"] == "open"
    assert inv(client)["balances"] == {"alice": 60000, "bob": 30000, "carol": 10000, "op": 0}

    assert client.post(f"/requests/{rid}/pay", headers=auth(a)).status_code == 403
    assert client.post(f"/requests/{rid}/pay", headers=auth(c)).status_code == 403
    assert client.post(f"/requests/{rid}/cancel", headers=auth(b)).status_code == 403
    assert client.post(f"/requests/{rid}/decline", headers=auth(a)).status_code == 403

    before = inv(client)["balances"]
    assert client.post(f"/requests/{rid}/pay", headers=auth(b)).status_code == 200
    after = inv(client)["balances"]
    assert after["bob"] == before["bob"] - 5000
    assert after["alice"] == before["alice"] + 5000
    assert inv(client)["requests"][0]["status"] == "paid"
    assert_invariants(client)

    for verb, tok in (("pay", b), ("decline", b), ("cancel", a)):
        assert client.post(f"/requests/{rid}/{verb}", headers=auth(tok)).status_code == 409, verb


def test_request_decline_and_cancel(client):
    a, b = token(client), token(client, "bob")
    rid1 = client.post("/requests", json={"to": "bob", "amount": 10}, headers=auth(a)).json()["id"]
    assert client.post(f"/requests/{rid1}/decline", headers=auth(b)).status_code == 200
    assert inv(client)["requests"][0]["status"] == "declined"

    rid2 = client.post("/requests", json={"to": "bob", "amount": 20}, headers=auth(a)).json()["id"]
    assert client.post(f"/requests/{rid2}/cancel", headers=auth(a)).status_code == 200
    assert inv(client)["requests"][0]["status"] == "cancelled"

    assert client.post(f"/requests/{rid2}/pay", headers=auth(b)).status_code == 409
    assert client.post("/requests/req_99999/pay", headers=auth(b)).status_code == 404


def test_request_filters(client):
    a, b = token(client), token(client, "bob")
    client.post("/requests", json={"to": "bob", "amount": 10}, headers=auth(a))
    client.post("/requests", json={"to": "alice", "amount": 20}, headers=auth(b))

    h = auth(a)
    everything = client.get("/requests", headers=h).json()["requests"]
    outgoing = client.get("/requests?direction=outgoing", headers=h).json()["requests"]
    incoming = client.get("/requests?direction=incoming", headers=h).json()["requests"]
    assert len(everything) == len(outgoing) + len(incoming) == 2
    assert {r["id"] for r in outgoing} & {r["id"] for r in incoming} == set()
    assert all(r["from"] == "alice" for r in outgoing)
    assert all(r["to"] == "alice" for r in incoming)

    client.post(f"/requests/{outgoing[0]['id']}/pay", headers=auth(b))
    assert all(r["status"] == "paid" for r in client.get("/requests?status=paid", headers=h).json()["requests"])
    assert all(r["status"] == "open" for r in client.get("/requests?status=open", headers=h).json()["requests"])
    assert client.get("/requests?status=paid,open", headers=h).json()["count"] == 2

    assert client.get("/requests?direction=sideways", headers=h).status_code == 422
    assert client.get("/requests?status=weird", headers=h).status_code == 422
    assert client.get("/requests").status_code == 401


# ---------------------------------------------------------------- W6


@pytest.mark.parametrize(
    "amount,count,expected",
    [
        (100, 3, [34, 33, 33]),
        (10, 4, [3, 3, 2, 2]),
        (10, 3, [4, 3, 3]),
        (1, 4, [1, 0, 0, 0]),
        (1000, 1, [1000]),
        (7, 3, [3, 2, 2]),
    ],
)
def test_split_distribution(client, amount, count, expected):
    handles = ["bob", "carol", "op"][:count]
    r = client.post(
        "/splits", json={"amount": amount, "participants": handles}, headers=auth(token(client))
    )
    assert r.status_code == 200, r.text
    assert r.json()["shares"] == expected
    assert sum(p["amount"] for p in r.json()["parts"]) == amount
    assert_invariants(client)


def test_split_remainder_goes_to_first(client):
    client.post("/auth/signup", json={"email": "dave@example.com", "password": PW, "display_name": "D"})
    r = client.post(
        "/splits",
        json={"amount": 10, "participants": ["dave", "op", "carol", "bob"]},
        headers=auth(token(client)),
    )
    parts = r.json()["parts"]
    assert r.json()["shares"] == [3, 3, 2, 2]
    assert [p["handle"] for p in parts[:2]] == ["dave", "op"]
    assert_invariants(client)


def test_split_negative_debit_rejected(client):
    snap = inv(client)["balances"]
    r = client.post("/splits", json={"amount": 10**9, "participants": ["bob"]}, headers=auth(token(client)))
    assert r.status_code == 422
    assert inv(client)["balances"] == snap
    assert client.post("/splits", json={"amount": 500, "participants": ["alice"]}, headers=auth(token(client, "op"))).status_code == 422


def test_split_validation(client):
    a = auth(token(client))
    assert client.post("/splits", json={"amount": 100, "participants": []}, headers=a).status_code == 422
    assert client.post("/splits", json={"amount": 100, "participants": ["bob", "bob"]}, headers=a).status_code == 422
    assert client.post("/splits", json={"amount": 100, "participants": ["ghost"]}, headers=a).status_code == 404
    assert client.post("/splits", json={"amount": 1.5, "participants": ["bob", "carol"]}, headers=a).status_code == 422
    assert client.post("/splits", json={"amount": 1, "participants": ["bob"]}).status_code == 401


# ---------------------------------------------------------------- W7


@pytest.mark.parametrize("case", ["payments", "requests", "pay", "splits", "settlements"])
def test_idempotent_replay_on_all_five(client, case):
    a, b, op = (token(client, h) for h in ("alice", "bob", "op"))

    if case == "payments":
        first = client.post("/payments", json={"to": "bob", "amount": 100}, headers=auth(a, "k"))
        again = lambda: client.post("/payments", json={"to": "bob", "amount": 100}, headers=auth(a, "k"))  # noqa: E731
    elif case == "requests":
        first = client.post("/requests", json={"to": "bob", "amount": 100}, headers=auth(a, "k"))
        again = lambda: client.post("/requests", json={"to": "bob", "amount": 100}, headers=auth(a, "k"))  # noqa: E731
    elif case == "pay":
        rid = client.post("/requests", json={"to": "bob", "amount": 100}, headers=auth(a, "k2")).json()["id"]
        first = client.post(f"/requests/{rid}/pay", headers=auth(b, "k"))
        again = lambda: client.post(f"/requests/{rid}/pay", headers=auth(b, "k"))  # noqa: E731
    elif case == "splits":
        first = client.post("/splits", json={"amount": 100, "participants": ["bob", "carol", "op"]}, headers=auth(a, "k"))
        again = lambda: client.post("/splits", json={"amount": 100, "participants": ["bob", "carol", "op"]}, headers=auth(a, "k"))  # noqa: E731
    else:
        body = {"entries": [{"from": "bob", "to": "carol", "amount": 100}]}
        first = client.post("/settlements", json=body, headers=auth(op, "k"))
        again = lambda: client.post("/settlements", json=body, headers=auth(op, "k"))  # noqa: E731

    assert first.status_code == 200, first.text
    before = inv(client)["balances"]
    replay = again()
    assert replay.status_code == first.status_code
    assert replay.json() == first.json()
    after = assert_invariants(client)
    assert after["balances"] == before, "replay double-processed"


def test_idempotency_is_scoped_per_user(client):
    a, b = token(client), token(client, "bob")
    one = client.post("/payments", json={"to": "carol", "amount": 500}, headers=auth(a, "shared"))
    two = client.post("/payments", json={"to": "carol", "amount": 500}, headers=auth(b, "shared"))
    assert one.status_code == 200 and two.status_code == 200
    assert one.json()["payment_id"] != two.json()["payment_id"]
    assert inv(client)["balances"]["bob"] == 29500


def test_different_key_processes_again(client):
    a = token(client)
    one = client.post("/payments", json={"to": "carol", "amount": 500}, headers=auth(a, "key-one"))
    two = client.post("/payments", json={"to": "carol", "amount": 500}, headers=auth(a, "key-two"))
    assert one.json()["payment_id"] != two.json()["payment_id"]


def test_same_key_different_body_conflicts(client):
    a = token(client)
    client.post("/payments", json={"to": "carol", "amount": 500}, headers=auth(a, "dup"))
    r = client.post("/payments", json={"to": "carol", "amount": 501}, headers=auth(a, "dup"))
    assert r.status_code == 409


def test_no_key_means_no_idempotency(client):
    a = token(client)
    one = client.post("/payments", json={"to": "carol", "amount": 100}, headers=auth(a))
    two = client.post("/payments", json={"to": "carol", "amount": 100}, headers=auth(a))
    assert one.json()["payment_id"] != two.json()["payment_id"]


# ---------------------------------------------------------------- W8


def test_activity_feed(client):
    a, b, op = (token(client, h) for h in ("alice", "bob", "op"))
    client.post("/payments", json={"to": "bob", "amount": 111}, headers=auth(a))
    rid = client.post("/requests", json={"to": "bob", "amount": 222}, headers=auth(a)).json()["id"]
    client.post(f"/requests/{rid}/pay", headers=auth(b))
    client.post("/splits", json={"amount": 300, "participants": ["bob", "carol", "op"]}, headers=auth(a))
    client.post("/settlements", json={"entries": [{"from": "carol", "to": "alice", "amount": 50}]}, headers=auth(op))

    for handle, tok in (("alice", a), ("bob", b)):
        types = {e["type"] for e in client.get("/activity", headers=auth(tok)).json()["activity"]}
        for expected in ("payment", "request_created", "request_paid", "split", "settlement"):
            assert expected in types, f"{handle} feed missing {expected}: {sorted(types)}"

    feed = client.get("/activity", headers=auth(a)).json()["activity"]
    assert all(e["handle"] == "alice" for e in feed)
    assert [e["id"] for e in feed] == sorted([e["id"] for e in feed], reverse=True)
    assert client.get("/activity").status_code == 401


def test_settlements_operator_only(client):
    snap = inv(client)["balances"]
    for handle in ("alice", "bob", "carol"):
        r = client.post(
            "/settlements",
            json={"entries": [{"from": "alice", "to": "bob", "amount": 10}]},
            headers=auth(token(client, handle)),
        )
        assert r.status_code == 403, handle
    assert inv(client)["balances"] == snap
    assert client.post("/settlements", json={"entries": [{"from": "a", "to": "b", "amount": 1}]}).status_code == 401


def test_settlement_moves_money_and_is_all_or_nothing(client):
    op = token(client, "op")
    before = inv(client)["balances"]
    r = client.post("/settlements", json={"entries": [{"from": "alice", "to": "bob", "amount": 1000}]}, headers=auth(op))
    assert r.status_code == 200
    after = inv(client)["balances"]
    assert after["alice"] == before["alice"] - 1000 and after["bob"] == before["bob"] + 1000
    assert_invariants(client)

    snap = inv(client)["balances"]
    bad = client.post(
        "/settlements",
        json={"entries": [{"from": "alice", "to": "bob", "amount": 5}, {"from": "carol", "to": "bob", "amount": 10**9}]},
        headers=auth(op),
    )
    assert bad.status_code == 422
    assert inv(client)["balances"] == snap
    assert client.post("/settlements", json={"entries": [{"from": "a", "to": "a", "amount": 1}]}, headers=auth(op)).status_code == 422
    assert client.post("/settlements", json={"entries": []}, headers=auth(op)).status_code == 422


# ---------------------------------------------------------------- inventory


def test_all_sixteen_endpoints_present(client):
    a, b, op = (token(client, h) for h in ("alice", "bob", "op"))
    rid = client.post("/requests", json={"to": "bob", "amount": 1}, headers=auth(a)).json()["id"]
    rid2 = client.post("/requests", json={"to": "bob", "amount": 1}, headers=auth(a)).json()["id"]
    export = client.get("/_test/export").json()

    probes = [
        client.get("/health"),
        client.post("/_test/reset", json=FIXTURE),
        client.get("/_test/export"),
        client.post("/_test/import", json=export),
        client.post("/auth/signup", json={"email": "zed@example.com", "password": PW, "display_name": "Z"}),
        client.post("/auth/login", json={"email": "alice@example.com", "password": PW}),
        client.get("/me", headers=auth(a)),
        client.post("/payments", json={"to": "bob", "amount": 1}, headers=auth(a)),
        client.post("/requests", json={"to": "bob", "amount": 1}, headers=auth(a)),
        client.post(f"/requests/{rid}/pay", headers=auth(b)),
        client.post(f"/requests/{rid}/decline", headers=auth(b)),
        client.post(f"/requests/{rid2}/cancel", headers=auth(a)),
        client.get("/requests", headers=auth(a)),
        client.post("/splits", json={"amount": 3, "participants": ["bob", "carol", "op"]}, headers=auth(a)),
        client.get("/activity", headers=auth(a)),
        client.post("/settlements", json={"entries": [{"from": "bob", "to": "carol", "amount": 1}]}, headers=auth(op)),
    ]
    assert len(probes) == 16
    bad = [(r.request.method, r.request.url, r.status_code) for r in probes if r.status_code >= 300]
    assert not bad, bad


def test_no_state_leaks_between_fixtures(client):
    client.post("/payments", json={"to": "bob", "amount": 5000}, headers=auth(token(client)))
    assert inv(client)["balances"]["alice"] == 55000
    client.post("/_test/reset", json=FIXTURE)
    assert inv(client)["balances"]["alice"] == 60000
    assert_invariants(client)
