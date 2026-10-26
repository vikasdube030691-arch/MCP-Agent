"""Exercise the server in-process with the FastMCP client."""

import asyncio

from fastmcp import Client

from server import mcp


async def main():
    async with Client(mcp) as client:
        tools = await client.list_tools()
        print("Tools:", [t.name for t in tools])

        result = await client.call_tool("add", {"a": 2, "b": 3})
        print("add(2, 3) =", result.data)

        result = await client.call_tool("word_count", {"text": "hello MCP world"})
        print("word_count =", result.data)

        resource = await client.read_resource("greeting://Vikas")
        print("greeting =", resource[0].text)


if __name__ == "__main__":
    asyncio.run(main())
