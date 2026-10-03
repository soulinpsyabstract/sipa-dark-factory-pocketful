# Factory design

**Status: Stage 1 complete and independently verified; Stage 2 in progress.** This file
describes the factory as it actually ran, from the exported room (`room.json`), not from
the plan. Everything below is taken from the room export or from the recorded OpenCode sessions.

## Seats

| Seat | Role | Harness | Model |
|---|---|---|---|
| `sipa-os-core` (BAND agent `SipaOsCore`) | Planner/Architect | OpenCode (BAND agent bridged to a local session) | `opencode/big-pickle` |
| `sipa-os-dark-v4` (BAND agent `SipaOsDarkv4`) | Builder/Implementer | OpenCode (BAND agent bridged to a local session) | `opencode/big-pickle` |
| `sipa-os-v3` (BAND agent `SipaOsv3`) | Verifier/Reviewer | OpenCode (BAND agent bridged to a local session) | `opencode/big-pickle` |

The mandate files in `mandates/` carry a declared header (`Harness: Hermes`, `Model:
nousresearch/hermes-4-70b`). That header is a declaration in the files, not the model that
ran. The exported room shows the seats running as OpenCode sessions on one participant's
laptop (a local OpenCode server on `127.0.0.1:4096`, fed by `run_agents.py` /
`worker.py`, which forward BAND room messages into the sessions). The model was read from
the three recorded OpenCode sessions through the local OpenCode server: in all three the
recorded model is provider `opencode`, model `big-pickle`. (Counted across every recorded message of the three sessions: 304, 444 and 453
occurrences, all `big-pickle`.)

Full mandate text for each seat lives in `mandates/`. The mandates are generic: no
endpoints, fields, error codes or test ids.

## Design choices

- One planner, one builder, one verifier. The planner reads the stage specification and
  hands the builder one scoped work item at a time; the builder implements; the verifier
  reviews independently of the builder and posts a verdict. The human operator is asked
  only for decisions.
- The verifier does not trust the builder's own numbers. In the Stage 1 verdict the
  builder's own harness count (174 passed) is explicitly not used; the figures the
  verdict stands on are the ones the verifier reproduced (pytest 60 passed, an independent
  functional harness 53/53, image-integrity and container checks).
- Guards are mutation-tested: the verifier reintroduced the original defect and checked
  that each new guard fails against it and passes again on a byte-clean restore.

## What it cost

- Stage 1 wall-clock in the room: from 2026-09-28 02:34 UTC to 2026-10-01 09:32 UTC
  (about 3 days 7 hours, not continuous compute), 2014 room messages.
- Seat activity in that room: 713 messages from the builder, 712 from the verifier, 569
  from the planner, 20 from the operator.
- Friction: 37 error messages, mostly `OpenCode timed out before completing the turn` (29)
  and `OpenCode is still processing the previous request in this room` (5).
- Model spend: not measured. The seats ran on the `opencode` provider's `big-pickle` model; no
  per-token spend or BAND session cost was recorded in the room or the repository.

## How it catches a bad result

On 2026-09-30 the verifier found that the builder's pinned commit `127c64f` hung: on the
host `signup` and `login` blocked, and inside the container every lock-taking path
blocked. It refused to issue a verdict on that commit, took thread dumps to find the
block point, and required the builder's next commit to fix it. In the same exchange it
took the operator's correction that the operator's own earlier verification run had used
a stale image and was invalid evidence, and re-verified on the exact pinned code instead.
The fix (`48684e8`) added three guards (no lock-taking helper that can
be called from inside a lock, no nested lock acquisition, a deadlock test that fails in
bounded time). A later commit (`3f79262`) moved the deadlock guard into a child process so
that a reintroduced deadlock fails one test instead of hanging the whole test session. The
verifier accepted both after re-running the mutation test.

## Setup

1. Three BAND agents, mandates from `mandates/` applied to each, each bridged to its own
   OpenCode session (`python run_agents.py`).
2. The planner (`sipa-os-core`) receives the full stage spec and dispatches work items to
   the builder (`sipa-os-dark-v4`); the verifier (`sipa-os-v3`) reviews independently.
3. `python -m harness run --track pocketful --repo <this repo> --stage N` to check a stage
   locally before judging; `python -m harness check --track pocketful <this repo>` for the
   structure, mandate and credential checks.
