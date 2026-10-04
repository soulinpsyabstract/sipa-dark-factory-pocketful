"""Request/response models.

Amounts are *strict* integers. ``Annotated[int, BeforeValidator(...)]`` is used
rather than pydantic's lax coercion because the default behaviour would quietly
accept ``10.0`` (a float) and ``True`` (a bool) as money, and the spec is
explicit that every amount is an integer number of minor units.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictInt,
    field_validator,
    model_validator,
)

from .security import MAX_PASSWORD_BYTES

MAX_AMOUNT = 2**53 - 1


def strict_int(value: Any) -> Any:
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError("amounts must be integers in minor units, not floats or booleans")
    if isinstance(value, str):
        raise ValueError("amounts must be integers in minor units, not strings")
    return value


#: a positive integer number of minor units
Amount = Annotated[int, BeforeValidator(strict_int), Field(gt=0, le=MAX_AMOUNT)]


class SignupIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=MAX_PASSWORD_BYTES)
    display_name: str = Field(min_length=1, max_length=80)
    handle: str | None = Field(default=None, min_length=1, max_length=20)


class LoginIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=MAX_PASSWORD_BYTES)


class PaymentIn(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    to: str = Field(min_length=1, max_length=20)
    amount: Amount
    note: str | None = Field(default=None, max_length=280)
    memo: str | None = Field(default=None, max_length=280)

    @field_validator("to", mode="before")
    @classmethod
    def _alias_recipient(cls, value: Any) -> Any:
        return value

    @property
    def message(self) -> str | None:
        return self.note if self.note is not None else self.memo


class RequestIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    to: str = Field(min_length=1, max_length=20)
    amount: Amount
    note: str | None = Field(default=None, max_length=280)
    memo: str | None = Field(default=None, max_length=280)

    @property
    def message(self) -> str | None:
        return self.note if self.note is not None else self.memo


class SplitIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    amount: Amount
    participants: list[str] = Field(min_length=1, max_length=50)
    note: str | None = Field(default=None, max_length=280)
    memo: str | None = Field(default=None, max_length=280)

    @property
    def message(self) -> str | None:
        return self.note if self.note is not None else self.memo


class SettlementEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    from_: str = Field(alias="from", min_length=1, max_length=20)
    to: str = Field(min_length=1, max_length=20)
    amount: Amount


class SettlementIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    entries: list[SettlementEntry] = Field(min_length=1, max_length=500)
    note: str | None = Field(default=None, max_length=280)

    @property
    def message(self) -> str | None:
        return self.note


class FixtureUser(BaseModel):
    model_config = ConfigDict(extra="ignore")

    handle: str | None = None
    username: str | None = None
    email: str | None = None
    display_name: str | None = None
    password: str | None = None
    balance: int = 0
    is_operator: bool = False


class AuthorizationIn(BaseModel):
    """Body of ``POST /authorizations``.

    Deliberately permissive: the field types are loose and the handler does the
    validating. ``stage-2/SPEC.md`` requires ``422 validation_failed`` for a bad
    ``amount``/``note``/``visibility``, but a pydantic constraint failure would
    surface as the shared ``validation_error`` code that the accepted Stage-1
    contract pins. Validating by hand keeps the two vocabularies apart.

    Both ``to`` (the team's Stage-1 convention) and ``to_handle`` (the spec's
    spelling) are accepted on input.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    to: str | None = Field(default=None)
    amount: Any = None
    note: str | None = None
    visibility: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _accept_to_handle(cls, data: Any) -> Any:
        if isinstance(data, dict) and data.get("to") is None and data.get("to_handle"):
            data = {**data, "to": data["to_handle"]}
        return data


class FixtureIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    users: list[FixtureUser] = Field(default_factory=list)
    balances: dict[str, int] = Field(default_factory=dict)
    seeded_total: int | None = None
    total: int | None = None
    #: stage-2: seeded open authorizations. These are held against the seeded
    #: balances, so a fixture that over-holds is rejected 422
    #: validation_failed and the live state is left untouched.
    authorizations: list[dict[str, Any]] = Field(default_factory=list)
    #: stage-2: service-wide default authorization lifetime. Positive integer
    #: seconds; defaults to 600 when omitted. ``StrictInt`` so that
    #: ``"600"``/``600.0``/``True`` are rejected 422 rather than coerced - the
    #: same strictness the Stage-1 amount models already use.
    authorization_ttl_seconds: StrictInt | None = None
    #: stage-2 display metadata. Additive only - the accepted Stage-1 contract
    #: has no currency model, so these default to the spec's example values and
    #: are carried through untouched.
    currency: str | None = None
    minor_units: int | None = None


def to_fixture_dict(payload: Any) -> dict[str, Any]:
    """Normalise a reset/import body into a plain dict for the store."""
    if payload is None:
        return {}
    if isinstance(payload, dict):
        return payload
    if hasattr(payload, "model_dump"):
        return payload.model_dump(by_alias=True, exclude_none=False)
    raise ValueError("fixture must be a JSON object")
