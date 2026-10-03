# Pocketful Stage 2 — run instructions

FastAPI service, in-memory state, no database. Amounts are **integer minor
units**. See `CONTRACT.md` for the inherited Stage-1 request/response contract
(field names, auth scheme, fixture shape, operator definition, status codes)
and `SPEC.md` for the Stage-2 authorization/hold additions.

## Build

```bash
docker build -t pocketful-stage2 stage-2
```

If your buildx builder uses the `docker-container` driver, add `--load` so the
image lands in the local image store (`docker buildx build --load ...`);
otherwise the build succeeds but `docker image ls` stays empty.

## Run — default port 8080

```bash
docker run --rm -p 8080:8080 pocketful-stage2
curl -s localhost:8080/health          # -> {"status":"ok"}
```

## Run — PORT override

The app reads `$PORT` at start-up and defaults to **8080**. Both the default and
overrides are honoured by the same image:

```bash
docker run --rm -e PORT=9000 -p 9000:9000 pocketful-stage2
curl -s localhost:9000/health

docker run --rm -e PORT=5555 -p 5555:5555 pocketful-stage2
curl -s localhost:5555/health
```

Run it entirely detached:

```bash
docker run -d --name pocketful-stage2 -e PORT=8080 -p 8080:8080 pocketful-stage2
docker logs -f pocketful-stage2
```

## Offline operation (D1)

The image is self-contained: every dependency is installed at **build** time and
there is no database, no runtime package fetch and no outbound call anywhere in
`app/`. The service must therefore come up and serve traffic with no network at
all:

```bash
docker run -d --name s2off --network none pocketful-stage2
docker exec s2off python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8080/health',timeout=5).read().decode())"
docker ps --filter "name=s2off" --format "{{.Status}}"   # -> Up ... (healthy)
```

`--network none` gives the container no network interfaces beyond its own
loopback, so `-p` publishing does not apply — that is expected, not a failure.
Check the endpoint from inside the container with `docker exec`, as above.

The offline gate also covers the port override:

```bash
docker run -d --name s2port --network none -e PORT=5555 pocketful-stage2
docker exec s2port python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:5555/health',timeout=5).read().decode())"
```

Any browser assets are served from the image itself (no CDN, no external font or
script origin), so the UI works under the same restriction.

## Operator account (needed for `POST /settlements`)

`/settlements` is operator-only. An account becomes an operator if its email is
listed in `$POCKETFUL_OPERATOR_EMAILS` at signup (comma separated), or if the
reset fixture sets `"is_operator": true` on that user.

```bash
docker run --rm \
  -e POCKETFUL_OPERATOR_EMAILS="operator@example.com" \
  -p 8080:8080 pocketful-stage2
```

## Verify a running container

`verify_stage1.py` is the inherited Stage-1 evidence generator. It drives a live
base URL and prints a PASS/FAIL line per check, exiting non-zero on the first
failure. It is kept inside `stage-2/` as the Stage-1 regression gate.

```bash
docker run --rm -e POCKETFUL_OPERATOR_EMAILS="operator@example.com" \
  -p 8080:8080 pocketful-stage2 &

python stage-2/verify_stage1.py --base http://127.0.0.1:8080
```

The same script is baked into the image, so it can also run inside the
container's own network namespace:

```bash
docker exec pocketful-stage2 python verify_stage1.py --base http://127.0.0.1:8080
```

## Unit / integration tests

The Stage-1 suite is copied into `stage-2/tests/` and must stay green on top of
the Stage-2 code:

```bash
cd stage-2
python -m pip install -r requirements.txt
python -m pytest tests -q
```

It also runs inside the image, which is the offline form of the same gate:

```bash
docker exec s2off python -m pytest tests -q --no-header -p no:cacheprovider
```

## Run without Docker

```bash
cd stage-2
python -m pip install -r requirements.txt
PORT=8080 python -m app.main          # honours $PORT, default 8080
```

## Status codes used (also in `CONTRACT.md`)

| Code | Meaning here |
|---|---|
| 200 | every success, including `/health`, `signup`, `login`, `/payments` and the Stage-1 mutations |
| 201 | resource created where Stage-2 specifies it — notably authorization **capture** |
| 401 | missing/invalid bearer token, or wrong email+password |
| 403 | authenticated but not permitted — non-operator `/settlements`, wrong actor on a request transition |
| 404 | addressable thing does not exist — unknown handle, unknown `request_id`, unknown authorization |
| 409 | state conflict — illegal request transition, `Idempotency-Key` reused with a different body, insufficient *available* funds on a Stage-2 authorization |
| 422 | semantically invalid — amount not a positive integer, insufficient funds on a Stage-1 endpoint, handle fails `^[a-z0-9_]{1,20}$`, a debit that would go negative |
| 500 | an invariant (I1–I4) was violated; never returned in normal operation |

**Wrong-state request transitions return `409`, not `422`.** `POST
/requests/{id}/pay|decline|cancel` is only legal from `open`; from `paid`,
`declined` or `cancelled` the call returns `409 invalid_state`. A *negative
balance* is the thing that returns `422`, and it is `422 insufficient_funds`
specifically on the Stage-1 endpoints.

> Open question for Core: `SPEC.md` describes insufficient funds as `409`
> throughout, while the accepted Stage-1 contract and its tests pin `422
> insufficient_funds`. Both cannot hold at once. Stage-2 keeps `422` on the
> Stage-1 endpoints so the copied suite stays green, and uses `409` for the new
> authorization endpoints.

## Invariants

* **I1** `sum(all balances) == seeded_total` — money is conserved. Asserted
  after every mutation inside the same lock as the mutation; a violation is a
  500, never a silent success.
* **I2** no balance is ever negative.
* **I3** every balance belongs to a known user.
* **I4** every stored password is a bcrypt digest (`$2b$…`); no plaintext is
  ever written to the store.

All four are reported live in `GET /_test/export` under `invariants`.

Stage 2 adds the funds invariant layered on top: for every user,
`total >= held >= 0` and `available == total - held`, where `held` is the sum of
the amounts of that user's unexpired open authorizations.

## Concurrency

State is guarded by a single `threading.Lock()` and all handlers are sync
(`def`), so they run on Starlette's worker thread pool. `POST /_test/reset`,
`GET /_test/export` and `POST /_test/import` are each atomic.

Authorization expiry is **lazy**: there is no background timer. An open
authorization past its `expires_at` is transitioned to `expired` (releasing its
hold) on the next read or write that observes it.
