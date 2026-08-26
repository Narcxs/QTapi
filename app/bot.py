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
import secrets
import time
from datetime import datetime, timezone

import httpx
from telegram import (BotCommand, InlineKeyboardButton, InlineKeyboardMarkup,
                      Update)
from telegram.constants import ChatType
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

from . import config, hwids, menthorq, mq_tokens, tokens, warns

_group_link_cache = {"link": None}

# ConvexValue license requests: users waiting to type their HWID, and pending
# admin approvals (short id -> request). In-memory is enough: worst case after
# a bot restart, the user taps the button again / the admin sees "expired".
_cv_awaiting_hwid = set()          # telegram user ids
_cv_requests = {}                  # req_id -> {user_id, username, hwid}

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
    if config.TELEGRAM_CHANNEL_LINK:
        rows.append([InlineKeyboardButton("📢 Main Channel",
                                          url=config.TELEGRAM_CHANNEL_LINK)])
    return InlineKeyboardMarkup(rows)


async def _links_row(context: ContextTypes.DEFAULT_TYPE):
    """One row with the public links (Documentation in the group + channel)."""
    row = []
    link = await _group_link(context)
    if link:
        row.append(InlineKeyboardButton("📖 Documentation", url=link))
    if config.TELEGRAM_CHANNEL_LINK:
        row.append(InlineKeyboardButton("📢 Main Channel",
                                        url=config.TELEGRAM_CHANNEL_LINK))
    return row


async def _menu_keyboard(context: ContextTypes.DEFAULT_TYPE,
                         admin: bool = False) -> InlineKeyboardMarkup:
    """Main menu: token / renew / api health / cv license / menthorq / links.
    The admin gets an extra row: create MenthorQ tokens (mq_...)."""
    rows = [[InlineKeyboardButton("🔑 My Token", callback_data="menu_token"),
             InlineKeyboardButton("♻️ Renew", callback_data="menu_renew")],
            [InlineKeyboardButton("🩺 API Health", callback_data="menu_health"),
             InlineKeyboardButton("🖥️ Get CV License", callback_data="menu_cv")],
            [InlineKeyboardButton("📊 MenthorQ Levels", callback_data="menu_mq")]]
    if admin:
        rows.append([InlineKeyboardButton("🔐 Create MQ Token",
                                          callback_data="mqt_menu")])
    links = await _links_row(context)
    if links:
        rows.append(links)
    return InlineKeyboardMarkup(rows)


async def _token_keyboard(context: ContextTypes.DEFAULT_TYPE) -> InlineKeyboardMarkup:
    """Under an active token card: renew (enabled at expiry) + links."""
    rows = [[InlineKeyboardButton("♻️ Renew", callback_data="menu_renew")]]
    links = await _links_row(context)
    if links:
        rows.append(links)
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
        f"<code>{base}/api/spx/classic/zero?token={token}</code>\n\n"
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
_GROUP_NOTICE = ("🔒 Please use my commands in a <b>private chat</b> with me — "
                 "open a DM and send /start.")


async def cmd_start(update: Update, context):
    """Hidden entry point (not in the menu): welcome + main menu buttons."""
    if update.effective_chat.type != ChatType.PRIVATE:
        await update.message.reply_text(_GROUP_NOTICE, parse_mode="HTML")
        return
    await update.message.reply_text(
        _WELCOME, parse_mode="HTML",
        reply_markup=await _menu_keyboard(context, _is_admin(update.effective_user.id)))


async def cmd_help(update: Update, context):
    if update.effective_chat.type != ChatType.PRIVATE:
        await update.message.reply_text(_GROUP_NOTICE, parse_mode="HTML")
        return
    txt = _HELP
    if _is_admin(update.effective_user.id):
        txt += ("\n\n<b>Admin</b>\n/list\n/stats\n/grant <id> [days]\n"
                "/revoke <token>\n/revokeuser <id>\n\n"
                "<b>ConvexValue</b>\n/cvadd <hwid>\n/cvremove <hwid>\n/cvlist\n\n"
                "<b>MenthorQ</b>\n/mqlist\n/mqrevoke <token>\n\n"
                "<b>Moderation</b>\n/warn [reason] (reply)\n/warns [id] (reply)\n/unwarn (reply)")
    await update.message.reply_text(
        txt, parse_mode="HTML",
        reply_markup=await _menu_keyboard(context, _is_admin(update.effective_user.id)))


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
# API health (fetched internally - no dependency on the public domain/proxy)
# --------------------------------------------------------------------------- #
async def _health_text() -> str:
    base = f"http://127.0.0.1:{config.SERVER_PORT}"
    try:
        async with httpx.AsyncClient(timeout=5.0) as c:
            h = (await c.get(f"{base}/health")).json()
            try:
                st = (await c.get(f"{base}/status")).json()
            except Exception:  # noqa: BLE001 - freshness is best-effort
                st = None
    except Exception as e:  # noqa: BLE001
        return ("<b>🩺 QTapi — API Health</b>\n\n"
                f"🔴 <b>API unreachable</b> ({type(e).__name__})\n"
                "The API process seems down — it needs a restart on the VPS.")

    lines = ["<b>🩺 QTapi — API Health</b>\n",
             f"API: ✅ {h.get('status', '?')}"]
    if h.get("poll_enabled"):
        lines.append(f"Polling: every {h.get('interval_s')}s")
    else:
        lines.append("Polling: ⏸️ disabled")
    lines.append("Tickers: " + ", ".join(h.get("tickers", [])))
    lines.append("Periods: " + ", ".join(h.get("periods", [])))
    if h.get("market_hours_only"):
        if h.get("market_open"):
            lines.append("Market: 🟢 OPEN (fetching)")
        else:
            nxt = (h.get("next_open_et") or "")[:16].replace("T", " ")
            lines.append(f"Market: 🔴 CLOSED — next fetch {nxt} ET")
    else:
        lines.append("Market hours gate: OFF (fetching 24/7)")
    if st:
        ages = [f.get("age_s") for t in st.get("files", {}).values()
                for f in t.values()
                if isinstance(f, dict) and f.get("age_s") is not None]
        if ages:
            lines.append(f"Freshest data: {min(ages)}s ago")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# MenthorQ levels (free text, no token required)
# --------------------------------------------------------------------------- #
def _fnum(v) -> str:
    if v is None:
        return "-"
    try:
        s = f"{float(v):,.2f}".rstrip("0").rstrip(".")
        return s or "0"
    except (TypeError, ValueError):
        return str(v)


def _mq_find(types: dict, level_type: str, name: str):
    for lv in types.get(level_type, []):
        if lv.get("name") == name:
            return lv.get("value")
    return None


def _mq_summary_text(data: dict) -> str:
    if not data:
        return ("<b>📊 MenthorQ Levels</b>\n\n"
                "No data yet — the first fetch runs shortly after server start. "
                "Try again in a minute.")
    from datetime import datetime, timezone
    upd = max((r.get("fetched_at", 0) for r in data.values()), default=0)
    upd_txt = (datetime.fromtimestamp(upd, tz=timezone.utc).strftime("%H:%M UTC")
               if upd else "?")
    lines = [f"<b>📊 MenthorQ Levels</b>  ·  updated {upd_txt}\n"]
    for t in config.MENTHORQ_TICKERS:
        rec = data.get(t)
        if not rec:
            continue
        types = rec.get("types", {})
        cr = _mq_find(types, "gamma_levels", "Call Resistance")
        ps = _mq_find(types, "gamma_levels", "Put Support")
        hvl = _mq_find(types, "gamma_levels", "HVL")
        mn = _mq_find(types, "gamma_levels", "1D Min")
        mx = _mq_find(types, "gamma_levels", "1D Max")
        lines.append(f"<b>{t}</b>  <i>({rec.get('date', '')})</i>")
        lines.append(f"CR <code>{_fnum(cr)}</code> · PS <code>{_fnum(ps)}</code> · "
                     f"HVL <code>{_fnum(hvl)}</code>")
        lines.append(f"1D <code>{_fnum(mn)}</code> – <code>{_fnum(mx)}</code>\n")
    lines.append("Tap a ticker for full levels (0DTE, blindspots, swing):")
    return "\n".join(lines)


_MQ_SECTIONS = (("gamma_levels", "Gamma Levels (EOD)"),
                ("gamma_levels_intraday", "Gamma Intraday"),
                ("blindspots", "Blindspots"),
                ("swing_levels", "Swing Levels"))


def _mq_detail_text(data: dict, ticker: str) -> str:
    rec = data.get(ticker)
    if not rec:
        return f"No MenthorQ data for {ticker} yet."
    lines = [f"<b>📊 MenthorQ — {ticker}</b>  <i>({rec.get('date', '')})</i>\n"]
    for lt, title in _MQ_SECTIONS:
        vals = rec.get("types", {}).get(lt)
        if not vals:
            continue
        lines.append(f"<b>{title}</b>")
        for lv in vals:
            lines.append(f"{lv.get('name')}: <code>{_fnum(lv.get('value'))}</code>")
        lines.append("")
    return "\n".join(lines).strip()


def _mq_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(t, callback_data=f"mq_{t}")
        for t in config.MENTHORQ_TICKERS
    ]])


# --------------------------------------------------------------------------- #
# buttons
# --------------------------------------------------------------------------- #
async def cb_menu(update: Update, context):
    """Handles the main-menu buttons."""
    q = update.callback_query
    user = q.from_user
    # buttons are private-chat only: never act from a group message
    msg_chat = getattr(getattr(q, "message", None), "chat", None)
    if msg_chat is not None and msg_chat.type != ChatType.PRIVATE:
        await q.answer("Please use me in a private chat — open a DM and send /start.",
                       show_alert=True)
        return
    if q.data == "mqt_menu":
        if not _is_admin(user.id):
            await q.answer("Admin only.", show_alert=True)
            return
        await q.answer()
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("1 week", callback_data="mqt_7"),
            InlineKeyboardButton("2 weeks", callback_data="mqt_14"),
            InlineKeyboardButton("1 month", callback_data="mqt_30"),
        ]])
        await q.edit_message_text(
            "<b>🔐 Create MenthorQ Token</b>\n\nChoose the validity period:",
            parse_mode="HTML", reply_markup=kb)
        return
    if q.data.startswith("mqt_"):
        if not _is_admin(user.id):
            await q.answer("Admin only.", show_alert=True)
            return
        days = int(q.data.split("_")[1])
        token, rec = mq_tokens.create(days)
        await q.answer("Token created ✅")
        try:
            await q.edit_message_text(
                "<b>🔐 MenthorQ Token created</b>\n\n"
                f"Valid until: <b>{mq_tokens.fmt_exp(rec)}</b>\n\n"
                f"<code>{token}</code>\n\n"
                "Share it with your client — they paste it in the indicator's "
                "<b>MQ Token</b> setting.\n"
                "Manage: /mqlist · /mqrevoke &lt;token&gt;",
                parse_mode="HTML")
        except Exception:  # noqa: BLE001 - message too old
            await q.message.reply_text(f"<code>{token}</code>", parse_mode="HTML")
        return
    if q.data == "menu_cv":
        _cv_awaiting_hwid.add(user.id)
        await q.answer()
        await q.message.reply_text(
            "<b>🖥️ ConvexValue license</b>\n\n"
            "Send me your <b>Machine ID (HWID)</b> in your next message.\n"
            "I'll forward it to the admin for approval.",
            parse_mode="HTML")
        return
    if q.data == "menu_health":
        text, markup = await _health_text(), await _menu_keyboard(context, _is_admin(user.id))
    elif q.data in ("menu_mq", "mq_back") or q.data.startswith("mq_"):
        # MenthorQ levels: free of charge, but GROUP MEMBERS ONLY
        if not await _is_member(context, user.id):
            text, markup = _JOIN, await _join_keyboard(context)
        elif q.data == "menu_mq" or q.data == "mq_back":
            text, markup = _mq_summary_text(menthorq.get_levels()), _mq_keyboard()
        else:
            text = _mq_detail_text(menthorq.get_levels(), q.data[3:])
            markup = InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Back", callback_data="mq_back")]])
    elif q.data == "menu_renew":
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


async def on_text(update: Update, context):
    """Captures the Machine ID after the user tapped 'Get CV License'."""
    user = update.effective_user
    if user.id not in _cv_awaiting_hwid:
        return
    _cv_awaiting_hwid.discard(user.id)
    hwid = (update.message.text or "").strip()
    if not (3 <= len(hwid) <= 128) or "\n" in hwid:
        await update.message.reply_text(
            "⚠️ That doesn't look like a valid Machine ID. "
            "Tap 🖥️ Get CV License and try again.")
        return
    if hwids.is_active(hwid):
        await update.message.reply_text(
            "✅ This Machine ID is already licensed — you can log in from the app.")
        return
    req_id = secrets.token_hex(3)
    _cv_requests[req_id] = {
        "user_id": user.id,
        "username": user.username or user.full_name,
        "hwid": hwid,
    }
    await update.message.reply_text(
        "⏳ Request sent! You'll be notified once the admin reviews it.")
    if not config.TELEGRAM_ADMIN_ID:
        log.warning("CV request from %s but TELEGRAM_ADMIN_ID is not set", user.id)
        return
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"cv_ok:{req_id}"),
        InlineKeyboardButton("❌ Refuse", callback_data=f"cv_no:{req_id}"),
    ]])
    await context.bot.send_message(
        config.TELEGRAM_ADMIN_ID,
        "<b>🖥️ ConvexValue license request</b>\n\n"
        f"👤 @{_cv_requests[req_id]['username']} (id {user.id})\n"
        f"🆔 <code>{hwid}</code>",
        parse_mode="HTML", reply_markup=kb)


async def cb_cv_decide(update: Update, context):
    """Admin approves/refuses a ConvexValue license request."""
    q = update.callback_query
    if not _is_admin(q.from_user.id):
        await q.answer("Only the admin can decide.", show_alert=True)
        return
    action, req_id = q.data.split(":", 1)
    req = _cv_requests.pop(req_id, None)
    if req is None:
        await q.answer("Request expired or already handled.", show_alert=True)
        return
    approved = action == "cv_ok"
    if approved:
        hwids.add(req["hwid"], username=req["username"],
                  telegram_id=req["user_id"])
    await q.answer("Approved ✅" if approved else "Refused ❌")
    try:
        verdict = "✅ APPROVED" if approved else "❌ REFUSED"
        await q.edit_message_text(
            (q.message.text_html or "") + f"\n\n<b>{verdict}</b>",
            parse_mode="HTML")
    except Exception:  # noqa: BLE001 - message too old/unchanged
        pass
    try:
        if approved:
            await context.bot.send_message(
                req["user_id"],
                "🎉 Your ConvexValue license is now <b>ACTIVE</b>!\n\n"
                f"Machine ID: <code>{req['hwid']}</code>\n"
                "You can now log in from the app.",
                parse_mode="HTML")
        else:
            await context.bot.send_message(
                req["user_id"],
                "❌ Your ConvexValue license request was refused.\n"
                "Contact the admin in the group for more info.")
    except Exception:  # noqa: BLE001 - user blocked the bot, etc.
        pass


async def cb_check_entry(update: Update, context):
    """Handles the '✅ Verify membership' button."""
    q = update.callback_query
    msg_chat = getattr(getattr(q, "message", None), "chat", None)
    if msg_chat is not None and msg_chat.type != ChatType.PRIVATE:
        await q.answer("Please use me in a private chat.", show_alert=True)
        return
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
# ConvexValue HWID licenses (admin only)
# --------------------------------------------------------------------------- #
async def cmd_cvadd(update: Update, context):
    if not _is_admin(update.effective_user.id):
        return
    hwid = " ".join(context.args).strip()
    if not hwid:
        await update.message.reply_text("Usage: /cvadd <hwid>")
        return
    hwids.add(hwid)
    await update.message.reply_text(f"✅ HWID added & activated:\n<code>{hwid}</code>",
                                    parse_mode="HTML")


async def cmd_cvremove(update: Update, context):
    if not _is_admin(update.effective_user.id):
        return
    hwid = " ".join(context.args).strip()
    if not hwid:
        await update.message.reply_text("Usage: /cvremove <hwid>")
        return
    ok = hwids.remove(hwid)
    await update.message.reply_text(
        f"🗑️ HWID removed:\n<code>{hwid}</code>" if ok
        else "HWID not found.", parse_mode="HTML")


async def cmd_cvlist(update: Update, context):
    if not _is_admin(update.effective_user.id):
        return
    items = hwids.list_all()
    if not items:
        await update.message.reply_text("No licensed devices yet. Add one with /cvadd <hwid>")
        return
    lines = ["<b>🖥️ Licensed devices (ConvexValue)</b>\n"]
    for hwid, rec in items:
        flag = "✅" if rec.get("active") else "❌"
        owner = f" — @{rec.get('username')}" if rec.get("username") else ""
        lines.append(f"{flag} <code>{hwid}</code>{owner}")
    text = "\n".join(lines)
    for i in range(0, len(text), 3500):        # respect Telegram's 4096 limit
        await update.message.reply_text(text[i:i + 3500], parse_mode="HTML")


# --------------------------------------------------------------------------- #
# MenthorQ tokens (admin only)
# --------------------------------------------------------------------------- #
async def cmd_mqlist(update: Update, context):
    if not _is_admin(update.effective_user.id):
        return
    items = mq_tokens.list_all()
    if not items:
        await update.message.reply_text(
            "No MenthorQ tokens yet. Create one with the 🔐 button in /start.")
        return
    lines = ["<b>🔐 MenthorQ tokens</b>\n"]
    for t, rec in items:
        flag = "REVOKED" if rec.get("revoked") else mq_tokens.fmt_exp(rec)
        label = f" ({rec.get('label')})" if rec.get("label") else ""
        lines.append(f"<code>{t[:16]}…</code> — {flag}{label}")
    text = "\n".join(lines)
    for i in range(0, len(text), 3500):
        await update.message.reply_text(text[i:i + 3500], parse_mode="HTML")


async def cmd_mqrevoke(update: Update, context):
    if not _is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Usage: /mqrevoke <token>")
        return
    ok = mq_tokens.revoke(context.args[0])
    await update.message.reply_text("Revoked." if ok else "Token not found.")


# --------------------------------------------------------------------------- #
# warning system (admin only - used in the GROUP by replying to a message)
# --------------------------------------------------------------------------- #
def _target_from_reply(update: Update):
    msg = update.message
    if msg and msg.reply_to_message and msg.reply_to_message.from_user:
        return msg.reply_to_message.from_user
    return None


def _display_name(user) -> str:
    return f"@{user.username}" if getattr(user, "username", None) else \
        getattr(user, "full_name", str(getattr(user, "id", "?")))


async def cmd_warn(update: Update, context):
    if not _is_admin(update.effective_user.id):
        return
    target = _target_from_reply(update)
    if target is None:
        await update.message.reply_text(
            "Usage: reply to a user's message with /warn [reason]")
        return
    if _is_admin(target.id) or getattr(target, "is_bot", False):
        await update.message.reply_text("You can't warn an admin or a bot.")
        return

    reason = " ".join(context.args).strip()
    n = warns.add(target.id, update.effective_user.id, reason)
    name = _display_name(target)

    if n >= config.WARN_THRESHOLD:
        # timed ban: Telegram lifts it automatically after WARN_BAN_DAYS
        until = int(time.time()) + config.WARN_BAN_DAYS * 86400
        try:
            await context.bot.ban_chat_member(
                config.TELEGRAM_GROUP_ID, target.id, until_date=until)
        except Exception as e:  # noqa: BLE001
            log.warning("ban failed for %s: %s", target.id, e)
            await update.message.reply_text(
                f"⚠️ {name} reached {n}/{config.WARN_THRESHOLD} warnings but I "
                "couldn't ban them — am I group admin with ban rights?")
            return
        warns.reset(target.id, banned=True)
        await update.message.reply_text(
            f"🔨 <b>{name}</b> has been <b>banned for "
            f"{config.WARN_BAN_DAYS} day(s)</b> "
            f"({config.WARN_THRESHOLD} warnings).", parse_mode="HTML")
        try:
            await context.bot.send_message(
                target.id,
                f"🔨 You have been banned from the group for "
                f"{config.WARN_BAN_DAYS} day(s) after "
                f"{config.WARN_THRESHOLD} warnings."
                + (f"\nLast reason: {reason}" if reason else ""))
        except Exception:  # noqa: BLE001 - user never started the bot
            pass
        return

    txt = f"⚠️ <b>{name}</b> — warning <b>{n}/{config.WARN_THRESHOLD}</b>"
    if reason:
        txt += f"\nReason: {reason}"
    if n == config.WARN_THRESHOLD - 1:
        txt += f"\n<i>Next warning = {config.WARN_BAN_DAYS} day(s) ban.</i>"
    await update.message.reply_text(txt, parse_mode="HTML")
    try:
        await context.bot.send_message(
            target.id,
            f"⚠️ You received a warning ({n}/{config.WARN_THRESHOLD}) in the group."
            + (f"\nReason: {reason}" if reason else ""))
    except Exception:  # noqa: BLE001
        pass


async def cmd_warns(update: Update, context):
    if not _is_admin(update.effective_user.id):
        return
    target = _target_from_reply(update)
    if target is None and context.args:
        try:
            tid = int(context.args[0])
            target = type("U", (), {"id": tid, "username": "", "full_name": str(tid)})()
        except ValueError:
            target = None
    if target is None:
        await update.message.reply_text(
            "Usage: reply with /warns, or /warns <telegram_id>")
        return
    rec = warns.get(target.id)
    n = len(rec.get("warns", []))
    lines = [f"⚠️ <b>{_display_name(target)}</b>: <b>{n}/{config.WARN_THRESHOLD}</b> "
             f"warnings · {rec.get('bans', 0)} ban(s) total."]
    for w in rec.get("warns", [])[-5:]:
        d = datetime.fromtimestamp(w.get("at", 0), tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M")
        r = f" — {w.get('reason')}" if w.get("reason") else ""
        lines.append(f"• {d} UTC{r}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_unwarn(update: Update, context):
    if not _is_admin(update.effective_user.id):
        return
    target = _target_from_reply(update)
    if target is None:
        await update.message.reply_text(
            "Usage: reply to a user's message with /unwarn")
        return
    n = warns.remove_last(target.id)
    await update.message.reply_text(
        f"✅ Last warning removed for <b>{_display_name(target)}</b> — "
        f"now {n}/{config.WARN_THRESHOLD}.", parse_mode="HTML")


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
    app.add_handler(CommandHandler("cvadd", cmd_cvadd))
    app.add_handler(CommandHandler("cvremove", cmd_cvremove))
    app.add_handler(CommandHandler("cvlist", cmd_cvlist))
    app.add_handler(CommandHandler("mqlist", cmd_mqlist))
    app.add_handler(CommandHandler("mqrevoke", cmd_mqrevoke))
    app.add_handler(CommandHandler("warn", cmd_warn))
    app.add_handler(CommandHandler("warns", cmd_warns))
    app.add_handler(CommandHandler("unwarn", cmd_unwarn))
    app.add_handler(CallbackQueryHandler(cb_check_entry, pattern="^check_entry$"))
    app.add_handler(CallbackQueryHandler(cb_menu, pattern="^menu_(token|renew|health|cv|mq)$"))
    app.add_handler(CallbackQueryHandler(cb_menu, pattern="^mq_(ES|NQ|VIX|GC|back)$"))
    app.add_handler(CallbackQueryHandler(cb_menu, pattern="^mqt_(menu|7|14|30)$"))
    app.add_handler(CallbackQueryHandler(cb_cv_decide, pattern="^cv_(ok|no):"))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, on_text))
    app.add_error_handler(on_error)

    log.info("QTapi bot starting (admin=%s, group=%s)...",
             config.TELEGRAM_ADMIN_ID, config.TELEGRAM_GROUP_ID)
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
