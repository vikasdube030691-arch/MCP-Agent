"""Settings, read from environment variables with local-dev defaults."""

import os
from pathlib import Path
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

# stdio only: the single local user this process acts for (must be a registered, active
# user). Ignored for HTTP requests.
LOCAL_USER_ID = os.getenv("EXPENSE_USER_ID")

# Built-in auth (default): users log in at POST /auth/login and get a JWT signed with a
# local RSA key, created in AUTH_KEY_DIR on first use. Keep that directory private.
AUTH_KEY_DIR = Path(os.getenv("AUTH_KEY_DIR", Path(__file__).resolve().parent.parent / "keys"))
TOKEN_ISSUER = "expense-tracker"
TOKEN_AUDIENCE = "expense-tracker-mcp"
TOKEN_TTL_HOURS = int(os.getenv("AUTH_TOKEN_TTL_HOURS", "12"))
MAX_FAILED_LOGINS = int(os.getenv("AUTH_MAX_FAILED_LOGINS", "5"))
LOCKOUT_MINUTES = int(os.getenv("AUTH_LOCKOUT_MINUTES", "15"))

# External auth (optional): verify JWTs from your own identity provider (Auth0, Cognito,
# Keycloak...) instead. Set either a JWKS URI or a PEM public key; the user id is read from
# AUTH_USER_CLAIM and must match a user registered with manage_users.py --user-id.
AUTH_JWKS_URI = os.getenv("AUTH_JWKS_URI")
AUTH_PUBLIC_KEY = os.getenv("AUTH_PUBLIC_KEY")
AUTH_ISSUER = os.getenv("AUTH_ISSUER")
AUTH_AUDIENCE = os.getenv("AUTH_AUDIENCE")
AUTH_USER_CLAIM = os.getenv("AUTH_USER_CLAIM", "sub")
