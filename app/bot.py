# -*- coding: utf-8 -*-
"""
QTapi Telegram bot (runs as its own process, separate from the API).

Users:
  /token   -> get a free API token (valid TOKEN_VALID_DAYS), MEMBERS ONLY
  /mytoken -> show your current token
  /renew   -> get a fresh token
  /start /help

Admin (only TELEGRAM_ADMIN_ID):
  /list                      list all tokens
  /stats                     counts
  /grant <telegram_id> [days]
  /revoke <token>
  /revokeuser <telegram_id>

Designed to NOT crash: every handler is wrapped by a global error handler, and
the membership check never raises. Run with:  python -m app.bot
"""
import logging

from telegram import Update
from telegram.constants import ChatType
from telegram.ext import Application, CommandHandler, ContextTypes

from . import config, tokens

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


def _token_msg(token: str, rec: dict) -> str:
    base = config.PUBLIC_BASE_URL.rstrip("/")
    return (
        f"✅ Your API token (valid until {tokens.fmt_exp(rec)}):\n\n"
        f"`{token}`\n\n"
        f"Use it by adding `?token=...` to any endpoint, e.g.:\n"
        f"{base}/api/classic/spx/zero?token={token}\n\n"
        f"Keep it private — it is tied to your account."
    )


# --------------------------------------------------------------------------- #
# user commands
# --------------------------------------------------------------------------- #
async def cmd_start(update: Update, context):
    await update.message.reply_text(
        "👋 Welcome to QTapi.\n\n"
        "/token — get your free API token (members only)\n"
        "/mytoken — show your current token\n"
        "/renew — get a fresh token\n"
        "/help — help"
    )


async def cmd_help(update: Update, context):
    txt = "Commands:\n/token — get token\n/mytoken — show token\n/renew — new token"
    if _is_admin(update.effective_user.id):
        txt += ("\n\nAdmin:\n/list\n/stats\n/grant <id> [days]\n"
                "/revoke <token>\n/revokeuser <id>")
    await update.message.reply_text(txt)


async def cmd_token(update: Update, context):
    user = update.effective_user
    if update.effective_chat.type != ChatType.PRIVATE:
        await update.message.reply_text("🔒 Send me /token in a PRIVATE chat so your token stays secret.")
        return
    if not await _is_member(context, user.id):
        await update.message.reply_text(
            "❌ You must be a member of our group to get a token.\n"
            "Join the group, then send /token again.")
        return
    token, rec = tokens.create_or_get(
        user.id, user.username or user.full_name, config.TOKEN_VALID_DAYS)
    await update.message.reply_text(_token_msg(token, rec), parse_mode="Markdown")


async def cmd_mytoken(update: Update, context):
    rec = tokens.get_user(update.effective_user.id)
    if not rec:
        await update.message.reply_text("You don't have an active token. Send /token.")
        return
    await update.message.reply_text(_token_msg(rec["token"], rec), parse_mode="Markdown")


async def cmd_renew(update: Update, context):
    user = update.effective_user
    if update.effective_chat.type != ChatType.PRIVATE:
        await update.message.reply_text("🔒 Send me /renew in a PRIVATE chat.")
        return
    if not await _is_member(context, user.id):
        await update.message.reply_text("❌ Members only. Join the group and try again.")
        return
    token, rec = tokens.renew(
        user.id, user.username or user.full_name, config.TOKEN_VALID_DAYS)
    await update.message.reply_text("♻️ New token issued.\n\n" + _token_msg(token, rec),
                                    parse_mode="Markdown")


# --------------------------------------------------------------------------- #
# admin commands
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
        f"Granted to {tid} for {days} day(s):\n`{token}`", parse_mode="Markdown")


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


def main():
    if not config.TELEGRAM_BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN is missing in server/.env")

    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("token", cmd_token))
    app.add_handler(CommandHandler("mytoken", cmd_mytoken))
    app.add_handler(CommandHandler("renew", cmd_renew))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("grant", cmd_grant))
    app.add_handler(CommandHandler("revoke", cmd_revoke))
    app.add_handler(CommandHandler("revokeuser", cmd_revokeuser))
    app.add_error_handler(on_error)

    log.info("QTapi bot starting (admin=%s, group=%s)...",
             config.TELEGRAM_ADMIN_ID, config.TELEGRAM_GROUP_ID)
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
