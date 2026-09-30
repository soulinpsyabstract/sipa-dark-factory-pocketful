# Pocketful API - Stage 1 Specification

## 1. Tech Stack Requirements
* **Language:** Python 3.11+
* **Framework:** FastAPI or Flask (FastAPI recommended)
* **Storage:** In-memory dictionary/data structures ONLY. Must be thread-safe using threading.Lock(). No external databases.
* **Security:** Password hashing must use bcrypt, scrypt, or Argon2.
* **Containerization:** The `stage-1/` directory must contain a valid `Dockerfile` and a `RUN.md` file explaining how to build and run the container.
* **Port:** The application must respect the `PORT` environment variable (default to 8080).

## 2. Core Business Logic
* **Currency:** All financial amounts are in minor units (integers).
* **Balances:** Balance invariants must be maintained. The sum of all balances must equal the seeded total. Negative balances are strictly prohibited and must return a 422 error.
* **Idempotency:** Required for all state-mutating financial endpoints (`/payments`, `/requests`, `/requests/{id}/pay`, `/splits`, `/settlements`). Keys are scoped to the user. Replaying an identical request with the same idempotency key must return the original response without double-processing.
* **Validation:** User handles must be derived from emails and validate against the regex: `^[a-z0-9_]{1,20}$`.
* **Splits:** Bills must be distributed equally. If division is uneven, the remainder units go to the first participants in the list.

## 3. Required Endpoints
**System & Testing**
* `GET /health` -> Returns 200 `{"status": "ok"}`
* `POST /_test/reset` -> Reset state with provided fixture
* `GET /_test/export` -> Export current state
* `POST /_test/import` -> Import state

**Authentication**
* `POST /auth/signup` -> Signup with email, password, display_name
* `POST /auth/login` -> Login with email and password
* `GET /me` -> Retrieve current authenticated user info

**Transactions & Operations**
* `POST /payments` -> Send payment
* `POST /requests` -> Create a payment request
* `POST /requests/{id}/pay` -> Fulfill a payment request
* `POST /requests/{id}/decline` -> Decline a request
* `POST /requests/{id}/cancel` -> Cancel an open request
* `GET /requests` -> List requests with direction and status filtering
* `POST /splits` -> Split a bill among participants
* `GET /activity` -> Retrieve activity feed
* `POST /settlements` -> Settlement batch processing (operator only)