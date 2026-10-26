"""Admin CLI for expense tracker users. Run on the server machine; not exposed over MCP.

  python manage_users.py create --email a@x.com --name "Asha"      # prompts for password
  python manage_users.py list
  python manage_users.py disable a@x.com        # blocks login and kills existing tokens
  python manage_users.py enable a@x.com
  python manage_users.py reset-password a@x.com
  python manage_users.py revoke-tokens a@x.com  # sign out everywhere
  python manage_users.py token a@x.com --hours 24   # issue a token without a password
  python manage_users.py delete a@x.com --yes   # removes the user AND their expenses

Users are referred to by email or user id.
"""

import argparse
import asyncio
import getpass
import sys

from expenses import db, tokens, users
from expenses.auth import uses_external_auth


def _ask_password() -> str:
    first = getpass.getpass("Password: ")
    if first != getpass.getpass("Repeat password: "):
        sys.exit("Passwords do not match")
    return first


async def _resolve(id_or_email: str) -> dict:
    user = await users.find_user(id_or_email)
    if user is None:
        sys.exit(f"No user {id_or_email!r}")
    return user


def _fmt(dt) -> str:
    return dt.strftime("%Y-%m-%d %H:%M") if dt else "-"


async def run(args: argparse.Namespace) -> None:
    await db.init_db()
    match args.command:
        case "create":
            if args.no_password:
                password = None
            else:
                password = args.password or _ask_password()
            user = await users.create_user(args.email, args.name, password, user_id=args.user_id)
            print(f"Created {user['_id']}  {user['email']}")

        case "list":
            rows = await users.list_users()
            print(f"{'USER ID':<20} {'EMAIL':<30} {'NAME':<18} {'STATUS':<8} LAST LOGIN")
            for u in rows:
                status = "active" if u["active"] else "disabled"
                if u.get("locked_until"):
                    status = "locked"
                print(f"{u['_id']:<20} {u['email']:<30} {u['name'][:18]:<18} {status:<8} "
                      f"{_fmt(u.get('last_login_at'))}")
            print(f"{len(rows)} user(s)")

        case "disable" | "enable":
            user = await _resolve(args.user)
            await users.set_active(user["_id"], args.command == "enable")
            print(f"{args.command}d {user['email']}")

        case "reset-password":
            user = await _resolve(args.user)
            await users.set_password(user["_id"], args.password or _ask_password())
            print(f"Password changed for {user['email']}; their existing tokens no longer work")

        case "revoke-tokens":
            user = await _resolve(args.user)
            await users.revoke_tokens(user["_id"])
            print(f"All tokens for {user['email']} revoked")

        case "token":
            if uses_external_auth():
                sys.exit("External auth is configured: tokens come from the identity provider")
            user = await _resolve(args.user)
            if not user["active"]:
                sys.exit("User is disabled")
            token, expires_in = tokens.issue_token(user["_id"], user["token_version"], args.hours)
            print(token)
            print(f"(valid for {expires_in // 3600}h)", file=sys.stderr)

        case "delete":
            user = await _resolve(args.user)
            if not args.yes:
                sys.exit(f"This deletes {user['email']} and ALL their expenses. Re-run with --yes")
            n = await users.delete_user(user["_id"])
            print(f"Deleted {user['email']} and {n} expense(s)")
    await db.close_db()


def main() -> None:
    p = argparse.ArgumentParser(description="Manage expense tracker users")
    sub = p.add_subparsers(dest="command", required=True)

    c = sub.add_parser("create", help="register a user")
    c.add_argument("--email", required=True)
    c.add_argument("--name", required=True)
    c.add_argument("--user-id", help="set the id, e.g. the 'sub' from an external identity provider")
    c.add_argument("--password", help="avoid: ends up in shell history. Prompted for if omitted")
    c.add_argument("--no-password", action="store_true",
                   help="user signs in only through the external identity provider")

    sub.add_parser("list", help="list users")
    for name, text in [("disable", "block a user and revoke their tokens"),
                       ("enable", "re-enable a user"),
                       ("revoke-tokens", "sign a user out everywhere")]:
        sub.add_parser(name, help=text).add_argument("user")

    r = sub.add_parser("reset-password", help="set a new password and revoke tokens")
    r.add_argument("user")
    r.add_argument("--password")

    t = sub.add_parser("token", help="issue an access token for a user")
    t.add_argument("user")
    t.add_argument("--hours", type=int)

    d = sub.add_parser("delete", help="delete a user and all their expenses")
    d.add_argument("user")
    d.add_argument("--yes", action="store_true")

    args = p.parse_args()
    try:
        asyncio.run(run(args))
    except users.UserError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
