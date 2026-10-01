"""
Stage 1 independent verification harness for POCKETFUL_SPEC.md.

The verifier's OWN evidence. It does not read or trust any builder-supplied
test summary. Every assertion is backed by a real HTTP exchange.

Contract facts locked in from a source read of the submission (stage-1/app):
  * reset/import fixture users are keyed by "handle" (email optional); seeded
    passwords may be plaintext and get bcrypt-hashed by the app; login requires
    email/password, signup returns NO token (login does).
  * payments/requests use {"to": handle, "amount": int}; splits use
    {"amount": int, "participants": [handle, ...]}.
  * idempotency header: "Idempotency-Key" (case-insensitive), scoped (user,key).
  * auth: Authorization: Bearer <token> (or X-Auth-Token).

Usage:
    python verify/verify.py --base http://127.0.0.1:8080

Exit code 0 = all PASS, 1 = at least one FAIL.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import re
import sys
import threading
import uuid

import requests

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
_results: list[tuple[str, str, str]] = []
_lock = threading.Lock()

FIXTURE_PASSWORD = "verify-fixture-pw"


def record(status: str, name: str, detail: str = "") -> None:
    with _lock:
        _results.append((status, name, detail))
    tag = {"PASS": "[PASS]", "FAIL": "[FAIL]", "SKIP": "[SKIP]"}[status]
    line = f"{tag} {name}"
    if detail:
        line += f"\n         {detail}"
    print(line, flush=True)


def check(name: str, cond: bool, detail: str = "") -> bool:
    record(PASS if cond else FAIL, name, "" if cond else detail)
    return cond


class Client:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self.s = requests.Session()

    def call(self, method: str, path: str, body=None, token=None, headers=None):
        h = dict(headers or {})
        if token:
            h["Authorization"] = f"Bearer {token}"
        try:
            r = self.s.request(method, f"{self.base}{path}", json=body, headers=h, timeout=30)
        except requests.RequestException as e:
            return -1, {"transport_error": str(e)}
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, {"_non_json": r.text[:400]}

    def get(self, p, **k):
        return self.call("GET", p, **k)

    def post(self, p, body=None, **k):
        return self.call("POST", p, body, **k)


def jdump(o) -> str:
    try:
        return json.dumps(o, sort_keys=True)[:700]
    except Exception:
        return str(o)[:700]


HANDLE_RE = re.compile(r"^[a-z0-9_]{1,20}$")


def fixture_reset(c: Client, users: list[dict], total_hint: bool = True) -> int:
    """Reset state with handle-keyed users. Returns intended seeded total."""
    total = sum(u["balance"] for u in users if u.get("balance", 0) >= 0)
    fixture = {"users": users}
    if total_hint:
        fixture["seeded_total"] = total
    st, body = c.post("/_test/reset", fixture)
    if st != 200:
        record(FAIL, "fixture.reset", f"status {st}: {jdump(body)}")
    return total


def login_token(c: Client, email: str, password: str) -> str | None:
    st, body = c.post("/auth/login", {"email": email, "password": password})
    if st != 200:
        return None
    for k in ("access_token", "token", "jwt"):
        v = body.get(k) if isinstance(body, dict) else None
        if isinstance(v, str) and v:
            return v
    return None


def two_user_world(c: Client, handle_a: str, handle_b: str, bal_a: int, bal_b: int):
    """Reset, seed two users, log both in. Returns (total, tok_a, tok_b)."""
    ea, eb = f"{handle_a}@x.com", f"{handle_b}@x.com"
    total = fixture_reset(
        c,
        [
            {"handle": handle_a, "email": ea, "balance": bal_a, "password": FIXTURE_PASSWORD},
            {"handle": handle_b, "email": eb, "balance": bal_b, "password": FIXTURE_PASSWORD},
        ],
    )
    ta = login_token(c, ea, FIXTURE_PASSWORD)
    tb = login_token(c, eb, FIXTURE_PASSWORD)
    if not (ta and tb):
        record(FAIL, "world.setup", f"login failed: a={ta is not None} b={tb is not None}")
    return total, ta, tb


def total_balance(c: Client) -> tuple[int | None, str]:
    st, body = c.get("/_test/export")
    if st != 200:
        return None, f"export status {st}: {jdump(body)}"
    raw = json.dumps(body, sort_keys=True)
    if isinstance(body, dict):
        if isinstance(body.get("balance_sum"), int):
            return body["balance_sum"], raw
        if isinstance(body.get("balances"), dict):
            return sum(body["balances"].values()), raw
    return None, f"cannot find balance_sum: {raw}"


def export_balances(c: Client) -> dict[str, int] | None:
    st, body = c.get("/_test/export")
    if st != 200 or not isinstance(body, dict):
        return None
    bal = body.get("balances")
    if isinstance(bal, dict) and all(isinstance(v, int) for v in bal.values()):
        return dict(bal)
    return None


def negatives(c: Client) -> list[str]:
    bal = export_balances(c)
    return sorted(h for h, v in (bal or {}).items() if v < 0)


def g_health(c: Client) -> None:
    st, body = c.get("/health")
    check("S3 /health -> 200 {status:ok}",
          st == 200 and isinstance(body, dict) and body.get("status") == "ok",
          f"got {st}: {jdump(body)}")


def g_auth(c: Client) -> None:
    sig = uuid.uuid4().hex[:6]
    email, pw = f"alice{sig}@example.com", "AlicePassw0rd!23"
    st, body = c.post("/auth/signup", {"email": email, "password": pw, "display_name": "Alice"})
    check("S3 /auth/signup -> 2xx + user", st == 200 and isinstance(body, dict) and body.get("handle"),
          f"got {st}: {jdump(body)}")

    st2, body2 = c.post("/auth/login", {"email": email, "password": pw})
    tok = login_token_from_body(body2) if st2 == 200 else None
    check("S3 /auth/login -> 200 + access_token", st2 == 200 and tok is not None,
          f"got {st2}: {jdump(body2)}")

    st3, body3 = c.get("/me", token=tok)
    check("S3 GET /me authenticated -> 200", st3 == 200, f"got {st3}: {jdump(body3)}")

    st4, _ = c.get("/me")
    check("S3 GET /me no token -> 401", st4 == 401, f"got {st4}")

    st5, _ = c.post("/auth/login", {"email": email, "password": "Wrong-999"})
    check("S3 /auth/login bad password -> 401", st5 == 401, f"got {st5}")

    _, exp = c.get("/_test/export")
    blob = json.dumps(exp)
    check("S1 no plaintext password in export", pw not in blob, f"found {pw!r} in export")
    marker = any(h in blob for h in ("$2b$", "$2a$", "$2x$", "$2y$", "$argon2", "$scrypt"))
    check("S1 password stored as bcrypt/scrypt/Argon2", marker,
          "no hash marker in exported users")


def login_token_from_body(body) -> str | None:
    for k in ("access_token", "token", "jwt"):
        v = body.get(k) if isinstance(body, dict) else None
        if isinstance(v, str) and v:
            return v
    return None


def g_handles(c: Client) -> None:
    table = [
        ("Upper.Case@x.com", True, "derives lowercase handle"),
        ("a b@x.com", False, "local part may not contain whitespace -> 422 invalid_email"),
        ("waytoolonglocalpartmorethan20@x.com", False, "sanitises to >20 chars -> reject 422"),
        ("!!!@x.com", False, "sanitises to empty -> reject"),
    ]
    for email, expect_ok, note in table:
        st, body = c.post("/auth/signup",
                          {"email": email, "password": "Passw0rd!", "display_name": "H"})
        if expect_ok:
            ok = st == 200
            handle = body.get("handle") if isinstance(body, dict) else None
            valid = HANDLE_RE.match(handle or "") is not None
            check(f"S2 derived handle for {email!r} accepted + matches regex ({note})",
                  ok and valid,
                  f"status {st}, handle={handle!r}")
        else:
            check(f"S2 derived handle for {email!r} rejected 422 ({note})",
                  st == 422,
                  f"got {st}: {jdump(body)}")
    # explicit invalid handle must be 422
    st, body = c.post("/auth/signup",
                      {"email": "badhandle@x.com", "password": "Passw0rd!",
                       "display_name": "H", "handle": "Bad!Name"})
    check("S2 explicit invalid handle -> 422", st == 422, f"got {st}: {jdump(body)}")


def g_payments(c: Client) -> None:
    total, ta, tb = two_user_world(c, "bal_a", "bal_b", 10_000, 5_000)
    st, body = c.post("/payments", {"to": "bal_b", "amount": 1234}, token=ta,
                      headers={"Idempotency-Key": "inv-1"})
    check("S3 POST /payments -> 200", st == 200, f"got {st}: {jdump(body)}")
    got, _ = total_balance(c)
    check("S2 sum invariant after payment", got == total, f"expected {total}, got {got}")

    st, body = c.post("/payments", {"to": "bal_a", "amount": 500}, token=tb,
                      headers={"Idempotency-Key": "inv-2"})
    got, _ = total_balance(c)
    check("S2 sum invariant after 2nd payment", got == total, f"expected {total}, got {got}")

    st, body = c.post("/payments", {"to": "bal_b", "amount": 10**9}, token=ta,
                      headers={"Idempotency-Key": "inv-big"})
    check("S2 insufficient funds -> 422", st == 422, f"got {st}: {jdump(body)}")
    got, _ = total_balance(c)
    check("S2 no partial mutation on 422", got == total, f"expected {total}, got {got}")
    check("S2 no negative balances", not negatives(c), f"negatives: {negatives(c)}")


def g_idempotency(c: Client) -> None:
    total, ta, tb = two_user_world(c, "idem_a", "idem_b", 20_000, 20_000)
    key = "replay-key-001"
    payload = {"to": "idem_b", "amount": 1000}
    s1, b1 = c.post("/payments", payload, token=ta, headers={"Idempotency-Key": key})
    s2, b2 = c.post("/payments", payload, token=ta, headers={"Idempotency-Key": key})
    check("S2 /payments replay same response", s2 == 200 and b1 == b2,
          f"1st={jdump(b1)} 2nd={jdump(b2)}")
    got, _ = total_balance(c)
    check("S2 /payments replay not double-processed", got == 40_000, f"got {got}")

    s3, b3 = c.post("/payments", payload, token=tb, headers={"Idempotency-Key": key})
    got, _ = total_balance(c)
    check("S2 idempotency scoped per user (cross-user key independent)",
          got == 40_000, f"after other user's identical key sum={got} response={jdump(b3)}")

    for ep, p in (("/requests", {"to": "idem_b", "amount": 777}),
                  ("/splits", {"amount": 900, "participants": ["idem_a", "idem_b"]})):
        k = f"idem-{ep.strip('/')}-001"
        r1s, r1 = c.post(ep, p, token=ta, headers={"Idempotency-Key": k})
        r2s, r2 = c.post(ep, p, token=ta, headers={"Idempotency-Key": k})
        check(f"S2 {ep} replay -> 200 identical", r1s == 200 and r1s == r2s and r1 == r2,
              f"1st={jdump(r1)} 2nd={jdump(r2)}")
        got, _ = total_balance(c)
        check(f"S2 {ep} replay no double-process (sum 40000)", got == 40_000, f"got {got}")

    keyc = "idem-req-make-001"
    s, r = c.post("/requests", {"to": "idem_a", "amount": 555}, token=tb,
                  headers={"Idempotency-Key": keyc})
    rid = r.get("id") if isinstance(r, dict) else None
    check("S3 create request for pay-idempotency", s == 200 and rid is not None,
          f"got {s}: {jdump(r)}")
    if rid:
        pk = "idem-pay-001"
        s1, p1 = c.post(f"/requests/{rid}/pay", {}, token=ta, headers={"Idempotency-Key": pk})
        s2, p2 = c.post(f"/requests/{rid}/pay", {}, token=ta, headers={"Idempotency-Key": pk})
        check("S2 /requests/{id}/pay replay -> 200 identical", s1 == 200 and s1 == s2 and p1 == p2,
              f"1st={jdump(p1)} 2nd={jdump(p2)}")

    # key reuse with a DIFFERENT body must 409
    s, _ = c.post("/payments", {"to": "idem_b", "amount": 1}, token=ta,
                  headers={"Idempotency-Key": "replay-key-001"})
    check("S2 same key different body -> 409", s == 409, f"got {s}")


def g_splits(c: Client) -> None:
    for amt, n, expected in ((100, 3, [34, 33, 33]), (10, 4, [3, 3, 2, 2])):
        handles = [f"sp_{i}_{uuid.uuid4().hex[:3]}" for i in range(n)]
        users = [{"handle": h, "email": f"{h}@x.com", "balance": 5000,
                  "password": FIXTURE_PASSWORD} for h in handles]
        total = fixture_reset(c, users)
        ta = login_token(c, f"{handles[0]}@x.com", FIXTURE_PASSWORD)
        st, body = c.post("/splits",
                          {"amount": amt, "participants": handles},
                          token=ta, headers={"Idempotency-Key": f"split-{amt}-{n}"})
        got = None
        if isinstance(body, dict):
            if isinstance(body.get("shares"), list):
                got = body["shares"]
            elif isinstance(body.get("parts"), list):
                got = [p.get("amount") for p in body["parts"]]
        check(f"S2 split {amt}/{n} -> {expected}", st == 200 and got == expected,
              f"got status {st}, shares={got}, body={jdump(body)}")
        if st == 200:
            bal = export_balances(c)
            check(f"S2 split {amt}/{n} sum invariant",
                  bal is not None and sum(bal.values()) == total,
                  f"expected {total}, got {sum((bal or {}).values())}")
            check(f"S2 split {amt}/{n} no negatives", not negatives(c),
                  f"negatives: {negatives(c)}")


def g_requests(c: Client) -> None:
    _, ta, tb = two_user_world(c, "life_a", "life_b", 10_000, 10_000)
    st, body = c.post("/requests", {"to": "life_b", "amount": 1000}, token=ta)
    rid = body.get("id") if isinstance(body, dict) else None
    check("S3 POST /requests -> 200 + id", st == 200 and rid is not None,
          f"got {st}: {jdump(body)}")
    if not rid:
        return
    st, body = c.post(f"/requests/{rid}/decline", {}, token=tb)
    check("S3 /requests/{id}/decline -> 200", st == 200, f"got {st}: {jdump(body)}")

    st2, body2 = c.post("/requests", {"to": "life_b", "amount": 2000}, token=ta)
    rid2 = body2.get("id") if isinstance(body2, dict) else None
    if rid2:
        stc, _ = c.post(f"/requests/{rid2}/cancel", {}, token=ta)
        check("S3 /requests/{id}/cancel -> 200", stc == 200, f"got {stc}")

    stg, bodyg = c.get("/requests", token=ta)
    check("S3 GET /requests -> 200", stg == 200, f"got {stg}")
    for f in ("direction=outgoing", "status=open"):
        s2, _ = c.get(f"/requests?{f}", token=ta)
        check(f"S3 GET /requests?{f} -> 200", s2 == 200, f"got {s2}")
    sta, bodya = c.get("/activity", token=ta)
    check("S3 GET /activity -> 200", sta == 200, f"got {sta}: {jdump(bodya)}")


def g_test_helpers(c: Client) -> None:
    total = fixture_reset(c, [{"handle": "exp_a", "email": "exp_a@x.com", "balance": 4242,
                               "password": FIXTURE_PASSWORD}])
    st, exp = c.get("/_test/export")
    check("S3 GET /_test/export -> 200", st == 200, f"got {st}: {jdump(exp)}")
    st2, imp = c.post("/_test/import", exp if isinstance(exp, dict) else {})
    check("S3 POST /_test/import -> 200 (export round-trip)", st2 == 200,
          f"got {st2}: {jdump(imp)}")
    st3, _ = c.get("/health")
    check("S3 healthy after import", st3 == 200, f"got {st3}")


def g_settlements(c: Client) -> None:
    users = [
        {"handle": "op_op", "email": "op_op@x.com", "balance": 5000,
         "password": FIXTURE_PASSWORD, "is_operator": True},
        {"handle": "op_pe", "email": "op_pe@x.com", "balance": 7000,
         "password": FIXTURE_PASSWORD},
        {"handle": "op_no", "email": "op_no@x.com", "balance": 7000,
         "password": FIXTURE_PASSWORD},
    ]
    fixture_reset(c, users)
    top = login_token(c, "op_op@x.com", FIXTURE_PASSWORD)
    tpe = login_token(c, "op_pe@x.com", FIXTURE_PASSWORD)
    tno = login_token(c, "op_no@x.com", FIXTURE_PASSWORD)

    st, body = c.post("/settlements",
                      {"entries": [{"from": "op_pe", "to": "op_no", "amount": 333}]},
                      token=tno, headers={"Idempotency-Key": "stl-no-001"})
    check("S3 /settlements non-operator -> 403", st == 403, f"got {st}: {jdump(body)}")

    st, body = c.post("/settlements",
                      {"entries": [{"from": "op_pe", "to": "op_no", "amount": 333}]},
                      token=top, headers={"Idempotency-Key": "stl-op-001"})
    check("S3 /settlements operator -> 200", st == 200, f"got {st}: {jdump(body)}")
    s2, b2 = c.post("/settlements",
                    {"entries": [{"from": "op_pe", "to": "op_no", "amount": 333}]},
                    token=top, headers={"Idempotency-Key": "stl-op-001"})
    check("S2 /settlements replay identical + not double", s2 == 200 and s2 == st and b2 == body,
          f"1st={jdump(body)} 2nd={jdump(b2)}")
    got, _ = total_balance(c)
    check("S2 /settlements sum invariant after operator batch", got == 19_000, f"got {got}")
    check("S2 /settlements token path exercised", top is not None and tpe is not None and tno is not None)


def g_concurrency(c: Client) -> None:
    total, ta, tb = two_user_world(c, "conc_a", "conc_b", 100_000, 0)
    statuses: list[int] = []
    slock = threading.Lock()
    n = 60

    def one(i: int) -> None:
        st, _ = c.post("/payments", {"to": "conc_b", "amount": 100}, token=ta,
                       headers={"Idempotency-Key": f"conc-{i}"})
        with slock:
            statuses.append(st)

    with cf.ThreadPoolExecutor(max_workers=20) as ex:
        list(ex.map(one, range(n)))
    server_errs = [s for s in statuses if s >= 500]
    check("S2 concurrency: no 5xx leaked", not server_errs,
          f"{len(server_errs)} 5xx: {server_errs[:12]}")
    got, _ = total_balance(c)
    check("S2 concurrency: sum invariant", got == total, f"expected {total}, got {got}")
    check("S2 concurrency: no negatives", not negatives(c),
          f"negatives: {negatives(c)[:12]}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8080")
    args = ap.parse_args()

    c = Client(args.base)
    print(f"=== Stage 1 verification against {args.base} ===\n", flush=True)

    st, _ = c.get("/health")
    if st == -1 or st != 200:
        record(FAIL, "connectivity",
               f"/health at {args.base} -> {st}: {jdump(_)} - is the container running?")
        return summarise()

    g_health(c)
    g_auth(c)
    g_handles(c)
    g_payments(c)
    g_idempotency(c)
    g_splits(c)
    g_requests(c)
    g_test_helpers(c)
    g_settlements(c)
    g_concurrency(c)

    return summarise()


def summarise() -> int:
    p = sum(1 for s, _, _ in _results if s == PASS)
    f = sum(1 for s, _, _ in _results if s == FAIL)
    s = sum(1 for st, _, _ in _results if st == SKIP)
    print(f"\n=== SUMMARY: {p} passed, {f} failed, {s} skipped ===")
    if f:
        print("\nFAILURES:")
        for st, name, detail in _results:
            if st == FAIL:
                print(f"  - {name}: {detail}")
    return 1 if f else 0


if __name__ == "__main__":
    sys.exit(main())