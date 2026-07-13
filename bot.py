"""
PatriotTap License Bot
Telegram bot for generating/managing license keys
Deploy on Koyeb via Docker
"""

import os
import sqlite3
import hashlib
import hmac
import secrets
import string
from datetime import datetime, timedelta
from functools import wraps

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
)

# ── Config ───────────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8706837235:AAHK8ADJM6KXZk9XRU2aHWIxkOyGpZwMs1Q")
ADMIN_IDS = [int(x) for x in os.environ.get("ADMIN_IDS", "8655580052").split(",") if x.strip()]
SECRET_KEY = os.environ.get("SECRET_KEY", "patriot-tap-secret-change-me")
DB_PATH = os.environ.get("DB_PATH", "keys.db")
KEY_PREFIX = "PT"
KEY_LENGTH = 16  # chars after prefix

# ── Database ─────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT UNIQUE NOT NULL,
            hwid TEXT DEFAULT '',
            days INTEGER NOT NULL,
            expires_at TEXT NOT NULL,
            revoked INTEGER DEFAULT 0,
            created_by INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            note TEXT DEFAULT ''
        )
    """)
    conn.commit()
    conn.close()

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

# ── Key generation ───────────────────────────────────────────────
def generate_key():
    alphabet = string.ascii_uppercase + string.digits
    # Remove confusing chars (0/O, I/1)
    alphabet = alphabet.replace("O", "").replace("I", "").replace("1", "")
    random_part = ''.join(secrets.choice(alphabet) for _ in range(KEY_LENGTH))
    return f"{KEY_PREFIX}-{random_part[:4]}-{random_part[4:8]}-{random_part[8:]}"

def generate_hmac_signature(key: str) -> str:
    return hmac.new(SECRET_KEY.encode(), key.encode(), hashlib.sha256).hexdigest()[:8]

def create_key(days: int, created_by: int, note: str = "") -> str:
    key = generate_key()
    expires_at = (datetime.utcnow() + timedelta(days=days)).isoformat()
    db = get_db()
    db.execute(
        "INSERT INTO keys (key, days, expires_at, created_by, note) VALUES (?, ?, ?, ?, ?)",
        (key, days, expires_at, created_by, note),
    )
    db.commit()
    db.close()
    return key

def revoke_key(key: str) -> bool:
    db = get_db()
    c = db.execute("UPDATE keys SET revoked = 1 WHERE key = ?", (key,))
    db.commit()
    changed = c.rowcount > 0
    db.close()
    return changed

def get_key_info(key: str):
    db = get_db()
    row = db.execute("SELECT * FROM keys WHERE key = ?", (key,)).fetchone()
    db.close()
    return dict(row) if row else None

def list_keys(active_only=True):
    db = get_db()
    if active_only:
        rows = db.execute(
            "SELECT * FROM keys WHERE revoked = 0 AND expires_at > datetime('now') ORDER BY created_at DESC"
        ).fetchall()
    else:
        rows = db.execute("SELECT * FROM keys ORDER BY created_at DESC LIMIT 50").fetchall()
    db.close()
    return [dict(r) for r in rows]

def count_keys():
    db = get_db()
    total = db.execute("SELECT COUNT(*) FROM keys").fetchone()[0]
    active = db.execute(
        "SELECT COUNT(*) FROM keys WHERE revoked = 0 AND expires_at > datetime('now')"
    ).fetchone()[0]
    db.close()
    return total, active

# ── Auth decorator ───────────────────────────────────────────────
def admin_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id not in ADMIN_IDS:
            await update.message.reply_text("Access denied.")
            return
        return await func(update, context)
    return wrapper

# ── Commands ─────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "**PatriotTap License Bot**\n\n"
        "Commands:\n"
        "/generate `<days> [note]` — Generate a key\n"
        "/revoke `<KEY>` — Revoke a key\n"
        "/info `<KEY>` — Key details\n"
        "/list — List active keys\n"
        "/stats — Key statistics\n"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def cmd_generate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Access denied.")
        return

    args = context.args
    if not args:
        await update.message.reply_text("Usage: `/generate 30 [note]`", parse_mode="Markdown")
        return

    try:
        days = int(args[0])
    except ValueError:
        await update.message.reply_text("Days must be a number.")
        return

    note = " ".join(args[1:]) if len(args) > 1 else ""

    key = create_key(days, update.effective_user.id, note)
    sig = generate_hmac_signature(key)
    expires = (datetime.utcnow() + timedelta(days=days)).strftime("%Y-%m-%d")

    text = (
        f"**Key Generated**\n\n"
        f"`{key}`\n\n"
        f"Duration: **{days}** days\n"
        f"Expires: **{expires}**\n"
        f"Signature: `{sig}`\n"
    )
    if note:
        text += f"Note: {note}\n"

    await update.message.reply_text(text, parse_mode="Markdown")

async def cmd_revoke(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Access denied.")
        return

    if not context.args:
        await update.message.reply_text("Usage: `/revoke PT-XXXX-XXXX-XXXX`", parse_mode="Markdown")
        return

    key = context.args[0].upper()
    info = get_key_info(key)
    if not info:
        await update.message.reply_text(f"Key `{key}` not found.", parse_mode="Markdown")
        return
    if info["revoked"]:
        await update.message.reply_text(f"Key `{key}` is already revoked.", parse_mode="Markdown")
        return

    revoke_key(key)
    await update.message.reply_text(f"Key `{key}` has been **revoked**.", parse_mode="Markdown")

async def cmd_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Access denied.")
        return

    if not context.args:
        await update.message.reply_text("Usage: `/info PT-XXXX-XXXX-XXXX`", parse_mode="Markdown")
        return

    key = context.args[0].upper()
    info = get_key_info(key)
    if not info:
        await update.message.reply_text(f"Key `{key}` not found.", parse_mode="Markdown")
        return

    status = "Revoked" if info["revoked"] else ("Expired" if datetime.fromisoformat(info["expires_at"]) < datetime.utcnow() else "Active")
    status_color = "Red" if status in ("Revoked", "Expired") else "Green"

    text = (
        f"**Key Info**\n\n"
        f"Key: `{info['key']}`\n"
        f"HWID: `{info['hwid'] or 'Unbound'}`\n"
        f"Duration: {info['days']} days\n"
        f"Expires: {info['expires_at']}\n"
        f"Status: **{status}**\n"
        f"Created: {info['created_at']}\n"
        f"Note: {info['note'] or 'None'}\n"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Access denied.")
        return

    keys = list_keys(active_only=False)
    if not keys:
        await update.message.reply_text("No keys found.")
        return

    text = "**All Keys**\n\n"
    for k in keys[:30]:  # Telegram message limit
        status = "Revoked" if k["revoked"] else ("Expired" if datetime.fromisoformat(k["expires_at"]) < datetime.utcnow() else "Active")
        text += f"`{k['key']}` — {status} — {k['expires_at'][:10]}\n"

    if len(keys) > 30:
        text += f"\n... and {len(keys) - 30} more"

    await update.message.reply_text(text, parse_mode="Markdown")

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Access denied.")
        return

    total, active = count_keys()
    text = (
        f"**Statistics**\n\n"
        f"Total keys: **{total}**\n"
        f"Active keys: **{active}**\n"
        f"Revoked/Expired: **{total - active}**\n"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

# ── Main ─────────────────────────────────────────────────────────
def main():
    if not BOT_TOKEN:
        print("ERROR: BOT_TOKEN not set")
        return
    if not ADMIN_IDS:
        print("WARNING: ADMIN_IDS not set, no one can manage keys")

    init_db()
    print("Database initialized.")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("generate", cmd_generate))
    app.add_handler(CommandHandler("gen", cmd_generate))
    app.add_handler(CommandHandler("revoke", cmd_revoke))
    app.add_handler(CommandHandler("info", cmd_info))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("stats", cmd_stats))

    print("Bot starting...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
