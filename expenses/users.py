"""User accounts: registration, password login, lockout, password reset, disabling and
token revocation."""

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import bcrypt
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from . import config, db

# Checked against when the email is unknown, so a login takes the same time either way.
_DUMMY_HASH = bcrypt.hashpw(b"not-a-real-password", bcrypt.gensalt()).decode()


class UserError(ValueError):
    pass


class UserExistsError(UserError):
    pass


def _users():
    return db.client()[config.MONGODB_DB][db.USERS]


def _now() -> datetime:
    return datetime.now(UTC)


def _normalise_email(email: str) -> str:
    return email.strip().lower()


def _hash_password(password: str) -> str:
    raw = password.encode()
    if len(password) < 8:
        raise UserError("Password must be at least 8 characters")
    if len(raw) > 72:
        raise UserError("Password must be at most 72 bytes")
    return bcrypt.hashpw(raw, bcrypt.gensalt()).decode()


def public_view(user: dict[str, Any]) -> dict[str, Any]:
    """A user record without secrets."""
    return {
        "user_id": user["_id"],
        "email": user["email"],
        "name": user["name"],
        "active": user["active"],
        "locked_until": user.get("locked_until"),
        "last_login_at": user.get("last_login_at"),
        "created_at": user["created_at"],
    }


async def create_user(
    email: str, name: str, password: str | None, user_id: str | None = None
) -> dict[str, Any]:
    """Register a user. ``password`` may be None for external-identity-provider users."""
    now = _now()
    user = {
        "_id": user_id or f"u_{uuid.uuid4().hex[:16]}",
        "email": _normalise_email(email),
        "name": name.strip(),
        "password_hash": _hash_password(password) if password is not None else None,
        "active": True,
        "token_version": 0,
        "failed_logins": 0,
        "locked_until": None,
        "last_login_at": None,
        "created_at": now,
        "updated_at": now,
    }
    try:
        await _users().insert_one(user)
    except DuplicateKeyError as e:
        raise UserExistsError("A user with that email or user id already exists") from e
    return user


async def get_user(user_id: str) -> dict[str, Any] | None:
    return await _users().find_one({"_id": user_id})


async def find_user(id_or_email: str) -> dict[str, Any] | None:
    if "@" in id_or_email:
        return await _users().find_one({"email": _normalise_email(id_or_email)})
    return await get_user(id_or_email)


async def list_users() -> list[dict[str, Any]]:
    return await _users().find().sort("created_at", 1).to_list()


async def _update(user_id: str, update: dict[str, Any]) -> dict[str, Any]:
    update.setdefault("$set", {})["updated_at"] = _now()
    user = await _users().find_one_and_update(
        {"_id": user_id}, update, return_document=ReturnDocument.AFTER
    )
    if user is None:
        raise UserError(f"User {user_id} not found")
    return user


async def set_active(user_id: str, active: bool) -> dict[str, Any]:
    """Enable or disable a user. Disabling also revokes every token they hold."""
    update: dict[str, Any] = {"$set": {"active": active}}
    if not active:
        update["$inc"] = {"token_version": 1}
    else:
        update["$set"].update(failed_logins=0, locked_until=None)
    return await _update(user_id, update)


async def set_password(user_id: str, password: str) -> dict[str, Any]:
    """Change the password and sign the user out everywhere."""
    return await _update(user_id, {
        "$set": {"password_hash": _hash_password(password), "failed_logins": 0,
                 "locked_until": None},
        "$inc": {"token_version": 1},
    })


async def revoke_tokens(user_id: str) -> dict[str, Any]:
    """Invalidate every token issued to the user so far."""
    return await _update(user_id, {"$inc": {"token_version": 1}})


async def delete_user(user_id: str) -> int:
    """Delete the user and all of their expenses. Returns the number of expenses deleted."""
    if await get_user(user_id) is None:
        raise UserError(f"User {user_id} not found")
    database = db.client()[config.MONGODB_DB]
    result = await database[db.EXPENSES].delete_many({"user_id": user_id})
    await database[db.COUNTERS].delete_one({"_id": {"name": "expense_id", "user_id": user_id}})
    await _users().delete_one({"_id": user_id})
    return result.deleted_count


async def authenticate(email: str, password: str) -> dict[str, Any] | None:
    """Check an email and password. Returns the user, or None for any kind of failure.

    After MAX_FAILED_LOGINS wrong passwords in a row the account is locked for
    LOCKOUT_MINUTES, even for the right password.
    """
    user = await _users().find_one({"email": _normalise_email(email)})
    stored = (user or {}).get("password_hash") or _DUMMY_HASH
    password_ok = bcrypt.checkpw(password.encode()[:72], stored.encode())

    if user is None or not user["active"] or not user.get("password_hash"):
        return None
    now = _now()
    if user.get("locked_until") and user["locked_until"] > now:
        return None

    if not password_ok:
        failures = user["failed_logins"] + 1
        locked_until = (now + timedelta(minutes=config.LOCKOUT_MINUTES)
                        if failures >= config.MAX_FAILED_LOGINS else None)
        await _update(user["_id"], {"$set": {
            "failed_logins": 0 if locked_until else failures,
            "locked_until": locked_until,
        }})
        return None

    return await _update(user["_id"], {"$set": {
        "failed_logins": 0, "locked_until": None, "last_login_at": now,
    }})


def _reset_hash(token: str) -> str:
    # The token is 256 random bits, so a fast hash is enough; only the hash is stored.
    return hashlib.sha256(token.encode()).hexdigest()


async def start_password_reset(email: str) -> tuple[dict[str, Any], str] | None:
    """Create a single-use reset token for the account with this email.

    Returns the user and the token to email them, or None when there is no active password
    account for the email or a reset was requested moments ago. Callers must answer the
    same either way, so the response doesn't reveal which emails exist.
    """
    user = await _users().find_one({"email": _normalise_email(email)})
    if user is None or not user["active"] or not user.get("password_hash"):
        return None
    now = _now()
    last = user.get("reset_requested_at")
    if last and now - last < timedelta(seconds=config.RESET_COOLDOWN_SECONDS):
        return None
    token = secrets.token_urlsafe(32)
    await _update(user["_id"], {"$set": {
        "reset_token_hash": _reset_hash(token),
        "reset_token_expires": now + timedelta(minutes=config.RESET_TOKEN_TTL_MINUTES),
        "reset_requested_at": now,
    }})
    return user, token


async def finish_password_reset(token: str, password: str) -> dict[str, Any]:
    """Set a new password with a reset token. The token works once; the user is signed out
    everywhere and any lockout is lifted."""
    password_hash = _hash_password(password)  # check it first, so a weak one keeps the token
    now = _now()
    user = await _users().find_one_and_update(
        {"reset_token_hash": _reset_hash(token), "reset_token_expires": {"$gt": now},
         "active": True},
        {"$set": {"password_hash": password_hash, "failed_logins": 0, "locked_until": None,
                  "updated_at": now},
         "$unset": {"reset_token_hash": "", "reset_token_expires": ""},
         "$inc": {"token_version": 1}},
        return_document=ReturnDocument.AFTER,
    )
    if user is None:
        raise UserError("This reset link is invalid or has expired. Request a new one.")
    return user
