import uuid

from pydantic import BaseModel, Field

from .config import DEFAULT_LIMIT

# ReserveIn accepts at most 50 seats, so this limit keeps every reservation
# total representable by Postgres BIGINT.
MAX_PRICE_PAISE = 9_223_372_036_854_775_807 // 50


class ShowIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    seats: list[str] = Field(min_length=1, max_length=100000)
    price_paise: int = Field(ge=0, le=MAX_PRICE_PAISE, strict=True)
    per_user_limit: int = Field(default=DEFAULT_LIMIT, ge=1, le=100)


class ReserveIn(BaseModel):
    seats: list[str] = Field(min_length=1, max_length=50)
    idempotency_key: str | None = Field(default=None, max_length=200)
    # any other body field (e.g. user_id) is ignored: identity is token-derived


def resv_json(r):
    return {
        "reservation_id": str(r["id"]),
        "show_id": str(r["show_id"]),
        "user_id": r["user_id"],
        "seats": list(r["seats"]),
        "amount_paise": r["amount_paise"],
        "status": r["status"],
    }


def valid_uuid(s):
    try:
        return uuid.UUID(s)
    except ValueError:
        return None
