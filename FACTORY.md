# Factory design

**Status: draft — fill in after the submitted dark-factory run.** This file has
to be enough on its own for another team to stand this factory up; the sections
below are placeholders for what the real run will produce.

## Seats

| Seat | Role | Harness | Model |
|---|---|---|---|
| `sipa-os-core` | Planner/Architect | Hermes | nousresearch/hermes-4-70b |
| `sipa-os-dark-v4` | Builder/Implementer | Hermes | nousresearch/hermes-4-70b |
| `sipa-os-v3` | Verifier/Reviewer | Hermes | nousresearch/hermes-4-70b |

Full mandate text for each seat lives in `mandates/`.

## Design choices

TODO after the run: why this seat split, why this handoff shape, what was tried
and dropped.

## What it cost

TODO after the run: wall-clock time per stage, model spend per stage (BAND
session cost, provider token spend if visible).

## How it catches a bad result

TODO after the run: a concrete case where the verifier rejected something the
builder produced, what it caught, and what changed as a result.

## Setup

1. Three BAND Desktop seats, mandates from `mandates/` applied to each.
2. Coordinator (`sipa-os-core`) receives the full stage spec and dispatches
   work items to `sipa-os-dark-v4`; `sipa-os-v3` independently reviews.
3. `python -m harness run --track pocketful --repo <this repo> --stage N` to
   check a stage locally before judging.
