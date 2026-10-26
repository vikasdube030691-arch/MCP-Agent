"""Checks that users can't see or touch each other's expenses over authenticated HTTP.

Starts the server on a local port with a throwaway RSA key, then connects as two users.
Run: python test_user_isolation.py
"""

import asyncio
import os
import socket

from fastmcp.server.auth.providers.jwt import RSAKeyPair

KEY = RSAKeyPair.generate()
ISSUER, AUDIENCE = "https://test-issuer.local", "expense-tracker"

os.environ.update(
    MONGODB_DB="expense_tracker_isolation_test",
    AUTH_PUBLIC_KEY=KEY.public_key,
    AUTH_ISSUER=ISSUER,
    AUTH_AUDIENCE=AUDIENCE,
)
os.environ.pop("EXPENSE_USER_ID", None)

import uvicorn
from fastmcp import Client
from fastmcp.exceptions import ToolError

from expense_server import mcp
from expenses import db


def token_for(user: str, issuer: str = ISSUER, key: RSAKeyPair = KEY) -> str:
    return key.create_token(subject=user, issuer=issuer, audience=AUDIENCE)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def call(client, tool, **args):
    return (await client.call_tool(tool, args)).structured_content


async def expect_error(client, tool, **args) -> str:
    try:
        await client.call_tool(tool, args)
    except ToolError as e:
        return str(e)
    raise AssertionError(f"{tool}({args}) should have failed")


async def expect_rejected_connection(url: str, auth: str | None, label: str):
    try:
        async with Client(url, auth=auth) as c:
            await c.list_tools()
    except Exception as e:
        print(f"  {label}: rejected ({type(e).__name__})")
        return
    raise AssertionError(f"{label} should have been rejected")


async def main():
    port = free_port()
    url = f"http://127.0.0.1:{port}/mcp"
    server = uvicorn.Server(uvicorn.Config(mcp.http_app(), port=port, log_level="warning"))
    serve_task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)

    try:
        print("Auth checks")
        await expect_rejected_connection(url, None, "no token")
        await expect_rejected_connection(url, "not-a-jwt", "garbage token")
        await expect_rejected_connection(
            url, token_for("alice", key=RSAKeyPair.generate()), "token signed by another key")
        await expect_rejected_connection(
            url, token_for("alice", issuer="https://evil.local"), "token from wrong issuer")

        async with (
            Client(url, auth=token_for("alice")) as alice,
            Client(url, auth=token_for("bob")) as bob,
        ):
            print("Setup")
            a1 = await call(alice, "add_expense", expense={
                "amount": 250, "description": "Alice lunch", "merchant": "Swiggy", "category": "food"})
            b = await call(bob, "bulk_add_expenses", expenses=[
                {"amount": 999, "description": "Bob dinner", "merchant": "Swiggy", "category": "food"},
                {"amount": 5000, "description": "Bob rent", "category": "bills"},
            ])
            print(f"  alice ids: [{a1['expense_id']}]  bob ids: {b['expense_ids']}")
            assert a1["expense_id"] == 1 and b["expense_ids"] == [1, 2], "ids are numbered per user"
            assert "user_id" not in a1, "user_id must not be exposed"

            print("Reads are isolated")
            assert (await call(alice, "get_expense_by_id", expense_id=1))["description"] == "Alice lunch"
            assert (await call(bob, "get_expense_by_id", expense_id=1))["description"] == "Bob dinner"
            print("  alice get #2 (bob's rent):", await expect_error(alice, "get_expense_by_id", expense_id=2))

            listing = await call(alice, "get_expenses")
            assert [e["description"] for e in listing["expenses"]] == ["Alice lunch"]
            assert listing["total_amount"] == 250

            swiggy = await call(alice, "search_expenses", merchant="swiggy")
            assert swiggy["total_matching"] == 1 and swiggy["total_amount"] == 250
            assert (await call(alice, "search_expenses", category="bills"))["total_matching"] == 0

            today = await call(alice, "get_recent_expenses", period="today")
            assert today["total_amount"] == 250 and today["by_category"] == {"food": 250}
            print("  get_expenses / search_expenses / get_recent_expenses: only alice's data")

            print("Writes are isolated")
            for tool, args in [
                ("update_expense", {"expense_id": 2, "changes": {"amount": 1}}),
                ("categorize_expense", {"expense_id": 2, "category": "hacked"}),
                ("split_expense", {"expense_id": 2, "split_type": "person",
                                   "parts": [{"person": "x"}, {"person": "y"}]}),
                ("delete_expense", {"expense_id": 2}),
            ]:
                print(f"  alice {tool} #2:", await expect_error(alice, tool, **args))

            # Alice editing her own #1 must not touch Bob's #1.
            await call(alice, "update_expense", expense_id=1, changes={"amount": 300})
            await call(alice, "delete_expense", expense_id=1)

            bob_view = await call(bob, "get_expenses")
            assert sorted((e["expense_id"], e["amount"], e["category"]) for e in bob_view["expenses"]) \
                == [(1, 999, "food"), (2, 5000, "bills")], bob_view
            assert all(e["split"] is None for e in bob_view["expenses"])
            print("  bob's expenses untouched:",
                  sorted((e["expense_id"], e["amount"]) for e in bob_view["expenses"]))

            # Smuggling user_id through tool input is rejected by the schemas.
            msg = await expect_error(alice, "add_expense", expense={
                "amount": 1, "description": "x", "user_id": "bob"})
            print("  alice add_expense with user_id='bob':", msg.splitlines()[0][:70])
            msg = await expect_error(bob, "update_expense", expense_id=1,
                                     changes={"user_id": "alice"})
            print("  bob update_expense setting user_id='alice':", msg.splitlines()[0][:70])
            assert (await call(bob, "get_expenses"))["total_matching"] == 2
            assert (await call(alice, "get_expenses"))["total_matching"] == 0

        print("\nALL ISOLATION CHECKS PASSED")
    finally:
        server.should_exit = True
        await serve_task
        await db.client().drop_database("expense_tracker_isolation_test")
        await db.close_db()


if __name__ == "__main__":
    asyncio.run(main())
