"""Request and response bodies."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StringConstraints, field_validator

from app.auth import USER_ID_PATTERN

MAX_SEATS_PER_SHOW = 50_000

SeatLabel = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,15}$")]
SeatStatus = Literal["available", "held", "confirmed"]
UserId = Annotated[str, StringConstraints(pattern=USER_ID_PATTERN)]
# Printable ASCII without spaces, so a key reads the same in a header and in a JSON body.
IDEMPOTENCY_KEY_PATTERN = r"^[!-~]{1,128}$"
IdempotencyKey = Annotated[str, StringConstraints(pattern=IDEMPOTENCY_KEY_PATTERN)]


class TokenRequest(BaseModel):
    user_id: UserId


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int
    user_id: str


class CreateShowRequest(BaseModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    seats: Annotated[list[SeatLabel], Field(min_length=1, max_length=MAX_SEATS_PER_SHOW)]
    # StrictInt: money is integer paise. 25000.0 or "25000" is rejected, not coerced.
    price_paise: Annotated[StrictInt, Field(gt=0, le=10_000_000_00)]
    per_user_limit: Annotated[StrictInt, Field(ge=1, le=1000)] | None = None

    @field_validator("seats")
    @classmethod
    def _seats_are_unique(cls, seats: list[str]) -> list[str]:
        return _reject_duplicates(seats)


class ReserveRequest(BaseModel):
    # Extra fields are kept only so a spoofed "user_id" can be logged; identity comes from the
    # token and nothing here is ever used as one.
    model_config = ConfigDict(extra="allow")

    seats: Annotated[list[SeatLabel], Field(min_length=1, max_length=100)]
    idempotency_key: IdempotencyKey | None = None

    @field_validator("seats")
    @classmethod
    def _seats_are_unique(cls, seats: list[str]) -> list[str]:
        return _reject_duplicates(seats)


class ReservationOut(BaseModel):
    reservation_id: UUID
    show_id: UUID
    user_id: str
    seats: list[str]
    amount_paise: int
    status: Literal["confirmed", "cancelled"]
    created_at: datetime


class SeatOut(BaseModel):
    label: str
    status: SeatStatus


class SeatCounts(BaseModel):
    total: int
    available: int
    held: int
    confirmed: int


class ShowOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    name: str
    price_paise: int
    per_user_limit: int
    created_at: datetime
    counts: SeatCounts
    seats: list[SeatOut]


def _reject_duplicates(seats: list[str]) -> list[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for seat in seats:
        if seat in seen:
            duplicates.add(seat)
        seen.add(seat)
    if duplicates:
        raise ValueError(f"duplicate seats: {', '.join(sorted(duplicates)[:10])}")
    return seats
