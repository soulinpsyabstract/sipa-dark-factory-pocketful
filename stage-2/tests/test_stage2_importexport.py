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
import json
import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.main import app  # noqa: E402
from app.security import hash_password, verify_password  # noqa: E402

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
    """Half one of the hold rule, and the direction SPEC.md:215 requires.

    "An earlier fixture may omit ``authorizations`` altogether; omission means an
    empty list." A stage-1-shaped payload therefore carries no holds, so
    ``held = 0`` and ``available = total`` - the 800 minor units go back to
    spendable *because the payload says there are none*, not because import
    failed to notice them.

    The inverse half is ``test_a_deprovisioning_fixture_drops_the_removed_payers_hold``.
    Together they pin that "absent" and "empty" are the same thing here, while a
    genuine deprovisioning also drops the removed payer's hold.
    """
    # Establish a live hold FIRST. Without this the assertion is trivially
    # true - the fixture starts with held == 0, so "held is 0 after import"
    # proves nothing about what import did. With the hold in place, this is
    # the leg that fails if a wrong rule preserves holds on an absent key.
    client.post(
        "/authorizations",
        json={"to_handle": "bob", "amount": 800},
        headers=auth(login(client, "ada"), "k1"),
    )
    assert wallet(client, "ada") == (2000, 800, 1200), "precondition: a live hold must exist"

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


def test_a_payload_tokens_key_is_ignored_entirely(client):
    """An incoming ``tokens`` key neither authenticates nor de-authenticates.

    Exports written before the key was dropped still have to import, so the key
    is accepted and discarded. It is not honoured (that would be an
    unauthenticated write into the session table) and it is not validated (that
    would make an old file unimportable over a field nobody reads any more).
    """
    ada = auth(login(client, "ada"))
    payload = export(client)
    assert "tokens" not in payload, "the current export must not carry tokens"

    payload["tokens"] = [{"token": "attacker-chosen", "handle": "ada"}]
    assert do_import(client, payload).status_code == 200

    # It does not authenticate: the chosen string is not a session.
    assert client.get("/me", headers=auth("attacker-chosen")).status_code == 401
    # And it does not de-authenticate: the live session is untouched.
    assert client.get("/me", headers=ada).status_code == 200


def test_a_payload_tokens_key_cannot_mint_a_session_for_another_user(client):
    """The regression that made the key dangerous. Measured on 066bb0a:

    a caller imported a payload carrying {"token": <their own string>, "handle":
    "ada"}, then authenticated as ada and moved 4000 minor units out of her
    wallet. ``/_test/import`` requires no authentication, so this was reachable
    by anyone who could reach the port at all.
    """
    login(client, "ada")
    ada = auth(login(client, "ada"))

    payload = export(client)
    payload["tokens"] = [{"token": "attacker-minted-this", "handle": "ada"}]
    assert do_import(client, payload).status_code == 200

    forged = auth("attacker-minted-this")
    assert client.get("/me", headers=forged).status_code == 401, (
        "a payload tokens key minted a working session"
    )
    pay = client.post(
        "/payments",
        headers={**forged, "Idempotency-Key": "esc"},
        json={"to": "bob", "amount": 4000},
    )
    assert pay.status_code == 401, f"forged token moved money: {pay.text}"
    assert client.get("/me", headers=ada).status_code == 200


def test_an_old_export_carrying_tokens_cannot_wipe_sessions(client):
    """The upgrade path: a pre-G file imports and leaves everyone signed in.

    This is the property that makes ignoring the key safe - the live table is
    preserved for surviving handles regardless of what the payload claims.
    """
    ada = auth(login(client, "ada"))
    bob = auth(login(client, "bob"))

    payload = export(client)
    payload["tokens"] = [
        {"token": "stale-ada", "handle": "ada"},
        {"token": "stale-bob", "handle": "bob"},
    ]
    assert do_import(client, payload).status_code == 200

    assert client.get("/me", headers=ada).status_code == 200
    assert client.get("/me", headers=bob).status_code == 200
    for stale in ("stale-ada", "stale-bob"):
        assert client.get("/me", headers=auth(stale)).status_code == 401


@pytest.mark.parametrize(
    "legacy",
    [
        # The shape the token-export era actually wrote, verified against the
        # emitting commits (ca71a9d, 6f07f4e, 1601616^).
        [{"token": "stale-ada", "handle": "ada"}],
        [{"token": "stale-ada", "handle": "ada"}, {"token": "stale-bob", "handle": "bob"}],
        # And the same information as a mapping.
        {"ada": "stale-ada"},
        {},
    ],
)
def test_a_well_formed_legacy_tokens_key_imports_and_is_discarded(client, legacy):
    """Half one of R2: a structurally valid legacy key restores, then does nothing.

    Refusing these would 422 the whole import over a field with no effect and
    make every genuinely legacy export unimportable, which is the opposite of
    what the compat rule is for.
    """
    ada = auth(login(client, "ada"))
    payload = export(client)
    payload["tokens"] = legacy
    r = do_import(client, payload)
    assert r.status_code == 200, r.text
    # The live session survives, and nothing from the payload authenticates.
    assert client.get("/me", headers=ada).status_code == 200
    for entry in legacy if isinstance(legacy, list) else [
        {"token": v} for v in legacy.values()
    ]:
        assert client.get("/me", headers=auth(entry["token"])).status_code == 401


@pytest.mark.parametrize(
    "junk",
    [
        7,
        "not-a-list",
        None,
        True,
        ["not-an-object"],
        [{"handle": "ada"}],
        [{"token": "t"}],
        [{"token": 1, "handle": "ada"}],
        [{"token": "t", "handle": 2}],
        {"ada": 1},
        {"ada": None},
        # A non-string mapping *key* is unreachable over JSON: `{"1": "t"}` is
        # what a caller sending `{1: "t"}` produces, and that is a well-formed
        # mapping, accepted by the leg above. JSON has only string keys, so the
        # validator cannot reject one and the test must not pretend to.
    ],
    ids=[
        "int",
        "string",
        "null",
        "bool",
        "list-of-non-objects",
        "entry-missing-token",
        "entry-missing-handle",
        "entry-token-not-string",
        "entry-handle-not-string",
        "mapping-value-not-string",
        "mapping-value-null",
    ],
)
def test_a_structurally_malformed_tokens_key_is_refused(client, junk):
    """Half two of R2: a structurally impossible key is 422, not swallowed.

    ``tokens`` is in no stage-1 or stage-2 contract, so no build ever emitted
    these. Importing them anyway silently swallows corruption in a restore path.
    """
    payload = export(client)
    payload["tokens"] = junk
    r = do_import(client, payload)
    assert r.status_code == 422, f"{junk!r} should be refused, got {r.status_code}: {r.text}"


def test_refusing_a_malformed_tokens_key_changes_nothing(client):
    """A 422 on a junk key must not cost the caller their world."""
    ada = auth(login(client, "ada"))
    before = _state(client)
    payload = export(client)
    payload["tokens"] = "garbage"
    assert do_import(client, payload).status_code == 422
    assert _state(client) == before
    assert client.get("/me", headers=ada).status_code == 200


def test_a_deprovisioning_import_still_signs_the_removed_user_out(client):
    """Dropping tokens must not weaken the leg that matters.

    Removing a handle scopes preservation to surviving handles, so the removed
    user's live session dies even though the payload carries a token for them.
    """
    ada = auth(login(client, "ada"))
    login(client, "bob")

    payload = export(client)
    payload["tokens"] = [{"token": "legacy-ada", "handle": "ada"}]
    payload["users"] = [u for u in payload["users"] if u.get("handle") != "ada"]
    payload["balances"] = {"bob": 0}
    payload["seeded_total"] = 0
    payload["authorizations"] = []

    assert do_import(client, payload).status_code == 200
    assert client.get("/me", headers=ada).status_code == 401, (
        "a deprovisioning import left the removed user authenticated"
    )


def test_a_deprovisioning_fixture_drops_the_removed_payers_hold(client):
    """The negative control for the hold rule, in the direction SPEC.md:215 wants.

    ``test_a_stage1_export_leaves_no_holds_behind`` pins one half: a payload with
    no ``authorizations`` key must release, because ``:215`` makes omission an
    empty list. This pins the other half, which is what stops that rule from
    degenerating into a blanket refusal.

    A fixture that *does* deprovision - an explicit ``authorizations: []`` and a
    payer who is no longer in ``users`` - must drop that payer's hold. The two
    cases look similar and must behave differently:

    ==========  ==================  ======================
    payload     payer in users     required outcome
    ==========  ==================  ======================
    key absent  present             hold released (omission = empty)
    ``[]``      present             hold released (explicit empty)
    ``[]``      removed             hold dropped (deprovisioned)
    key present present             hold preserved
    ==========  ==================  ======================

    Both halves matter. An implementation that treats an absent key as "no
    opinion" passes a suite that never creates a live hold before importing a
    stage-1 payload, and an implementation that treats an explicit ``[]`` as
    "no opinion" passes a suite that never checks the deprovisioning leg. Either
    is a real defect: the first leaks a double-spend, the second reserves funds
    against a user who no longer exists.
    """
    ada = login(client, "ada")
    bob = login(client, "bob")

    # One live hold, so leg one is observable from a surviving user and leg two
    # from the exported world. bob starts on zero, so ada is the payer.
    client.post(
        "/authorizations",
        json={"to_handle": "bob", "amount": 800},
        headers=auth(ada, "k1"),
    )
    assert wallet(client, "ada") == (2000, 800, 1200), "precondition: ada must hold"

    payload = export(client)
    payload["authorizations"] = []
    assert do_import(client, payload).status_code == 200

    # Leg one: explicit empty, the payer still present -> the hold is released.
    assert wallet(client, "ada") == (2000, 0, 2000), (
        "an explicit empty authorizations list did not release the hold"
    )

    # Leg two: same payload, but the payer is deprovisioned too -> nothing may
    # survive, and the world must come back empty rather than dangling.
    client.post(
        "/authorizations",
        json={"to_handle": "bob", "amount": 800},
        headers=auth(login(client, "ada"), "k2"),
    )
    assert wallet(client, "ada") == (2000, 800, 1200), "precondition: a live hold again"

    payload = export(client)
    payload["users"] = []
    payload["balances"] = {}
    payload["seeded_total"] = 0
    payload["authorizations"] = []
    # Everything else that names a handle has to go too, or the import is
    # rightly refused for referencing a user that no longer exists and we
    # never reach the hold assertions.
    payload["requests"] = []
    payload["activity"] = []
    payload["idempotency"] = []
    assert do_import(client, payload).status_code == 200

    after = export(client)
    assert after["authorizations"] == [], (
        f"a deprovisioned payer's hold survived: {after['authorizations']}"
    )
    assert after["users"] == [], "the deprovisioning import left users behind"
    assert after["seeded_total"] == 0


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


def test_the_export_carries_no_tokens_and_no_bearer_token_anywhere(client):
    """Amended D3, leg (a): the export is not a credential-bearing file.

    Two separate assertions. The structural one is that there is no ``tokens``
    key. The stronger one is that no live bearer token appears anywhere in the
    serialised payload - a structural check alone would still pass if a token
    leaked into, say, the activity rows or a payment body.
    """
    ada = login(client, "ada")
    login(client, "bob")
    client.post(
        "/authorizations",
        json={"to_handle": "bob", "amount": 800},
        headers=auth(ada, "k1"),
    )
    client.post("/payments", json={"to": "bob", "amount": 100}, headers=auth(ada, "p1"))

    snap = export(client)
    assert "tokens" not in snap, "the export still carries a tokens key"

    body = json.dumps(snap)
    assert ada not in body, "a live bearer token appears in the export"
    assert "tokens" not in body

    # And the session is unaffected by having exported it.
    assert client.get("/me", headers=auth(ada)).status_code == 200


def test_an_imported_password_hash_is_inert_so_no_account_can_be_taken_over(client):
    """Import preserves the live credential; it never authenticates from the payload.

    The ruling: an imported ``password_hash`` is ignored. It is not installed,
    not used to authenticate, and therefore neither an attack primitive nor a
    lockout.

    Both halves are asserted, and the second is the one that keeps this a
    *restore* rule rather than a *disable login* rule:

    * attacker 401 - the injected digest grants nothing;
    * owner 200 - the live credential survived the import untouched, so an
      unmodified export still round-trips to a working login.

    Measured on a clean ``--network none`` container before the fix, with an
    attacker-chosen digest built the correct way (``hash_password``, which
    applies the SHA-256 pre-hash the app uses)::

        baseline login as ada .................. 200
        import with rewritten password_hash .... 200
        login as ada with ATTACKER's password .. 200   <- takeover
        login as ada with her real password .... 401   <- locked out

    After the fix, same payload::

        baseline login as ada .................. 200
        import with rewritten password_hash .... 200
        login as ada with ATTACKER's password .. 401
        login as ada with her real password .... 200

    The reason the earlier 401 looked like a defence is worth recording: a naive
    ``bcrypt(pw)`` digest never verifies against ``security._prehash`` and so
    returned 401 regardless. That was an accident of the pre-hash, not a
    property of the import path. ``hash_password`` is a public function in the
    same module, so the real attack needs nothing an attacker does not have.

    The reasoning behind the ruling, in one line: ``SPEC.md:150-151`` names what
    must survive an upgrade - the browser session (``:151``), pending requests
    and their retry identity (``:152-157``). A password is not on that list. The
    same reading governs ``authorizations``, where ``:215`` explicitly makes
    omission mean "no holds" rather than "leave them alone".
    """
    snap = export(client)
    hijack = hash_password("attacker-chosen-password")
    assert verify_password("attacker-chosen-password", hijack)
    assert not verify_password(PW, hijack), "the attacker's digest must not match ada's password"

    for user in snap["users"]:
        if user["handle"] == "ada":
            user["password_hash"] = hijack
    assert do_import(client, snap).status_code == 200

    attacker = client.post(
        "/auth/login", json={"email": "ada@example.com", "password": "attacker-chosen-password"}
    )
    assert attacker.status_code == 401, (
        "import installed a password_hash from the payload; /_test/import can take over any account"
    )

    owner = client.post("/auth/login", json={"email": "ada@example.com", "password": PW})
    assert owner.status_code == 200, (
        "import discarded the live credential; an unmodified export no longer round-trips to a login"
    )


def test_an_unmodified_export_still_round_trips_its_own_logins(client):
    """The other half of the credential rule, isolated from any tampering.

    A ruling of "import ignores ``password_hash``" can be implemented two ways:
    by preserving the live credential, or by discarding credentials wholesale.
    The second also makes the attacker's login fail, so only this test tells
    them apart - it imports an untouched export and requires every seeded login
    to keep working.

    Measured on a clean ``--network none`` container::

        login before import ................. 200 200 200
        import of an unmodified export ...... 200
        login after import .................. 200 200 200
    """
    before = {h: client.post("/auth/login", json={"email": f"{h}@example.com", "password": PW}).status_code
              for h in ("ada", "bob", "cyd")}
    assert before == {"ada": 200, "bob": 200, "cyd": 200}, "precondition: all three must log in"

    assert do_import(client, export(client)).status_code == 200

    after = {h: client.post("/auth/login", json={"email": f"{h}@example.com", "password": PW}).status_code
             for h in ("ada", "bob", "cyd")}
    assert after == before, "an unmodified export stopped round-tripping its own logins"


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

    The buckets that carry authority - ``authorizations`` above all - are
    compared by value, not by length. A count would pass while a hold or a live
    session was silently rewritten, which is precisely the W5 defect shape:
    import dropped every authorization, so a count could stay right while the
    hold itself vanished.

    Sessions are not in the export at all any more, so they are checked
    behaviourally by ``test_a_rejected_import_leaves_live_sessions_working``.
    """
    snap = export(client)
    return {
        "balances": snap["balances"],
        "requests": snap["requests"],
        "authorizations": snap["authorizations"],
        "activity": snap["activity"],
        "idempotency": snap["idempotency"],
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
        
        pytest.param(lambda p: p.__setitem__("requests", "not-a-list"), id="requests-not-a-list"),
        pytest.param(lambda p: p.__setitem__("authorizations", {"nope": 1}), id="authorizations-not-a-list"),
        pytest.param(lambda p: p.__setitem__("activity", 7), id="activity-not-a-list"),
        pytest.param(lambda p: p.__setitem__("idempotency", "nope"), id="idempotency-not-a-list"),
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


def test_an_imported_request_amount_is_validated_like_the_write_path(client):
    """An imported ``requests.amount`` is refused unless the API would accept it.

    This was ``xfail(strict)`` while the field went unvalidated. Core measured the
    consequences on ``980c679f1f76`` and they are worse than an inconsistency:

      * ``amount: -500`` imported then paid ran **backwards** - the payer *gained*
        500 and the requester lost 500 they never agreed to give up. An
        unauthorized transfer. The app's own invariant report called it healthy,
        because a transfer that conserves the sum is not a conservation failure,
        so nothing announced it.
      * ``amount`` as a string raised ``TypeError`` out of ``_require_available``,
        propagating to a 500 on ``POST /requests/{id}/pay``.

    Both were reachable through **unauthenticated** ``POST /_test/import``.

    The rule enforced is the write path's own ``Amount``: strict ``int``, greater
    than zero, at most ``MAX_AMOUNT``. An import must be at least as strict as the
    API it feeds, so there is deliberately no looser import-specific variant.
    """
    bad = [
        -5,                                  # negative: pays backwards
        0,                                   # not positive
        "500",                               # string: 500s on pay
        1.5,                                 # float
        True,                                # bool is an int subclass in Python
        2 ** 53,                             # above MAX_AMOUNT
        None,                                # missing
    ]
    for value in bad:
        payload = export(client)
        payload["requests"].append(
            {"id": "r_x", "from": "ada", "to": "bob", "status": "open", "amount": value}
        )
        r = do_import(client, payload)
        assert r.status_code == 422, (
            f"import accepted requests.amount={value!r} ({r.status_code}); "
            "the write path would refuse it"
        )


def test_a_string_imported_amount_is_a_422_not_a_500(client):
    """The string leg on its own, because it was a 500 rather than an inconsistency.

    A negative amount is a *wrong value*: it imports and then pays backwards, a
    money defect. A string amount is a different class of failure - it is a
    Python ``TypeError`` raised out of ``_require_available`` on the *pay* path,
    propagating as an unhandled 500. So it is pinned separately rather than as
    one more entry in the table above:

      * the finding was that a 500 is reachable through **unauthenticated**
        ``POST /_test/import``, which is its own problem independent of the
        value being wrong;
      * a negative amount fails loudly and visibly at import. A string amount
        sat dormant in an accepted import and detonated later, on an unrelated
        request, with a stack trace instead of a status code.

    Measured on ``980c679f1f76`` before the fix::

        import {"amount": "500"} .................. 200
        POST /requests/{id}/pay ................... 500   <- TypeError
    """
    bob = login(client, "bob")
    login(client, "ada")

    payload = export(client)
    payload["requests"].append(
        {"id": "r_str", "from": "ada", "to": "bob", "status": "open", "amount": "500"}
    )
    r = do_import(client, payload)
    assert r.status_code == 422, (
        f"import accepted a string amount ({r.status_code}); it detonates as a "
        "500 later, on the pay path, from an unauthenticated import"
    )

    # And it must be a clean rejection, not a half-applied world: the string
    # request must not exist afterwards, and the real one must still pay.
    # bob has no funds, so ada is the payer here: bob requests, ada pays.
    real_id = client.post(
        "/requests", json={"to": "ada", "amount": 500}, headers=auth(bob, "rq2")
    ).json()["id"]
    pay = client.post(f"/requests/{real_id}/pay", json={}, headers=auth(login(client, "ada"), "pay1"))
    assert pay.status_code in (200, 201), pay.text
    assert pay.json()["amount"] == 500, "a real amount must not be coerced"


def test_an_imported_negative_request_amount_cannot_be_paid_backwards(client):
    """The money consequence itself, not just the status code.

    A 422 is only worth something if the inverted payment is genuinely
    unreachable afterwards. This pins the reachable path end to end: build the
    bad import, show it is refused, and show the original request is still
    payable at its own amount with the conservation invariant intact.
    """
    # bob requests from ada, so ada is the payer.
    bob = login(client, "bob")
    request_id = client.post(
        "/requests", json={"to": "ada", "amount": 500}, headers=auth(bob, "rq1")
    ).json()["id"]

    payload = export(client)
    payload["requests"].append(
        {"id": "r_x", "from": "ada", "to": "bob", "status": "open", "amount": -500}
    )
    assert do_import(client, payload).status_code == 422

    token = login(client, "ada")
    before = wallet(client, "ada")
    pay = client.post(f"/requests/{request_id}/pay", json={}, headers=auth(token, "pay1"))
    assert pay.status_code in (200, 201), pay.text
    # Paid at its real amount: ada paid 500, she did not gain it.
    assert pay.json()["amount"] == 500
    ada_after = wallet(client, "ada")
    assert ada_after[0] == before[0] - 500, "the payment moved the wrong way"


def test_an_imported_negative_authorization_amount_is_refused(client):
    """An open authorization reserving a negative amount is refused.

    It used to import ``200`` and then contribute nothing to ``held``, leaving a
    record that exists, is open, and reserves nothing. Core noted this was not
    directly exploitable; it is still an authorization that claims to hold funds
    and holds none, and the write path would never have created it.
    """
    payload = export(client)
    payload["authorizations"].append(
        {"id": "a_x", "from": "ada", "to": "bob", "status": "open", "amount": -3000}
    )
    r = do_import(client, payload)
    assert r.status_code == 422, f"import accepted a negative authorization amount ({r.status_code})"


def test_an_imported_activity_amount_is_validated(client):
    """Activity entries carry money that already moved, so they get the same rule."""
    token = login(client, "ada")
    client.post("/payments", json={"to": "bob", "amount": 100}, headers=auth(token, "p1"))

    payload = export(client)
    assert payload["activity"], "needs a populated feed for this to be meaningful"
    payload["activity"][0]["amount"] = -100
    r = do_import(client, payload)
    assert r.status_code == 422, f"import accepted a negative activity amount ({r.status_code})"


def test_activity_entries_without_an_amount_still_import(client):
    """The rule is conditional, so an entry with no amount is left alone.

    Every activity entry currently carries an ``amount`` key, so the absent case
    is constructed rather than observed.
    """
    token = login(client, "ada")
    client.post("/payments", json={"to": "bob", "amount": 100}, headers=auth(token, "p1"))

    payload = export(client)
    payload["activity"].append(
        {"id": "act_x", "handle": "ada", "type": "note", "actor": "ada",
         "counterparty": None, "direction": None, "related_id": None, "memo": None}
    )
    assert do_import(client, payload).status_code == 200


def test_negative_balances_are_still_rejected_and_this_did_not_loosen_them(client):
    """Core confirmed balance validation already worked. Guard against regression.

    The amount work adds validation to ``requests``, ``authorizations`` and
    ``activity``. It must not have relaxed the existing check on money that
    actually leaves an account.
    """
    for balances in ({"ada": -1, "bob": 2001}, {"ada": 2001, "bob": -1}):
        r = do_import(client, {"users": [dict(u) for u in USERS], "balances": balances,
                               "seeded_total": 2000})
        assert r.status_code == 422, f"import accepted negative balances {balances}"


def test_a_rejected_amount_import_changes_nothing(client):
    """A 422 on the new checks must still be atomic."""
    _busy_world(client)
    before = _state(client)
    payload = export(client)
    payload["requests"].append(
        {"id": "r_x", "from": "ada", "to": "bob", "status": "open", "amount": -5}
    )
    assert do_import(client, payload).status_code == 422
    assert _state(client) == before, "a refused import left the world changed"


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
    payload["requests"].append(
        {"id": "r_x", "from": "ada", "to": "ghost", "amount": 1, "status": "open"}
    )
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


def test_a_rejected_import_leaves_sessions_and_authorizations_byte_identical(client):
    """A 422 must not touch the buckets that carry authority.

    Asserted by value rather than by count: a hold can be rewritten without
    changing how many there are.

    Sessions are not in the export any more, so "byte-identical" is checked
    against the server's own token table - the exact token strings minted before
    the failed import must still authenticate, which is only true if the table
    was never rewritten.
    """
    _busy_world(client)
    before = _state(client)
    assert before["authorizations"], "needs a populated world to be meaningful"

    # Capture the live token strings by value, before anything is attempted.
    live = {"ada": login(client, "ada"), "bob": login(client, "bob")}
    assert all(
        client.get("/me", headers=auth(tok)).status_code == 200 for tok in live.values()
    ), "needs live sessions to be meaningful"

    payload = export(client)
    # Move the money first, so a partial write that lands before validation
    # fails would be unmistakable in the balances too.
    payload["balances"] = {"ada": 1111, "bob": 889, "cyd": 0}
    payload["requests"].append(
        {"id": "r_x", "from": "ada", "to": "ghost", "amount": 1, "status": "open"}
    )

    assert do_import(client, payload).status_code == 422

    after = _state(client)
    assert after["authorizations"] == before["authorizations"], (
        "authorizations were rewritten by a rejected import"
    )
    assert after == before

    # The same token strings, still working: the table was preserved, not
    # rebuilt with fresh tokens.
    for handle, tok in live.items():
        r = client.get("/me", headers=auth(tok))
        assert r.status_code == 200, f"{handle}'s live session did not survive the 422"
        assert r.json()["handle"] == handle


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
    """Amended D3, leg (c): an import must not leave a removed user signed in.

    A token that was *already live in the daemon* - never carried in the payload,
    because the export does not carry tokens - for a handle the imported payload
    does not contain, must stop working. If it survives, an import that
    deprovisions a user leaves them authenticated: an authentication bypass.
    """
    ada = login(client, "ada")
    bob = login(client, "bob")
    assert client.get("/me", headers=auth(ada)).status_code == 200

    payload = export(client)
    payload["users"] = [u for u in payload["users"] if u.get("handle") != "ada"]
    payload["balances"] = {"bob": 0}
    payload["seeded_total"] = 0
    payload["authorizations"] = []

    assert do_import(client, payload).status_code == 200

    assert client.get("/me", headers=auth(ada)).status_code == 401, (
        "a deprovisioned user's session survived the import - auth bypass"
    )
    assert client.get("/me", headers=auth(bob)).status_code == 200, (
        "a surviving user's session was dropped needlessly"
    )


def test_a_live_session_for_a_removed_user_is_dropped_from_a_stage1_shaped_payload(client):
    """Same rule from the other payload shape.

    A stage-1 export cannot even express authorizations, let alone sessions. It
    still takes the full-export path, and a handle it removes must not stay
    signed in. Bare fixtures are the one exception - they take the reset path
    and start token-free by design (``test_reset_still_wipes_tokens``).
    """
    ada = login(client, "ada")
    login(client, "bob")

    payload = {
        "users": [
            {"handle": "bob", "email": "bob@example.com", "balance": 500, "password": PW}
        ],
        "balances": {"bob": 500},
        "seeded_total": 500,
    }
    assert do_import(client, payload).status_code == 200

    assert client.get("/me", headers=auth(ada)).status_code == 401, (
        "a deprovisioned user's session survived a stage-1 shaped import"
    )


def test_a_stage1_shaped_export_preserves_live_sessions(client):
    """Amended D3: every export is session-free, so preservation is the rule.

    With no ``tokens`` key in any payload there is nothing to restore, and the
    server's own token table is the authority. Wiping it would break SPEC.md line
    151 outright - a browser signed in before the upgrade must still be signed in
    afterwards - so sessions for surviving handles are preserved.
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


def test_a_rejected_import_leaves_live_sessions_working(client):
    """Rollback must restore the session table, not just the balances."""
    ada = login(client, "ada")
    bob = login(client, "bob")

    payload = export(client)
    payload["requests"].append(
        {"id": "r_x", "from": "ada", "to": "ghost", "amount": 1, "status": "open"}
    )
    assert do_import(client, payload).status_code == 422

    assert client.get("/me", headers=auth(ada)).status_code == 200
    assert client.get("/me", headers=auth(bob)).status_code == 200
