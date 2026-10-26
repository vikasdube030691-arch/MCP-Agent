"""Who is calling: resolves the current user from a verified token, never from tool input."""

from fastmcp.exceptions import ToolError
from fastmcp.server.auth.providers.jwt import JWTVerifier
from fastmcp.server.dependencies import get_access_token, get_http_request

from . import config


def build_auth() -> JWTVerifier | None:
    """JWT verifier for HTTP transport, or None when no key source is configured."""
    if not (config.AUTH_JWKS_URI or config.AUTH_PUBLIC_KEY):
        return None
    return JWTVerifier(
        jwks_uri=config.AUTH_JWKS_URI,
        public_key=config.AUTH_PUBLIC_KEY,
        issuer=config.AUTH_ISSUER,
        audience=config.AUTH_AUDIENCE,
    )


def _is_http_request() -> bool:
    try:
        get_http_request()
        return True
    except RuntimeError:
        return False


def current_user_id() -> str:
    """The id of the user making this request.

    HTTP: taken from the verified access token; unauthenticated HTTP calls are refused.
    stdio: the process serves one local user, configured with EXPENSE_USER_ID.
    """
    token = get_access_token()
    if token is not None:
        user_id = token.claims.get(config.AUTH_USER_CLAIM) or token.subject
        if not user_id:
            raise ToolError("Access token does not identify a user")
        return str(user_id)

    if _is_http_request():
        raise ToolError("Authentication required")

    if config.LOCAL_USER_ID:
        return config.LOCAL_USER_ID
    raise ToolError("No user configured: set EXPENSE_USER_ID for local (stdio) use")
