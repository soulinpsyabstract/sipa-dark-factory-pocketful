# FACTORY - Pocketful Stage 2 (Sealed)

## Gate
- **Target:** pocketful-stage2:w11-45bb71b
- **Image ID:** sha256:4318dc6a8e7e4a9d69737aaac053453025adc640c14a25e6cb091bd1af76633a
- **Frozen at:** 485fe6fd6f1686de210a317e8323e36e19f291fc (git repo state)
- **Shipped stage-2 tree:** 2bacd3707fd84c3a84aad120f61002c63497e628 (unchanged across 10+ commits)
- **latest:** 829b1807… unmoved, banned
- **Duplicate (evidence, not target):** pocketful-stage2:w12-746634a / sha256:ccdd4d6c… (27/27 byte-identical to w11)
- **Excluded (counterexample):** sha256:ab15423c… (383 passed inside itself; produced unauthenticated takeover — ATTACKER 200, /me 200 handle=ada; OWNER 401). Retained.

## Artifact Provenance
- **Binding:** 27/27 application files blob-identical between 45bb71b:stage-2 and the image; 0 differing; 0 image-only. 
- **Build exclusion:** the five non-shipped files (.dockerignore, .gitignore, CONTRACT.md, Dockerfile, SPEC.md) are absent because the Dockerfile's COPY list never copies them (not .dockerignore). 
- **Py count:** 25 .py files shipped; total files in image 27.
- **Spec:** stage-2/SPEC.md frozen (content pin as recorded in RULINGS.md).

## Measurements
- **In-image (artifact certifies itself, measured inside):** /health → {"status":"ok"}; 404 passed on --network none; R3: attacker 401, owner 200.
- **Host-side (identical bytes):** erify_stage1.py 174/0; Stage-1 60 passed; D6 12/12 with 5 red-proof failures as recorded; six routes 200 text/html; escaping/offline checks green.
- **Takeover probe (contrast):** w11-45bb71b → ATTACKER 401/OWNER 200; ab15423c → ATTACKER 200/handle=ada/OWNER 401.

## Rulings (highlights)
- **R1–R5, R6, R7, R8:** settled; ledger authoritative (RULINGS.md).
- **Credential boundary:** discriminated by branch shape (elif vs if) — password_hash = existing_hash identical in both builds, so single-line grep cannot discriminate; remedy honour_password_hash/_credential_from_import absent in vulnerable build (reads as "already fixed").
- **Idempotency/raw bytes:** D6 uses raw-byte fingerprints; identical replay reuses stored response, divergent bodies return 409. lready_voided additive; W5 disqualified, V3 removed.
- **Image ID is not a content pin:** two different image IDs certify identical bytes (w11/w12) — rebuilding to "name the current tip" is not achievable; cite tree hash 2bacd3707fd84c3a84aad120f61002c63497e628. 
- **e8c0381 authorship:** **UNRESOLVED** and recorded as non-adjudicated (root-only commit, stage-2 tree unchanged; dispute doesn't change artifact). 
- **Tier D:** independent third-party verification unavailable; planner-verified under documented fallback.

## Controls / Instruments
- erify/docstructure.py: structural heading check with positive controls; detects merged headings (catches the defect at 7098886) and takes --file for historical commits.
- Tree hash, blob identity, and in-image measurements are the binding instruments.

## Status
- **Repo:** clean at 485fe6f; stage-2 tree unchanged since 2bacd370.
- **No rebuild, no retag, no commit of artifact files.** latest unmoved and banned.
- **Ledger sealed in repo.** FACTORY.md is the submission summary (this file).
