"""
PatriotTap License Bot
Telegram bot for generating/managing license keys
Uses Turso (libSQL) HTTP API — shared DB with Netlify Functions
Deploy on Koyeb via Docker
"""

import os
import json
import secrets
import string
import hashlib
import hmac
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timedelta, timezone
from functools import wraps

import httpx
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, ContextTypes,
)

# ── Config — ЗАПОЛНИ СВОИ ДАННЫЕ ─────────────────────────────────
BOT_TOKEN = "8706837235:AAHK8ADJM6KXZk9XRU2aHWIxkOyGpZwMs1Q"
ADMIN_IDS = [8655580052]
SECRET_KEY = "patriot-tap-change-me"         # любой секрет

# Turso (https://turso.tech → Create Database → Copy URL + Token)
TURSO_URL = "libsql://patriottap-tkkqwin-bot.aws-eu-west-1.turso.io"
TURSO_TOKEN = "eyJhbGciOiJFZERTQSIsInR5cCI6IkpXVCJ9.eyJhIjoicnciLCJpYXQiOjE3ODM5MTUxODMsImlkIjoiMDE5ZjU5OWQtNjUwMS03YTNhLWI0YzctMmJkMDMxZTA5YmI3Iiwia2lkIjoiUHhjWmN3SXBsQllrMGE0VDhjWWppcGZBTkY3TEJMbW5uYXVRR2hxODRBbyIsInJpZCI6ImI1Mzk2M2IyLTMyNmMtNDBmYi05NWM0LTdmNzQyZmY0MDJhNSJ9.KtjElXqsEo_6jn7Nmv6UrrvKKWtJ0_10Ce936sXlkgu0Yo_jK_GXVzoAva3rYC_iJu21YtV7xHsk-ucJkL0tCg"

KEY_PREFIX = "PT"
KEY_LENGTH = 16

http_client = httpx.AsyncClient(timeout=15)


# ── Health check (Render requires a port) ───────────────────────
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, format, *args):
        pass

def start_health_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"Health check on port {port}")


# ── Turso HTTP helper ────────────────────────────────────────────
async def turso_exec(sql: str, args: list = None) -> dict:
    """Execute SQL via Turso HTTP API v2 pipeline"""
    url = f"{TURSO_URL}/v2/pipeline"
    stmts = [{"type": "execute", "stmt": {"sql": sql}}]
    if args:
        stmts[0]["stmt"]["args"] = [{"type": "text", "value": str(a)} for a in args]

    resp = await http_client.post(url, json={"requests": stmts}, headers={
        "Authorization": f"Bearer {TURSO_TOKEN}",
        "Content-Type": "application/json",
    })
    resp.raise_for_status()
    return resp.json()


async def turso_query(sql: str, args: list = None) -> list:
    """Query rows from Turso"""
    url = f"{TURSO_URL}/v2/pipeline"
    stmts = [{"type": "execute", "stmt": {"sql": sql}}]
    if args:
        stmts[0]["stmt"]["args"] = [{"type": "text", "value": str(a)} for a in args]
    stmts.append({"type": "close"})

    resp = await http_client.post(url, json={"requests": stmts}, headers={
        "Authorization": f"Bearer {TURSO_TOKEN}",
        "Content-Type": "application/json",
    })
    resp.raise_for_status()
    data = resp.json()

    # Parse result into list of dicts
    results = data.get("results", [])
    if not results:
        return []
    first = results[0]
    cols = [c["name"] for c in first.get("result", {}).get("cols", [])]
    rows = []
    for row in first.get("result", {}).get("rows", []):
        rows.append({cols[i]: row[i].get("value") for i in range(len(cols))})
    return rows


# ── Database init ────────────────────────────────────────────────
async def init_db():
    await turso_exec("""
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
    print("Turso DB initialized.")


# ── Key generation ───────────────────────────────────────────────
def generate_key() -> str:
    alphabet = string.ascii_uppercase + string.digits
    alphabet = alphabet.replace("O", "").replace("I", "").replace("1", "")
    rand = ''.join(secrets.choice(alphabet) for _ in range(KEY_LENGTH))
    return f"{KEY_PREFIX}-{rand[:4]}-{rand[4:8]}-{rand[8:]}"


async def create_key(days: int, created_by: int, note: str = "") -> str:
    key = generate_key()
    expires_at = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
    await turso_exec(
        "INSERT INTO keys (key, days, expires_at, created_by, note) VALUES (?, ?, ?, ?, ?)",
        [key, days, expires_at, created_by, note],
    )
    return key


async def revoke_key(key: str) -> bool:
    result = await turso_exec("UPDATE keys SET revoked = 1 WHERE key = ?", [key])
    return True


async def get_key_info(key: str):
    rows = await turso_query("SELECT * FROM keys WHERE key = ?", [key])
    return rows[0] if rows else None


async def list_keys(active_only=True):
    if active_only:
        return await turso_query(
            "SELECT * FROM keys WHERE revoked = 0 AND expires_at > datetime('now') ORDER BY created_at DESC"
        )
    return await turso_query("SELECT * FROM keys ORDER BY created_at DESC LIMIT 50")


async def count_keys():
    total_r = await turso_query("SELECT COUNT(*) as c FROM keys")
    active_r = await turso_query(
        "SELECT COUNT(*) as c FROM keys WHERE revoked = 0 AND expires_at > datetime('now')"
    )
    return total_r[0]["c"], active_r[0]["c"]


# ── Commands ─────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "*PatriotTap License Bot*\n\n"
        "Commands:\n"
        "/generate `<days> [note]` — Generate a key\n"
        "/revoke `<KEY>` — Revoke a key\n"
        "/info `<KEY>` — Key details\n"
        "/list — List active keys\n"
        "/stats — Statistics\n"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_generate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return await update.message.reply_text("Access denied.")

    args = context.args
    if not args:
        return await update.message.reply_text("Usage: `/generate 30 [note]`", parse_mode="Markdown")

    try:
        days = int(args[0])
    except ValueError:
        return await update.message.reply_text("Days must be a number.")

    note = " ".join(args[1:]) if len(args) > 1 else ""
    key = await create_key(days, update.effective_user.id, note)
    sig = hmac.new(SECRET_KEY.encode(), key.encode(), hashlib.sha256).hexdigest()[:8]
    expires = (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%d")

    text = (
        f"*Key Generated*\n\n"
        f"`{key}`\n\n"
        f"Duration: *{days}* days\n"
        f"Expires: *{expires}*\n"
        f"Signature: `{sig}`\n"
    )
    if note:
        text += f"Note: {note}\n"
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_revoke(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return await update.message.reply_text("Access denied.")
    if not context.args:
        return await update.message.reply_text("Usage: `/revoke PT-XXXX-XXXX-XXXX`", parse_mode="Markdown")

    key = context.args[0].upper()
    info = await get_key_info(key)
    if not info:
        return await update.message.reply_text(f"Key `{key}` not found.", parse_mode="Markdown")

    await revoke_key(key)
    await update.message.reply_text(f"Key `{key}` revoked.", parse_mode="Markdown")


async def cmd_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return await update.message.reply_text("Access denied.")
    if not context.args:
        return await update.message.reply_text("Usage: `/info PT-XXXX-XXXX-XXXX`", parse_mode="Markdown")

    key = context.args[0].upper()
    info = await get_key_info(key)
    if not info:
        return await update.message.reply_text(f"Key `{key}` not found.", parse_mode="Markdown")

    status = "Revoked" if info["revoked"] else ("Expired" if info["expires_at"] < datetime.now(timezone.utc).isoformat() else "Active")

    text = (
        f"*Key Info*\n\n"
        f"Key: `{info['key']}`\n"
        f"HWID: `{info['hwid'] or 'Unbound'}`\n"
        f"Duration: {info['days']} days\n"
        f"Expires: {info['expires_at']}\n"
        f"Status: *{status}*\n"
        f"Created: {info['created_at']}\n"
        f"Note: {info['note'] or 'None'}\n"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return await update.message.reply_text("Access denied.")

    keys = await list_keys(active_only=False)
    if not keys:
        return await update.message.reply_text("No keys found.")

    text = "*All Keys*\n\n"
    for k in keys[:30]:
        status = "Revoked" if k["revoked"] else ("Expired" if k["expires_at"] < datetime.now(timezone.utc).isoformat() else "Active")
        text += f"`{k['key']}` — {status} — {k['expires_at'][:10]}\n"
    if len(keys) > 30:
        text += f"\n...and {len(keys) - 30} more"

    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return await update.message.reply_text("Access denied.")

    total, active = await count_keys()
    text = (
        f"*Statistics*\n\n"
        f"Total: *{total}*\n"
        f"Active: *{active}*\n"
        f"Revoked/Expired: *{total - active}*\n"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


# ── Main ─────────────────────────────────────────────────────────
async def post_init(app: Application):
    await init_db()
    print(f"Admin IDs: {ADMIN_IDS}")
    print("Bot ready.")


def main():
    if not BOT_TOKEN or BOT_TOKEN == "ТВОЙ_БОТ_ТОКЕН":
        return print("ERROR: Заполни BOT_TOKEN в bot.py")
    if not TURSO_URL or "your-db" in TURSO_URL:
        return print("ERROR: Заполни TURSO_URL и TURSO_TOKEN в bot.py")

    start_health_server()

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

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
