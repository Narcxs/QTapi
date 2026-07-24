# -*- coding: utf-8 -*-
"""
QTapi Telegram bot (runs as its own process, separate from the API).

User commands (the only ones shown in Telegram's command menu):
  /mytoken -> show your current API token (issues the first one for members)
  /renew   -> get a fresh token - ONLY once the current one has expired
  /help    -> how to use the API

Everything is also available through inline buttons (🔑 My Token / ♻️ Renew /
📖 Documentation). API access requires membership of the Telegram group.

Admin (only TELEGRAM_ADMIN_ID, hidden from the menu):
  /list                      list all tokens
  /stats                     counts
  /grant <telegram_id> [days]
  /revoke <token>
  /revokeuser <telegram_id>

Designed to NOT crash: every handler is wrapped by a global error handler, and
the membership check never raises. Run with:  python -m app.bot
"""
import logging

from telegram import (BotCommand, InlineKeyboardButton, InlineKeyboardMarkup,
                      Update)
from telegram.constants import ChatType
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes)

from . import config, tokens

_group_link_cache = {"link": None}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("bot")

_MEMBER_OK = {"creator", "administrator", "member", "restricted"}


def _is_admin(user_id: int) -> bool:
    return config.TELEGRAM_ADMIN_ID and user_id == config.TELEGRAM_ADMIN_ID


async def _is_member(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    """True if the user belongs to the required group. Never raises."""
    if not config.TELEGRAM_GROUP_ID:
        return True
    try:
        m = await context.bot.get_chat_member(config.TELEGRAM_GROUP_ID, user_id)
        return m.status in _MEMBER_OK
    except Exception as e:  # noqa: BLE001
        log.warning("membership check failed for %s: %s", user_id, e)
        return False


async def _group_link(context: ContextTypes.DEFAULT_TYPE):
    """Return an invite link for the group, or None. Cached after first success."""
    if config.TELEGRAM_GROUP_LINK:
        return config.TELEGRAM_GROUP_LINK
    if _group_link_cache["link"]:
        return _group_link_cache["link"]
    if not config.TELEGRAM_GROUP_ID:
        return None
    try:
        chat = await context.bot.get_chat(config.TELEGRAM_GROUP_ID)
        link = chat.invite_link or await context.bot.export_chat_invite_link(
            config.TELEGRAM_GROUP_ID)
        _group_link_cache["link"] = link
        return link
    except Exception as e:  # noqa: BLE001
        log.warning("cannot get group invite link: %s", e)
        return None


# --------------------------------------------------------------------------- #
# keyboards
# --------------------------------------------------------------------------- #
async def _join_keyboard(context: ContextTypes.DEFAULT_TYPE) -> InlineKeyboardMarkup:
    rows = []
    link = await _group_link(context)
    if link:
        rows.append([InlineKeyboardButton("➡️ Join the group", url=link)])
    rows.append([InlineKeyboardButton("✅ Verify membership", callback_data="check_entry")])
    return InlineKeyboardMarkup(rows)


async def _menu_keyboard(context: ContextTypes.DEFAULT_TYPE) -> InlineKeyboardMarkup:
    """Main menu: token / renew / documentation."""
    rows = [[InlineKeyboardButton("🔑 My Token", callback_data="menu_token"),
             InlineKeyboardButton("♻️ Renew", callback_data="menu_renew")]]
    link = await _group_link(context)
    if link:
        rows.append([InlineKeyboardButton("📖 Documentation (pinned in group)", url=link)])
    return InlineKeyboardMarkup(rows)


async def _token_keyboard(context: ContextTypes.DEFAULT_TYPE) -> InlineKeyboardMarkup:
    """Under an active token card: renew (enabled at expiry) + docs."""
    rows = [[InlineKeyboardButton("♻️ Renew", callback_data="menu_renew")]]
    link = await _group_link(context)
    if link:
        rows.append([InlineKeyboardButton("📖 Documentation (pinned in group)", url=link)])
    return InlineKeyboardMarkup(rows)


# --------------------------------------------------------------------------- #
# messages (HTML everywhere: tokens contain "_" which breaks Markdown)
# --------------------------------------------------------------------------- #
_WELCOME = (
    "<b>👋 Welcome to QTapi</b>\n\n"
    "Real-time options Gamma Exposure (GEX) & orderflow data for "
    "<b>SPX, SPY, NDX and QQQ</b>, refreshed every ~2 seconds.\n\n"
    "Use the menu below to manage your API access.\n"
    "📖 The full API documentation is pinned in our Telegram group."
)

_HELP = (
    "<b>📚 QTapi — Help</b>\n\n"
    "QTapi provides real-time options Gamma Exposure (GEX) and orderflow data "
    "for <b>SPX, SPY, NDX and QQQ</b>, refreshed every ~2 seconds.\n\n"
    "<b>Commands</b>\n"
    "/mytoken — show your current API token\n"
    "/renew — get a fresh token (available once your token expires)\n"
    "/help — show this message\n\n"
    "<b>Using your token</b>\n"
    "Append <code>?token=YOUR_TOKEN</code> to any endpoint.\n\n"
    "📖 The full API documentation is pinned in our Telegram group.\n\n"
    "<i>Questions? Ask in the group — the team is there to help.</i>"
)

_JOIN = (
    "<b>🔒 Members only</b>\n\n"
    "API access is free, but reserved for members of our Telegram group.\n\n"
    "1️⃣ Tap <b>Join the group</b>\n"
    "2️⃣ Come back here and tap <b>Verify membership</b>"
)

_REVOKED = (
    "<b>🚫 Access suspended</b>\n\n"
    "Your token was revoked. Please contact the group admins if you believe "
    "this is a mistake."
)

_PRIVATE_ONLY = (
    "🔒 Please use this command in a <b>private chat</b> with me, "
    "so your token stays secret."
)


def _token_msg(token: str, rec: dict) -> str:
    base = config.PUBLIC_BASE_URL.rstrip("/")
    return (
        "<b>🔑 QTapi — API Access Token</b>\n\n"
        "Status: <b>✅ Active</b>\n"
        f"Valid until: <b>{tokens.fmt_exp(rec)}</b>\n\n"
        f"<code>{token}</code>\n\n"
        "<b>Quick start</b>\n"
        "Append <code>?token=YOUR_TOKEN</code> to any endpoint, e.g.:\n"
        f"<code>{base}/api/classic/spx/zero?token={token}</code>\n\n"
        "📖 The full API documentation is pinned in our Telegram group.\n\n"
        "<i>Keep this token private — it is tied to your account.</i>"
    )


def _renewed_msg(token: str, rec: dict) -> str:
    return "♻️ <b>Your token has been renewed.</b>\n\n" + _token_msg(token, rec)


def _expired_msg(rec: dict) -> str:
    return (
        "<b>⚠️ Your API token has expired</b>\n\n"
        f"Expired on: <b>{tokens.fmt_exp(rec)}</b>\n\n"
        "Renew now to restore your API access."
    )


def _renew_wait_msg(rec: dict) -> str:
    return (
        "<b>⏳ Renewal not available yet</b>\n\n"
        f"Your current token is still valid until <b>{tokens.fmt_exp(rec)}</b>.\n"
        "You can request a fresh token once it expires."
    )


# --------------------------------------------------------------------------- #
# shared payloads (used by both the commands and the menu buttons)
# --------------------------------------------------------------------------- #
async def _mytoken_payload(context, user):
    """Show the current token; issue the first one to a new member."""
    if not await _is_member(context, user.id):
        return _JOIN, await _join_keyboard(context)
    rec = tokens.get_user(user.id)
    if rec:
        return _token_msg(rec["token"], rec), await _token_keyboard(context)
    last = tokens.get_user_any(user.id)
    if last and last.get("revoked"):
        return _REVOKED, None
    if last:  # had one, now expired -> invite to renew
        return _expired_msg(last), InlineKeyboardMarkup(
            [[InlineKeyboardButton("♻️ Renew now", callback_data="menu_renew")]])
    # brand-new member -> issue the first token
    token, rec = tokens.create_or_get(
        user.id, user.username or user.full_name, config.TOKEN_VALID_DAYS)
    return _token_msg(token, rec), await _token_keyboard(context)


async def _renew_payload(context, user):
    """Renew ONLY once the current token has expired."""
    if not await _is_member(context, user.id):
        return _JOIN, await _join_keyboard(context)
    rec = tokens.get_user(user.id)
    if rec:  # still valid -> refuse
        return _renew_wait_msg(rec), InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔑 My Token", callback_data="menu_token")]])
    last = tokens.get_user_any(user.id)
    if last and last.get("revoked"):
        return _REVOKED, None
    token, rec = tokens.renew(
        user.id, user.username or user.full_name, config.TOKEN_VALID_DAYS)
    msg = _renewed_msg(token, rec) if last else _token_msg(token, rec)
    return msg, await _token_keyboard(context)


# --------------------------------------------------------------------------- #
# user commands
# --------------------------------------------------------------------------- #
async def cmd_start(update: Update, context):
    """Hidden entry point (not in the menu): welcome + main menu buttons."""
    await update.message.reply_text(
        _WELCOME, parse_mode="HTML",
        reply_markup=await _menu_keyboard(context))


async def cmd_help(update: Update, context):
    txt = _HELP
    if _is_admin(update.effective_user.id):
        txt += ("\n\n<b>Admin</b>\n/list\n/stats\n/grant <id> [days]\n"
                "/revoke <token>\n/revokeuser <id>")
    await update.message.reply_text(
        txt, parse_mode="HTML", reply_markup=await _menu_keyboard(context))


async def cmd_mytoken(update: Update, context):
    if update.effective_chat.type != ChatType.PRIVATE:
        await update.message.reply_text(_PRIVATE_ONLY, parse_mode="HTML")
        return
    text, markup = await _mytoken_payload(context, update.effective_user)
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=markup)


async def cmd_renew(update: Update, context):
    if update.effective_chat.type != ChatType.PRIVATE:
        await update.message.reply_text(_PRIVATE_ONLY, parse_mode="HTML")
        return
    text, markup = await _renew_payload(context, update.effective_user)
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=markup)


# --------------------------------------------------------------------------- #
# buttons
# --------------------------------------------------------------------------- #
async def cb_menu(update: Update, context):
    """Handles 🔑 My Token / ♻️ Renew menu buttons."""
    q = update.callback_query
    user = q.from_user
    if q.data == "menu_renew":
        # renew only works after expiry -> popup instead of touching the card
        if await _is_member(context, user.id):
            rec = tokens.get_user(user.id)
            if rec:
                await q.answer(
                    f"⏳ Your token is still valid until {tokens.fmt_exp(rec)}.\n"
                    "Renewal becomes available once it expires.",
                    show_alert=True)
                return
        text, markup = await _renew_payload(context, user)
    else:  # menu_token
        text, markup = await _mytoken_payload(context, user)
    await q.answer()
    try:
        await q.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
    except Exception:  # noqa: BLE001 - message unchanged/too old
        await q.message.reply_text(text, parse_mode="HTML", reply_markup=markup)


async def cb_check_entry(update: Update, context):
    """Handles the '✅ Verify membership' button."""
    q = update.callback_query
    user = q.from_user
    if await _is_member(context, user.id):
        await q.answer("✅ Membership verified!")
        text, markup = await _mytoken_payload(context, user)
        try:
            await q.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
        except Exception:  # noqa: BLE001 - message unchanged/too old
            await q.message.reply_text(text, parse_mode="HTML", reply_markup=markup)
    else:
        await q.answer(
            "❌ You're not in the group yet. Join it, then tap Verify membership again.",
            show_alert=True)


# --------------------------------------------------------------------------- #
# admin commands (hidden from the menu)
# --------------------------------------------------------------------------- #
async def cmd_list(update: Update, context):
    if not _is_admin(update.effective_user.id):
        return
    items = tokens.list_all()
    if not items:
        await update.message.reply_text("No tokens issued yet.")
        return
    lines = []
    for t, rec in items:
        flag = "REVOKED" if rec.get("revoked") else tokens.fmt_exp(rec)
        lines.append(f"{rec.get('telegram_id')} @{rec.get('username')} — {flag}")
    text = "\n".join(lines)
    for i in range(0, len(text), 3500):        # respect Telegram's 4096 limit
        await update.message.reply_text(text[i:i + 3500])


async def cmd_stats(update: Update, context):
    if not _is_admin(update.effective_user.id):
        return
    s = tokens.stats()
    await update.message.reply_text(
        f"Total: {s['total']}\nActive: {s['active']}\n"
        f"Expired: {s['expired']}\nRevoked: {s['revoked']}")


async def cmd_grant(update: Update, context):
    if not _is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Usage: /grant <telegram_id> [days]")
        return
    try:
        tid = int(context.args[0])
        days = int(context.args[1]) if len(context.args) > 1 else config.TOKEN_VALID_DAYS
    except ValueError:
        await update.message.reply_text("Invalid arguments.")
        return
    token, rec = tokens.renew(tid, "granted", days)
    await update.message.reply_text(
        f"Granted to {tid} for {days} day(s):\n<code>{token}</code>", parse_mode="HTML")


async def cmd_revoke(update: Update, context):
    if not _is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Usage: /revoke <token>")
        return
    ok = tokens.revoke_token(context.args[0])
    await update.message.reply_text("Revoked." if ok else "Token not found.")


async def cmd_revokeuser(update: Update, context):
    if not _is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Usage: /revokeuser <telegram_id>")
        return
    try:
        tid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Invalid id.")
        return
    n = tokens.revoke_user(tid)
    await update.message.reply_text(f"Revoked {n} token(s) for {tid}.")


# --------------------------------------------------------------------------- #
# never crash
# --------------------------------------------------------------------------- #
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("Unhandled bot error: %s", context.error)
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text(
                "⚠️ Something went wrong. Please try again.")
    except Exception:  # noqa: BLE001
        pass


async def _post_init(app: Application):
    """Publish the 3 user commands in Telegram's menu (the '/' button)."""
    await app.bot.set_my_commands([
        BotCommand("mytoken", "Show your current API token"),
        BotCommand("renew", "Get a fresh token (after expiry)"),
        BotCommand("help", "How to use QTapi"),
    ])


def main():
    if not config.TELEGRAM_BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN is missing in server/.env")

    app = (Application.builder()
           .token(config.TELEGRAM_BOT_TOKEN)
           .concurrent_updates(True)     # handle many users at once
           .post_init(_post_init)        # publish the command menu
           .build())
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("mytoken", cmd_mytoken))
    app.add_handler(CommandHandler("renew", cmd_renew))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("grant", cmd_grant))
    app.add_handler(CommandHandler("revoke", cmd_revoke))
    app.add_handler(CommandHandler("revokeuser", cmd_revokeuser))
    app.add_handler(CallbackQueryHandler(cb_check_entry, pattern="^check_entry$"))
    app.add_handler(CallbackQueryHandler(cb_menu, pattern="^menu_(token|renew)$"))
    app.add_error_handler(on_error)

    log.info("QTapi bot starting (admin=%s, group=%s)...",
             config.TELEGRAM_ADMIN_ID, config.TELEGRAM_GROUP_ID)
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
