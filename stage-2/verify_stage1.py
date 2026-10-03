#!/usr/bin/env python3
"""Evidence generator for Pocketful Stage 1.

Drives a *running* instance over HTTP and asserts every acceptance criterion
from the work items. Prints one PASS/FAIL line per check and exits non-zero if
anything failed.

    python verify_stage1.py --base http://127.0.0.1:8080

    # the container is only guaranteed to have its deps installed, so this
    # runs with stdlib + whatever the image has; httpx is in requirements.txt.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from typing import Any

import httpx

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

#: Every account funded. A participant whose balance cannot cover its share is
#: correctly refused with 422, so split tests need participants who can pay.
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


class Suite:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0
        self.failures: list[str] = []
        self.section = ""

    def head(self, title: str) -> None:
        self.section = title
        print(f"\n--- {title} ---")

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        if ok:
            self.passed += 1
            print(f"[PASS] {name}")
        else:
            self.failed += 1
            line = f"{self.section} / {name}" + (f"  -- {detail}" if detail else "")
            self.failures.append(line)
            print(f"[FAIL] {name}" + (f"  -- {detail}" if detail else ""))
        return ok

    def summary(self) -> int:
        print(f"\n=== SUMMARY: {self.passed} passed, {self.failed} failed ===")
        for f in self.failures:
            print(f"  FAILED: {f}")
        return 1 if self.failed else 0


#: Returned instead of a real bearer token when login fails. Subsequent calls
#: then draw 401s and get reported as FAILs, instead of aborting the section.
BAD_TOKEN = "__login_failed__"


class Api:
    def __init__(self, base: str, on_error: Any = None) -> None:
        self.base = base.rstrip("/")
        self.client = httpx.Client(base_url=self.base, timeout=60.0)
        self.on_error = on_error

    def fail(self, what: str, exc: BaseException) -> None:
        detail = f"{type(exc).__name__}: {exc}"
        if self.on_error is not None:
            self.on_error(what, detail)
        else:
            print(f"[FAIL] {what}  -- {detail}")

    def c(self) -> httpx.Client:
        return httpx.Client(base_url=self.base, timeout=60.0)

    # -- system ---------------------------------------------------------
    def health(self) -> httpx.Response:
        return self.client.get("/health")

    def reset(self, fixture: dict[str, Any] | None = None) -> httpx.Response:
        return self.client.post("/_test/reset", json=fixture if fixture is not None else FIXTURE)

    def export(self) -> httpx.Response:
        return self.client.get("/_test/export")

    def import_(self, payload: Any) -> httpx.Response:
        return self.client.post("/_test/import", json=payload)

    # -- auth -----------------------------------------------------------
    def login(self, email: str, password: str = PW) -> httpx.Response:
        return self.client.post("/auth/login", json={"email": email, "password": password})

    def token(self, handle: str, email: str | None = None) -> str:
        """Log in and return a bearer token.

        Never raises: a failed login is recorded as a FAIL and yields a sentinel
        so the remaining checks in the section still run (and report the 401s
        they actually received) rather than truncating the whole work item.
        """
        if email is None:
            email = "op@example.com" if handle == "op" else f"{handle}@example.com"
        try:
            r = self.login(email)
            r.raise_for_status()
            return str(r.json()["access_token"])
        except Exception as exc:  # noqa: BLE001
            self.fail(f"login as {email!r} to obtain a token", exc)
            return BAD_TOKEN

    def me(self, token: str) -> httpx.Response:
        return self.client.get("/me", headers={"Authorization": f"Bearer {token}"})

    # -- operations -----------------------------------------------------
    def pay(self, token: str, to: str, amount: Any, key: str | None = None, **extra: Any) -> httpx.Response:
        h = {"Authorization": f"Bearer {token}"}
        if key:
            h["Idempotency-Key"] = key
        return self.client.post("/payments", json={"to": to, "amount": amount, **extra}, headers=h)

    def request_(self, token: str, to: str, amount: Any, key: str | None = None) -> httpx.Response:
        h = {"Authorization": f"Bearer {token}"}
        if key:
            h["Idempotency-Key"] = key
        return self.client.post("/requests", json={"to": to, "amount": amount}, headers=h)

    def act(self, token: str, rid: str, verb: str, key: str | None = None) -> httpx.Response:
        h = {"Authorization": f"Bearer {token}"}
        if key:
            h["Idempotency-Key"] = key
        return self.client.post(f"/requests/{rid}/{verb}", headers=h)

    def split(self, token: str, amount: Any, parts: list[str], key: str | None = None) -> httpx.Response:
        h = {"Authorization": f"Bearer {token}"}
        if key:
            h["Idempotency-Key"] = key
        return self.client.post("/splits", json={"amount": amount, "participants": parts}, headers=h)

    def activity(self, token: str) -> httpx.Response:
        return self.client.get("/activity", headers={"Authorization": f"Bearer {token}"})

    def settle(self, token: str, entries: list[dict[str, Any]], key: str | None = None) -> httpx.Response:
        h = {"Authorization": f"Bearer {token}"}
        if key:
            h["Idempotency-Key"] = key
        return self.client.post("/settlements", json={"entries": entries}, headers=h)


def invariant_ok(exp: dict[str, Any]) -> bool:
    inv = exp.get("invariants", {})
    return bool(
        inv.get("balance_sum_equals_seeded_total")
        and inv.get("no_negative_balances")
        and inv.get("all_balances_owned_by_users")
        and inv.get("no_plaintext_passwords")
    )


def invariant_detail(exp: dict[str, Any]) -> str:
    inv = exp.get("invariants", {})
    return (
        f"sum={inv.get('sum_of_balances')} seeded={inv.get('seeded_total')} "
        f"neg={inv.get('negative_balances')} orphan={inv.get('orphan_balances')} "
        f"plaintext={inv.get('plaintext_password_users')}"
    )


# ======================================================================
# W1
# ======================================================================


def w1(api: Api, s: Suite) -> None:
    s.head("W1  system + health")
    r = api.health()
    s.check("GET /health -> 200", r.status_code == 200, f"got {r.status_code}")
    s.check('GET /health body == {"status":"ok"}', r.json() == {"status": "ok"}, r.text)


# ======================================================================
# W2
# ======================================================================


def w2(api: Api, s: Suite) -> None:
    s.head("W2  store + /_test harness")
    r = api.reset(FIXTURE)
    s.check("POST /_test/reset -> 200", r.status_code == 200, r.text[:200])

    exp = api.export().json()
    s.check(
        "reset seeded total is 100000",
        exp["seeded_total"] == 100000,
        f"got {exp['seeded_total']}",
    )
    s.check(
        "balances sum exactly to the seeded total",
        sum(exp["balances"].values()) == 100000 == exp["balance_sum"],
        f"sum={sum(exp['balances'].values())} balance_sum={exp['balance_sum']}",
    )
    s.check("invariants hold after reset", invariant_ok(exp), invariant_detail(exp))

    # round trip: export -> import -> export must be identical
    snapshot = api.export().json()
    r = api.import_(snapshot)
    s.check("POST /_test/import (export payload) -> 200", r.status_code == 200, r.text[:200])
    again = api.export().json()
    for field in ("seeded_total", "balances", "requests", "activity"):
        s.check(
            f"export/import round-trips `{field}`",
            again[field] == snapshot[field],
            f"{snapshot[field]!r} != {again[field]!r}",
        )
    s.check("invariants hold after import", invariant_ok(again), invariant_detail(again))

    # reset with a bare, differently-shaped fixture
    alt = {
        "users": [
            {"handle": "x", "balance": 7},
            {"handle": "y", "balance": 3},
        ]
    }
    r = api.reset(alt)
    s.check("reset with derived seeded_total -> 200", r.status_code == 200, r.text[:200])
    exp = api.export().json()
    s.check(
        "derived seeded_total == 10 == balance sum",
        exp["seeded_total"] == 10 and sum(exp["balances"].values()) == 10,
        f"seeded={exp['seeded_total']} sum={sum(exp['balances'].values())}",
    )

    # a fixture that would break I1 must be rejected, not installed
    bad = {"users": [{"handle": "x", "balance": 7}], "seeded_total": 999}
    r = api.reset(bad)
    s.check(
        "fixture whose seeded_total != sum is rejected 422",
        r.status_code == 422,
        f"got {r.status_code}",
    )
    api.reset(FIXTURE)


# ======================================================================
# W3
# ======================================================================


def w3(api: Api, s: Suite) -> None:
    s.head("W3  auth")
    r = api.client.post(
        "/auth/signup",
        json={"email": "Dana.Lopez+work@example.com", "password": PW, "display_name": "Dana"},
    )
    s.check("POST /auth/signup -> 200", r.status_code == 200, r.text[:200])
    s.check(
        "handle derived from email local part",
        r.json().get("handle") == "danalopezwork",
        f"got {r.json().get('handle')!r}",
    )

    for bad in ["!!!@example.com", "   @example.com", "a" * 40 + "@example.com"]:
        r = api.client.post(
            "/auth/signup", json={"email": bad, "password": PW, "display_name": "X"}
        )
        s.check(
            f"handle from {bad!r} fails ^[a-z0-9_]{{1,20}}$ -> 422",
            r.status_code == 422,
            f"got {r.status_code}",
        )

    r = api.client.post(
        "/auth/signup", json={"email": "zed@example.com", "password": PW, "display_name": "Z", "handle": "BAD-HANDLE!"}
    )
    s.check("explicit non-conforming handle -> 422", r.status_code == 422, f"got {r.status_code}")

    r = api.client.post(
        "/auth/signup", json={"email": "alice@example.com", "password": PW, "display_name": "dup"}
    )
    s.check("duplicate handle/email -> 4xx", 400 <= r.status_code < 500, f"got {r.status_code}")

    r = api.login("danalopezwork@example.com", "wrong-password")
    s.check("login with wrong password -> 401", r.status_code == 401, f"got {r.status_code}")

    r = api.login("nobody@example.com")
    s.check("login with unknown email -> 401", r.status_code == 401, f"got {r.status_code}")

    tok = api.token("danalopezwork", email="Dana.Lopez+work@example.com")
    r = api.me(tok)
    s.check("GET /me with bearer -> 200", r.status_code == 200, r.text[:200])
    s.check("GET /me returns the right handle", r.json().get("handle") == "danalopezwork", r.text[:120])

    s.check("GET /me with no token -> 401", api.client.get("/me").status_code == 401)
    s.check("GET /me with junk token -> 401", api.me("not-a-real-token").status_code == 401)
    s.check("GET /me with wrong scheme -> 401", api.client.get("/me", headers={"Authorization": PW}).status_code == 401)

    # I4 - no plaintext anywhere in the dumped store
    exp = api.export()
    body = exp.text
    s.check(
        "every stored password is a bcrypt digest",
        all(u["password_hash"].startswith("$2") for u in exp.json()["users"]),
    )
    s.check(f"plaintext password {PW!r} absent from the whole store dump", PW not in body)
    s.check("I4 reported true by /_test/export", exp.json()["invariants"]["no_plaintext_passwords"])


# ======================================================================
# W4
# ======================================================================


def w4(api: Api, s: Suite) -> None:
    s.head("W4  payments + balance invariants")
    api.reset(FIXTURE)
    alice, bob, carol = api.token("alice"), api.token("bob"), api.token("carol")

    before = api.export().json()["balances"]
    r = api.pay(alice, "bob", 2500)
    s.check("POST /payments -> 200", r.status_code == 200, r.text[:200])
    after = api.export().json()["balances"]
    s.check(
        "sender debited by exactly the amount",
        after["alice"] == before["alice"] - 2500,
        f"{before['alice']} -> {after['alice']}",
    )
    s.check(
        "recipient credited by exactly the amount",
        after["bob"] == before["bob"] + 2500,
        f"{before['bob']} -> {after['bob']}",
    )
    exp = api.export().json()
    s.check("sum invariant after payment", invariant_ok(exp), invariant_detail(exp))

    snap = api.export().json()["balances"]
    r = api.pay(alice, "bob", 10**9)
    s.check("insufficient funds -> 422", r.status_code == 422, f"got {r.status_code}")
    s.check(
        "insufficient funds leaves BOTH balances untouched",
        api.export().json()["balances"] == snap,
        f"{snap} -> {api.export().json()['balances']}",
    )

    bob_bal = api.export().json()["balances"]["bob"]
    r = api.pay(bob, "alice", bob_bal + 1)
    s.check("transfer larger than the sender's balance -> 422", r.status_code == 422, f"got {r.status_code}")
    exp = api.export().json()
    s.check("no negative balance exists", exp["invariants"]["no_negative_balances"], invariant_detail(exp))

    for bad, label in [(0, "zero"), (-5, "negative"), (1.5, "float"), ("100", "string"), (True, "bool"), (None, "null")]:
        r = api.pay(alice, "bob", bad)
        s.check(f"amount {label} ({bad!r}) -> 422", r.status_code == 422, f"got {r.status_code}")

    r = api.pay(alice, "ghost", 100)
    s.check("unknown recipient -> 404", r.status_code == 404, f"got {r.status_code}")

    r = api.pay(alice, "alice", 100)
    s.check("self payment -> 422", r.status_code == 422, f"got {r.status_code}")

    r = api.client.post("/payments", json={"to": "bob", "amount": 100})
    s.check("unauthenticated payment -> 401", r.status_code == 401, f"got {r.status_code}")

    s.check("sum invariant after the whole W4 attack run", invariant_ok(api.export().json()), invariant_detail(api.export().json()))


# ======================================================================
# W5
# ======================================================================


def w5(api: Api, s: Suite) -> None:
    s.head("W5  requests")
    api.reset(FIXTURE)
    alice, bob, carol = api.token("alice"), api.token("bob"), api.token("carol")

    r = api.request_(alice, "bob", 5000)
    s.check("POST /requests -> 200", r.status_code == 200, r.text[:200])
    rid = r.json()["id"]
    s.check("new request is `open`", r.json()["status"] == "open", r.text[:120])
    s.check("creating a request moves no money", api.export().json()["balances"] == {"alice": 60000, "bob": 30000, "carol": 10000, "op": 0})

    r = api.act(alice, rid, "pay")
    s.check("creator cannot pay their own request -> 403", r.status_code == 403, f"got {r.status_code}")

    r = api.act(carol, rid, "pay")
    s.check("unrelated user cannot pay -> 403", r.status_code == 403, f"got {r.status_code}")

    r = api.act(alice, rid, "cancel")
    s.check("creator can cancel an open request -> 200", r.status_code == 200, r.text[:200])
    s.check("cancelled status", {x["id"]: x["status"] for x in api.export().json()["requests"]}[rid] == "cancelled")

    # State guard: each verb has one legal actor (pay/decline = recipient,
    # cancel = creator), and the actor check (403) legitimately precedes the
    # state check (409). So reaching 409 means using the right actor.
    s.check("pay on a cancelled request -> 409 (as recipient)", api.act(bob, rid, "pay").status_code == 409)
    s.check("decline on a cancelled request -> 409 (as recipient)", api.act(bob, rid, "decline").status_code == 409)
    s.check("cancel on a cancelled request -> 409 (as creator)", api.act(alice, rid, "cancel").status_code == 409)
    s.check("wrong actor still gets 403 even on a terminal request", api.act(carol, rid, "cancel").status_code == 403)

    # a fresh one for the pay path
    r = api.request_(alice, "bob", 7000)
    rid2 = r.json()["id"]
    before = api.export().json()["balances"]
    r = api.act(bob, rid2, "pay")
    s.check("recipient can pay an open request -> 200", r.status_code == 200, r.text[:200])
    after = api.export().json()["balances"]
    s.check("pay moves funds recipient -> creator", after["bob"] == before["bob"] - 7000 and after["alice"] == before["alice"] + 7000, f"{before} -> {after}")
    s.check("pay flips status to paid", {x["id"]: x["status"] for x in api.export().json()["requests"]}[rid2] == "paid")
    s.check("sum invariant after pay", invariant_ok(api.export().json()), invariant_detail(api.export().json()))

    # pay/decline are the recipient's, cancel is the creator's
    s.check("pay on a paid request -> 409", api.act(bob, rid2, "pay").status_code == 409)
    s.check("decline on a paid request -> 409", api.act(bob, rid2, "decline").status_code == 409)
    s.check("cancel on a paid request -> 409", api.act(alice, rid2, "cancel").status_code == 409)

    r = api.request_(alice, "bob", 3000)
    rid3 = r.json()["id"]
    s.check("recipient can decline an open request -> 200", api.act(bob, rid3, "decline").status_code == 200)
    s.check("decline flips status to declined", {x["id"]: x["status"] for x in api.export().json()["requests"]}[rid3] == "declined")

    r = api.request_(alice, "bob", 1234)
    s.check("unknown request id -> 404", api.act(bob, r.json()["id"][:-1] + "zzz", "pay").status_code == 404)

    # filters
    r = api.request_(carol, "bob", 500)
    s.check("fourth request created", r.status_code == 200)
    out = api.list_requests(alice)
    incoming = [x for x in out if x["from"] != "alice"]
    outgoing = [x for x in out if x["from"] == "alice"]
    s.check("GET /requests (default) returns all of alice's", len(out) == len(incoming) + len(outgoing))
    s.check("incoming/outgoing partition is disjoint and total", len({x["id"] for x in incoming} & {x["id"] for x in outgoing}) == 0)

    api.list_requests(alice, direction="incoming")
    api.list_requests(alice, direction="outgoing")
    st = api.list_requests(alice, status="open")
    s.check("status=open returns only open", all(x["status"] == "open" for x in st), str([x["status"] for x in st]))
    s.check("status=paid returns only paid", all(x["status"] == "paid" for x in api.list_requests(alice, status="paid")))
    s.check("direction=outgoing all created by alice", all(x["from"] == "alice" for x in api.list_requests(alice, direction="outgoing")))
    s.check("direction=incoming none created by alice", all(x["from"] != "alice" for x in api.list_requests(alice, direction="incoming")))
    s.check("direction=bogus -> 422", api.client.get("/requests?direction=sideways", headers={"Authorization": f"Bearer {alice}"}).status_code == 422)
    s.check("status=bogus -> 422", api.client.get("/requests?status=weird", headers={"Authorization": f"Bearer {alice}"}).status_code == 422)
    s.check("GET /requests unauthenticated -> 401", api.client.get("/requests").status_code == 401)
    s.check("sum invariant after the whole W5 run", invariant_ok(api.export().json()), invariant_detail(api.export().json()))


# helper bound late so w5 can use it
def _list_requests(self, token, direction="all", status="all"):
    return self.client.get(
        "/requests", params={"direction": direction, "status": status}, headers={"Authorization": f"Bearer {token}"}
    ).json().get("requests", [])


Api.list_requests = _list_requests


# ======================================================================
# W6
# ======================================================================


def w6(api: Api, s: Suite) -> None:
    s.head("W6  splits")
    api.reset(RICH_FIXTURE)
    alice = api.token("alice")

    r = api.split(alice, 100, ["bob", "carol", "dave"])
    s.check("POST /splits 100 over 3 -> 200", r.status_code == 200, r.text[:200])
    s.check("100 / 3 -> [34, 33, 33]", r.json().get("shares") == [34, 33, 33], str(r.json().get("shares")))
    s.check("parts sum back to the amount", sum(p["amount"] for p in r.json()["parts"]) == 100)
    s.check("sum invariant after split", invariant_ok(api.export().json()), invariant_detail(api.export().json()))

    r = api.split(alice, 10, ["bob", "carol", "dave", "op"])
    s.check("POST /splits 10 over 4 -> 200", r.status_code == 200, r.text[:200])
    s.check("10 / 4 -> [3, 3, 2, 2]", r.json().get("shares") == [3, 3, 2, 2], str(r.json().get("shares")))
    s.check("remainder units went to the FIRST participants", [p["handle"] for p in r.json()["parts"][:2]] == ["bob", "carol"], str(r.json().get("parts")))
    s.check("parts sum back to the amount", sum(p["amount"] for p in r.json()["parts"]) == 10)

    # ordering matters: the reversed list must move the remainder
    r = api.split(alice, 10, ["dave", "op", "carol", "bob"])
    s.check(
        "reversed order -> [3, 3, 2, 2] with the remainder on dave/op",
        r.json().get("shares") == [3, 3, 2, 2] and [p["handle"] for p in r.json()["parts"][:2]] == ["dave", "op"],
        str(r.json().get("parts")),
    )

    # payer in their own list is not debited
    api.reset(RICH_FIXTURE)
    alice = api.token("alice")
    before = api.export().json()["balances"]
    r = api.split(alice, 100, ["alice", "bob"])
    after = api.export().json()["balances"]
    s.check("payer in own list: shares still split evenly", r.json().get("shares") == [50, 50], str(r.json().get("shares")))
    s.check("payer in own list: only the other party is debited", after["bob"] == before["bob"] - 50 and after["alice"] == before["alice"] + 50, f"{before} -> {after}")

    # rejections
    api.reset(FIXTURE)
    alice = api.token("alice")
    snap = api.export().json()["balances"]
    r = api.split(alice, 10**9, ["bob"])
    s.check("debit that would go negative -> 422", r.status_code == 422, f"got {r.status_code}")
    s.check("rejected split left every balance untouched", api.export().json()["balances"] == snap)
    s.check("payer without funds -> 422", api.split(api.token("op"), 500, ["alice"]).status_code == 422)
    s.check("duplicate participants -> 422", api.split(alice, 100, ["bob", "bob"]).status_code == 422)
    s.check("empty participants -> 422", api.split(alice, 100, []).status_code == 422)
    s.check("unknown participant -> 404", api.split(alice, 100, ["ghost"]).status_code == 404)
    s.check("non-integer amount -> 422", api.split(alice, 1.5, ["bob", "carol"]).status_code == 422)
    s.check("split unauthenticated -> 401", api.client.post("/splits", json={"amount": 1, "participants": ["bob"]}).status_code == 401)
    s.check("sum invariant after the whole W6 run", invariant_ok(api.export().json()), invariant_detail(api.export().json()))


# ======================================================================
# W7
# ======================================================================


def w7(api: Api, s: Suite) -> None:
    s.head("W7  idempotency on all 5 financial endpoints")
    api.reset(RICH_FIXTURE)
    alice, bob, carol, op = (api.token(h) for h in ("alice", "bob", "carol", "op"))

    def replay_case(name, first_call, replay_call):
        """Assert a replay returns the original response and moves no money.

        The balance snapshot is taken *between* the first call and the replay,
        so a change in `balances` means the replay really did re-process.
        """
        first = first_call()
        s.check(f"{name}: first call -> 2xx", first.status_code == 200, first.text[:200])
        before_state = api.export().json()["balances"]

        r2 = replay_call()
        s.check(
            f"{name}: replay returns the ORIGINAL status",
            r2.status_code == first.status_code,
            f"{first.status_code} != {r2.status_code}",
        )
        s.check(
            f"{name}: replay returns the ORIGINAL body",
            r2.json() == first.json(),
            "body differs",
        )
        after = api.export().json()
        s.check(
            f"{name}: replay did not double-process (balances unchanged)",
            after["balances"] == before_state,
            f"{before_state} -> {after['balances']}",
        )
        s.check(f"{name}: sum invariant after replay", invariant_ok(after), invariant_detail(after))

    # payments
    replay_case(
        "POST /payments",
        lambda: api.pay(alice, "bob", 1000, key="k-pay"),
        lambda: api.pay(alice, "bob", 1000, key="k-pay"),
    )

    # requests
    replay_case(
        "POST /requests",
        lambda: api.request_(alice, "bob", 2000, key="k-req"),
        lambda: api.request_(alice, "bob", 2000, key="k-req"),
    )

    # requests/{id}/pay
    rid = api.request_(alice, "bob", 3000, key="k-req2").json()["id"]
    replay_case(
        "POST /requests/{id}/pay",
        lambda: api.act(bob, rid, "pay", key="k-pay-req"),
        lambda: api.act(bob, rid, "pay", key="k-pay-req"),
    )

    # splits
    replay_case(
        "POST /splits",
        lambda: api.split(alice, 300, ["bob", "carol", "dave"], key="k-split"),
        lambda: api.split(alice, 300, ["bob", "carol", "dave"], key="k-split"),
    )

    # settlements
    replay_case(
        "POST /settlements",
        lambda: api.settle(op, [{"from": "bob", "to": "carol", "amount": 400}], key="k-set"),
        lambda: api.settle(op, [{"from": "bob", "to": "carol", "amount": 400}], key="k-set"),
    )

    # cross-user key independence
    api.reset(RICH_FIXTURE)
    alice, bob, carol, op = (api.token(h) for h in ("alice", "bob", "carol", "op"))
    shared = "shared-key-across-users"
    r_alice = api.pay(alice, "carol", 500, key=shared)
    r_bob = api.pay(bob, "carol", 500, key=shared)
    s.check("same key + different user -> both process (2xx)", r_alice.status_code == 200 and r_bob.status_code == 200, f"{r_alice.status_code}/{r_bob.status_code}")
    s.check(
        "cross-user same key produced two distinct payments",
        r_alice.json()["payment_id"] != r_bob.json()["payment_id"],
        "the second call was swallowed as a replay",
    )
    # carol is credited by both payments (10000 + 500 + 500), bob is debited one
    s.check(
        "both debits actually happened (bob 10000->9500, carol 10000->11000)",
        api.export().json()["balances"]["bob"] == 9500 and api.export().json()["balances"]["carol"] == 11000,
        str(api.export().json()["balances"]),
    )

    # different key, same logical operation -> processes normally
    r2 = api.pay(alice, "carol", 500, key="a-different-key")
    s.check("different key, same operation -> processes again", r2.json()["payment_id"] != r_alice.json()["payment_id"])

    # same key, different body -> 409
    r3 = api.pay(alice, "carol", 501, key=shared)
    s.check("same key + different body -> 409", r3.status_code == 409, f"got {r3.status_code}")

    # no key at all -> plain processing
    a = api.pay(alice, "carol", 100)
    bb = api.pay(alice, "carol", 100)
    s.check("no Idempotency-Key -> every call processes", a.json()["payment_id"] != bb.json()["payment_id"])

    s.check("sum invariant after the whole W7 run", invariant_ok(api.export().json()), invariant_detail(api.export().json()))


# ======================================================================
# W8
# ======================================================================


def w8(api: Api, s: Suite) -> None:
    s.head("W8  activity + settlements")
    api.reset(RICH_FIXTURE)
    alice, bob, carol, op = (api.token(h) for h in ("alice", "bob", "carol", "op"))

    api.pay(alice, "bob", 111)
    rid = api.request_(alice, "bob", 222).json()["id"]
    api.act(bob, rid, "pay")
    api.split(alice, 300, ["bob", "carol", "dave"])
    api.settle(op, [{"from": "carol", "to": "alice", "amount": 50}])

    # The settlement is carol -> alice, so it appears in those two feeds only.
    per_handle = {
        "alice": {"payment", "request_created", "request_paid", "split", "settlement"},
        "bob": {"payment", "request_created", "request_paid", "split"},
        "carol": {"settlement"},
    }
    for handle, expected in per_handle.items():
        types = {e["type"] for e in api.activity(api.token(handle)).json()["activity"]}
        missing = sorted(expected - types)
        s.check(f"activity contains {sorted(expected)} for {handle}", not missing, f"missing {missing}; has {sorted(types)}")

    feed = api.activity(alice).json()["activity"]
    s.check("activity is newest-first", [e["id"] for e in feed] == sorted([e["id"] for e in feed], reverse=True))
    s.check("activity only contains the caller's entries", all(e["handle"] == "alice" for e in feed))
    s.check("GET /activity unauthenticated -> 401", api.client.get("/activity").status_code == 401)

    # settlements are operator-only
    snap = api.export().json()["balances"]
    for who, tok in (("alice", alice), ("bob", bob), ("carol", carol)):
        r = api.settle(tok, [{"from": "alice", "to": "bob", "amount": 10}])
        s.check(f"non-operator `{who}` settlement -> 403", r.status_code == 403, f"got {r.status_code}")
    s.check("rejected settlements moved no money", api.export().json()["balances"] == snap)
    s.check("settlement unauthenticated -> 401", api.client.post("/settlements", json={"entries": [{"from": "a", "to": "b", "amount": 1}]}).status_code == 401)

    before = api.export().json()["balances"]
    r = api.settle(op, [{"from": "alice", "to": "bob", "amount": 1000}])
    s.check("operator settlement -> 200", r.status_code == 200, r.text[:200])
    after = api.export().json()["balances"]
    s.check("settlement moved funds alice -> bob", after["alice"] == before["alice"] - 1000 and after["bob"] == before["bob"] + 1000, f"{before} -> {after}")
    s.check("sum invariant after settlement", invariant_ok(api.export().json()), invariant_detail(api.export().json()))

    # all-or-nothing batch
    snap = api.export().json()["balances"]
    r = api.settle(op, [{"from": "alice", "to": "bob", "amount": 5}, {"from": "carol", "to": "bob", "amount": 10**9}])
    s.check("partially-infeasible batch -> 422", r.status_code == 422, f"got {r.status_code}")
    s.check("all-or-nothing: no transfer from the batch committed", api.export().json()["balances"] == snap, f"{snap} -> {api.export().json()['balances']}")
    s.check("settlement self-transfer -> 422", api.settle(op, [{"from": "alice", "to": "alice", "amount": 1}]).status_code == 422)
    s.check("settlement empty batch -> 422", api.settle(op, []).status_code == 422)
    s.check("sum invariant after the whole W8 run", invariant_ok(api.export().json()), invariant_detail(api.export().json()))


# ======================================================================
# concurrency
# ======================================================================


def concurrency(api: Api, s: Suite, requests_count: int = 60, threads: int = 20) -> None:
    s.head(f"concurrency: {requests_count} requests across {threads} threads")
    errors: list[str] = []
    codes: list[int] = []
    guard = threading.Lock()
    tokens = {"alice": api.token("alice"), "bob": api.token("bob"), "carol": api.token("carol")}
    names = list(tokens)
    counter = {"n": 0}
    cl = threading.Lock()

    def worker(idx: int) -> None:
        local = api.c()
        for step in range(requests_count // threads):
            n = step * threads + idx
            who = names[n % len(names)]
            tok = tokens[who]
            with cl:
                counter["n"] += 1
            try:
                pick = n % 5
                if pick == 0:
                    resp = local.post("/_test/reset", json=FIXTURE)
                elif pick == 1:
                    resp = local.post("/_test/import", json=api.export().json())
                elif pick == 2:
                    resp = local.post("/payments", json={"to": names[(n + 1) % 3], "amount": 1}, headers={"Authorization": f"Bearer {tok}"})
                elif pick == 3:
                    resp = local.post("/auth/login", json={"email": f"{who}@example.com", "password": PW})
                else:
                    resp = local.get("/activity", headers={"Authorization": f"Bearer {tok}"})
                with guard:
                    codes.append(resp.status_code)
                    if resp.status_code >= 500:
                        errors.append(f"5xx from {resp.request.method} {resp.request.url}: {resp.text[:200]}")
            except Exception as exc:  # noqa: BLE001 - this is exactly what we are hunting
                with guard:
                    errors.append(f"{type(exc).__name__}: {exc}")

    threads_list = [threading.Thread(target=worker, args=(i,)) for i in range(threads)]
    start = time.time()
    for t in threads_list:
        t.start()
    for t in threads_list:
        t.join()
    elapsed = time.time() - start

    s.check(f"issued {counter['n']} requests with no transport exceptions", not errors, "; ".join(errors[:3]))
    s.check("no 5xx responses", all(c < 500 for c in codes), f"{[c for c in codes if c >= 500][:5]}")
    exp = api.export().json()
    s.check("sum invariant survives the hammer", invariant_ok(exp), invariant_detail(exp))
    s.check(f"state is coherent after {counter['n']} racing requests ({elapsed:.1f}s)", exp["balance_sum"] == exp["seeded_total"])


# ======================================================================
# endpoint inventory
# ======================================================================


def inventory(api: Api, s: Suite) -> None:
    s.head("spec section 3: all 16 endpoints exist")

    # The state-replacing endpoints run FIRST and eagerly: on a cold store
    # there is no `alice` to authenticate as, and /_test/reset and
    # /_test/import both wipe every bearer token. Tokens are minted after.
    system_results = []
    for name, fn in [
        ("GET  /health", api.health),
        ("POST /_test/reset", lambda: api.reset(FIXTURE)),
        ("GET  /_test/export", api.export),
        ("POST /_test/import", lambda: api.import_(api.export().json())),
    ]:
        try:
            system_results.append((name, fn()))
        except Exception as exc:  # noqa: BLE001
            system_results.append((name, None, f"{type(exc).__name__}: {exc}"))

    try:
        tok = api.token("alice")
        bob = api.token("bob")
        op = api.token("op")
        rids = [api.request_(tok, "bob", 1).json()["id"] for _ in range(3)]
    except Exception as exc:  # noqa: BLE001
        s.check("authenticated probes could be prepared", False, f"{type(exc).__name__}: {exc}")
        return

    authed_probes = [
        ("POST /auth/signup", lambda: api.client.post("/auth/signup", json={"email": "zed@example.com", "password": PW, "display_name": "Z"})),
        ("POST /auth/login", lambda: api.login("alice@example.com")),
        ("GET  /me", lambda: api.me(tok)),
        ("POST /payments", lambda: api.pay(tok, "bob", 1)),
        ("POST /requests", lambda: api.request_(tok, "bob", 1)),
        ("POST /requests/{id}/pay", lambda: api.act(bob, rids[0], "pay")),
        ("POST /requests/{id}/decline", lambda: api.act(bob, rids[1], "decline")),
        ("POST /requests/{id}/cancel", lambda: api.act(tok, rids[2], "cancel")),
        ("GET  /requests", lambda: api.client.get("/requests", headers={"Authorization": f"Bearer {tok}"})),
        ("POST /splits", lambda: api.split(tok, 3, ["bob", "carol"])),
        ("GET  /activity", lambda: api.activity(tok)),
        ("POST /settlements", lambda: api.settle(op, [{"from": "bob", "to": "carol", "amount": 1}])),
    ]
    probes = system_results + authed_probes
    s.check("all 16 endpoints are declared", len(probes) == 16, f"counted {len(probes)}")

    for entry in probes:
        if len(entry) == 3:
            s.check(f"{entry[0]} -> 2xx (no 404/405)", False, entry[2])
            continue
        name, resp = entry
        if callable(resp):
            try:
                resp = resp()
            except Exception as exc:  # noqa: BLE001
                s.check(f"{name} -> 2xx (no 404/405)", False, f"{type(exc).__name__}: {exc}")
                continue
        s.check(
            f"{name} -> 2xx (no 404/405)",
            200 <= resp.status_code < 300,
            f"got {resp.status_code} {resp.text[:120]}",
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Pocketful stage-1 evidence generator")
    parser.add_argument("--base", default="http://127.0.0.1:8080")
    parser.add_argument("--skip-concurrency", action="store_true")
    args = parser.parse_args()

    s = Suite()
    api = Api(args.base, on_error=lambda what, detail: s.check(what, False, detail))
    try:
        api.health()
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] connectivity\n       cannot reach {args.base}: {type(exc).__name__}: {exc}")
        print("\n=== SUMMARY: 0 passed, 1 failed, 0 skipped ===")
        return 1

    print(f"pocketful stage-1 verification against {args.base}")

    # Each work item is isolated: a crash inside one section is recorded as a
    # failure and the rest of the suite still runs, so a broken build still
    # produces a complete verdict rather than a truncated log.
    sections = [
        ("W1", w1), ("inventory", inventory), ("W2", w2), ("W3", w3),
        ("W4", w4), ("W5", w5), ("W6", w6), ("W7", w7), ("W8", w8),
    ]
    if not args.skip_concurrency:
        sections.append(("concurrency", lambda a, su: concurrency(a, su)))

    for name, fn in sections:
        try:
            fn(api, s)
        except Exception as exc:  # noqa: BLE001
            import traceback

            s.check(f"{name} section ran to completion", False, f"{type(exc).__name__}: {exc}")
            traceback.print_exc()

    if args.skip_concurrency:
        s.head("concurrency")
        s.check("concurrency hammer", True, "SKIPPED via --skip-concurrency")
    return s.summary()


if __name__ == "__main__":
    sys.exit(main())
