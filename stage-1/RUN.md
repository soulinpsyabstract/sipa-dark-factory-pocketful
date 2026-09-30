# Pocketful Stage 1 — run instructions

FastAPI service, in-memory state, no database. Amounts are **integer minor
units**. See `CONTRACT.md` for the full request/response contract (field names,
auth scheme, fixture shape, operator definition, status codes).

## Build

```bash
docker build -t pocketful-s1 stage-1
```

## Run — default port 8080

```bash
docker run --rm -p 8080:8080 pocketful-s1
curl -s localhost:8080/health          # -> {"status":"ok"}
```

## Run — PORT override

The app reads `$PORT` at start-up and defaults to **8080**. Both the default and
overrides are honoured by the same image:

```bash
docker run --rm -e PORT=9000 -p 9000:9000 pocketful-s1
curl -s localhost:9000/health

docker run --rm -e PORT=5555 -p 5555:5555 pocketful-s1
curl -s localhost:5555/health
```

Run it entirely detached:

```bash
docker run -d --name pocketful-s1 -e PORT=8080 -p 8080:8080 pocketful-s1
docker logs -f pocketful-s1
```

## Operator account (needed for `POST /settlements`)

`/settlements` is operator-only. An account becomes an operator if its email is
listed in `$POCKETFUL_OPERATOR_EMAILS` at signup (comma separated), or if the
reset fixture sets `"is_operator": true` on that user.

```bash
docker run --rm \
  -e POCKETFUL_OPERATOR_EMAILS="operator@example.com" \
  -p 8080:8080 pocketful-s1
```

## Verify a running container

`verify_stage1.py` is the evidence generator. It drives a live base URL and
prints a PASS/FAIL line per check, exiting non-zero on the first failure.

```bash
docker run --rm -e POCKETFUL_OPERATOR_EMAILS="operator@example.com" \
  -p 8080:8080 pocketful-s1 &

python stage-1/verify_stage1.py --base http://127.0.0.1:8080
```

The same script is baked into the image, so it can also run inside the
container's own network namespace:

```bash
docker exec pocketful-s1 python verify_stage1.py --base http://127.0.0.1:8080
```

## Unit / integration tests

```bash
cd stage-1
python -m pip install -r requirements.txt
python -m pytest tests -v
```

## Run without Docker

```bash
cd stage-1
python -m pip install -r requirements.txt
PORT=8080 python -m app.main          # honours $PORT, default 8080
```

## Status codes used (also in `CONTRACT.md`)

| Code | Meaning here |
|---|---|
| 200 | every success, including `/health`, `signup`, `login` and all mutations |
| 401 | missing/invalid bearer token, or wrong email+password |
| 403 | authenticated but not permitted — non-operator `/settlements`, wrong actor on a request transition |
| 404 | addressable thing does not exist — unknown handle, unknown `request_id` |
| 409 | state conflict — illegal request transition, `Idempotency-Key` reused with a different body |
| 422 | semantically invalid — amount not a positive integer, insufficient funds, handle fails `^[a-z0-9_]{1,20}$`, a debit that would go negative |
| 500 | an invariant (I1–I4) was violated; never returned in normal operation |

**Wrong-state request transitions return `409`, not `422`.** `POST
/requests/{id}/pay|decline|cancel` is only legal from `open`; from `paid`,
`declined` or `cancelled` the call returns `409 invalid_state`. A *negative
balance* is the thing that returns `422`, and it is `422 insufficient_funds`
specifically.

## Invariants

* **I1** `sum(all balances) == seeded_total` — money is conserved. Asserted
  after every mutation inside the same lock as the mutation; a violation is a
  500, never a silent success.
* **I2** no balance is ever negative.
* **I3** every balance belongs to a known user.
* **I4** every stored password is a bcrypt digest (`$2b$…`); no plaintext is
  ever written to the store.

All four are reported live in `GET /_test/export` under `invariants`.

## Concurrency

State is guarded by a single `threading.Lock()` and all handlers are sync
(`def`), so they run on Starlette's worker thread pool. `POST /_test/reset`,
`GET /_test/export` and `POST /_test/import` are each atomic.
