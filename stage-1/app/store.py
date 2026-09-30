"""In-memory state for the whole Pocketful stage-1 service.

Design constraints taken straight from POCKETFUL_SPEC.md section 1:

* storage is plain Python dicts / lists only - no external database
* every mutation is serialised behind one ``threading.Lock()``

Nothing in here reaches for I/O. Endpoints are declared as *sync* FastAPI
handlers, so they execute on Starlette's worker thread pool; that is what makes
the lock load-bearing rather than decorative, and it is what lets the
concurrency test in ``verify.py`` actually exercise it.

The four invariants asserted on every mutation are:

I1  sum(balances.values()) == seeded_total
I2  every balance >= 0
I3  every balance belongs to a known user
I4  every stored password is a bcrypt hash (never plaintext)
"""

from __future__ import annotations

import itertools
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, TypeVar

from .errors import AppError, insufficient_funds, validation_error

T = TypeVar("T")

HANDLE_RE = r"^[a-z0-9_]{1,20}$"

#: bcrypt digests always start with one of these; a plaintext password can not.
_BCRYPT_PREFIXES = ("$2a$", "$2b$", "$2x$", "$2y$")

REQUEST_STATUSES = ("open", "paid", "declined", "cancelled")
TERMINAL_REQUEST_STATUSES = ("paid", "declined", "cancelled")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_bcrypt_hash(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(_BCRYPT_PREFIXES)


class InvariantViolation(AssertionError):
    """Raised when one of I1-I4 does not hold. Always a 500, never silent."""


class Store:
    """The single mutable world, guarded by one lock."""

    def __init__(self) -> None:
        # Spec wording: threading.Lock(). Never call back into the store while
        # holding it (all helpers below are _-prefixed and lock-free).
        self.lock = threading.Lock()

        self.users: dict[str, dict[str, Any]] = {}
        self.balances: dict[str, int] = {}
        self.requests: dict[str, dict[str, Any]] = {}
        self.activity: list[dict[str, Any]] = []
        self.idempotency: dict[tuple[str, str], dict[str, Any]] = {}
        self.tokens: dict[str, str] = {}
        self.seeded_total: int = 0

        self._ids = {
            "request": itertools.count(1),
            "payment": itertools.count(1),
            "split": itertools.count(1),
            "settlement": itertools.count(1),
            "activity": itertools.count(1),
        }

    # ------------------------------------------------------------------
    # lock-free internals. Callers must already hold self.lock.
    # ------------------------------------------------------------------

    def _next_id(self, kind: str, prefix: str) -> str:
        return f"{prefix}_{next(self._ids[kind])}"

    def _add_activity(
        self,
        *,
        owner: str,
        type: str,
        actor: str,
        direction: str,
        amount: int,
        counterparty: str | None = None,
        related_id: str | None = None,
        memo: str | None = None,
        parts: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        entry = {
            "id": self._next_id("activity", "act"),
            "handle": owner,
            "type": type,
            "actor": actor,
            "counterparty": counterparty,
            "direction": direction,
            "amount": amount,
            "related_id": related_id,
            "memo": memo,
            "parts": parts,
            "created_at": now_iso(),
        }
        self.activity.append(entry)
        return entry

    def _check_invariants(self) -> None:
        """I1-I4. Raises InvariantViolation, which must never escape to a 200."""
        # I3 - every balance is owned by a real user
        for handle in self.balances:
            if handle not in self.users:
                raise InvariantViolation(f"I3 broken: balance for unknown user {handle!r}")

        # I1 - conservation of money
        total = sum(self.balances.values())
        if total != self.seeded_total:
            raise InvariantViolation(
                f"I1 broken: sum(balances)={total} != seeded_total={self.seeded_total}"
            )

        # I2 - no negative balances
        for handle, amount in self.balances.items():
            if amount < 0:
                raise InvariantViolation(f"I2 broken: negative balance for {handle!r}: {amount}")

        # I4 - no plaintext credentials
        for handle, user in self.users.items():
            if not is_bcrypt_hash(user.get("password_hash")):
                raise InvariantViolation(f"I4 broken: non-bcrypt password_hash for {handle!r}")

    def invariant_report(self) -> dict[str, Any]:
        total = sum(self.balances.values())
        negative = sorted(h for h, b in self.balances.items() if b < 0)
        unknown = sorted(h for h in self.balances if h not in self.users)
        plaintext = sorted(
            h for h, u in self.users.items() if not is_bcrypt_hash(u.get("password_hash"))
        )
        return {
            "sum_of_balances": total,
            "seeded_total": self.seeded_total,
            "balance_sum_equals_seeded_total": total == self.seeded_total,
            "no_negative_balances": not negative,
            "negative_balances": negative,
            "all_balances_owned_by_users": not unknown,
            "orphan_balances": unknown,
            "no_plaintext_passwords": not plaintext,
            "plaintext_password_users": plaintext,
        }

    def assert_invariants(self) -> None:
        self._check_invariants()

    # ------------------------------------------------------------------
    # users / balances
    # ------------------------------------------------------------------

    def _require_user(self, handle: str) -> dict[str, Any]:
        user = self.users.get(handle)
        if user is None:
            raise AppError(404, "unknown_user", f"no such user: {handle!r}", handle=handle)
        return user

    def _transfer(self, frm: str, to: str, amount: int) -> None:
        """Move minor units. Caller has already checked both users exist."""
        available = self.balances.get(frm, 0)
        if available < amount:
            raise insufficient_funds(frm, available, amount)
        self.balances[frm] = available - amount
        self.balances[to] = self.balances.get(to, 0) + amount

    # ------------------------------------------------------------------
    # fixture handling (reset / import share this)
    # ------------------------------------------------------------------

    def _install_fixture(self, fixture: dict[str, Any]) -> None:
        """Replace the entire world. Atomic: callers hold the lock."""
        raw_users = fixture.get("users", [])
        if not isinstance(raw_users, list):
            raise validation_error("fixture 'users' must be a list")
        raw_balances = fixture.get("balances", {}) or {}
        if not isinstance(raw_balances, dict):
            raise validation_error("fixture 'balances' must be an object of handle -> integer")

        from .security import hash_password  # local import: avoids a cycle

        users: dict[str, dict[str, Any]] = {}
        balances: dict[str, int] = {}

        for entry in raw_users:
            if not isinstance(entry, dict):
                raise validation_error("each fixture user must be an object")
            handle = entry.get("handle") or entry.get("username")
            if not isinstance(handle, str) or not handle:
                raise validation_error("each fixture user needs a 'handle'")
            if not is_valid_handle(handle):
                raise validation_error(
                    f"handle {handle!r} does not match {HANDLE_RE}", code="invalid_handle"
                )
            if handle in users:
                raise validation_error(f"duplicate handle in fixture: {handle!r}")

            raw_balance = entry.get("balance", 0)
            if isinstance(raw_balance, bool) or not isinstance(raw_balance, int):
                raise validation_error(f"balance for {handle!r} must be an integer")
            if raw_balance < 0:
                raise validation_error(f"balance for {handle!r} must not be negative")

            password = entry.get("password")
            if password is None:
                password = DEFAULT_FIXTURE_PASSWORD
            if not isinstance(password, str) or not password:
                raise validation_error(f"password for {handle!r} must be a non-empty string")

            email = entry.get("email") or f"{handle}@pocketful.test"
            users[handle] = {
                "handle": handle,
                "email": str(email).strip().lower(),
                "display_name": str(entry.get("display_name") or handle),
                "password_hash": hash_password(password),
                "is_operator": bool(entry.get("is_operator", False)),
                "created_at": entry.get("created_at") or now_iso(),
            }
            balances[handle] = raw_balance

        for handle, amount in raw_balances.items():
            if not is_valid_handle(handle):
                raise validation_error(
                    f"handle {handle!r} does not match {HANDLE_RE}", code="invalid_handle"
                )
            if isinstance(amount, bool) or not isinstance(amount, int):
                raise validation_error(f"balance for {handle!r} must be an integer")
            if amount < 0:
                raise validation_error(f"balance for {handle!r} must not be negative")
            if handle not in users:
                raise validation_error(
                    f"balances references unknown user {handle!r}", code="unknown_user"
                )
            balances[handle] = amount

        seeded_total = fixture.get("seeded_total", fixture.get("total"))
        if seeded_total is None:
            seeded_total = sum(balances.values())
        if isinstance(seeded_total, bool) or not isinstance(seeded_total, int):
            raise validation_error("'seeded_total' must be an integer")
        if seeded_total < 0:
            raise validation_error("'seeded_total' must not be negative")
        if seeded_total != sum(balances.values()):
            raise validation_error(
                f"seeded_total {seeded_total} != sum of balances {sum(balances.values())}; "
                "the balance-sum invariant would be violated by this fixture",
                code="fixture_total_mismatch",
            )

        self.users = users
        self.balances = balances
        self.seeded_total = seeded_total
        self.requests = {}
        self.activity = []
        self.idempotency = {}
        self.tokens = {}
        self._ids = {k: itertools.count(1) for k in self._ids}
        self._check_invariants()

    def _export(self) -> dict[str, Any]:
        return {
            "seeded_total": self.seeded_total,
            "users": [dict(u) for u in self.users.values()],
            "balances": dict(self.balances),
            "requests": [dict(r) for r in self.requests.values()],
            "activity": [dict(a) for a in self.activity],
            "idempotency": [
                {
                    "user": key[0],
                    "key": key[1],
                    "fingerprint": rec.get("fingerprint"),
                    "status_code": rec.get("status_code"),
                    "body": rec.get("body"),
                    "created_at": rec.get("created_at"),
                }
                for key, rec in self.idempotency.items()
            ],
            "balance_sum": sum(self.balances.values()),
            "invariants": self.invariant_report(),
        }

    def _import(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Accept either a full export or a bare fixture.

        A full export (one that carries ``requests`` / ``activity`` /
        ``idempotency`` alongside ``users``) is restored verbatim, so
        export -> import -> export round-trips. A bare fixture goes through
        ``_install_fixture`` and clears requests/activity/idempotency exactly
        as ``/_test/reset`` does.
        """
        if not isinstance(payload, dict):
            raise validation_error("import payload must be a JSON object")

        is_full_export = "requests" in payload or "activity" in payload or "idempotency" in payload
        if is_full_export:
            return self._install_full_export(payload)

        if "users" in payload or "balances" in payload or "seeded_total" in payload or "total" in payload:
            self._install_fixture(payload)
            return self._export()

        raise validation_error(
            "import payload must contain at least one of 'users', 'balances', 'seeded_total'"
        )

    def _install_full_export(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Restore a snapshot verbatim. Seed users first, then overlay the rest."""
        self._install_fixture(
            {
                "users": payload.get("users", []),
                "balances": payload.get("balances", {}),
                "seeded_total": payload.get("seeded_total", payload.get("total")),
            }
        )

        requests_payload = payload.get("requests") or []
        if not isinstance(requests_payload, list):
            raise validation_error("export 'requests' must be a list")
        restored_requests: dict[str, dict[str, Any]] = {}
        for record in requests_payload:
            if not isinstance(record, dict):
                raise validation_error("each exported request must be an object")
            record = dict(record)
            request_id = record.get("id")
            if not request_id:
                raise validation_error("each exported request needs an 'id'")
            if record.get("status") not in REQUEST_STATUSES:
                raise validation_error(
                    f"exported request {request_id!r} has unknown status {record.get('status')!r}"
                )
            # Re-validate the endpoints against the seeded user set.
            for role in ("from", "to"):
                handle = record.get(role)
                if not isinstance(handle, str) or handle not in self.users:
                    handle = record.get(f"{role}_handle")
                if not isinstance(handle, str) or handle not in self.users:
                    raise validation_error(
                        f"exported request {request_id!r} references unknown user {handle!r}",
                        code="unknown_user",
                    )
                record[role] = handle
                record[f"{role}_handle"] = handle
            restored_requests[request_id] = record
        self.requests = restored_requests

        activity_payload = payload.get("activity") or []
        if not isinstance(activity_payload, list):
            raise validation_error("export 'activity' must be a list")
        restored_activity: list[dict[str, Any]] = []
        for entry in activity_payload:
            if not isinstance(entry, dict):
                raise validation_error("each exported activity entry must be an object")
            entry = dict(entry)
            entry.setdefault("id", self._next_id("activity", "act"))
            if entry.get("handle") not in self.users:
                raise validation_error(
                    f"exported activity references unknown user {entry.get('handle')!r}",
                    code="unknown_user",
                )
            restored_activity.append(entry)
        self.activity = restored_activity
        self._ids["activity"] = itertools.count(len(restored_activity) + 1)

        idem_payload = payload.get("idempotency") or []
        if not isinstance(idem_payload, list):
            raise validation_error("export 'idempotency' must be a list")
        restored_idem: dict[tuple[str, str], dict[str, Any]] = {}
        for record in idem_payload:
            if not isinstance(record, dict):
                raise validation_error("each exported idempotency record must be an object")
            user, key = record.get("user"), record.get("key")
            if not isinstance(user, str) or user not in self.users:
                raise validation_error(
                    f"exported idempotency record references unknown user {user!r}",
                    code="unknown_user",
                )
            if not isinstance(key, str) or not key:
                raise validation_error("exported idempotency record needs a 'key'")
            restored_idem[(user, key)] = {
                "fingerprint": record.get("fingerprint"),
                "status_code": int(record.get("status_code", 200)),
                "body": record.get("body"),
                "created_at": record.get("created_at") or now_iso(),
            }
        self.idempotency = restored_idem

        # A restored snapshot must still satisfy every invariant.
        self._check_invariants()
        return self._export()

    # ------------------------------------------------------------------
    # transaction() - the one door into the world
    # ------------------------------------------------------------------

    def transaction(self, fn: Callable[[], T]) -> T:
        """Run ``fn`` under the lock, then re-assert I1-I4 before releasing.

        If an invariant ever breaks the mutation is reported as a 500 rather
        than being committed silently. The lock is released via ``finally`` so
        a raising handler can never wedge the process.
        """
        with self.lock:
            result = fn()
            self._check_invariants()
            return result

    def read(self, fn: Callable[[], T]) -> T:
        with self.lock:
            return fn()

    # ------------------------------------------------------------------
    # idempotency (spec section 2)
    # ------------------------------------------------------------------

    def _idem_replay(
        self, user: str, key: str | None, fingerprint: str
    ) -> tuple[int, dict[str, Any]] | None:
        if not key:
            return None
        record = self.idempotency.get((user, key))
        if record is None:
            return None
        if record.get("fingerprint") != fingerprint:
            raise AppError(
                409,
                "idempotency_key_reuse",
                f"Idempotency-Key {key!r} was already used by {user} for a different request body",
                idempotency_key=key,
            )
        return int(record["status_code"]), record["body"]

    def _idem_remember(
        self,
        user: str,
        key: str | None,
        fingerprint: str,
        status_code: int,
        body: dict[str, Any],
    ) -> None:
        if not key:
            return
        self.idempotency[(user, key)] = {
            "fingerprint": fingerprint,
            "status_code": status_code,
            "body": body,
            "created_at": now_iso(),
        }

    def idempotency_keys(self) -> list[str]:
        return [f"{u}/{k}" for (u, k) in self.idempotency]


DEFAULT_FIXTURE_PASSWORD = "pocketful-fixture-pw"


def is_valid_handle(handle: Any) -> bool:
    import re

    return isinstance(handle, str) and re.fullmatch(HANDLE_RE, handle) is not None


def derive_handle(email: str) -> str:
    """Derive a candidate handle from an email address.

    Lower-cases the local part and drops every character outside
    ``[a-z0-9_]``; the result is then validated against ``^[a-z0-9_]{1,20}$``.
    A local part that sanitises down to nothing (e.g. ``"!!!@x.com"``) fails
    that regex and the signup is rejected with 422.
    """
    import re

    local = email.strip().split("@", 1)[0].lower()
    return re.sub(r"[^a-z0-9_]", "", local)[:20]
