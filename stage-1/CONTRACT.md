# Pocketful Stage 1 — JSON contract

The spec (§3) names 16 endpoints but fixes no field names, no auth scheme, no
`/_test/reset` fixture shape and no definition of "operator". This file is the
normative answer. `@sipa-os-v3` should check against this.

Conventions used everywhere:

* **Money** is an **integer** in minor units. `10.0`, `"10"` and `true` are all
  rejected with `422` — only a JSON integer is accepted.
* **Handles** match `^[a-z0-9_]{1,20}$`.
* Auth is `Authorization: Bearer <token>`. `X-Auth-Token: <token>` is also
  accepted. `Idempotency-Key` is read case-insensitively.
* Success is always `200`. Errors carry
  `{"detail", "code", "error": {"code", "message", ...}}`.

---

## 1. `GET /health`

`200` → `{"status": "ok"}`

## 2. `POST /_test/reset`

Replaces **all** state (users, balances, requests, activity, idempotency,
tokens, counters) with the fixture. Atomic.

```json
{
  "seeded_total": 100000,
  "users": [
    {
      "handle": "alice",
      "email": "alice@example.com",
      "display_name": "Alice",
      "password": "hunter2hunter2",
      "balance": 60000,
      "is_operator": false
    }
  ],
  "balances": { "bob": 40000 }
}
```

* `handle` required per user, validated against the handle regex → `422`.
* `email` optional, defaults to `<handle>@pocketful.test`.
* `password` optional, defaults to `pocketful-fixture-pw`. Stored as a bcrypt
  hash like any other password, so a seeded user can `POST /auth/login`.
* `balance` optional int ≥ 0, defaults to `0`. The separate `balances` map
  (handle → int) is applied afterwards and wins on conflict.
* `is_operator` optional bool.
* `seeded_total` (alias `total`) **optional**. If omitted it is set to the sum
  of the seeded balances. If supplied it **must** equal that sum, else `422
  fixture_total_mismatch` — this is deliberate, it prevents installing a state
  in which invariant I1 is already false.

`200` → `{"status","action","seeded_total","balance_sum","handles","balances","users","invariants","reset_at"}`

## 3. `GET /_test/export`

`200` → full snapshot:

```json
{
  "status": "ok",
  "seeded_total": 100000,
  "balance_sum": 100000,
  "users": [ { "handle", "email", "display_name", "password_hash", "is_operator", "created_at" } ],
  "balances": { "alice": 60000, "bob": 40000 },
  "requests": [ { "id", "from", "to", "amount", "status", "note", "created_at", "updated_at" } ],
  "activity": [ { "id", "handle", "type", "actor", "counterparty", "direction", "amount", "related_id", "memo", "created_at" } ],
  "idempotency": [ { "user", "key", "fingerprint", "status_code", "body", "created_at" } ],
  "invariants": {
    "sum_of_balances": 100000,
    "seeded_total": 100000,
    "balance_sum_equals_seeded_total": true,
    "no_negative_balances": true,
    "negative_balances": [],
    "all_balances_owned_by_users": true,
    "orphan_balances": [],
    "no_plaintext_passwords": true,
    "plaintext_password_users": []
  },
  "exported_at": "..."
}
```

`password_hash` is a bcrypt digest, exported so a seeded user survives an
export → import → login round trip. Never plaintext.

## 4. `POST /_test/import`

Accepts **either** shape:

* a full export (§3) — restored verbatim, including requests, activity and
  idempotency records, so export → import → export is a true round trip;
* a bare fixture (`{"users": […], "seeded_total": n}`) — same semantics as
  `/_test/reset`.

Invalid or invariant-breaking payloads are `422`, and the previous state is
left intact (import is all-or-nothing).

## 5. `POST /auth/signup`

```json
{ "email": "Alice.Smith@example.com", "password": "…", "display_name": "Alice" }
```

* `handle` is **derived from the email**: local part lower-cased, every
  character outside `[a-z0-9_]` dropped, then validated against
  `^[a-z0-9_]{1,20}$` **without truncation**.
  `Alice.Smith@…` → `alicesmith`. Truncation is deliberately not performed: it
  would map distinct emails onto a single handle and defeat the regex.
  An email whose local part sanitises to nothing (`"!!!@x.com"`) fails the
  regex → `422 invalid_handle`; so does a sanitised local part longer than
  20 characters (`"a"*40 + "@example.com"`) → `422 invalid_handle`.
* An **optional explicit `handle`** is also accepted: it must match the regex
  (`422 invalid_handle` otherwise) and, when valid, is used as the handle.
* Duplicate email → `409 email_taken`; duplicate handle → `409 handle_taken`.
* New user starts at balance `0` (does not change the sum, so I1 holds).

`200` → `{"status","handle","email","display_name","is_operator","balance","created_at","user":{…}}`
(the user fields are present both at the top level and nested under `user`).
**Signup returns no token** — `POST /auth/login` is the token path.

## 6. `POST /auth/login`

```json
{ "email": "alice@example.com", "password": "…" }
```

`200` → `{"status","access_token","token","token_type":"bearer","handle", …user fields…}`
(`access_token` and `token` are the same value.)

Wrong password **and** unknown email both return `401 invalid_credentials`
with an identical body, so the endpoint cannot enumerate accounts.

## 7. `GET /me`

`200` → `{"status","handle", …user fields…, "user":{…}}`. `401` without a valid
bearer token.

## 8. `POST /payments`  *(idempotent)*

```json
{ "to": "bob", "amount": 2500, "note": "lunch" }
```

* `amount` must be a **positive JSON integer**.
* `to` must be a valid, existing, non-self handle. Unknown → `404
  unknown_user`; self → `422 self_transfer_not_allowed`.
* Insufficient funds → `422 insufficient_funds`, **no balance changes**.

`200` → `{"status","payment_id","id","from","to","amount","note","created_at","balances":{from,to},"balance_sum","seeded_total"}`

## 9. `POST /requests`  *(idempotent)*

Body as `/payments`. Creates an `open` request from the caller to `to`. **No
balance moves** — funds move on `pay`.

`200` → `{"status","id","from","to","amount","status":"open","note","created_at","updated_at"}`

## 10. `POST /requests/{id}/pay`  *(idempotent)*

Only the **recipient** (`to`) may pay, and only from `open`. Moves funds
recipient → creator, sets `status = "paid"`.

* wrong actor → `403 not_request_recipient`
* not `open` → `409 invalid_state`
* unknown id → `404 unknown_request`

`200` → the request record plus `balances`, `balance_sum`, `seeded_total`.

## 11. `POST /requests/{id}/decline`

Only the recipient, only from `open`. Sets `status = "declined"`. No money
moves. Same 403/409/404 rules as `pay`.

## 12. `POST /requests/{id}/cancel`

Only the **creator**, only from `open`. Sets `status = "cancelled"`. No money
moves. Wrong actor → `403 not_request_creator`; not open → `409 invalid_state`.

## 13. `GET /requests`

Query: `direction` ∈ `incoming` | `outgoing` | `all` (default `all`);
`status` ∈ `open` | `paid` | `declined` | `cancelled` | comma-separated list
| `all` (default `all`). Anything else → `422`.

* `incoming` — I am the recipient. `outgoing` — I created it.

`200` → `{"status","requests":[…],"items":[…same…],"count","direction","status_filter"}`

## 14. `POST /splits`  *(idempotent)*

```json
{ "amount": 100, "participants": ["bob", "carol", "dan"] }
```

Equal division with the **remainder going to the first participants in the
list**: `base, remainder = divmod(amount, len(participants))`, and participant
`i` gets `base + 1` when `i < remainder`.

* `100` over 3 → `[34, 33, 33]`
* `10` over 4 → `[3, 3, 2, 2]`
* parts always sum back to `amount`.

The caller is the **payer**: every participant other than the caller is
debited their share and the caller is credited the total. If the caller is
also in the list, their own share is not moved. `total_charged` is the sum of
the participants actually debited.

* duplicate participant → `422 duplicate_participants`
* any debit that would go negative → `422 insufficient_funds`, no mutation
* payer cannot cover the total charged → `422 insufficient_funds`

`200` → `{"status","split_id","id","amount","payer","participants","parts":[{handle,amount}],"shares":[ints],"total_charged","note","created_at","balance_sum","seeded_total"}`

## 15. `GET /activity`

The caller's own feed, newest first. Query: `limit` (1–1000, default 100),
`type` (optional exact activity type).

Entry: `{"id","handle","type","actor","counterparty","direction","amount","related_id","memo","created_at"}`
plus `"parts"` on the payer's split entry.

`type` values emitted: `payment`, `request_created`, `request_paid`,
`request_declined`, `request_cancelled`, `split`, `settlement`.

`200` → `{"status","activity":[…],"items":[…same…],"count","handle","balance"}`

## 16. `POST /settlements`  *(idempotent)*

**Operator only.** A user is an operator if `$POCKETFUL_OPERATOR_EMAILS`
contains their email at signup, or if the reset fixture set
`"is_operator": true`. Any authenticated non-operator → `403 operator_required`.

```json
{ "entries": [ { "from": "bob", "to": "carol", "amount": 100 } ] }
```

All-or-nothing: every transfer is validated against a projected balance
snapshot before anything commits, so a batch that fails part-way leaves every
balance untouched. Self-transfer → `422`.

`200` → `{"status","settlement_id","id","operator","transfers":[…],"entries":[…same…],"count","total_amount","created_at","balance_sum","seeded_total"}`

---

## Idempotency (spec §2)

Applies to the five mutating financial endpoints: `POST /payments`,
`POST /requests`, `POST /requests/{id}/pay`, `POST /splits`,
`POST /settlements`.

* Key read from the `Idempotency-Key` header (case-insensitive; also accepts
  `X-Idempotency-Key`).
* **Scoped per user** — key `k` used by `alice` and by `bob` are two
  independent records. A cross-user replay is a fresh operation.
* Same user + same key + **identical body** → the **original** status code and
  response body are returned and nothing is re-processed (balances unchanged).
* Same user + same key + **different body** → `409 idempotency_key_reuse`.
* A different key for the same logical operation processes normally.
* Only `2xx` responses are remembered, so a rejected attempt never poisons a
  retry under the same key.
* No `Idempotency-Key` header → the request just processes normally.
