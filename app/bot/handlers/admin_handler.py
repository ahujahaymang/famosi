"""
Admin Telegram command handler.

All commands are gated behind ``is_admin(telegram_user_id)`` — any non-admin
who somehow reaches this handler receives a silent rejection.

Commands
--------
/admin                 — show the admin help menu
/stats                 — DB-level snapshot (users, subscriptions, total requests)
/metrics               — in-memory 24h operational digest (LLM cost, latency, intents)
/pending               — list users awaiting approval
/approve <tg_id>       — approve a pending user (activates 7-day trial)
/reject  <tg_id>       — reject and remove a pending user
/users                 — list the 20 most-recent users
/user    <tg_id>       — detail view for a single user

All commands send structured Markdown replies. Health data, raw message text,
and any PII beyond Telegram user IDs (which are opaque integers) are never
included in the output.

Privacy contract
----------------
- Never include user message text, food names, symptoms, or health values in
  any admin reply.
- Only structural / operational fields are shown: user IDs (integers),
  subscription status, country, role, LLM call counts, token totals, cost
  estimates, latency percentiles.
"""

from __future__ import annotations

import structlog
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app.dependencies import _AsyncSessionFactory
from app.services.admin_service import (
    approve_user_db,
    get_db_stats,
    get_user_detail_db,
    is_admin,
    list_all_users_db,
    list_pending,
    metrics,
    reject_user_db,
)

logger = structlog.get_logger(__name__)

_ADMIN_HELP = """\
🔐 *Famosi Admin Panel*

*Observation*
/stats   — DB snapshot (users, subscriptions, requests)
/metrics — 24h LLM & latency digest

*User management*
/users          — list 20 most recent users
/user \\<tg\\_id\\>  — detail for a single user
/pending        — users awaiting approval

*Approval*
/approve \\<tg\\_id\\>  — approve user (starts 7-day trial)
/reject  \\<tg\\_id\\>  — reject and remove user

/admin — show this menu
"""


# ---------------------------------------------------------------------------
# Guard decorator
# ---------------------------------------------------------------------------


def _admin_only(handler):
    """Decorator that silently drops updates from non-admin users."""

    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_user is None:
            return
        if not is_admin(update.effective_user.id):
            logger.warning(
                "admin_command_rejected_non_admin",
                telegram_user_id=update.effective_user.id,
            )
            return
        await handler(update, context)

    wrapper.__name__ = handler.__name__
    return wrapper


# ---------------------------------------------------------------------------
# Reply helper
# ---------------------------------------------------------------------------


async def _reply(update: Update, text: str) -> None:
    """Send a Markdown reply; fall back to plain text on parse error."""
    assert update.message is not None
    try:
        await update.message.reply_text(text, parse_mode="Markdown")
    except Exception:
        await update.message.reply_text(text)


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


@_admin_only
async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the admin help menu."""
    await _reply(update, _ADMIN_HELP)


@_admin_only
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Return a DB-level snapshot of users, subscriptions, and total requests."""
    async with _AsyncSessionFactory() as db:
        stats = await get_db_stats(db)

    subs = stats["subscriptions"]
    text = (
        "📈 *Famosi — DB Stats*\n\n"
        f"👥 Total users: *{stats['total_users']}*\n"
        f"⏳ Pending approval: *{stats['pending_approval']}*\n\n"
        f"💳 Subscriptions:\n"
        f"  • Active: *{subs['active']}*\n"
        f"  • Trial: *{subs['trial']}*\n"
        f"  • Total: *{subs['total']}*\n\n"
        f"📋 Total requests logged: *{stats['total_requests_logged']:,}*"
    )
    await _reply(update, text)


@_admin_only
async def cmd_metrics(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Return the 24h in-memory operational digest."""
    await _reply(update, metrics.daily_digest_text())


@_admin_only
async def cmd_pending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List all users awaiting approval with one-tap approve/reject hints."""
    entries = list_pending()
    if not entries:
        await _reply(update, "✅ No users pending approval.")
        return

    lines = ["⏳ *Users awaiting approval:*\n"]
    for e in entries:
        lines.append(
            f"• `{e['telegram_user_id']}` — {e['role']} from {e['country']}\n"
            f"  `/approve {e['telegram_user_id']}` | `/reject {e['telegram_user_id']}`"
        )
    await _reply(update, "\n".join(lines))


@_admin_only
async def cmd_approve(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /approve <telegram_user_id>

    Activate a 7-day trial for the given user and notify them via Telegram.
    """
    assert update.message is not None
    args = context.args or []
    if not args or not args[0].lstrip("-").isdigit():
        await _reply(update, "Usage: `/approve <telegram_user_id>`")
        return

    tg_id = int(args[0])
    try:
        async with _AsyncSessionFactory() as db:
            result_text = await approve_user_db(db, tg_id)
    except LookupError as exc:
        await _reply(update, f"❌ {exc}")
        return
    except Exception as exc:
        logger.exception("admin_approve_failed", telegram_user_id=tg_id)
        await _reply(update, f"❌ Unexpected error: {exc}")
        return

    await _reply(update, result_text)

    # Notify the user that their account is now active
    try:
        await update.get_bot().send_message(
            chat_id=tg_id,
            text=(
                "🎉 Your Famosi account has been approved!\n\n"
                "Your 7-day free trial is now active. Just send me a message "
                "to start tracking your pregnancy journey.\n\n"
                "Type /help to see everything I can do."
            ),
        )
    except Exception:
        logger.warning("admin_approve_user_notify_failed", telegram_user_id=tg_id)
        await _reply(
            update,
            "⚠️ Approval recorded but couldn't notify the user — "
            "they may have blocked the bot.",
        )


@_admin_only
async def cmd_reject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /reject <telegram_user_id>

    Remove the user record and notify them that their request was not approved.
    """
    assert update.message is not None
    args = context.args or []
    if not args or not args[0].lstrip("-").isdigit():
        await _reply(update, "Usage: `/reject <telegram_user_id>`")
        return

    tg_id = int(args[0])
    try:
        async with _AsyncSessionFactory() as db:
            result_text = await reject_user_db(db, tg_id)
    except LookupError as exc:
        await _reply(update, f"❌ {exc}")
        return
    except Exception as exc:
        logger.exception("admin_reject_failed", telegram_user_id=tg_id)
        await _reply(update, f"❌ Unexpected error: {exc}")
        return

    await _reply(update, result_text)

    # Notify the user
    try:
        await update.get_bot().send_message(
            chat_id=tg_id,
            text=(
                "We're sorry, but we're not able to approve your Famosi "
                "account at this time. If you think this was a mistake, "
                "please contact support."
            ),
        )
    except Exception:
        logger.warning("admin_reject_user_notify_failed", telegram_user_id=tg_id)


@_admin_only
async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List the 20 most recent users with a short status snapshot."""
    async with _AsyncSessionFactory() as db:
        users = await list_all_users_db(db, limit=20, offset=0)

    if not users:
        await _reply(update, "No users registered yet.")
        return

    lines = [f"👥 *Users (last {len(users)})*\n"]
    for u in users:
        pending_tag = " ⏳" if u["approval_pending"] else ""
        sub = u["subscription_status"]
        sub_icon = {"active": "✅", "trial": "🕐", "grace": "⚠️", "inactive": "❌"}.get(
            sub, "❓"
        )
        lines.append(
            f"`{u['telegram_user_id']}` "
            f"[{u['role']}·{u['country']}] "
            f"{sub_icon}{sub}{pending_tag}"
        )

    lines.append("\nUse `/user <tg_id>` for full details.")
    await _reply(update, "\n".join(lines))


@_admin_only
async def cmd_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/user <telegram_user_id> — show full detail for one user."""
    args = context.args or []
    if not args or not args[0].lstrip("-").isdigit():
        await _reply(update, "Usage: `/user <telegram_user_id>`")
        return

    tg_id = int(args[0])
    async with _AsyncSessionFactory() as db:
        detail = await get_user_detail_db(db, tg_id)

    if detail is None:
        await _reply(update, f"❌ No user found with telegram_user_id `{tg_id}`.")
        return

    pending_tag = "\n⏳ *Awaiting approval*" if detail["approval_pending"] else ""
    sub_status = detail["subscription_status"]
    trial_end = detail["trial_end"] or "—"
    period_end = detail["current_period_end"] or "—"

    text = (
        f"👤 *User detail*{pending_tag}\n\n"
        f"Telegram ID: `{detail['telegram_user_id']}`\n"
        f"DB ID: `{detail['id']}`\n"
        f"Role: `{detail['role']}`\n"
        f"Country: `{detail['country']}`\n"
        f"Timezone: `{detail['timezone']}`\n"
        f"Language: `{detail['language']}`\n"
        f"Due date: `{detail['due_date'] or '—'}`\n"
        f"Onboarding complete: `{detail['onboarding_complete']}`\n\n"
        f"Subscription: `{sub_status}`\n"
        f"Trial end: `{trial_end}`\n"
        f"Period end: `{period_end}`"
    )
    if detail["approval_pending"]:
        text += (
            f"\n\n`/approve {tg_id}` — approve this user\n"
            f"`/reject {tg_id}` — reject and remove"
        )
    await _reply(update, text)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register(application: Application) -> None:
    """
    Register all admin command handlers on *application*.

    Called from ``app.main._register_handlers``.
    Admin commands are registered at group 0 (same as other handlers).
    The ``_admin_only`` decorator silently drops non-admin updates, so
    there is no risk of leaking admin functionality to regular users.
    """
    application.add_handler(CommandHandler("admin", cmd_admin))
    application.add_handler(CommandHandler("stats", cmd_stats))
    application.add_handler(CommandHandler("metrics", cmd_metrics))
    application.add_handler(CommandHandler("pending", cmd_pending))
    application.add_handler(CommandHandler("approve", cmd_approve))
    application.add_handler(CommandHandler("reject", cmd_reject))
    application.add_handler(CommandHandler("users", cmd_users))
    application.add_handler(CommandHandler("user", cmd_user))

    logger.debug("admin_handlers_registered")
