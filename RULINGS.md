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

**Both directions are now pinned, and both were red-proven.** A test that imports a stage-1 payload
without first creating a hold asserts `held == 0` trivially — the fixture starts at zero — so it
proves nothing about what import did. `test_a_stage1_export_leaves_no_holds_behind` now establishes a
live 800 hold *before* the import. The inverse half,
`test_a_deprovisioning_fixture_drops_the_removed_payers_hold`, covers the cases that look similar and
must differ:

| payload | payer in `users` | required outcome |
| --- | --- | --- |
| key absent | present | hold released (omission = empty) |
| `[]` | present | hold released (explicit empty) |
| `[]` | removed | hold dropped (deprovisioned) |
| key present | present | hold preserved |

Red proof, each break injected at the real dispatch point in `store.py`:

| injected wrong rule | caught by |
| --- | --- |
| absent key preserves live holds (`cb18e6e`) | `..._stage1_export_leaves_no_holds_behind` → `(2000, 800, 1200)` != `(2000, 0, 2000)` |
| explicit `[]` preserves live holds (blanket refusal) | `..._deprovisioning_fixture_drops_the_removed_payers_hold` → `(2000, 800, 1200)` != `(2000, 0, 2000)` |

Without the first break the old stage-1 test passed for the wrong reason; without the second the whole
suite passed with a blanket refusal in place. Neither defect was visible before these pins.

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

## R3 — Import never authenticates from the payload. SUPERSEDES the earlier R3.
**Credential takeover via `/_test/import` is closed.** The earlier ruling recorded the opposite and
is withdrawn; see the correction note below.

**The rule: an imported `password_hash` is inert. Import preserves the live stored credential and
never authenticates from the payload.**

- `stage-2/app/store.py` — `Store._credential_from_import` returns the live digest for a surviving
  handle; `_install_fixture(honour_password_hash=False)` is the import path, `True` is the reset path.

Two halves, both pinned:

- injected `hash_password(attacker)` → attacker `401`, owner `200` (inert, not destructive);
- unmodified export → every seeded login still `200` (it is a *restore* rule, not *disable login*).

Measured on `--network none`, before and after:

| payload | before | after |
| --- | --- | --- |
| injected digest, attacker login | `200` | `401` |
| injected digest, owner login | `401` | `200` |
| unmodified export, owner login | `200` | `200` |

**Why, from the spec text.** `SPEC.md:150-151` names what must survive an upgrade: the browser session
(`:151`), pending requests and their retry identity (`:152-157`). A password is not on that list. The
same reading governs `authorizations`, where `:215` explicitly defines omission as "no holds" rather
than "leave the holds alone". A secret the spec does not name as payload-carried is not carried by an
import.

**Asymmetry with `/_test/reset`.** Reset still installs the payload's digest verbatim: it is a
bootstrap with no live world, not a restore onto one.

**Residual: exports are still secrets.** They carry real bcrypt digests. Minting users, setting
balances and erasing the world remain unrestricted. **The single control is that `/_test/*` must never
be reachable from a network** — now load-bearing rather than hygiene.

### Correction: what `e8296fc` recorded, and why it was wrong
`e8296fc` pinned "an imported `password_hash` is honoured, so `/_test/import` is full authority
including account takeover", on the measured result attacker `200` / owner `401`. That measurement was
correct. The inference drawn from it — that import must honour the digest — was not, because it
treated the observed behaviour as the rule rather than as a defect in it. The earlier probe's `401`
was read as a defence for a different reason: it used a raw `bcrypt.hashpw`, which cannot verify
against `security._prehash` (SHA-256 → base64 before bcrypt) and so returned `401` regardless of the
import path. `hash_password` is a public function in the same module, so the real attack needed
nothing an attacker did not have. The tripwire is now inverted and `..._a_taken_over_account_works`
is replaced by `test_an_imported_password_hash_is_inert_so_no_account_can_be_taken_over`.

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

Pinned by `test_imported_captured_and_remaining_amounts_are_validated`. **That pin was added later
than the fix** — `RULINGS.md` asserted these legs were measured while no test exercised them, which is
the "named leg with no test behind it" failure, in the ledger rather than in the section map. Red proof:
restoring the `e8296fc` behaviour (assign both fields straight from the payload) fails the test at
`captured_amount=-500` → `200`.

Two asymmetries the pin records, because they are not obvious and would be re-broken by a
"simplification":

- **`remaining_amount = None` is legal and means "not stated"** — the export omits the field on a hold
  that was never derived, so the import path skips validation rather than rejecting a payload its own
  export could produce.
- **`captured_amount = None` is illegal** — the field is defaulted to `0` when absent, so an explicit
  null is a malformed value, not an omission. → `422`.

**The string leg is pinned as its own case**, not as one more row in the `bad` table:
`test_a_string_imported_amount_is_a_422_not_a_500`. A negative amount is a *wrong value* that fails
loudly at import; a string amount is a different class — a `TypeError` out of `_require_available` on
the **pay** path, propagating as an unhandled `500` from an **unauthenticated** import. On
`980c679f1f76` it imported `200` and detonated later, on an unrelated request, with a stack trace
instead of a status code.

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

`1f94fcb` / `w7-token-only` is **not gate-eligible**. It is contained by no other branch and is the tip
of nothing. It exists solely as evidence that this divergence happened, and it must survive to the next
reader — so it is **documented here rather than deleted.**

**Why it matters beyond tidiness: it is the only artifact in this repository carrying a byte-identical
commit message to a good tip and completely different content.** That pair is the cleanest available
proof of R7 itself. Proven by hashing the message, not by comparing it by eye:

```
git log -1 --format=%B 1f94fcb | git hash-object --stdin  ->  9ac8b9f89878aff302b69891a9efacf33021da3b
git log -1 --format=%B 05f7d8d | git hash-object --stdin  ->  9ac8b9f89878aff302b69891a9efacf33021da3b
```

Measured divergence, `main` vs `1f94fcb`:

| | `main` | `1f94fcb` |
|---|---|---|
| `routers/operations.py` `_fingerprint` calls | 6 | 6 |
| `routers/operations.py` `model_dump` fingerprinting | 0 | **4** |
| `tests/test_stage2_idempotency_bytes.py` | present | **absent** |

The defect is the four `model_dump` sites: fingerprinting a parsed model compares *values*, so two
different raw bodies that deserialise identically collide and a divergent retry replays silently
instead of returning `409`. The `_fingerprint` call count is identical on both, which is why counting
calls would have missed it — `0afd2be` replaced the `model_dump` arguments, not the calls. The fork
also lacks §H entirely: `tests/test_stage2_ui_auth_split.py` and `tests/test_stage2_ui_forms.py` are
absent, 15 files and 3625 lines behind `main`.

## R8 — Ledger. SETTLED.
`verdict-6226ed1.txt` — **never created.** W5 is permanently disqualified and never ships.
`withdrawn-verdict-f89e0a4.txt` tracked at `6a9ed09`. `withdrawn-verdict-48684e8.txt` tracked at
`43097d3` — a V3 ACCEPT from a seat with no gate authority, permanently removed by operator directive,
and recording no image ID at all. **No rename is outstanding.** The duplicate `stash@{0}` that carried
it was **dropped**, after confirming via `git ls-files` that the rename was already tracked. The
remaining `stash@{0}` (`generic mandates and spec injection`) is **left alone** — it is not a seat's
work to commit on request, and it is not this ledger's to spend.

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

---

## Verification-skew finding — check the revision, not just the command

Several rulings were re-issued in chat after they had been implemented and committed. The cause was
mechanical, not a comprehension failure: each message cited a `HEAD` that was a real ancestor, and each
verification command ran correctly against that stale revision and returned a correct answer about the
wrong commit.

Two worked examples, reproduced verbatim:

```
git grep -n password_hash 69585e6 -- 'stage-2/tests/test_stage2_*.py'  ->  NONE
git grep -n password_hash fb0b881 -- 'stage-2/tests/test_stage2_*.py'  ->  7 matches
```

A grep that returns `NONE` is not evidence that work was not done unless the revision is the tip.
**Run `git rev-parse --short HEAD` in the same command block as any `git grep`, `git show`, or
`git ls-files` that is being used to decide whether something exists.**

## Counting convention

**Count collected node IDs, not `def` lines.** `@pytest.mark.parametrize` expands one definition into
several tests, so `^def test_` undercounts. `pytest --collect-only -q | Measure-Object -Line` is the
instrument. A `grep` for `def test_` is a way of finding *names*, never of counting *tests*.