"""Settings, read from environment variables with local-dev defaults."""

import os
from zoneinfo import ZoneInfo

MONGODB_URI = os.getenv("MONGODB_URI", "mongodb://localhost:27017")
MONGODB_DB = os.getenv("MONGODB_DB", "expense_tracker")
DEFAULT_CURRENCY = os.getenv("EXPENSE_CURRENCY", "INR")

# Used to interpret dates without a timezone and to work out "today".
TIMEZONE = ZoneInfo(os.getenv("EXPENSE_TIMEZONE", "Asia/Kolkata"))

# Transport: "stdio" for a local client (Claude Desktop / Claude Code), "http" for a shared server.
MCP_TRANSPORT = os.getenv("MCP_TRANSPORT", "stdio")
MCP_HOST = os.getenv("MCP_HOST", "127.0.0.1")
MCP_PORT = int(os.getenv("MCP_PORT", "8000"))

# stdio only: the single local user this process acts for. Ignored for HTTP requests.
LOCAL_USER_ID = os.getenv("EXPENSE_USER_ID")

# HTTP auth: verify JWT bearer tokens from your identity provider (Auth0, Cognito, Keycloak...).
# Set either a JWKS URI or a PEM public key. The user is taken from AUTH_USER_CLAIM.
AUTH_JWKS_URI = os.getenv("AUTH_JWKS_URI")
AUTH_PUBLIC_KEY = os.getenv("AUTH_PUBLIC_KEY")
AUTH_ISSUER = os.getenv("AUTH_ISSUER")
AUTH_AUDIENCE = os.getenv("AUTH_AUDIENCE")
AUTH_USER_CLAIM = os.getenv("AUTH_USER_CLAIM", "sub")
