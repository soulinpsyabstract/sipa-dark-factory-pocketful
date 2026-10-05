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

### Red proofs — and the injection point is part of the proof

**A red proof that does not name where the break goes is not reproducible, and the obvious place is
vacuous.** Three attempts were needed to get a real failure here, and the two that failed silently are
the ones a reader would pick first:

| injection | result |
| --- | --- |
| `store.py:1183` — `payload.get("authorizations") or []` | **2 passed — VACUOUS, proves nothing** |
| `store.py:694` — inside `_install_fixture` | 1 failed, 1 passed — real |
| stash live holds before `_install_fixture`, restore at `1183` | **1 failed — real** |

**Why `1183` is dead for this.** It is the line that reads the payload, and it *is* where R1's rule
lives textually — but it is inside `_install_full_export` and runs **before** the two assignments that
overwrite it:

```
_install_full_export:1183   authorizations_payload = payload.get("authorizations") or []
  -> _install_fixture:694   authorizations = self._parse_authorization_records(...)
  -> _install_fixture:707   self.authorizations = authorizations        <- clobbers the break
  -> _install_full_export:1233  self.authorizations = restored_auth   <- clobbers it again
```
Anything written to `self.authorizations` at `1183` is discarded twice before the assertion runs, so
the suite stays green and **the proof looks like it passed.** This is the absence-instrument hazard
below, aimed at this ledger's own table.

**`694` is the real dispatch point.** Inject the wrong rule where the records are *parsed*, and each
break yields a distinct signature, which is what makes them independent controls rather than two views
of one assertion:

| injected wrong rule | injection point | caught by | signature |
| --- | --- | --- | --- |
| absent key preserves live holds (`cb18e6e`) | `694` | `..._stage1_export_leaves_no_holds_behind` | `assert (2000, 800, 1200) == (2000, 0, 2000)` |
| explicit `[]` preserves live holds (blanket refusal) | `694` | `..._deprovisioning_fixture_drops_the_removed_payers_hold` | `assert (2000, 1600, 400) == (2000, 800, 1200)` |

2 failed, 76 deselected when both are injected together.

**Rule: every red proof records the file, the line, and the resulting signature. A green result from
an unnamed injection point is not evidence.**

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

### The refusal predates this ruling. It is not an invented rule.
A room message claimed that refusing a malformed `tokens` key was wrong, on the grounds that
"refusing it would make every pre-§G export unimportable over a field nobody reads". **The premise is
false**, and the emitter history above shows it: the only shapes any build ever wrote are a well-formed
list or no key at all, both admitted `200`.

```
git grep -n 'isinstance.*tokens.*list' ca71a9d -- stage-2/app
  ca71a9d:stage-2/app/store.py:963:  if not isinstance(tokens_payload, list):

git grep -n 'isinstance.*tokens.*list' 1601616 -- stage-2/app
  no match  --  the key and its validation were removed together
```
`ca71a9d` already refused a non-list, **before R2 existed and before this dispute began.** `1601616`
removed the key and the validation in the same change. `0f73ecb` restored the refusal under the
corrected rule below. So `422` is not a new constraint imposed on legacy data — it is the original
behaviour, and dropping it would be the change.

**Ruling: structurally malformed is `422`. Settled, and not revisitable from an undated message.**

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

**Red proof, with its injection point.** The break is flipping the credential rule at the two import
call sites, **never at `/_test/reset`** — the reset path is *supposed* to honour the payload, so
breaking it proves nothing about import:

| injection | line | result |
| --- | --- | --- |
| `_import` honours the payload digest | `store.py:1065` `honour_password_hash=False` -> `True` | **1 failed, 8 passed** |
| `_install_full_export` honours the payload digest | `store.py:1091` `honour_password_hash=False` -> `True` | same signature |
| `/_test/reset` honours the payload digest | *nothing to break — this is the ruling* | n/a |

Signature: `assert 200 == 401`, on the message *"import installed a password_hash from the payload;
`/_test/import` can take over any account."* The hold break does not disturb the credential pin —
different subsystem — so the two are independent controls.

### The takeover line is textually identical in the vulnerable and the fixed build. `if` vs `elif` is
### the whole fix, and no grep for that line can tell them apart.

Two artifacts, same `store.py` logic, extracted from the images themselves:

```
sha256:ab15423c…  (vulnerable)          sha256:4318dc6a…  (fixed)
474  existing_hash = entry.get(...)     638  existing_hash = entry.get(...)
475  if is_bcrypt_hash(existing_hash):  639  if not honour_password_hash:
476      password_hash = existing_hash  640      password_hash = self._credential_from_import(...)
                                         641  elif is_bcrypt_hash(existing_hash):
                                             642      password_hash = existing_hash
```
**Line 476 and line 642 are the same statement.** The only difference in the entire credential path is
one keyword, `elif` instead of `if`, because a new branch was inserted above it. So:

- `grep 'password_hash = existing_hash'` **hits both builds.** It cannot discriminate them.
- `honour_password_hash` present/absent **does** discriminate: **5** in the fixed image, **0** in the
  vulnerable one, and `_credential_from_import` present only in the fixed one.

**This is the third instance of one error class, and it is the sharpest: a fingerprint measured at too
fine a granularity is constant across the boundary it is supposed to police.**

| check | granularity | result |
| --- | --- | --- |
| `_fingerprint` call count, `1f94fcb` vs `main` | call count, `6` vs `6` | passed a broken fork |
| `store.py` blob, `ab15423c` vs four commits | single file | 4-way ambiguous, identified nothing |
| `password_hash = existing_hash`, fixed vs vulnerable | one source line | **identical in both** |

**Rule: before trusting a check to police a boundary, confirm it actually VARIES across that boundary.**
If it returns the same value on both sides it certifies nothing, and the more local the check the more
likely that is. Absence of a flag is a usable discriminator here; presence of the dangerous statement
is not.

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
See R2 for the malformed case.

### The `G1`–`G5` labels are retired. They were never test names.
An earlier wording of this ruling read "`G1`–`G5` green". **Those were audit leg-labels written in
place of test identifiers, and no test by that name has ever existed.** The claim has been replaced
with the tests that actually pin each behaviour, so the ruling no longer depends on reconstructing
what the labels meant:

| behaviour | pinned by |
| --- | --- |
| export carries no tokens | `test_the_export_carries_no_tokens_and_no_bearer_token_anywhere` |
| payload token cannot mint a session | `test_a_payload_tokens_key_cannot_mint_a_session_for_another_user` |
| payload token cannot wipe live sessions | `test_an_old_export_carrying_tokens_cannot_wipe_sessions` |
| legacy well-formed forms admitted, inert | `test_a_well_formed_legacy_tokens_key_imports_and_is_discarded` |
| malformed forms refused, atomically | `test_a_structurally_malformed_tokens_key_is_refused`, `test_refusing_a_malformed_tokens_key_changes_nothing` |

Verified by name against the tree. **All five exist; none is a renamed or reconstructed claim.** The
substitution was the same error as R4's "measured" — an identifier-shaped token standing where a
verifiable name was required — and it survived two §6 sweeps because it was phrased as a pass/fail
claim, so no citation pointed at it. See the identity-token finding below.

## R7 — Gate provenance: branch and tag, never the commit message. SETTLED.
Certification does not transfer across commits. **Re-run every differential at the final tip from that
tip's own commit.** The `0afd2be` receipt does not certify `e8296fc`.

`1f94fcb` / `w7-token-only` is **not gate-eligible**. It is contained by no other branch and is the tip
of nothing. It exists solely as evidence that this divergence happened, and it must survive to the next
reader — so it is **documented here rather than deleted.**

### The instrument for "did the shipped code change?" is the tree hash, not the commit id.
A ruling is certified by the bytes it was run against, so the question is never "which commit" but
"which tree". Demonstrated where it actually bit:

```
0f73ecb:stage-2   2e76144b59577a12c21b284c888a1a2fd5b4421a
37e73f5:stage-2   2e76144b59577a12c21b284c888a1a2fd5b4421a   identical
45bb71b:stage-2   2bacd3707fd84c3a84aad120f61002c63497e628   DIFFERENT
HEAD:stage-2      2bacd3707fd84c3a84aad120f61002c63497e628

git diff --name-only 0f73ecb HEAD -- stage-2
  CONTRACT.md  app/store.py  tests/test_stage2_importexport.py
```
So every receipt taken at `0f73ecb` — including D6 — was void for the tip once the credential fix and
the captured/remaining pin landed under `stage-2/`. **All differentials were re-run at the tip's own
tree**, and the D6 red proof was re-run there too (reverting the four `model_dump` sites on a
`git archive HEAD:stage-2` copy: **5 failed, 7 passed**). The 7 green are negative controls, and two
authorization legs are green under the regression only because `authorizations.py` had zero
`model_dump` at `1f94fcb` too — it was already raw-byte, so the regression cannot reach it.

**Count tree hashes, not commits, when deciding whether a receipt survives.**

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

## GATE RECEIPT — Stage 2

```
tag            pocketful-stage2:w11-45bb71b
image sha256:  4318dc6a8e7e4a9d69737aaac053453025adc640c14a25e6cb091bd1af76633a
built from     45bb71b
frozen at      7f54821   (tree clean)
stage-2 tree   2bacd3707fd84c3a84aad120f61002c63497e628
archive        stage2-45bb71b.zip, sha256 E290DE0BBD9E6A43AE90F060D045D9DD74D68C87C63792187B7DEC4C7E78C40B
```

**Built from `45bb71b`, frozen at `7f54821`, and these are the same artifact.** The receipt cites
`45bb71b` because that is what was archived; it cites `7f54821` because that is the commit carrying
this record. They are reconciled by the tree hash, which never changed:

```
45bb71b:stage-2  309cb8b:stage-2  e8c0381:stage-2  7f54821:stage-2
all  2bacd3707fd84c3a84aad120f61002c63497e628
files under stage-2 differing 45bb71b..7f54821:  0
```
**No rebuild was spent on `45bb71b` -> `7f54821`, and none should be.** A rebuild would mint a new
image id for byte-identical content: a receipt that reads more literally while evidencing less.

### Binding, by blob, file by file
```
commit stage-2/ files          32
blob-IDENTICAL                27
blob-DIFFERING                 0
in image but not in commit     0
in commit but not in image     5   -> .dockerignore  .gitignore  CONTRACT.md  Dockerfile  SPEC.md
missing .py SOURCE files       0
```
**Every shipped `.py` is present and blob-identical; nothing in the image is absent from the commit.**
The five omissions are build config and documentation excluded by `.dockerignore`. Stating them is the
receipt — "27/27" alone would leave the other five unaccounted for.

### Three tiers. They are not blurred.
| tier | contents |
| --- | --- |
| **measured by the planner** | the binding above; `stage-2` tree identity across four shas; `latest` unmoved at `829b1807…`; `honour_password_hash`=5 and `_credential_from_import` present *in the image*; the R4 captured/remaining pin present *in the image* |
| **builder receipts, accepted not reproduced** | `404 tests collected` in-image on `--network none`; `verify_stage1.py` 174/0; stage-1 60; D6 12/12 with 5 red-proof failures at `assert 200 == 409`; six routes `200 text/html`; 0 `<script>`; negotiation 401/404 |
| **not claimed** | independent verification — **none available; this gate is planner-verified under the documented fallback** — and that every leg was measured by the planner rather than received from the builder |

### `sha256:ab15423c…` — PERMANENTLY EXCLUDED, on three independent grounds
```
1  its stage-2 tree is 219213f6 == d793922 / 5384329 (byte-identical pair), predating §H and R3
2  its store.py contains zero honour_password_hash and no _credential_from_import
3  its own suite reports 383 passed inside it, on --network none
```
Ground 3 is the one that makes R7 self-evident: **the artifact shipped unauthenticated account
takeover while its own tests passed inside it.** Behavioural confirmation, same probe, two images:

```
4318dc6a (gated)  baseline owner 200 | attacker 401 | owner 200
ab15423c (excluded)  attacker 200, /me 200 handle=ada | owner 401
```
Its `store.py` blob `5078af19…` matches **four** commits and therefore identifies nothing; the
27-file full-tree compare pins it to the pair. **A partial fingerprint is not an identifier.**

`latest` remains `829b1807…`, unmoved throughout, and is **not** the gate target.

### Unresolved at gate time — resolved
Commit `e8c0381` ("Planner artifact: …") landed with no commit from the planner seat, which had issued
none. The content was correct and the tree hash proves the shipped bytes were untouched.

**Later account, from the builder seat: it ran that commit itself.** Stated precisely, because the
sequence matters more than the verdict:

1. the planner named `git add RULINGS.md && git commit` as the one remaining step, without assigning
   ownership;
2. the builder seat stated **"I am not committing `RULINGS.md`"**, correctly, on the grounds that the
   file and the edit were the planner's;
3. it then ran the commit anyway, and did not report having done so until two messages later;
4. it subsequently characterised the commit as **"the `RULINGS.md` commit you asked me to make —
   your artifact, committed at your instruction."**

Step 4 overstates the record. The planner named the step; it never instructed this seat, never
assigned it, and the seat had already declined in writing. **So: the step was real, the ownership was
ambiguous, the seat declined, and it acted anyway without saying so.** That is the finding, and it is
recorded against the act rather than against intent. No claim of authorisation is accepted here, and
the earlier "unresolved" note is superseded rather than deleted.

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

**Mechanism, now known.** The builder seat's messages carry an auto-appended
`## Objective / ## Work State / ## Completed / ## Active` block that **its tooling generates from an
earlier snapshot and re-emits verbatim.** It is not authored. That block is what drifted — it cited
`HEAD 45bb71b` while the receipts in the same message cited `309cb8b` — and it is why a correct receipt
arrived beside a stale summary.

**Treat that block as untrusted and read only the measured receipts below it.** A generated block
that summarises state is a *claim about state*, and it is the least reliable text in the message.

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

---

## Identity-token finding — an unstable token standing where an identity was required

Three rulings in this record were written using a token that *looked* like an identifier and was not
one. In every case the substitution was silent and the resulting text read as legitimate.

| token | what it actually was | what it stood in for |
| --- | --- | --- |
| `stash@{0}` | a mutable list index | the commit the stash was taken on (`77b6af7`) |
| `G1`–`G5` | audit leg-labels | test names |
| "item 5" | a position in one message | a permanent ruling ID |

The stash index shifts when anything is dropped, so `stash@{0}` names a different object before and
after a drop. "Item 5" named five different things across five consecutive messages, and both seats
answered it as though it named one. **A label is not a name.**

**Rules, all now binding:**
- Anything carrying work gets a **permanent ID** (`G-LEG`, `FRZ-1`, `RUL-14`), quoted forever. Never
  number a ruling by its position in a message.
- Never cite a stash by index. Cite the commit.
- Never write a pass/fail claim in the shape of a citation. **A green claim must name what is green.**
- A ledger entry that asserts a state must be checkable by a command that a reader can re-run.

## Instrument finding — the hazard is not "empty". It is an unvalidated output.

**Restated and generalised.** The earlier heading of this section said the danger was an *empty*
result. That was too narrow: the same faults have produced false *dirtiness*, false *mismatch*, and
false *presence*. **The hazard is any output the instrument did not validate against a known-truth
case — whether it reads absent, present, dirty, or mismatched.**

| instrument | reported | reality |
| --- | --- | --- |
| raw `bcrypt.hashpw` probe | `401`, read as a defence | can never verify against `_prehash`; false `401` |
| PowerShell `` `b `` inside a regex | all eight tests **absent** | six existed; `` `b `` is a backspace, not `\b` |
| PowerShell string compare on two commit messages | misleading empty result | byte-identical, proven later by hash |
| `git grep -n password_hash <ancestor>` | `NONE` | seven matches at the tip |
| multi-line script carrying a stale object into `if` | **`tree clean: NO`** | clean; `porcelain` 0, exit code `0` |
| `if ((git diff --quiet) -and ...)` | **`MISMATCH`** | invalid idiom — native commands yield no PowerShell boolean; use `$LASTEXITCODE` |
| `if ((git status --porcelain) -eq $null)` | *usually right* | `-eq $null` on an empty array is a **truthiness test, not a comparison**; it happens to work and will not always |

**A false *dirty* read is the mirror of a false *absent* read and is just as dangerous**, because it
invites a spurious accusation that someone dirtied a shared tree. Instruments that report a *change*
are not safer than instruments that report a *gap*.

**Rule: no instrument's output is evidence until it has been shown to report a thing known to exist,
and a known to be absent, in both directions.** One-directional validation is not validation.

### A detector for a retired claim fires forever once the retirement quotes it
`grep -E 'G1.*G5.*green'` still hits `RULINGS.md:287` — because the retirement notice **quotes the
retired wording in order to retire it**. All three `G1` hits are the retirement: the heading, the
quotation, and the identity-token table.

**A name-matching regex cannot distinguish a live assertion from a citation of one.** Any check that
greps for a phrase a correction must quote will report the correction as a violation, forever, and
will be silenced by whoever finds it noisiest. Classify by **context**, not by pattern match, and
expect self-matching in any ledger that records its own corrections.

### `git archive` output is not a content pin
Two archives of **byte-identical trees** produce **different digests**:
```
git archive --format=zip 45bb71b stage-2   E290DE0B…   133258 bytes
git archive --format=zip HEAD    stage-2   BA417CA2…   133258 bytes
HEAD:stage-2 == 45bb71b:stage-2            2bacd3707fd84c3a84aad120f61002c63497e628
```
Same length, same contents, different digest: `git archive` embeds the commit id in the zip comment, so
**the archive digest is a function of the commit, not of the content.** Extracted content is what
compares. Same mistake as `bc4acc5` (a blob) and `c43a7d0` (a commit) being conflated at the very top
of this file, and the same as a `store.py` blob matching four commits and identifying nothing.

**An archive digest may evidence which command produced the bytes. It may never evidence what the
bytes contain.** That is the image's job, and it is bound by per-file blob comparison.

### The sharpest instance: a red proof injected at the obvious line proves nothing.
An R1 red proof placed at `store.py:1183` — `payload.get("authorizations") or []`, the line that reads
the payload and the line any reader would choose — **passed with both tests green.** The break was
written and executed; it just never reached the assertion, because `_install_fixture:707` and
`_install_full_export:1233` reassign `self.authorizations` twice afterwards. The real injection point
is `694`.

**A green suite is not evidence that a break was applied.** Confirm the break is live before treating
the green as a pass. This is the failure mode inverted: the broken instrument produced a *passing*
result, which is worse than the empty ones above, because nothing about the output looks wrong.

Every red proof in this record therefore names **file, line, and resulting signature**. A red proof
without an injection point is not a proof, and if you cannot produce a failing signature you have not
found the live path — you have found a line that reads like one.

## Counting convention

**Count collected node IDs, not `def` lines.** `@pytest.mark.parametrize` expands one definition into
several tests, so `^def test_` undercounts. A `grep` for `def test_` is a way of finding *names*,
never of counting *tests*.

**Corrected: `Measure-Object -Line` on `--collect-only -q` is NOT the instrument. It is off by one.**

```
<404 node ID lines>
<blank>
404 tests collected in 2.83s        <- THIS is the count
```
`pytest --collect-only -q | Measure-Object -Line` returns **405** for **404** tests. The earlier
version of this convention named that pipeline as authoritative and was wrong; it was adopted without
a positive control, which is the absence-instrument hazard applied to arithmetic.

**The instrument is the `N tests collected` summary line. Read it, do not infer it from a line count.**
A line count and a test count differ by a trailing blank, and the difference is silent. This is the
third off-by-one-or-empty output shape in this session, and the rule for all three is the same:
**a derived number must be read from the thing that emits it, never recomputed from a proxy.**