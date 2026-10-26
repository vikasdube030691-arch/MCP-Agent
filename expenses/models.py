"""Entity schemas for expenses.

Pydantic models validate tool input and describe tool output. The matching
MongoDB-side validator lives in ``db.EXPENSE_JSON_SCHEMA``.
"""

from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .config import DEFAULT_CURRENCY

Amount = Annotated[float, Field(gt=0, description="Amount spent, e.g. 250 for ₹250")]


def _clean_category(v: str | None) -> str | None:
    return v.strip().lower() if v else v


def _clean_currency(v: str | None) -> str | None:
    return v.upper() if v else v


def _clean_tags(v: list[str] | None) -> list[str] | None:
    return sorted({t.strip().lower() for t in v if t.strip()}) if v is not None else v


class PaymentMethod(StrEnum):
    CASH = "cash"
    UPI = "upi"
    CARD = "card"
    NET_BANKING = "net_banking"
    WALLET = "wallet"
    OTHER = "other"


class SplitType(StrEnum):
    CATEGORY = "category"
    PERSON = "person"


class _Input(BaseModel):
    """Tool input: unknown fields (e.g. a smuggled user_id) are rejected, not ignored."""

    model_config = ConfigDict(extra="forbid")


class SplitPart(_Input):
    """One share of a split expense."""

    category: str | None = Field(None, description="Category for this share (category splits)")
    person: str | None = Field(None, description="Who owes this share (person splits)")
    amount: float | None = Field(
        None, gt=0, description="Share amount. Leave every share empty to split equally."
    )
    settled: bool = Field(False, description="Whether this person has paid back their share")


class ExpenseCreate(_Input):
    """Fields accepted when adding an expense."""

    amount: Amount
    description: str = Field(..., min_length=1, description="What the money was spent on, e.g. 'Lunch'")
    category: str | None = Field(None, description="e.g. food, groceries, travel, bills")
    merchant: str | None = Field(None, description="Who was paid, e.g. Swiggy, Amazon")
    payment_method: PaymentMethod | None = None
    currency: str = Field(DEFAULT_CURRENCY, min_length=3, max_length=3)
    date: datetime | None = Field(
        None, description="When it was spent (ISO date or datetime). Defaults to now."
    )
    tags: list[str] = Field(default_factory=list)
    notes: str | None = None

    _category = field_validator("category")(_clean_category)
    _currency = field_validator("currency")(_clean_currency)
    _tags = field_validator("tags")(_clean_tags)


class ExpenseUpdate(_Input):
    """Fields that can be changed on an existing expense. Omitted fields are left alone."""

    amount: float | None = Field(None, gt=0)
    description: str | None = Field(None, min_length=1)
    category: str | None = None
    merchant: str | None = None
    payment_method: PaymentMethod | None = None
    currency: str | None = Field(None, min_length=3, max_length=3)
    date: datetime | None = None
    tags: list[str] | None = None
    notes: str | None = None

    _category = field_validator("category")(_clean_category)
    _currency = field_validator("currency")(_clean_currency)
    _tags = field_validator("tags")(_clean_tags)

    @model_validator(mode="after")
    def _not_empty(self):
        if not self.model_fields_set:
            raise ValueError("Provide at least one field to update")
        return self


class Split(BaseModel):
    type: SplitType
    parts: list[SplitPart]


class Expense(ExpenseCreate):
    """An expense as stored and returned."""

    model_config = ConfigDict(extra="ignore")

    expense_id: int = Field(..., description="Short numeric id, e.g. 102")
    date: datetime
    split: Split | None = None
    created_at: datetime
    updated_at: datetime


class ExpenseList(BaseModel):
    expenses: list[Expense]
    count: int = Field(..., description="Number of expenses returned on this page")
    total_matching: int = Field(..., description="Number of expenses matching, ignoring the limit")
    total_amount: float = Field(..., description="Sum over all matching expenses, ignoring the limit")
    by_category: dict[str, float] = Field(
        default_factory=dict, description="Total of all matching expenses per category"
    )
    period_start: datetime | None = None
    period_end: datetime | None = None


class BulkAddResult(BaseModel):
    inserted: int
    expense_ids: list[int]
    total_amount: float
