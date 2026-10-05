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

import copy
import hashlib
import itertools
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, TypeVar

from .errors import AppError, insufficient_funds, validation_error

T = TypeVar("T")

HANDLE_RE = r"^[a-z0-9_]{1,20}$"

#: bcrypt digests always start with one of these; a plaintext password can not.
_BCRYPT_PREFIXES = ("$2a$", "$2b$", "$2x$", "$2y$")

REQUEST_STATUSES = ("open", "paid", "declined", "cancelled")
TERMINAL_REQUEST_STATUSES = ("paid", "declined", "cancelled")

AUTHORIZATION_STATUSES = ("open", "captured", "voided", "expired")
TERMINAL_AUTHORIZATION_STATUSES = ("captured", "voided", "expired")

#: stage-2 fixture defaults, per spec section "Model".
DEFAULT_AUTHORIZATION_TTL_SECONDS = 600
DEFAULT_CURRENCY = "EUR"
DEFAULT_MINOR_UNITS = 2

#: ``POST /authorizations`` bounds. The spec fixes these exactly.
MIN_AUTHORIZATION_AMOUNT = 1
MAX_AUTHORIZATION_AMOUNT = 1_000_000_000
MAX_AUTHORIZATION_NOTE_CHARS = 200
AUTHORIZATION_VISIBILITIES = ("public", "private")

#: Ceiling on an explicitly requested page for ``GET /requests`` and
#: ``GET /authorizations``. Truncation is opt-in: omitting ``limit`` returns
#: every match, because the accepted Stage-1 list never truncated. This only
#: bounds a page a caller deliberately asked for.
MAX_LIST_LIMIT = 10_000


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_iso(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp into an aware datetime, or ``None``.

    ``None`` means "no usable timestamp", which callers read as "does not
    expire" rather than as an error, so a partially specified fixture record
    cannot wedge the funds model.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text[-1] in ("Z", "z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def authorization_open_at(record: dict[str, Any], now: datetime) -> bool:
    """True while ``record`` still holds funds: ``open`` and not past expiry.

    Deliberately a pure predicate over ``(record, now)`` so the lazy-expiry
    sweep, the ``held`` computation and fixture validation all share one
    definition that cannot drift from the others.
    """
    if record.get("status") != "open":
        return False
    expires_at = parse_iso(record.get("expires_at"))
    return expires_at is None or expires_at > now


def authorization_remaining(record: dict[str, Any]) -> int:
    """Minor units this record still holds.

    The spec's definition: the amount still held, zero when closed. A record is
    closed unless it is ``open``, and a final capture or a void releases the
    uncaptured remainder *without* capturing it - so ``amount - captured``
    alone would keep reporting that released remainder as still held. Every
    caller (the ``held`` computation, capture validation and the public
    projection) goes through here so they cannot disagree.
    """
    if record.get("status") != "open":
        return 0
    amount = record.get("amount")
    if isinstance(amount, bool) or not isinstance(amount, int):
        return 0
    captured = record.get("captured_amount")
    if isinstance(captured, bool) or not isinstance(captured, int) or captured < 0:
        captured = 0
    return max(0, amount - captured)


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
        self.authorizations: dict[str, dict[str, Any]] = {}
        self.activity: list[dict[str, Any]] = []
        self.idempotency: dict[tuple[str, str], dict[str, Any]] = {}
        self.tokens: dict[str, str] = {}
        #: Per-payment visibility, for the D2 activity feed's ``data-visibility``
        #: attribute only. Deliberately NOT part of any JSON projection and NOT
        #: part of an export: ``/activity`` must stay byte-identical, so the
        #: screens read the value from here instead of gaining a field.
        self.payment_visibility: dict[str, str] = {}
        self.seeded_total: int = 0
        #: stage-2 fixture knobs. Defaulted per the spec (600 seconds, EUR, 2
        #: decimal places) and re-seeded by every reset/import.
        self.authorization_ttl_seconds: int = DEFAULT_AUTHORIZATION_TTL_SECONDS
        self.currency: str = DEFAULT_CURRENCY
        self.minor_units: int = DEFAULT_MINOR_UNITS

        self._ids = {
            "request": itertools.count(1),
            "payment": itertools.count(1),
            "split": itertools.count(1),
            "settlement": itertools.count(1),
            "activity": itertools.count(1),
            "authorization": itertools.count(1),
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

        # I5 - a hold can never exceed a balance, so available is never negative
        now = datetime.now(timezone.utc)
        for handle in self.users:
            held = self._held_amount(handle, now)
            balance = self.balances.get(handle, 0)
            if held < 0:
                raise InvariantViolation(f"I5 broken: negative hold for {handle!r}: {held}")
            if held > balance:
                raise InvariantViolation(
                    f"I5 broken: held {held} exceeds balance {balance} for {handle!r}; "
                    "available would be negative"
                )

    def invariant_report(self) -> dict[str, Any]:
        total = sum(self.balances.values())
        negative = sorted(h for h, b in self.balances.items() if b < 0)
        unknown = sorted(h for h in self.balances if h not in self.users)
        plaintext = sorted(
            h for h, u in self.users.items() if not is_bcrypt_hash(u.get("password_hash"))
        )
        now = datetime.now(timezone.utc)
        over_held = sorted(
            h for h in self.users if self._held_amount(h, now) > self.balances.get(h, 0)
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
            # stage-2 funds invariant
            "holds_within_balance": not over_held,
            "over_held_users": over_held,
            "no_negative_available": not over_held,
            "open_authorizations": sum(
                1 for r in self.authorizations.values() if authorization_open_at(r, now)
            ),
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
        """Move minor units. Caller has already checked both users exist.

        The debit is measured against **available**, not raw balance, so funds
        held by an open authorization can never be spent. That makes this the
        single choke point for "held funds are unspendable": every debit path
        (payments, request pay, splits, settlements, capture) funnels through
        here. With no authorizations present ``available == balance``, so
        Stage-1 behaviour is bit-for-bit unchanged.
        """
        self._require_available(frm, amount)
        self.balances[frm] = self.balances[frm] - amount
        self.balances[to] = self.balances.get(to, 0) + amount

    def _transfer_from_hold(self, payer: str, receiver: str, amount: int) -> None:
        """Move ``amount`` from ``payer`` to ``receiver`` against a hold.

        Deliberately *not* ``_transfer``: that checks ``available``, and the
        payer's available is already reduced by the very hold being consumed, so
        requiring it here would make every capture unsatisfiable. This is safe
        because the caller has already checked ``amount`` against that
        authorization's remainder, and the remainder is part of the payer's
        balance (I1: ``balance == total``), so the payer cannot go negative.
        """
        if self.balances.get(payer, 0) < amount:
            raise insufficient_funds(payer, self.balances.get(payer, 0), amount)
        self.balances[payer] = self.balances[payer] - amount
        self.balances[receiver] = self.balances.get(receiver, 0) + amount

    # ------------------------------------------------------------------
    # funds: total / held / available  (stage-2 section 3)
    #
    #   held      = sum(remaining of the user's unexpired *open* authorizations)
    #   available = total - held        (>= 0 by invariant I5)
    #
    # Every helper below is lock-free: callers already hold self.lock.
    # ------------------------------------------------------------------

    def _expire_authorizations(self, now: datetime | None = None) -> int:
        """Lazily transition overdue open authorizations to ``expired``.

        There is no background timer. Any read or write that observes an
        authorization past its ``expires_at`` performs the transition here,
        which releases its hold. Expiry creates no activity entry: it is an
        authorization lifecycle event, not a money movement.
        """
        moment = now or datetime.now(timezone.utc)
        stamp = moment.isoformat()
        changed = 0
        for record in self.authorizations.values():
            if record.get("status") != "open":
                continue
            expires_at = parse_iso(record.get("expires_at"))
            if expires_at is None or expires_at > moment:
                continue
            record["status"] = "expired"
            record["updated_at"] = stamp
            record["closed_at"] = stamp
            changed += 1
        return changed

    def _held_amount(self, handle: str, now: datetime | None = None) -> int:
        """Sum of the user's unexpired open authorization **remainders**.

        The remainder, not the face amount: after a partial capture
        (``final: false``) the authorization stays open while holding only what
        is left, so summing ``amount`` would over-reserve and make the released
        part permanently unspendable.
        """
        moment = now or datetime.now(timezone.utc)
        held = 0
        for record in self.authorizations.values():
            if record.get("from") != handle:
                continue
            if authorization_open_at(record, moment):
                held += authorization_remaining(record)
        return held

    def _available_amount(self, handle: str, now: datetime | None = None) -> int:
        return self.balances.get(handle, 0) - self._held_amount(handle, now)

    def _funds(self, handle: str, now: datetime | None = None) -> dict[str, int]:
        """The funds triple for one user: total, held and available."""
        moment = now or datetime.now(timezone.utc)
        total = self.balances.get(handle, 0)
        held = self._held_amount(handle, moment)
        return {
            "total": total,
            "balance": total,
            "held": held,
            "available": total - held,
        }

    def _require_available(self, handle: str, amount: int, now: datetime | None = None) -> int:
        """Raise ``insufficient_funds`` unless ``handle`` can spend ``amount``."""
        moment = now or datetime.now(timezone.utc)
        available = self._available_amount(handle, moment)
        if available < amount:
            raise insufficient_funds(handle, available, amount)
        return available

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

            # A payload produced by /_test/export already carries a bcrypt
            # digest, so it is restored verbatim; only a fixture that supplies
            # a plaintext `password` is hashed here. This is what makes
            # export -> import -> login a true round trip.
            existing_hash = entry.get("password_hash")
            if is_bcrypt_hash(existing_hash):
                password_hash = existing_hash
            else:
                password = entry.get("password")
                if password is None:
                    password = DEFAULT_FIXTURE_PASSWORD
                if not isinstance(password, str) or not password:
                    raise validation_error(f"password for {handle!r} must be a non-empty string")
                password_hash = _fixture_password_hash(password)

            email = entry.get("email") or f"{handle}@pocketful.test"
            users[handle] = {
                "handle": handle,
                "email": str(email).strip().lower(),
                "display_name": str(entry.get("display_name") or handle),
                "password_hash": password_hash,
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

        # Seeded authorizations are parsed and fully validated BEFORE anything is
        # assigned, so a rejected fixture leaves the live world untouched - the
        # 422 validation_failed path is atomic by construction.
        authorizations = self._parse_authorization_records(
            fixture.get("authorizations") or [], balances
        )

        ttl = self._parse_authorization_ttl(fixture.get("authorization_ttl_seconds"))
        currency, minor_units = self._parse_display_metadata(
            fixture.get("currency"), fixture.get("minor_units")
        )

        self.users = users
        self.balances = balances
        self.seeded_total = seeded_total
        self.requests = {}
        self.authorizations = authorizations
        self.activity = []
        self.idempotency = {}
        self.tokens = {}
        self.payment_visibility = {}
        self.authorization_ttl_seconds = ttl
        self.currency = currency
        self.minor_units = minor_units
        self._ids = {k: itertools.count(1) for k in self._ids}
        self._sync_authorization_ids()
        self._check_invariants()

    @staticmethod
    def _parse_authorization_ttl(raw: Any) -> int:
        """``authorization_ttl_seconds`` - default 600, must be a positive int."""
        if raw is None:
            return DEFAULT_AUTHORIZATION_TTL_SECONDS
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise validation_error(
                "'authorization_ttl_seconds' must be an integer number of seconds",
                code="validation_failed",
            )
        if raw <= 0:
            raise validation_error(
                "'authorization_ttl_seconds' must be a positive number of seconds",
                code="validation_failed",
            )
        return raw

    @staticmethod
    def _parse_display_metadata(raw_currency: Any, raw_minor_units: Any) -> tuple[str, int]:
        """Currency/minor units are additive display metadata."""
        currency = DEFAULT_CURRENCY
        if raw_currency is not None:
            if not isinstance(raw_currency, str) or not raw_currency.strip():
                raise validation_error(
                    "'currency' must be a non-empty string", code="validation_failed"
                )
            currency = raw_currency.strip().upper()

        minor_units = DEFAULT_MINOR_UNITS
        if raw_minor_units is not None:
            if isinstance(raw_minor_units, bool) or not isinstance(raw_minor_units, int):
                raise validation_error(
                    "'minor_units' must be an integer", code="validation_failed"
                )
            if not 0 <= raw_minor_units <= 6:
                raise validation_error(
                    "'minor_units' must be between 0 and 6", code="validation_failed"
                )
            minor_units = raw_minor_units
        return currency, minor_units

    # ------------------------------------------------------------------
    # authorization records
    # ------------------------------------------------------------------

    def _create_authorization(
        self,
        *,
        sender: str,
        recipient: str,
        amount: int,
        note: str | None,
        visibility: str,
    ) -> dict[str, Any]:
        """Build, register and return a fresh ``open`` authorization.

        The caller is responsible for having already checked availability and
        every other rejection case, and for holding the lock.
        """
        created = datetime.now(timezone.utc)
        record = {
            "id": self._next_id("authorization", "a"),
            "from": sender,
            "from_handle": sender,
            "to": recipient,
            "to_handle": recipient,
            "amount": amount,
            "captured_amount": 0,
            "remaining_amount": amount,
            "note": note,
            "visibility": visibility,
            "status": "open",
            "created_at": created.isoformat(),
            "updated_at": created.isoformat(),
            "expires_at": (created + timedelta(seconds=self.authorization_ttl_seconds)).isoformat(),
            "payment_id": None,
            "payment_ids": [],
        }
        self.authorizations[record["id"]] = record
        return record

    def _parse_authorization_records(
        self,
        raw: Any,
        balances: dict[str, int],
    ) -> dict[str, dict[str, Any]]:
        """Validate authorization records and normalise their shape.

        Shared by ``_install_fixture`` (seeded holds) and, from W6,
        ``_install_full_export`` (restored holds) so both entry points agree on
        what a well-formed record is.

        ``balances`` is passed explicitly rather than read from ``self``: during
        an import the new balances are not installed yet, so reading
        ``self.balances`` would validate the incoming holds against the
        *outgoing* world and let an over-hold slip through.
        """
        if not isinstance(raw, list):
            raise validation_error("'authorizations' must be a list", code="validation_failed")

        now = datetime.now(timezone.utc)
        parsed: dict[str, dict[str, Any]] = {}
        for entry in raw:
            if not isinstance(entry, dict):
                raise validation_error("each authorization must be an object", code="validation_failed")
            record = dict(entry)

            auth_id = record.get("id") or record.get("authorization_id")
            if not isinstance(auth_id, str) or not auth_id:
                raise validation_error("each authorization needs an 'id'", code="validation_failed")
            if auth_id in parsed:
                raise validation_error(f"duplicate authorization id: {auth_id!r}", code="validation_failed")

            status = record.get("status") or "open"
            if status not in AUTHORIZATION_STATUSES:
                raise validation_error(
                    f"authorization {auth_id!r} has unknown status {status!r}", code="validation_failed"
                )

            amount = record.get("amount")
            if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
                raise validation_error(
                    f"authorization {auth_id!r} needs a positive integer 'amount'",
                    code="validation_failed",
                    id=auth_id,
                )

            sender = record.get("from") or record.get("from_handle")
            if not isinstance(sender, str) or sender not in balances:
                raise validation_error(
                    f"authorization {auth_id!r} references unknown user {sender!r}",
                    code="unknown_user",
                    id=auth_id,
                )

            recipient = record.get("to", record.get("to_handle"))
            if recipient is not None and (
                not isinstance(recipient, str) or recipient not in balances
            ):
                raise validation_error(
                    f"authorization {auth_id!r} references unknown user {recipient!r}",
                    code="unknown_user",
                    id=auth_id,
                )

            captured = record.get("captured_amount") or 0
            if isinstance(captured, bool) or not isinstance(captured, int) or captured < 0:
                raise validation_error(
                    f"authorization {auth_id!r} needs a non-negative integer 'captured_amount'",
                    code="validation_failed",
                    id=auth_id,
                )
            if captured > amount:
                raise validation_error(
                    f"authorization {auth_id!r} captured_amount {captured} exceeds amount {amount}",
                    code="validation_failed",
                    id=auth_id,
                )

            record["id"] = auth_id
            record["status"] = status
            record["amount"] = amount
            record["from"] = sender
            record["from_handle"] = sender
            record["to"] = recipient
            record["to_handle"] = recipient
            record["captured_amount"] = captured
            record["remaining_amount"] = authorization_remaining(record)
            record["payment_id"] = record.get("payment_id")
            record["payment_ids"] = list(record.get("payment_ids") or [])
            record["note"] = record.get("note")
            record["created_at"] = record.get("created_at") or now.isoformat()
            record["updated_at"] = record.get("updated_at") or record["created_at"]
            if status in TERMINAL_AUTHORIZATION_STATUSES:
                record.setdefault("closed_at", record["updated_at"])
            parsed[auth_id] = record

        # Seeded holds must fit inside the seeded balances, otherwise the fixture
        # would install a world where available is already negative.
        holds: dict[str, int] = {}
        for record in parsed.values():
            if authorization_open_at(record, now):
                sender = record["from"]
                holds[sender] = holds.get(sender, 0) + int(record["amount"])
        for handle, held in holds.items():
            balance = balances.get(handle, 0)
            if held > balance:
                raise validation_error(
                    f"seeded authorizations hold {held} minor units for {handle!r} but the "
                    f"seeded balance is only {balance}; available would be negative",
                    code="validation_failed",
                    handle=handle,
                    held=held,
                    balance=balance,
                )

        return parsed

    def authorization_public(self, record: dict[str, Any]) -> dict[str, Any]:
        """The public projection of one authorization record.

        Emits both vocabularies on purpose: ``authorization_id``/``from_handle``/
        ``to_handle`` from the stage-2 spec, and ``id``/``from``/``to`` from the
        accepted Stage-1 contract. Both are additive, so neither lineage has to
        guess. ``remaining_amount`` comes from ``authorization_remaining`` so it
        reads zero for a closed record, never a stale derived value.
        """
        amount = int(record.get("amount") or 0)
        captured = int(record.get("captured_amount") or 0)
        return {
            "authorization_id": record["id"],
            "id": record["id"],
            "from": record.get("from"),
            "from_handle": record.get("from_handle") or record.get("from"),
            "to": record.get("to"),
            "to_handle": record.get("to_handle") or record.get("to"),
            "amount": amount,
            "captured_amount": captured,
            "remaining_amount": authorization_remaining(record),
            "note": record.get("note"),
            "visibility": record.get("visibility") or "public",
            "status": record.get("status"),
            "created_at": record.get("created_at"),
            "updated_at": record.get("updated_at"),
            "expires_at": record.get("expires_at"),
            "payment_id": record.get("payment_id"),
            "payment_ids": list(record.get("payment_ids") or []),
            "closed_at": record.get("closed_at"),
        }

    def _sync_authorization_ids(self) -> None:
        """Restart the id counter above any authorization id already in use."""
        highest = 0
        for auth_id in self.authorizations:
            suffix = auth_id.rsplit("_", 1)[-1]
            if suffix.isdigit():
                highest = max(highest, int(suffix))
        self._ids["authorization"] = itertools.count(highest + 1)

    def _export(self) -> dict[str, Any]:
        return {
            "seeded_total": self.seeded_total,
            "currency": self.currency,
            "minor_units": self.minor_units,
            "authorization_ttl_seconds": self.authorization_ttl_seconds,
            "users": [dict(u) for u in self.users.values()],
            "balances": dict(self.balances),
            "requests": [dict(r) for r in self.requests.values()],
            # stage-2: authorization records ride along with the snapshot, which
            # is what makes the lazy-expiry transition observable from outside.
            "authorizations": [dict(a) for a in self.authorizations.values()],
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
            # Deliberately NOT here: the live bearer tokens.
            #
            # SPEC.md line 151 requires that "a browser signed in before that
            # export/import upgrade must remain signed in afterwards", but read
            # the wording: that is an in-place upgrade on the same server, not a
            # session migration to another host. The server's own token table
            # survives an import, so the tokens never need to travel. Putting
            # them in the payload would satisfy the same requirement while
            # turning every export into a directly credential-bearing file -
            # strictly worse than stage 1, whose export held only bcrypt hashes
            # that cannot be used to authenticate. See
            # ``_install_full_export`` for how sessions are preserved instead.
            "balance_sum": sum(self.balances.values()),
            "invariants": self.invariant_report(),
        }

    # ------------------------------------------------------------------
    # rollback support, so a rejected import leaves the world untouched
    # ------------------------------------------------------------------

    #: Every mutable attribute an import may touch. ``_ids`` is rebuilt from
    #: the restored lengths rather than copied, because ``itertools.count``
    #: is not copyable.
    _IMPORT_FIELDS = (
        "users",
        "balances",
        "requests",
        "authorizations",
        "activity",
        "idempotency",
        "tokens",
        "seeded_total",
        "authorization_ttl_seconds",
        "currency",
        "minor_units",
    )

    def _capture_state(self) -> dict[str, Any]:
        """Deep-copy every field ``_import`` may write, plus the id counters."""
        saved = {name: copy.deepcopy(getattr(self, name)) for name in self._IMPORT_FIELDS}
        saved["_id_positions"] = {
            kind: next(counter) - 1 for kind, counter in self._ids.items()
        }
        return saved

    def _restore_state(self, saved: dict[str, Any]) -> None:
        """Put back a ``_capture_state`` snapshot verbatim.

        Each field is rebound to a fresh object, so no live caller can be
        holding a reference into the discarded copy.
        """
        for name in self._IMPORT_FIELDS:
            setattr(self, name, copy.deepcopy(saved[name]))
        self._ids = {
            kind: itertools.count(position + 1)
            for kind, position in saved["_id_positions"].items()
        }

    def _import(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Accept either a full export or a bare fixture.

        A full export (one that carries ``requests`` / ``activity`` /
        ``idempotency`` alongside ``users``) is restored verbatim, so
        export -> import -> export round-trips. A bare fixture goes through
        ``_install_fixture`` and clears requests/activity/idempotency exactly
        as on ``/_test/reset`` does.

        Import is all-or-nothing: the whole payload is validated and applied
        under a snapshot, and any rejection puts every field back exactly as it
        was. A 422 therefore never leaves a half-replaced world behind.
        """
        if not isinstance(payload, dict):
            raise validation_error("import payload must be a JSON object")

        is_full_export = "requests" in payload or "activity" in payload or "idempotency" in payload
        saved = self._capture_state()
        try:
            if is_full_export:
                return self._install_full_export(payload, saved.get("tokens") or {})

            if "users" in payload or "balances" in payload or "seeded_total" in payload or "total" in payload:
                self._install_fixture(payload)
                return self._export()
        except Exception:
            self._restore_state(saved)
            raise

        raise validation_error(
            "import payload must contain at least one of 'users', 'balances', 'seeded_total'"
        )


    def _install_full_export(
        self, payload: dict[str, Any], live_tokens: dict[str, str] | None = None
    ) -> dict[str, Any]:
        """Restore a snapshot verbatim. Seed users first, then overlay the rest.

        ``live_tokens`` is the session table as it stood *before* the import,
        captured by ``_import``. ``_install_fixture`` clears it, so it has to be
        handed in rather than read back off ``self``.
        """
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

        # Stage-2 holds must survive the round trip: an exported authorization
        # that was dropped here would silently un-reserve its money and let the
        # payer spend the same funds twice.
        authorizations_payload = payload.get("authorizations") or []
        if not isinstance(authorizations_payload, list):
            raise validation_error("export 'authorizations' must be a list")
        restored_auth: dict[str, dict[str, Any]] = {}
        for record in authorizations_payload:
            if not isinstance(record, dict):
                raise validation_error("each exported authorization must be an object")
            record = dict(record)
            auth_id = record.get("id") or record.get("authorization_id")
            if not auth_id or not isinstance(auth_id, str):
                raise validation_error("each exported authorization needs an 'id'")
            if record.get("status") not in AUTHORIZATION_STATUSES:
                raise validation_error(
                    f"exported authorization {auth_id!r} has unknown status {record.get('status')!r}"
                )
            for role in ("from", "to"):
                handle = record.get(role) or record.get(f"{role}_handle")
                if not isinstance(handle, str) or handle not in self.users:
                    raise validation_error(
                        f"exported authorization {auth_id!r} references unknown user {handle!r}",
                        code="unknown_user",
                    )
                record[role] = handle
                record[f"{role}_handle"] = handle
            record["id"] = auth_id
            record.setdefault("captured_amount", 0)
            record.setdefault("payment_ids", [])
            restored_auth[auth_id] = record
        self.authorizations = restored_auth

        # Sessions are preserved server-side, never *required* to travel.
        #
        # The export carries no `tokens`, so there is nothing to restore and
        # nothing to validate: the live table is simply kept for the handles this
        # import retains, and dropped for every handle it removes.
        #
        # Keeping a dropped handle's session would be an authentication bypass -
        # an import that deprovisions a user must leave them signed out - so
        # preservation is scoped to surviving handles, never to the table as a
        # whole.
        #
        # SPEC.md line 151 is satisfied by this: "a browser signed in before that
        # export/import upgrade must remain signed in afterwards" describes an
        # in-place upgrade on the same server, and this is the same server, so
        # its own sessions are still valid afterwards.
        raw_tokens = payload.get("tokens")
        if raw_tokens is None:
            self.tokens = {
                token: handle
                for token, handle in (live_tokens or {}).items()
                if handle in self.users
            }
        else:
            # A legacy stage-2 payload, written by a build from before the
            # export stopped carrying tokens. Honour it so an
            # already-generated file still imports.
            #
            # The one thing that is *not* honoured is a token naming a handle
            # this import removes: honouring it would leave a deprovisioned user
            # authenticated. Such tokens are dropped rather than refused, so a
            # legacy file still imports in exactly the case where it removes
            # someone, instead of failing closed on a 422.
            if not isinstance(raw_tokens, list):
                raise validation_error("export 'tokens' must be a list")
            restored: dict[str, str] = {}
            for record in raw_tokens:
                if not isinstance(record, dict):
                    raise validation_error("each exported token must be an object")
                token, handle = record.get("token"), record.get("handle")
                if not isinstance(token, str) or not token:
                    raise validation_error("each exported token needs a 'token'")
                if not isinstance(handle, str) or not handle:
                    raise validation_error("each exported token needs a 'handle'")
                if handle not in self.users:
                    continue
                restored[token] = handle
            self.tokens = restored

        # A restored snapshot must still satisfy every invariant.
        self._check_invariants()
        return self._export()

    # ------------------------------------------------------------------
    # transaction() - the one door into the world
    # ------------------------------------------------------------------

    def transaction(self, fn: Callable[[], T]) -> T:
        """Run ``fn`` under the lock, then re-assert I1-I5 before releasing.

        If an invariant ever breaks the mutation is reported as a 500 rather
        than being committed silently. The lock is released via ``finally`` so
        a raising handler can never wedge the process.

        The lazy-expiry sweep runs first, so every write observes overdue
        authorizations and releases their holds before the mutation is applied.
        """
        with self.lock:
            self._expire_authorizations()
            result = fn()
            self._check_invariants()
            return result

    def read(self, fn: Callable[[], T]) -> T:
        """Run ``fn`` under the lock, applying lazy expiry first.

        Expiry is a genuine state transition, so it happens on reads too: a
        ``GET /me`` after an authorization lapsed must report the released
        hold, not the stale one.
        """
        with self.lock:
            self._expire_authorizations()
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

# bcrypt is deliberately slow. Every /_test/reset re-hashes every seeded
# user, and the concurrency suites reset in a loop, so digests are memoised.
# The memo is keyed by a SHA-256 of the password, never by the password
# itself, and holds only bcrypt digests - no plaintext is retained, and this
# dict is never read by the store or by /_test/export.
_FIXTURE_HASH_MEMO: dict[str, str] = {}
_FIXTURE_HASH_MEMO_MAX = 512


def _fixture_password_hash(password: str) -> str:
    from .security import hash_password

    memo_key = hashlib.sha256(password.encode("utf-8")).hexdigest()
    digest = _FIXTURE_HASH_MEMO.get(memo_key)
    if digest is None:
        digest = hash_password(password)
        if len(_FIXTURE_HASH_MEMO) >= _FIXTURE_HASH_MEMO_MAX:
            _FIXTURE_HASH_MEMO.clear()
        _FIXTURE_HASH_MEMO[memo_key] = digest
    return digest


def is_valid_handle(handle: Any) -> bool:
    import re

    return isinstance(handle, str) and re.fullmatch(HANDLE_RE, handle) is not None


def derive_handle(email: str) -> str:
    """Derive a candidate handle from an email address.

    Lower-cases the local part and drops every character outside
    ``[a-z0-9_]``. The result is then validated against
    ``^[a-z0-9_]{1,20}$`` **without truncation**, so the regex is genuinely
    enforced on the derived value rather than on a shortened copy of it.

    Consequences, both covered by tests and documented in CONTRACT.md:

    * a local part that sanitises down to nothing (``"!!!@x.com"``) fails the
      regex and signup is rejected 422;
    * a local part longer than 20 characters fails the regex and signup is
      rejected 422, rather than being silently cut down to 20.
    """
    import re

    local = email.strip().split("@", 1)[0].lower()
    return re.sub(r"[^a-z0-9_]", "", local)
