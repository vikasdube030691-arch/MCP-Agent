"""Multi-user checks over authenticated HTTP: login, token validation, account control,
and that users can't see or touch each other's expenses.

Starts the server on a local port with a throwaway signing key and database.
Run: python test_user_isolation.py
"""

import asyncio
import os
import socket
import tempfile

os.environ.update(
    MONGODB_DB="expense_tracker_isolation_test",
    AUTH_KEY_DIR=tempfile.mkdtemp(prefix="expense-keys-"),
    AUTH_MAX_FAILED_LOGINS="3",
)
for var in ("EXPENSE_USER_ID", "AUTH_JWKS_URI", "AUTH_PUBLIC_KEY"):
    os.environ.pop(var, None)

import httpx
import uvicorn
from fastmcp import Client
from fastmcp.exceptions import ToolError
from fastmcp.server.auth.providers.jwt import RSAKeyPair

from expense_server import mcp
from expenses import config, db, tokens, users


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
        return str(e).splitlines()[0][:80]
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
    base = f"http://127.0.0.1:{port}"
    url = f"{base}/mcp"
    server = uvicorn.Server(uvicorn.Config(mcp.http_app(), port=port, log_level="warning"))
    serve_task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)

    http = httpx.AsyncClient(base_url=base)

    async def login(email: str, password: str) -> httpx.Response:
        return await http.post("/auth/login", json={"email": email, "password": password})

    async def token(email: str, password: str) -> str:
        r = await login(email, password)
        assert r.status_code == 200, r.text
        return r.json()["access_token"]

    try:
        alice = await users.create_user("alice@example.com", "Alice", "alice-pass-1")
        bob = await users.create_user("Bob@Example.com", "Bob", "bob-pass-1")
        carol = await users.create_user("carol@example.com", "Carol", "carol-pass-1")

        print("Login")
        r = await login("alice@example.com", "wrong-password")
        print("  wrong password:", r.status_code, r.json()["error"])
        assert r.status_code == 401
        r2 = await login("nobody@example.com", "whatever-pass")
        assert r2.status_code == 401 and r2.json() == r.json(), "unknown email looks the same"
        assert (await http.post("/auth/login", content=b"nope")).status_code == 400
        alice_token = await token("alice@example.com", "alice-pass-1")
        bob_token = await token("bob@example.com", "bob-pass-1")  # email is case-insensitive
        print("  alice and bob logged in")

        print("Token checks")
        await expect_rejected_connection(url, None, "no token")
        await expect_rejected_connection(url, "not-a-jwt", "garbage token")
        foreign = RSAKeyPair.generate().create_token(
            subject=alice["_id"], issuer=config.TOKEN_ISSUER, audience=config.TOKEN_AUDIENCE)
        await expect_rejected_connection(url, foreign, "token signed by another key")
        async with Client(url, auth=tokens.issue_token("u_not_registered", 0)[0]) as ghost:
            print("  token for unregistered user:", await expect_error(ghost, "get_expenses"))

        async with (
            Client(url, auth=alice_token) as a,
            Client(url, auth=bob_token) as b,
        ):
            assert (await call(a, "whoami"))["email"] == "alice@example.com"
            assert (await call(b, "whoami"))["email"] == "bob@example.com"

            print("Setup")
            a1 = await call(a, "add_expense", expense={
                "amount": 250, "description": "Alice lunch", "merchant": "Swiggy", "category": "food"})
            bb = await call(b, "bulk_add_expenses", expenses=[
                {"amount": 999, "description": "Bob dinner", "merchant": "Swiggy", "category": "food"},
                {"amount": 5000, "description": "Bob rent", "category": "bills"},
            ])
            assert a1["expense_id"] == 1 and bb["expense_ids"] == [1, 2], "ids are numbered per user"
            assert "user_id" not in a1

            print("Reads are isolated")
            assert (await call(a, "get_expense_by_id", expense_id=1))["description"] == "Alice lunch"
            assert (await call(b, "get_expense_by_id", expense_id=1))["description"] == "Bob dinner"
            print("  alice get #2 (bob's rent):", await expect_error(a, "get_expense_by_id", expense_id=2))
            listing = await call(a, "get_expenses")
            assert [e["description"] for e in listing["expenses"]] == ["Alice lunch"]
            swiggy = await call(a, "search_expenses", merchant="swiggy")
            assert swiggy["total_matching"] == 1 and swiggy["total_amount"] == 250
            assert (await call(a, "search_expenses", category="bills"))["total_matching"] == 0
            today = await call(a, "get_recent_expenses", period="today")
            assert today["by_category"] == {"food": 250}
            print("  list / search / recent: only alice's data")

            print("Writes are isolated")
            for tool, args in [
                ("update_expense", {"expense_id": 2, "changes": {"amount": 1}}),
                ("categorize_expense", {"expense_id": 2, "category": "hacked"}),
                ("split_expense", {"expense_id": 2, "split_type": "person",
                                   "parts": [{"person": "x"}, {"person": "y"}]}),
                ("delete_expense", {"expense_id": 2}),
            ]:
                print(f"  alice {tool} #2:", await expect_error(a, tool, **args))
            await call(a, "update_expense", expense_id=1, changes={"amount": 300})
            print("  alice add_expense with user_id='bob':", await expect_error(
                a, "add_expense", expense={"amount": 1, "description": "x", "user_id": "bob"}))
            bob_view = await call(b, "get_expenses")
            assert sorted((e["expense_id"], e["amount"], e["category"]) for e in bob_view["expenses"]) \
                == [(1, 999, "food"), (2, 5000, "bills")]
            print("  bob's expenses untouched")

            print("Account control")
            await users.set_active(bob["_id"], False)
            print("  disabled bob, his open session:", await expect_error(b, "get_expenses"))
            assert (await login("bob@example.com", "bob-pass-1")).status_code == 401
            await users.set_active(bob["_id"], True)
            print("  re-enabled bob, old token still:", await expect_error(b, "get_expenses"))

            await users.set_password(alice["_id"], "alice-pass-2")
            print("  alice password reset, old token:", await expect_error(a, "get_expenses"))
            assert (await login("alice@example.com", "alice-pass-1")).status_code == 401

        async with Client(url, auth=await token("alice@example.com", "alice-pass-2")) as a:
            assert (await call(a, "get_expenses"))["total_amount"] == 300
            print("  alice new password works and sees her data")

        for _ in range(config.MAX_FAILED_LOGINS):
            await login("carol@example.com", "bad-guess-000")
        r = await login("carol@example.com", "carol-pass-1")
        print(f"  carol after {config.MAX_FAILED_LOGINS} wrong passwords, right password:", r.status_code)
        assert r.status_code == 401

        deleted = await users.delete_user(bob["_id"])
        left = await db.client()[config.MONGODB_DB][db.EXPENSES].count_documents({"user_id": bob["_id"]})
        print(f"  deleted bob: {deleted} expenses removed, {left} left")
        assert deleted == 2 and left == 0

        print("\nALL MULTI-USER CHECKS PASSED")
    finally:
        await http.aclose()
        server.should_exit = True
        await serve_task
        await db.client().drop_database("expense_tracker_isolation_test")
        await db.close_db()


if __name__ == "__main__":
    asyncio.run(main())
