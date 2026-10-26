"""A simple MCP server built with FastMCP."""

from datetime import datetime

from fastmcp import FastMCP

mcp = FastMCP("demo-server")


@mcp.tool
def add(a: float, b: float) -> float:
    """Add two numbers."""
    return a + b


@mcp.tool
def word_count(text: str) -> dict:
    """Count words, characters, and lines in a piece of text."""
    return {
        "words": len(text.split()),
        "characters": len(text),
        "lines": len(text.splitlines()) or (1 if text else 0),
    }


@mcp.tool
def current_time() -> str:
    """Return the current local date and time in ISO format."""
    return datetime.now().isoformat(timespec="seconds")


@mcp.resource("config://app")
def app_config() -> dict:
    """Static configuration exposed as a resource."""
    return {"name": "demo-server", "version": "0.1.0"}


@mcp.resource("greeting://{name}")
def greeting(name: str) -> str:
    """A templated resource that greets someone by name."""
    return f"Hello, {name}!"


@mcp.prompt
def summarize(text: str) -> str:
    """Prompt template asking the model to summarize text."""
    return f"Summarize the following text in three bullet points:\n\n{text}"


if __name__ == "__main__":
    # Default transport is stdio (for Claude Desktop / Claude Code).
    # For HTTP instead: mcp.run(transport="http", host="127.0.0.1", port=8000)
    mcp.run()
