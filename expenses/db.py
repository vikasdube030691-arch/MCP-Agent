"""MongoDB connection, collection schema, indexes and the per-user repository."""

from typing import Any

from pymongo import ASCENDING, DESCENDING, AsyncMongoClient, ReturnDocument
from pymongo.asynchronous.collection import AsyncCollection
from pymongo.errors import CollectionInvalid

from . import config

EXPENSES = "expenses"
COUNTERS = "counters"

_nullable_string = {"bsonType": ["string", "null"]}

# Server-side guard so documents written outside this app still have the right shape.
EXPENSE_JSON_SCHEMA = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": ["user_id", "expense_id", "amount", "description", "currency", "date",
                     "created_at", "updated_at"],
        "properties": {
            "user_id": {"bsonType": "string", "minLength": 1},
            "expense_id": {"bsonType": ["int", "long"], "minimum": 1},
            "amount": {"bsonType": ["double", "int", "long", "decimal"], "minimum": 0,
                       "exclusiveMinimum": True},
            "description": {"bsonType": "string", "minLength": 1},
            "category": _nullable_string,
            "merchant": _nullable_string,
            "payment_method": {"enum": [None, "cash", "upi", "card", "net_banking",
                                        "wallet", "other"]},
            "currency": {"bsonType": "string", "minLength": 3, "maxLength": 3},
            "date": {"bsonType": "date"},
            "tags": {"bsonType": "array", "items": {"bsonType": "string"}},
            "notes": _nullable_string,
            "split": {
                "bsonType": ["object", "null"],
                "required": ["type", "parts"],
                "properties": {
                    "type": {"enum": ["category", "person"]},
                    "parts": {
                        "bsonType": "array",
                        "minItems": 2,
                        "items": {
                            "bsonType": "object",
                            "required": ["amount"],
                            "properties": {
                                "category": _nullable_string,
                                "person": _nullable_string,
                                "amount": {"bsonType": ["double", "int", "long", "decimal"]},
                                "settled": {"bsonType": "bool"},
                            },
                        },
                    },
                },
            },
            "created_at": {"bsonType": "date"},
            "updated_at": {"bsonType": "date"},
        },
    }
}

_client: AsyncMongoClient | None = None


def client() -> AsyncMongoClient:
    global _client
    if _client is None:
        _client = AsyncMongoClient(config.MONGODB_URI, tz_aware=True,
                                   serverSelectionTimeoutMS=5000)
    return _client


def _db():
    return client()[config.MONGODB_DB]


async def init_db() -> None:
    """Create the collection with its validator and indexes. Safe to run repeatedly."""
    db = _db()
    try:
        await db.create_collection(EXPENSES, validator=EXPENSE_JSON_SCHEMA)
    except CollectionInvalid:
        await db.command("collMod", EXPENSES, validator=EXPENSE_JSON_SCHEMA)

    if COUNTERS not in await db.list_collection_names():
        await db.create_collection(COUNTERS)

    coll = db[EXPENSES]
    # Ids are numbered per user, so the old globally-unique index must go.
    if "expense_id_1" in await coll.index_information():
        await coll.drop_index("expense_id_1")

    # Every index leads with user_id: each query is scoped to one user.
    await coll.create_index([("user_id", ASCENDING), ("expense_id", ASCENDING)], unique=True)
    await coll.create_index([("user_id", ASCENDING), ("date", DESCENDING)])
    await coll.create_index([("user_id", ASCENDING), ("category", ASCENDING), ("date", DESCENDING)])
    await coll.create_index([("user_id", ASCENDING), ("merchant", ASCENDING), ("date", DESCENDING)])
    await coll.create_index([("user_id", ASCENDING), ("split.parts.person", ASCENDING)])


async def close_db() -> None:
    global _client
    if _client is not None:
        await _client.close()
        _client = None


def _strip(doc: dict[str, Any] | None) -> dict[str, Any] | None:
    if doc is None:
        return None
    return {k: v for k, v in doc.items() if k not in ("_id", "user_id")}


class ExpenseRepository:
    """All expense reads and writes for one user.

    Tools only reach MongoDB through this class, and every method forces the
    user_id filter, so one user's expenses can't be read or changed by another.
    Returned documents have ``_id`` and ``user_id`` removed.
    """

    def __init__(self, user_id: str):
        if not user_id:
            raise ValueError("user_id is required")
        self.user_id = user_id

    @property
    def _coll(self) -> AsyncCollection:
        return _db()[EXPENSES]

    def _scope(self, filt: dict[str, Any] | None = None) -> dict[str, Any]:
        # user_id is set last so a caller-supplied filter can never override it.
        return {**(filt or {}), "user_id": self.user_id}

    async def next_ids(self, count: int = 1) -> list[int]:
        """Reserve ``count`` consecutive expense ids for this user, atomically."""
        doc = await _db()[COUNTERS].find_one_and_update(
            {"_id": {"name": "expense_id", "user_id": self.user_id}},
            {"$inc": {"seq": count}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        last = doc["seq"]
        return list(range(last - count + 1, last + 1))

    async def insert_many(self, docs: list[dict[str, Any]]) -> None:
        await self._coll.insert_many([{**d, "user_id": self.user_id} for d in docs], ordered=True)

    async def get(self, expense_id: int) -> dict[str, Any] | None:
        return _strip(await self._coll.find_one(self._scope({"expense_id": expense_id})))

    async def find(
        self, filt: dict[str, Any], *, sort: list[tuple[str, int]], skip: int, limit: int
    ) -> list[dict[str, Any]]:
        cursor = self._coll.find(self._scope(filt)).sort(sort).skip(skip).limit(limit)
        return [_strip(d) async for d in cursor]

    async def aggregate(self, filt: dict[str, Any], stages: list[dict[str, Any]]) -> list[dict]:
        pipeline = [{"$match": self._scope(filt)}, *stages]
        return await (await self._coll.aggregate(pipeline)).to_list()

    async def update(self, expense_id: int, fields: dict[str, Any]) -> dict[str, Any] | None:
        fields = {k: v for k, v in fields.items() if k not in ("_id", "user_id")}
        doc = await self._coll.find_one_and_update(
            self._scope({"expense_id": expense_id}),
            {"$set": fields},
            return_document=ReturnDocument.AFTER,
        )
        return _strip(doc)

    async def delete(self, expense_id: int) -> dict[str, Any] | None:
        return _strip(await self._coll.find_one_and_delete(self._scope({"expense_id": expense_id})))
