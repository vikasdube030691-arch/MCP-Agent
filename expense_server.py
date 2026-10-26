"""Expense tracker MCP server built with FastMCP, backed by MongoDB."""

import re
from contextlib import asynccontextmanager
from datetime import UTC, datetime, time, timedelta
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse

from expenses import config, db, tokens, users
from expenses.auth import build_auth, current_user_id, uses_external_auth
from expenses.config import TIMEZONE
from expenses.db import ExpenseRepository
from expenses.models import (
    BulkAddResult,
    Expense,
    ExpenseCreate,
    ExpenseList,
    ExpenseUpdate,
    PaymentMethod,
    Split,
    SplitPart,
    SplitType,
)

ExpenseId = Annotated[int, Field(ge=1, description="Numeric expense id, e.g. 102")]
Period = Literal["today", "yesterday", "this_week", "last_7_days", "this_month", "last_30_days"]


@asynccontextmanager
async def lifespan(server: FastMCP):
    await db.init_db()
    try:
        yield {}
    finally:
        await db.close_db()


mcp = FastMCP(
    "expense-tracker",
    lifespan=lifespan,
    auth=build_auth(),
    instructions=(
        "Tracks personal expenses for the signed-in user; each user only ever sees their own. "
        "Amounts are in the expense's currency (default INR). "
        "To edit, delete, categorize or split an expense the user describes in words "
        "(e.g. 'the lunch expense'), find its expense_id with search_expenses first."
    ),
)


# ---------- helpers ----------

async def _repo() -> ExpenseRepository:
    """Data access scoped to the authenticated caller."""
    return ExpenseRepository(await current_user_id())


def _now() -> datetime:
    return datetime.now(UTC)


def _to_utc(dt: datetime) -> datetime:
    """Treat naive datetimes as local time and store everything in UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TIMEZONE)
    return dt.astimezone(UTC)


def _money(x: float) -> float:
    return round(x + 0.0, 2)


def _to_expense(doc: dict[str, Any]) -> Expense:
    doc = {k: v for k, v in doc.items() if k not in ("_id", "user_id")}
    for key in ("date", "created_at", "updated_at"):
        doc[key] = doc[key].astimezone(TIMEZONE)
    return Expense.model_validate(doc)


def _new_doc(data: ExpenseCreate, expense_id: int, now: datetime) -> dict[str, Any]:
    doc = data.model_dump()
    doc.update(
        expense_id=expense_id,
        amount=_money(data.amount),
        date=_to_utc(data.date) if data.date else now,
        split=None,
        created_at=now,
        updated_at=now,
    )
    return doc


# Another user's expense id gets the same "not found" as a missing one, so ids don't leak.
async def _get_doc(repo: ExpenseRepository, expense_id: int) -> dict[str, Any]:
    doc = await repo.get(expense_id)
    if doc is None:
        raise ToolError(f"Expense {expense_id} not found")
    return doc


async def _set_fields(repo: ExpenseRepository, expense_id: int, fields: dict[str, Any]) -> Expense:
    fields["updated_at"] = _now()
    doc = await repo.update(expense_id, fields)
    if doc is None:
        raise ToolError(f"Expense {expense_id} not found")
    return _to_expense(doc)


async def _list(
    repo: ExpenseRepository,
    filt: dict[str, Any],
    *,
    limit: int,
    skip: int = 0,
    sort: list[tuple[str, int]] | None = None,
) -> ExpenseList:
    docs = await repo.find(filt, sort=sort or [("date", -1), ("expense_id", -1)],
                           skip=skip, limit=limit)
    expenses = [_to_expense(d) for d in docs]

    totals = await repo.aggregate(filt, [
        {"$group": {"_id": {"$ifNull": ["$category", "uncategorized"]},
                    "total": {"$sum": "$amount"}, "n": {"$sum": 1}}},
    ])
    by_category = {t["_id"]: _money(t["total"]) for t in sorted(totals, key=lambda t: -t["total"])}

    return ExpenseList(
        expenses=expenses,
        count=len(expenses),
        total_matching=sum(t["n"] for t in totals),
        total_amount=_money(sum(t["total"] for t in totals)),
        by_category=by_category,
    )


def _resolve_split(total: float, parts: list[SplitPart]) -> list[SplitPart]:
    """Fill in missing share amounts and check the shares add up to the total."""
    given = sum(p.amount for p in parts if p.amount is not None)
    missing = [p for p in parts if p.amount is None]

    if missing:
        remaining = _money(total - given)
        if remaining <= 0:
            raise ToolError(f"Shares already add up to {given}, nothing left for the others")
        each = _money(remaining / len(missing))
        for p in missing:
            p.amount = each
        missing[0].amount = _money(each + remaining - each * len(missing))  # rounding remainder

    if abs(sum(p.amount for p in parts) - total) > 0.01:
        raise ToolError(
            f"Shares add up to {_money(sum(p.amount for p in parts))} but the expense is {total}"
        )
    return parts


def _period_range(period: Period) -> tuple[datetime, datetime]:
    today = datetime.combine(datetime.now(TIMEZONE).date(), time.min, tzinfo=TIMEZONE)
    tomorrow = today + timedelta(days=1)
    match period:
        case "today":
            return today, tomorrow
        case "yesterday":
            return today - timedelta(days=1), today
        case "this_week":
            return today - timedelta(days=today.weekday()), tomorrow
        case "last_7_days":
            return today - timedelta(days=6), tomorrow
        case "this_month":
            return today.replace(day=1), tomorrow
        case "last_30_days":
            return today - timedelta(days=29), tomorrow


# ---------- login ----------

@mcp.custom_route("/auth/login", methods=["POST"])
async def login(request: Request) -> JSONResponse:
    """Exchange {"email", "password"} for a bearer token to use with the MCP endpoint."""
    if uses_external_auth():
        return JSONResponse({"error": "login is handled by the identity provider"}, status_code=404)
    try:
        body = await request.json()
        email, password = body["email"], body["password"]
        if not isinstance(email, str) or not isinstance(password, str):
            raise TypeError
    except (ValueError, KeyError, TypeError):
        return JSONResponse({"error": "send JSON with string fields email and password"},
                            status_code=400)

    user = await users.authenticate(email, password)
    if user is None:
        # One message for every failure, so it doesn't reveal which emails exist.
        return JSONResponse(
            {"error": "invalid email or password, or the account is locked or disabled"},
            status_code=401,
        )
    token, expires_in = tokens.issue_token(user["_id"], user["token_version"])
    return JSONResponse({"access_token": token, "token_type": "Bearer", "expires_in": expires_in})


# ---------- tools ----------

@mcp.tool
async def whoami() -> dict:
    """The signed-in user's id, name and email."""
    user = await users.get_user(await current_user_id())
    return {"user_id": user["_id"], "name": user["name"], "email": user["email"]}


@mcp.tool
async def add_expense(expense: ExpenseCreate) -> Expense:
    """Add a new expense, e.g. 'I spent ₹250 on lunch'."""
    repo = await _repo()
    [expense_id] = await repo.next_ids()
    doc = _new_doc(expense, expense_id, _now())
    await repo.insert_many([doc])
    return _to_expense(doc)


@mcp.tool
async def get_expenses(
    limit: Annotated[int, Field(ge=1, le=200)] = 20,
    skip: Annotated[int, Field(ge=0, description="Number of expenses to skip, for paging")] = 0,
    category: str | None = None,
    sort_by: Literal["date", "amount"] = "date",
    order: Literal["desc", "asc"] = "desc",
) -> ExpenseList:
    """List expenses, newest first by default, with paging. 'Show my recent expenses'."""
    filt = {"category": category.strip().lower()} if category else {}
    direction = -1 if order == "desc" else 1
    return await _list(await _repo(), filt, limit=limit, skip=skip,
                       sort=[(sort_by, direction), ("expense_id", direction)])


@mcp.tool
async def get_expense_by_id(expense_id: ExpenseId) -> Expense:
    """Get one expense by its id, e.g. 'Show expense 102'."""
    return _to_expense(await _get_doc(await _repo(), expense_id))


@mcp.tool
async def update_expense(expense_id: ExpenseId, changes: ExpenseUpdate) -> Expense:
    """Edit an expense. Only the fields given in `changes` are modified.
    e.g. 'Change lunch expense to ₹300'. If the amount changes on a split expense,
    the shares are scaled to match."""
    repo = await _repo()
    fields = changes.model_dump(exclude_unset=True)
    if "date" in fields:
        fields["date"] = _to_utc(fields["date"]) if fields["date"] else _now()
    if "amount" in fields:
        fields["amount"] = _money(fields["amount"])
        doc = await _get_doc(repo, expense_id)
        if doc.get("split"):
            ratio = fields["amount"] / doc["amount"]
            parts = doc["split"]["parts"]
            for p in parts[1:]:
                p["amount"] = _money(p["amount"] * ratio)
            parts[0]["amount"] = _money(fields["amount"] - sum(p["amount"] for p in parts[1:]))
            fields["split.parts"] = parts
    return await _set_fields(repo, expense_id, fields)


@mcp.tool
async def delete_expense(expense_id: ExpenseId) -> Expense:
    """Delete an expense permanently and return what was deleted, e.g. 'Remove expense 102'."""
    doc = await (await _repo()).delete(expense_id)
    if doc is None:
        raise ToolError(f"Expense {expense_id} not found")
    return _to_expense(doc)


@mcp.tool
async def search_expenses(
    query: Annotated[str | None, Field(
        description="Free text matched against description, merchant and notes"
    )] = None,
    merchant: Annotated[str | None, Field(description="e.g. Swiggy (partial, case-insensitive)")] = None,
    category: str | None = None,
    payment_method: PaymentMethod | None = None,
    min_amount: Annotated[float | None, Field(ge=0)] = None,
    max_amount: Annotated[float | None, Field(ge=0)] = None,
    start_date: Annotated[datetime | None, Field(description="Inclusive, ISO date or datetime")] = None,
    end_date: Annotated[datetime | None, Field(
        description="Inclusive; a bare date covers that whole day"
    )] = None,
    tags: Annotated[list[str] | None, Field(description="Expenses having all of these tags")] = None,
    person: Annotated[str | None, Field(description="Someone the expense was split with")] = None,
    limit: Annotated[int, Field(ge=1, le=200)] = 50,
) -> ExpenseList:
    """Search expenses with any combination of filters, e.g. 'Find all Swiggy payments'.
    The totals cover every match, not just the returned page."""
    conditions: list[dict[str, Any]] = []

    if query:
        rx = {"$regex": re.escape(query), "$options": "i"}
        conditions.append({"$or": [{"description": rx}, {"merchant": rx}, {"notes": rx}]})
    if merchant:
        rx = {"$regex": re.escape(merchant), "$options": "i"}
        conditions.append({"$or": [{"merchant": rx}, {"description": rx}]})
    if category:
        cat = category.strip().lower()
        conditions.append({"$or": [{"category": cat}, {"split.parts.category": cat}]})
    if payment_method:
        conditions.append({"payment_method": payment_method.value})
    if min_amount is not None or max_amount is not None:
        rng = {}
        if min_amount is not None:
            rng["$gte"] = min_amount
        if max_amount is not None:
            rng["$lte"] = max_amount
        conditions.append({"amount": rng})
    if start_date or end_date:
        rng = {}
        if start_date:
            rng["$gte"] = _to_utc(start_date)
        if end_date:
            if end_date.time() == time.min:  # a bare date: include the whole day
                rng["$lt"] = _to_utc(end_date + timedelta(days=1))
            else:
                rng["$lte"] = _to_utc(end_date)
        conditions.append({"date": rng})
    if tags:
        conditions.append({"tags": {"$all": [t.strip().lower() for t in tags]}})
    if person:
        conditions.append({"split.parts.person": {"$regex": f"^{re.escape(person)}$", "$options": "i"}})

    filt = {"$and": conditions} if conditions else {}
    return await _list(await _repo(), filt, limit=limit)


@mcp.tool
async def bulk_add_expenses(
    expenses: Annotated[list[ExpenseCreate], Field(min_length=1, max_length=500)],
) -> BulkAddResult:
    """Add many expenses at once, e.g. 'Import these five transactions'."""
    repo = await _repo()
    now = _now()
    ids = await repo.next_ids(len(expenses))
    docs = [_new_doc(e, i, now) for e, i in zip(expenses, ids)]
    await repo.insert_many(docs)
    return BulkAddResult(
        inserted=len(docs),
        expense_ids=ids,
        total_amount=_money(sum(d["amount"] for d in docs)),
    )


@mcp.tool
async def categorize_expense(
    expense_id: ExpenseId,
    category: Annotated[str, Field(min_length=1, description="e.g. groceries, food, travel")],
) -> Expense:
    """Assign a category to an expense, e.g. 'Mark this as groceries'."""
    return await _set_fields(await _repo(), expense_id, {"category": category.strip().lower()})


@mcp.tool
async def split_expense(
    expense_id: ExpenseId,
    split_type: Annotated[SplitType, Field(
        description="'category' to divide the cost across categories, "
                    "'person' to share it with people"
    )],
    parts: Annotated[list[SplitPart], Field(min_length=2, description=(
        "The shares. Give each a category (category split) or person (person split). "
        "Amounts may be left out to split the rest equally."
    ))],
) -> Expense:
    """Split an expense between categories or people, replacing any earlier split.
    e.g. 'Split ₹1,000 between food and travel' or 'Split dinner equally with Rahul and me'."""
    repo = await _repo()
    doc = await _get_doc(repo, expense_id)
    key = "category" if split_type is SplitType.CATEGORY else "person"
    for p in parts:
        value = getattr(p, key)
        if not value or not value.strip():
            raise ToolError(f"Every share needs a {key} for a {split_type.value} split")
        setattr(p, key, value.strip().lower() if key == "category" else value.strip())

    parts = _resolve_split(doc["amount"], parts)
    split = Split(type=split_type, parts=parts)
    return await _set_fields(repo, expense_id, {"split": split.model_dump(mode="json")})


@mcp.tool
async def get_recent_expenses(
    period: Annotated[Period, Field(description="Time window, in the configured timezone")] = "today",
    limit: Annotated[int, Field(ge=1, le=200)] = 50,
) -> ExpenseList:
    """Latest expenses in a time window with totals, e.g. 'What did I spend today?'."""
    start, end = _period_range(period)
    result = await _list(await _repo(), {"date": {"$gte": _to_utc(start), "$lt": _to_utc(end)}},
                         limit=limit)
    result.period_start, result.period_end = start, end
    return result


if __name__ == "__main__":
    if config.MCP_TRANSPORT == "http":
        # Every MCP request needs a bearer token: see build_auth().
        mcp.run(transport="http", host=config.MCP_HOST, port=config.MCP_PORT)
    else:
        mcp.run()
