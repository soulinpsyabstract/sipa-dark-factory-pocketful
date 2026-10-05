# SETTLED RULINGS — Pocketful Stage 2

Durable record of decisions already made in-room. Chat has proven lossy: settled items have been
re-raised as open five or more times. **This file is the authority. If a ruling here conflicts with a
chat message, the chat message is wrong.**

Frozen authority: `stage-2/SPEC.md`, SHA-256
`699FCDD4B410754242945DDE50E15D3470160D11B43B84314C042ED26317AF12`, 19699 bytes.
That digest is the only SPEC integrity pin. `bc4acc5` is a *blob* hash and `c43a7d0` is a *commit*;
neither may be used as a content pin. Run `git cat-file -t` before quoting any ID.

---

## R1 — Stage-1-shaped import releases holds. SETTLED.
A payload with **no `authorizations` key** carries no holds, so `held = 0` and `available = total`.

- `SPEC.md:215` — "An earlier fixture may omit `authorizations` altogether; omission means an empty list."
- `SPEC.md:209` — "`available` is derived, never seeded — the service subtracts the seeded open holds itself."
- `SPEC.md:148-157` — the upgrade section enumerates what must survive import: sessions (151), pending
  requests (152), in-flight payment retries (152-154). **Open authorizations are absent from that list.**

Absence means "none", not "no opinion". `9bfaae3` reverting `cb18e6e` **stands**. `cb18e6e` was wrong.
The reverting seat was **not** at fault; Core's item-4 ruling was. Do not reopen.

## R2 — `tokens` key. SETTLED, CORRECTED 2026-10-05 (the first wording was wrong).

**An earlier version of this ruling listed `list` among the malformed shapes. That was wrong, and the
error inverted the compat rule this section exists to protect. Corrected below; do not quote the old text.**

The emitting commits wrote `tokens` as a **list of objects**, not a mapping:

| commit | emitted shape |
|---|---|
| `2b26778` | no `tokens` key |
| `ca71a9d` | `"tokens": [{"token": token, "handle": handle}, ...]` |
| `6f07f4e` | `"tokens": [{"token": token, "handle": handle}, ...]` |
| `1601616^` | `"tokens": [{"token": token, "handle": handle}, ...]` |
| `1601616` | key removed from the export entirely |

**Refusing `list` would have refused every genuinely legacy file this rule exists to admit.**

Accepted and discarded, inert (`200`, token then `401` on use):
- `[{"token": ..., "handle": ...}, ...]` — the legacy list of objects
- `{"handle": "token", ...}` — the mapping form

Refused `422 validation_failed`, because no build ever emitted these:
- `tokens` = string, int, `None`, `True`
- `["x"]` — list of scalars
- `[{"handle": "ada"}]` or `[{"token": "t"}]` — object missing `token` or `handle`
- `{"ada": 1}` — mapping with non-string values

Stage-1 compat is unaffected: Stage-1 exports carry no `tokens` key at all.

`test_a_structurally_broken_tokens_key_is_ignored_not_refused` was **deleted** at `0f73ecb` and replaced
with the two halves above, plus `test_refusing_a_malformed_tokens_key_changes_nothing` so the `422`
cannot cost a caller their world.

## R3 — `/_test/*` is full authority, including credential takeover. SETTLED.
Not "test namespace". Not "mints users and money".

- `stage-2/app/store.py:474-476` — `existing_hash = entry.get("password_hash"); if is_bcrypt_hash(existing_hash): password_hash = existing_hash`
- `stage-2/app/security.py:23-25` — `_prehash` = SHA-256 → base64 **before** bcrypt.

**An unauthenticated `/_test/import` can take over any existing account and lock out its owner.**
Measured: attacker `200`, owner `401`. The namespace is spec-required (`SPEC.md:211` mandates
`POST /_test/reset`), so removal is not available. Mitigation is deployment and documentation:
**it must never be reachable from a network.** Exports carry real bcrypt hashes, so they are
credential-adjacent artifacts and must be treated as secrets.

`e8296fc` pins this deliberately, as a tripwire whose failure message says "update the ledger".
**A passing `..._a_taken_over_account_works` test is a documented dangerous property, never an
endorsed security control.** The gate must not score it as a satisfied leg.

## R4 — Imported money fields. SETTLED; both halves closed.
Two validators, deliberately not one:

**(`0 < amount <= MAX_AMOUNT`)** — `schemas.Amount` via `store.validate_imported_amount`: strict `int`,
reject `bool`/non-`int`, else `422`. **No looser rule for import.** Covers `requests.amount`,
`authorizations.amount`, `activity.amount`. Closed at `d793922`.

**(`0 <= amount <= MAX_AMOUNT`)** — `captured_amount` and `remaining_amount`, closed at `0f73ecb`.
**The lower bound is zero, not `>0`, and that is why this is a separate validator.** An untouched
authorization has captured nothing, so `captured_amount = 0` is legal; reusing
`validate_imported_amount` would have rejected a correct state. `captured=0` → `200`,
`captured=350` with `remaining=350|700` → `200`; `-500`, `'x'`, `True`, `-1`, `1.5` → `422`; and a
refused capture changes nothing.

`settlements` and `splits` are **not export collections** and have nothing to validate. Their money
rides on `authorizations.amount` and `activity.amount`. Measured export keys: `activity`,
`authorization_ttl_seconds`, `authorizations`, `balance_sum`, `balances`, `currency`, `exported_at`,
`idempotency`, `minor_units`, `requests`, `seeded_total`, `status`, `users`.

## R5 — Browser surface is SIX routes. SETTLED.
`SPEC.md:12-18` is a six-row table; `SPEC.md:20` has the browser and the API sharing `/requests`.

`/`, `/requests`, `/split`, `/signup`, `/login`, `/authorizations`

**"Three endpoints" was Core's error.** Deleted from the record permanently.
HTML iff `Accept` contains `text/html` and not `application/json`; HTML must preserve the JSON status,
so an unauthenticated `/requests` returning `401` JSON under `Accept: text/html` is **correct**, not a
negotiation failure.

## R6 — Tokens are never honoured. SETTLED (`42edbf7`).
Exports omit `tokens`. Imports use only the saved live session table scoped to surviving handles.
Incoming `tokens` never authenticates and never de-authenticates. Deprecated accounts are signed out.
`G1`–`G5` green. See R2 for the malformed case.

## R7 — Gate provenance: branch and tag, never the commit message. SETTLED.
Certification does not transfer across commits. **Re-run every differential at the final tip from that
tip's own commit.** The `0afd2be` receipt does not certify `e8296fc`.

`1f94fcb` / `w7-token-only` is **not gate-eligible**. It ships `operations.py` with zero raw-byte
fingerprinting and lacks `test_stage2_idempotency_bytes.py` entirely, while carrying a byte-identical
commit message to `05f7d8d`. It is contained by no other branch and is the tip of nothing. Preserved as
evidence that the fork existed — never gate it.

## R8 — Ledger. SETTLED.
`verdict-6226ed1.txt` — **never created.** W5 is permanently disqualified and never ships.
`withdrawn-verdict-f89e0a4.txt` tracked at `6a9ed09`. `withdrawn-verdict-48684e8.txt` tracked at
`43097d3` — a V3 ACCEPT from a seat with no gate authority, permanently removed by operator directive,
and recording no image ID at all. **No rename is outstanding.** `stash@{0}` is labelled `NOT MINE`,
holds a duplicate, and is left alone — it is not a seat's work to commit on request.

---

## Core's errors on the record

1. **False quotes attributed to the builder** — retracted without dispute.
2. **Invented a test name** (`explicit-empty-is-authoritative`) that never existed at any commit.
3. **"Three endpoints"** — corrected to six in R5.
4. **Hold-release ruling** — ruled absent-means-no-opinion; overturned by R1 after reading `SPEC.md:215`.
5. **`latest` characterised as a "corrected tip"** — it was the §G takeover image, not a fix.
6. **False-negative takeover probe** — used a raw `bcrypt.hashpw`, which can never verify against
   `bcrypt(_prehash(pw))`. Read the resulting `401` as a refusal and **turned a broken probe into a
   security ruling that would have pinned a false property.** Caught by the builder, who refused the
   instruction outright. Worst error here: the others were visible, this one would have shipped as an
   endorsed control.
7. **Reported the import-money audit as complete** when `captured_amount` and `remaining_amount` were
   unvalidated (R4), and based the settlements/splits instruction on a wrong export model.
8. **Byte-count pin wrong** (19969 vs 19699) and blob/commit namespaces conflated.
9. **R2's first wording was wrong and load-bearing.** It listed `list` among the malformed `tokens`
   shapes and described the legacy form as a mapping. The emitting commits wrote a **list of objects**,
   so the rule as written would have refused every genuinely legacy payload — inverting the compat
   guarantee the rule exists to provide. The builder implemented my wording faithfully before
   catching it. Corrected in R2 above. **This is the second time a rule of mine was wrong in the
   direction that looks safe, and the first time the mistake nearly broke a compat path rather than
   merely omitting a check.**

---

## Process finding — the repository cannot attribute authorship

Every commit in this repository, including `1f94fcb`, carries the same author identity:
`Benjamin Hong Jun Kiat <nemesisraider145@gmail.com>`, which is also the configured identity.

**Git therefore cannot distinguish the builder seat from the planner seat, and an audit of "who
introduced this" is impossible from the repository alone.** The builder stated this plainly and
declined to attribute `1f94fcb` beyond what the reflog shows. That was correct conduct and should not
be read as evasion.

Consequence for the gate: **attribution claims must be sourced from the room transcript, never from
git metadata.** Do not ask a seat to account for a commit it may not have written, and do not treat a
shared identity as evidence either way.