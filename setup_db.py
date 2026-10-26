"""Create the expense tracker database, collections, validator and indexes.

Safe to re-run: existing collections and data are kept, and the schema and
indexes are brought up to date. The server also does this on startup.

Run: python setup_db.py
"""

import asyncio

from expenses import config, db


async def main():
    await db.init_db()

    database = db.client()[config.MONGODB_DB]
    print(f"Database: {config.MONGODB_DB}  ({config.MONGODB_URI})")

    infos = {c["name"]: c async for c in await database.list_collections()}
    for name in (db.USERS, db.EXPENSES, db.COUNTERS):
        coll = database[name]
        count = await coll.count_documents({})
        validator = infos[name].get("options", {}).get("validator")
        print(f"\nCollection: {name}  ({count} documents)")
        if validator:
            schema = validator["$jsonSchema"]
            print(f"  validator: required = {schema['required']}")
        for index_name, info in (await coll.index_information()).items():
            keys = ", ".join(f"{k} {'asc' if d == 1 else 'desc'}" for k, d in info["key"])
            print(f"  index {index_name}: {keys}{'  [unique]' if info.get('unique') else ''}")

    await db.close_db()


if __name__ == "__main__":
    asyncio.run(main())
