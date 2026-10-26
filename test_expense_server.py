"""End-to-end check of every expense tool against a throwaway MongoDB database.

Run: python test_expense_server.py
"""

import asyncio
import os

os.environ["MONGODB_DB"] = "expense_tracker_test"
os.environ["EXPENSE_USER_ID"] = "local-test-user"  # stdio-style single user

from fastmcp import Client
from fastmcp.exceptions import ToolError

from expense_server import mcp
from expenses import db, users


async def call(client, tool, **args):
    return (await client.call_tool(tool, args)).structured_content


async def main():
    await db.client().drop_database("expense_tracker_test")
    try:
        await db.init_db()
        await users.create_user("local@test.dev", "Local Tester", "local-password",
                                user_id="local-test-user")
        async with Client(mcp) as client:
            tools = sorted(t.name for t in await client.list_tools())
            print("tools:", tools)
            assert len(tools) == 11
            assert (await call(client, "whoami"))["user_id"] == "local-test-user"

            lunch = await call(client, "add_expense", expense={
                "amount": 250, "description": "Lunch", "category": "Food",
                "payment_method": "upi"})
            eid = lunch["expense_id"]
            print("add_expense ->", eid, lunch["amount"], lunch["category"], lunch["date"])

            bulk = await call(client, "bulk_add_expenses", expenses=[
                {"amount": 449, "description": "Dinner order", "merchant": "Swiggy", "category": "food"},
                {"amount": 1200, "description": "Groceries", "merchant": "BigBasket"},
                {"amount": 199, "description": "Snacks", "merchant": "swiggy instamart"},
                {"amount": 1000, "description": "Trip snacks and cab", "date": "2026-10-01"},
                {"amount": 600, "description": "Movie", "category": "entertainment", "tags": ["Weekend"]},
            ])
            print("bulk_add_expenses ->", bulk)

            one = await call(client, "get_expense_by_id", expense_id=eid)
            assert one["description"] == "Lunch"

            upd = await call(client, "update_expense", expense_id=eid, changes={"amount": 300})
            print("update_expense ->", upd["amount"])
            assert upd["amount"] == 300 and upd["category"] == "food"

            groceries_id = bulk["expense_ids"][1]
            cat = await call(client, "categorize_expense", expense_id=groceries_id, category="Groceries")
            assert cat["category"] == "groceries"

            trip_id = bulk["expense_ids"][3]
            split = await call(client, "split_expense", expense_id=trip_id, split_type="category",
                               parts=[{"category": "food"}, {"category": "travel"}])
            print("split_expense (category) ->", split["split"])

            dinner_id = bulk["expense_ids"][0]
            split = await call(client, "split_expense", expense_id=dinner_id, split_type="person",
                               parts=[{"person": "me"}, {"person": "Rahul"}, {"person": "Amit"}])
            print("split_expense (person) ->", [p["amount"] for p in split["split"]["parts"]])
            assert round(sum(p["amount"] for p in split["split"]["parts"]), 2) == 449

            upd = await call(client, "update_expense", expense_id=dinner_id, changes={"amount": 500})
            print("rescaled split ->", [p["amount"] for p in upd["split"]["parts"]])

            swiggy = await call(client, "search_expenses", merchant="swiggy")
            print("search swiggy ->", swiggy["total_matching"], swiggy["total_amount"])
            assert swiggy["total_matching"] == 2

            assert (await call(client, "search_expenses", category="travel"))["total_matching"] == 1
            assert (await call(client, "search_expenses", person="rahul"))["total_matching"] == 1
            assert (await call(client, "search_expenses", tags=["weekend"]))["total_matching"] == 1
            assert (await call(client, "search_expenses", min_amount=500, max_amount=1000))["total_matching"] == 3
            assert (await call(client, "search_expenses", end_date="2026-10-01"))["total_matching"] == 1

            listing = await call(client, "get_expenses", limit=3, sort_by="amount")
            print("get_expenses top 3 by amount ->",
                  [(e["expense_id"], e["amount"]) for e in listing["expenses"]],
                  "of", listing["total_matching"])

            today = await call(client, "get_recent_expenses", period="today")
            print("get_recent_expenses today ->", today["count"], today["total_amount"], today["by_category"])
            assert today["count"] == 5  # the 2026-10-01 one is excluded

            deleted = await call(client, "delete_expense", expense_id=eid)
            print("delete_expense ->", deleted["expense_id"])

            for tool, args in [
                ("get_expense_by_id", {"expense_id": eid}),
                ("split_expense", {"expense_id": groceries_id, "split_type": "category",
                                   "parts": [{"category": "a", "amount": 100}, {"category": "b", "amount": 100}]}),
                ("add_expense", {"expense": {"amount": -5, "description": "bad"}}),
            ]:
                try:
                    await client.call_tool(tool, args)
                    raise AssertionError(f"{tool} should have failed")
                except ToolError as e:
                    print(f"{tool} rejected as expected: {str(e).splitlines()[0][:90]}")

        print("\nALL CHECKS PASSED")
    finally:
        await db.client().drop_database("expense_tracker_test")
        await db.close_db()


if __name__ == "__main__":
    asyncio.run(main())
