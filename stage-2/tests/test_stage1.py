"""In-process test suite for Pocketful stage 1.

Runs the FastAPI app through ``TestClient`` - no server, no Docker, no network.
``verify_stage1.py`` is the counterpart that drives a live container over HTTP.

    python -m pytest tests -v
"""

from __future__ import annotations

import ast
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

#: Every account funded, for the tests that need participants who *can* be
#: debited. The default FIXTURE gives `op` and any new signup a zero balance,
#: and a zero-balance participant is correctly refused with 422.
RICH_FIXTURE = {
    "seeded_total": 100000,
    "users": [
        {"handle": "alice", "email": "alice@example.com", "balance": 60000, "password": PW},
        {"handle": "bob", "email": "bob@example.com", "balance": 10000, "password": PW},
        {"handle": "carol", "email": "carol@example.com", "balance": 10000, "password": PW},
        {"handle": "dave", "email": "dave@example.com", "balance": 10000, "password": PW},
        {"handle": "op", "email": "op@example.com", "balance": 10000, "password": PW, "is_operator": True},
    ],
}


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
    # main.main() reads $PORT; spec section 1 requires a default of 8080.
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app", "main.py"), encoding="utf-8").read()
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


@pytest.mark.parametrize("email", ["!!!@example.com", "   @example.com", "@example.com"])
def test_signup_rejects_unconformable_handle(client, email):
    r = client.post("/auth/signup", json={"email": email, "password": PW, "display_name": "X"})
    assert r.status_code == 422


def test_long_local_part_is_rejected_not_truncated(client):
    # Derivation does not truncate: the regex is enforced on the derived
    # value, so an over-long local part is 422 rather than silently cut to 20.
    r = client.post("/auth/signup", json={"email": "a" * 40 + "@example.com", "password": PW, "display_name": "X"})
    assert r.status_code == 422
    r = client.post("/auth/signup", json={"email": "a" * 21 + "@example.com", "password": PW, "display_name": "X"})
    assert r.status_code == 422
    r = client.post("/auth/signup", json={"email": "a" * 20 + "@example.com", "password": PW, "display_name": "X"})
    assert r.status_code == 200
    assert r.json()["handle"] == "a" * 20


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
    by_id = {r["id"]: r for r in inv(client)["requests"]}
    assert by_id[rid1]["status"] == "declined"

    rid2 = client.post("/requests", json={"to": "bob", "amount": 20}, headers=auth(a)).json()["id"]
    assert client.post(f"/requests/{rid2}/cancel", headers=auth(a)).status_code == 200
    by_id = {r["id"]: r for r in inv(client)["requests"]}
    assert by_id[rid2]["status"] == "cancelled"

    assert client.post(f"/requests/{rid2}/pay", headers=auth(b)).status_code == 409
    assert client.post("/requests/req_99999/pay", headers=auth(b)).status_code == 404


def test_request_pay_flips_status(client):
    a, b = token(client), token(client, "bob")
    rid = client.post("/requests", json={"to": "bob", "amount": 10}, headers=auth(a)).json()["id"]
    assert client.post(f"/requests/{rid}/pay", headers=auth(b)).status_code == 200
    by_id = {r["id"]: r for r in inv(client)["requests"]}
    assert by_id[rid]["status"] == "paid"


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
    # Every participant must be able to cover its share: a debit that would
    # leave a balance negative is correctly refused with 422.
    client.post("/_test/reset", json=RICH_FIXTURE)
    handles = ["bob", "carol", "dave", "op"][:count]
    r = client.post(
        "/splits", json={"amount": amount, "participants": handles}, headers=auth(token(client))
    )
    assert r.status_code == 200, r.text
    assert r.json()["shares"] == expected
    assert sum(p["amount"] for p in r.json()["parts"]) == amount
    assert_invariants(client)


def test_split_remainder_goes_to_first(client):
    client.post("/_test/reset", json=RICH_FIXTURE)
    r = client.post(
        "/splits",
        json={"amount": 10, "participants": ["dave", "op", "carol", "bob"]},
        headers=auth(token(client)),
    )
    assert r.status_code == 200, r.text
    parts = r.json()["parts"]
    assert r.json()["shares"] == [3, 3, 2, 2]
    assert [p["handle"] for p in parts[:2]] == ["dave", "op"]
    assert_invariants(client)


def test_split_payer_in_own_list_is_not_debited(client):
    client.post("/_test/reset", json=RICH_FIXTURE)
    before = inv(client)["balances"]
    r = client.post(
        "/splits",
        json={"amount": 100, "participants": ["alice", "bob"]},
        headers=auth(token(client)),
    )
    assert r.status_code == 200, r.text
    assert r.json()["shares"] == [50, 50]
    assert r.json()["total_charged"] == 50
    after = inv(client)["balances"]
    assert after["alice"] == before["alice"] + 50
    assert after["bob"] == before["bob"] - 50
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
    # RICH_FIXTURE so the splits case has participants who can be debited.
    client.post("/_test/reset", json=RICH_FIXTURE)
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
        first = client.post("/splits", json={"amount": 100, "participants": ["bob", "carol", "dave"]}, headers=auth(a, "k"))
        again = lambda: client.post("/splits", json={"amount": 100, "participants": ["bob", "carol", "dave"]}, headers=auth(a, "k"))  # noqa: E731
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
    client.post("/_test/reset", json=RICH_FIXTURE)
    a, b, op = (token(client, h) for h in ("alice", "bob", "op"))
    client.post("/payments", json={"to": "bob", "amount": 111}, headers=auth(a))
    rid = client.post("/requests", json={"to": "bob", "amount": 222}, headers=auth(a)).json()["id"]
    client.post(f"/requests/{rid}/pay", headers=auth(b))
    client.post("/splits", json={"amount": 300, "participants": ["bob", "carol", "dave"]}, headers=auth(a))
    client.post("/settlements", json={"entries": [{"from": "carol", "to": "alice", "amount": 50}]}, headers=auth(op))

    # The settlement is carol -> alice, so it lands in those two feeds only.
    per_handle = {
        "alice": {"payment", "request_created", "request_paid", "split", "settlement"},
        "bob": {"payment", "request_created", "request_paid", "split"},
        "carol": {"settlement"},
    }
    for handle, expected in per_handle.items():
        types = {e["type"] for e in client.get("/activity", headers=auth(token(client, handle))).json()["activity"]}
        missing = expected - types
        assert not missing, f"{handle} feed missing {sorted(missing)}: has {sorted(types)}"

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
    # /_test/reset replaces the world and invalidates every token, so it must
    # run before the tokens below are minted.
    client.post("/_test/reset", json=FIXTURE)
    a, b, op = (token(client, h) for h in ("alice", "bob", "op"))
    # three separate requests: `pay` and `decline` are both terminal, so each
    # transition needs a request that is still `open`.
    rids = [
        client.post("/requests", json={"to": "bob", "amount": 1}, headers=auth(a)).json()["id"]
        for _ in range(3)
    ]

    probes = [
        client.get("/health"),
        client.get("/_test/export"),
        client.post("/auth/signup", json={"email": "zed@example.com", "password": PW, "display_name": "Z"}),
        client.post("/auth/login", json={"email": "alice@example.com", "password": PW}),
        client.get("/me", headers=auth(a)),
        client.post("/payments", json={"to": "bob", "amount": 1}, headers=auth(a)),
        client.post("/requests", json={"to": "bob", "amount": 1}, headers=auth(a)),
        client.post(f"/requests/{rids[0]}/pay", headers=auth(b)),
        client.post(f"/requests/{rids[1]}/decline", headers=auth(b)),
        client.post(f"/requests/{rids[2]}/cancel", headers=auth(a)),
        client.get("/requests", headers=auth(a)),
        client.post("/splits", json={"amount": 3, "participants": ["bob", "carol"]}, headers=auth(a)),
        client.get("/activity", headers=auth(a)),
        client.post("/settlements", json={"entries": [{"from": "bob", "to": "carol", "amount": 1}]}, headers=auth(op)),
        # /_test/import last: it replaces the world, so it invalidates every
        # token issued above and would 401 every later probe.
        client.post("/_test/import", json=client.get("/_test/export").json()),
        # /_test/reset is probed here; re-mint so nothing after it needs a token
        client.post("/_test/reset", json=FIXTURE),
    ]

    assert len(probes) == 16
    bad = [(r.request.method, r.request.url, r.status_code) for r in probes if r.status_code >= 300]
    assert not bad, bad


def test_import_invalidates_outstanding_tokens(client):
    tok = token(client)
    assert client.get("/me", headers=auth(tok)).status_code == 200
    client.post("/_test/import", json=client.get("/_test/export").json())
    # documented behaviour: import replaces all state, tokens included
    assert client.get("/me", headers=auth(tok)).status_code == 401
    assert client.get("/me", headers=auth(token(client))).status_code == 200


def test_no_state_leaks_between_fixtures(client):
    client.post("/payments", json={"to": "bob", "amount": 5000}, headers=auth(token(client)))
    assert inv(client)["balances"]["alice"] == 55000
    client.post("/_test/reset", json=FIXTURE)
    assert inv(client)["balances"]["alice"] == 60000
    assert_invariants(client)


# ======================================================================
# Regression gate: self-deadlock on the authenticated surface
# ======================================================================


def _authenticated_surface_probe(q):
    """Child-process body: exercise the authenticated surface, report statuses.

    Runs in a *spawned* process so it gets its own copy of the app and its own
    ``store`` global. A deadlocked request inside this process therefore cannot
    poison the pytest session, and the parent can simply terminate us.
    """
    from fastapi.testclient import TestClient

    from app.main import app as child_app

    out: dict[str, object] = {}
    try:
        with TestClient(child_app) as c:
            c.post("/_test/reset", json=FIXTURE)
            r = c.post(
                "/auth/signup",
                json={"email": "noah@example.com", "password": PW, "display_name": "Noah"},
            )
            out["signup"] = r.status_code
            out["signup_handle"] = r.json().get("handle") if r.status_code == 200 else None

            r = c.post("/auth/login", json={"email": "alice@example.com", "password": PW})
            out["login"] = r.status_code
            tok = r.json().get("access_token") if r.status_code == 200 else None

            if tok:
                r = c.get("/me", headers={"Authorization": f"Bearer {tok}"})
                out["me"] = r.status_code
                out["me_handle"] = r.json().get("handle") if r.status_code == 200 else None

                r = c.post(
                    "/payments",
                    json={"to": "bob", "amount": 100},
                    headers={"Authorization": f"Bearer {tok}"},
                )
                out["payment"] = r.status_code

            r = c.get("/health")
            out["health"] = r.status_code
    except BaseException as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
    q.put(out)


def test_authenticated_surface_never_self_deadlocks():
    """P0 regression: signup/login/me must return, not hang.

    At commit 127c64f the first ``POST /auth/signup`` blocked forever, which
    poisoned the whole instance. The failure mode is a *hang*, so a data-only
    assertion is worthless - it would never be reached. The probe therefore runs
    in a spawned child process with a hard wall-clock bound: a reintroduced
    deadlock makes the child fail to exit, and this test FAILS instead of
    hanging the suite.

    The child-process isolation matters as much as the bound. An in-process
    deadlocked request leaves ``store.lock`` held forever, which then stalls
    ``TestClient.close()`` in fixture teardown and hangs every *subsequent*
    test in the session - so the guard would trade one hang for another.
    """
    import multiprocessing as mp

    timeout = 60
    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    proc = ctx.Process(target=_authenticated_surface_probe, args=(q,), daemon=True)
    proc.start()
    proc.join(timeout)

    if proc.is_alive():
        proc.terminate()
        proc.join(10)
        if proc.is_alive():  # pragma: no cover - terminate is best effort
            proc.kill()
            proc.join(10)
        pytest.fail(
            f"the authenticated surface did not return within {timeout}s - the request is "
            f"deadlocked. threading.Lock() is not reentrant, so a lock-taking helper was "
            f"almost certainly called from inside store.transaction(...)."
        )

    assert q.empty() is False, "probe process exited without reporting a result"
    out = q.get_nowait()
    assert "error" not in out, f"probe raised: {out['error']}"

    assert out.get("signup") == 200, f"POST /auth/signup -> {out.get('signup')}"
    assert out.get("signup_handle") == "noah", out.get("signup_handle")
    assert out.get("login") == 200, f"POST /auth/login -> {out.get('login')}"
    assert out.get("me") == 200, f"GET /me -> {out.get('me')}"
    assert out.get("me_handle") == "alice", out.get("me_handle")
    # the instance must still be usable: a stuck lock would show up here too
    assert out.get("payment") == 200, f"POST /payments -> {out.get('payment')}"
    assert out.get("health") == 200, f"GET /health -> {out.get('health')}"


def test_store_lock_is_not_reentrant():
    """Spec 1 mandates ``threading.Lock()``; an RLock would mask deadlocks."""
    from app import store as store_mod  # noqa: F401

    s = Store()
    # threading.Lock() is _thread.lock; threading.RLock() is _thread.RLock.
    assert type(s.lock).__name__ == "lock", (
        f"spec requires threading.Lock(), got {type(s.lock)}"
    )
    assert not isinstance(s.lock, type(threading.RLock())), "store.lock must not be an RLock"
    assert not hasattr(s.lock, "_is_rlock"), "store.lock must not be an RLock"


# ======================================================================
# Static guard: no lock-taking helper called from inside the critical section
# ======================================================================


def _source_files():
    app_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
    for root, _dirs, names in os.walk(app_dir):
        for n in sorted(names):
            if n.endswith(".py"):
                yield os.path.join(root, n)


#: Methods on ``Store`` that take ``self.lock``. Any of these invoked while the
#: same thread already holds it blocks forever, because ``threading.Lock`` is
#: not reentrant.
LOCK_TAKERS = {"read", "transaction"}


def _collect_functions(tree):
    """name -> ast.FunctionDef for every top-level and nested def in a module."""
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.setdefault(node.name, node)
    return out


def _called_names(node):
    """Names invoked anywhere inside ``node``."""
    names = set()
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        f = sub.func
        if isinstance(f, ast.Name):
            names.add(f.id)
        elif isinstance(f, ast.Attribute):
            names.add(f.attr)
    return names


def _takes_lock_directly(func_node):
    for sub in ast.walk(func_node):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute):
            if sub.func.attr in LOCK_TAKERS:
                return True
    return False


def test_no_nested_lock_acquisition_in_app_package():
    """Mechanically forbid taking the store lock while already holding it.

    The P0 at commit 127c64f was exactly this bug class: ``user_public()``
    called ``store.read(...)`` but was itself invoked from inside
    ``store.transaction(work)``. Both call sites look correct in isolation, so
    review does not catch it - the failure only appears at runtime as a
    permanent hang. It is therefore rejected here by static analysis.

    The rule that matters: the callback handed to ``store.read``/
    ``store.transaction`` runs *with the lock held*, so neither the callback
    nor anything it calls may take the lock again. Inline lambdas that only
    touch state are the intended idiom and are allowed; a *named* helper (or a
    transitive chain of them) that takes the lock is not.
    """
    import ast

    modules = {}
    functions_by_name = {}
    for path in _source_files():
        with open(path, "rb") as fh:
            tree = ast.parse(fh.read(), filename=path)
        rel = os.path.relpath(path, os.path.dirname(path))
        modules[rel] = (tree, _collect_functions(tree))
        for name, node in modules[rel][1].items():
            functions_by_name.setdefault(name, []).append((rel, node))

    # Transitive closure: does this function end up taking the lock?
    def takes_lock(rel, name, seen):
        key = (rel, name)
        if key in seen:
            return False
        seen.add(key)
        node = modules[rel][1].get(name)
        if node is None:
            return False
        if _takes_lock_directly(node):
            return True
        for callee in _called_names(node):
            if callee in LOCK_TAKERS:
                continue
            for cand_rel, cand_node in functions_by_name.get(callee, []):
                if cand_node is node:
                    continue
                if takes_lock(cand_rel, callee, seen):
                    return True
        return False

    offenders = []
    for rel, (tree, funcs) in modules.items():
        for fn_name, fn_node in funcs.items():
            for call in ast.walk(fn_node):
                if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
                    continue
                if call.func.attr not in LOCK_TAKERS:
                    continue
                cb_args = list(call.args) + [kw.value for kw in call.keywords]
                if not cb_args:
                    continue
                cb = cb_args[0]
                if isinstance(cb, ast.Lambda):
                    for callee in _called_names(cb):
                        if callee in LOCK_TAKERS:
                            offenders.append(
                                f"{rel}:{cb.lineno}: inline callback to store.{call.func.attr}() "
                                f"calls {callee}() - the lock is already held"
                            )
                            continue
                        for cand_rel, _ in functions_by_name.get(callee, []):
                            if takes_lock(cand_rel, callee, set()):
                                offenders.append(
                                    f"{rel}:{cb.lineno}: inline callback to "
                                    f"store.{call.func.attr}() calls {callee}(), which takes the "
                                    f"lock - the lock is already held"
                                )
                elif isinstance(cb, ast.Name):
                    for cand_rel, _ in functions_by_name.get(cb.id, []):
                        if takes_lock(cand_rel, cb.id, set()):
                            offenders.append(
                                f"{rel}:{call.lineno}: store.{call.func.attr}({cb.id}) - "
                                f"{cb.id}() takes the lock but runs with the lock already held"
                            )

    assert not offenders, "nested store-lock acquisition detected:\n  " + "\n  ".join(offenders)


def test_public_projection_helpers_take_no_lock():
    """``_user_public`` is the single projection and is lock-free by contract.

    The lock-taking ``user_public`` twin was deleted precisely because an unused
    lock-taking helper is an invitation to reintroduce the P0 self-deadlock.
    This asserts both halves: the surviving projection takes no lock, and the
    trap is still gone.
    """
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app", "dependencies.py")
    with open(path, "rb") as fh:
        tree = ast.parse(fh.read(), filename=path)

    found = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        if node.name == "user_public":
            pytest.fail(
                "app/dependencies.py defines a lock-taking user_public(); it is a trap "
                "- call it inside a work() and the authenticated surface deadlocks"
            )
        if node.name != "_user_public":
            continue
        found = True
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute):
                assert sub.func.attr not in LOCK_TAKERS, (
                    f"_user_public() must be lock-free but calls .{sub.func.attr}() "
                    f"at line {sub.lineno}"
                )
    assert found, "_user_public() is missing - the lock-free projection was removed"

