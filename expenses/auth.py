"""Who is calling: resolves the current user from a verified token, never from tool input."""

from fastmcp.exceptions import ToolError
from fastmcp.server.auth.providers.jwt import JWTVerifier
from fastmcp.server.dependencies import get_access_token, get_http_request

from . import config, tokens, users


def uses_external_auth() -> bool:
    return bool(config.AUTH_JWKS_URI or config.AUTH_PUBLIC_KEY)


def build_auth() -> JWTVerifier:
    """Verifier for bearer tokens on HTTP: an external identity provider if one is
    configured, otherwise the tokens this server issues at /auth/login."""
    if uses_external_auth():
        return JWTVerifier(
            jwks_uri=config.AUTH_JWKS_URI,
            public_key=config.AUTH_PUBLIC_KEY,
            issuer=config.AUTH_ISSUER,
            audience=config.AUTH_AUDIENCE,
        )
    return JWTVerifier(
        public_key=tokens.public_key_pem(),
        issuer=config.TOKEN_ISSUER,
        audience=config.TOKEN_AUDIENCE,
        algorithm="RS256",
    )


def _is_http_request() -> bool:
    try:
        get_http_request()
        return True
    except RuntimeError:
        return False


async def _active_user(user_id: str) -> dict:
    user = await users.get_user(user_id)
    if user is None or not user["active"]:
        raise ToolError("User account not found or disabled")
    return user


async def current_user_id() -> str:
    """The id of the registered, active user making this request.

    HTTP: taken from the verified access token; unauthenticated calls are refused, and
    tokens are refused once the user is disabled, deleted, or has their tokens revoked.
    stdio: the process serves one local user, configured with EXPENSE_USER_ID.
    """
    token = get_access_token()
    if token is not None:
        user_id = token.claims.get(config.AUTH_USER_CLAIM) or token.subject
        if not user_id:
            raise ToolError("Access token does not identify a user")
        user = await _active_user(str(user_id))
        if not uses_external_auth() and token.claims.get("ver") != user["token_version"]:
            raise ToolError("Access token has been revoked; log in again")
        return user["_id"]

    if _is_http_request():
        raise ToolError("Authentication required")

    if config.LOCAL_USER_ID:
        return (await _active_user(config.LOCAL_USER_ID))["_id"]
    raise ToolError("No user configured: set EXPENSE_USER_ID for local (stdio) use")
