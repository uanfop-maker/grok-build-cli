#!/usr/bin/env python3
import asyncio
import json
import os
import re
import sqlite3
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, MessageHandler, CommandHandler,
    CallbackQueryHandler, filters, ContextTypes,
)

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_USER_ID = int(os.environ.get("ALLOWED_USER_ID", "0"))
AUTH_JSON_PATH = "/root/.grok/auth.json"
OIDC_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
OIDC_TOKEN_URL = "https://auth.x.ai/oauth2/token"
XAI_API_BASE = "https://api.x.ai/v1"
CHAT_MODEL = "grok-3"
DB_PATH = "/data/grok_sessions.db"

ANSI = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')

SYSTEM_PROMPT = """你是 @DaDaBiteGrokBot，運行在 Zeabur 雲端的 Grok AI 助手。

你有兩種能力：
1. 對話模式（目前）：回答問題、聊天、提供建議。
2. 程式執行模式：用戶輸入 `/build <任務>` 讓 Grok Build CLI 實際寫程式、建立專案。

當用戶需要「寫程式」、「建立腳本」、「開發功能」時，主動提示他們用 `/build` 指令。
用繁體中文回覆，除非用戶用其他語言。"""


# ── Database ──────────────────────────────────────────────────────────────────

def init_db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (session_id) REFERENCES sessions(id)
        );
        CREATE TABLE IF NOT EXISTS user_state (
            user_id INTEGER PRIMARY KEY,
            current_session_id TEXT,
            FOREIGN KEY (current_session_id) REFERENCES sessions(id)
        );
    """)
    con.commit()
    con.close()


def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def get_or_create_session(user_id: int) -> str:
    """Return the user's current session ID, creating one if needed."""
    with db() as con:
        row = con.execute("SELECT current_session_id FROM user_state WHERE user_id=?", (user_id,)).fetchone()
        if row and row["current_session_id"]:
            # Verify session still exists
            s = con.execute("SELECT id FROM sessions WHERE id=?", (row["current_session_id"],)).fetchone()
            if s:
                return row["current_session_id"]
        # Create default session
        sid = f"sess_{uuid.uuid4().hex[:8]}"
        con.execute("INSERT INTO sessions (id, user_id, name, created_at) VALUES (?,?,?,?)",
                    (sid, user_id, "預設對話", now_iso()))
        con.execute("INSERT OR REPLACE INTO user_state (user_id, current_session_id) VALUES (?,?)",
                    (user_id, sid))
        return sid


def set_current_session(user_id: int, session_id: str):
    with db() as con:
        con.execute("INSERT OR REPLACE INTO user_state (user_id, current_session_id) VALUES (?,?)",
                    (user_id, session_id))


def get_session_name(session_id: str) -> str:
    with db() as con:
        row = con.execute("SELECT name FROM sessions WHERE id=?", (session_id,)).fetchone()
        return row["name"] if row else "未知"


def list_sessions(user_id: int) -> list:
    with db() as con:
        return con.execute(
            "SELECT id, name, created_at FROM sessions WHERE user_id=? ORDER BY created_at DESC",
            (user_id,)
        ).fetchall()


def get_history(session_id: str, limit: int = 40) -> list[dict]:
    with db() as con:
        rows = con.execute(
            "SELECT role, content FROM messages WHERE session_id=? ORDER BY id DESC LIMIT ?",
            (session_id, limit)
        ).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


def save_message(session_id: str, role: str, content: str):
    with db() as con:
        con.execute(
            "INSERT INTO messages (session_id, role, content, created_at) VALUES (?,?,?,?)",
            (session_id, role, content, now_iso())
        )


def clear_session_messages(session_id: str):
    with db() as con:
        con.execute("DELETE FROM messages WHERE session_id=?", (session_id,))


def delete_session(session_id: str, user_id: int):
    with db() as con:
        con.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
        con.execute("DELETE FROM sessions WHERE id=? AND user_id=?", (session_id, user_id))
        # Clear user state if it pointed here
        row = con.execute("SELECT current_session_id FROM user_state WHERE user_id=?", (user_id,)).fetchone()
        if row and row["current_session_id"] == session_id:
            con.execute("UPDATE user_state SET current_session_id=NULL WHERE user_id=?", (user_id,))


# ── xAI API ──────────────────────────────────────────────────────────────────

def strip_ansi(t):
    return ANSI.sub('', t).replace('\r\n', '\n').replace('\r', '\n').strip()


def load_auth():
    with open(AUTH_JSON_PATH) as f:
        data = json.load(f)
    for k, v in data.items():
        if "x.ai" in k:
            return k, v
    raise ValueError("No xAI auth entry")


def parse_iso_dt(s: str) -> datetime:
    s = re.sub(r'(\.\d{6})\d+', r'\1', s).replace('Z', '+00:00')
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def get_access_token() -> str:
    auth_key, auth_data = load_auth()
    try:
        exp = parse_iso_dt(auth_data.get("expires_at", ""))
        if datetime.now(timezone.utc) >= exp - timedelta(minutes=5):
            await refresh_access_token(auth_key, auth_data)
            _, auth_data = load_auth()
    except Exception:
        pass
    return auth_data["key"]


async def refresh_access_token(auth_key: str, auth_data: dict):
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(OIDC_TOKEN_URL, data={
            "grant_type": "refresh_token",
            "refresh_token": auth_data["refresh_token"],
            "client_id": OIDC_CLIENT_ID,
        })
        resp.raise_for_status()
        tokens = resp.json()
    with open(AUTH_JSON_PATH) as f:
        full = json.load(f)
    expires_in = tokens.get("expires_in", 7200)
    full[auth_key]["key"] = tokens["access_token"]
    full[auth_key]["expires_at"] = (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).isoformat()
    if "refresh_token" in tokens:
        full[auth_key]["refresh_token"] = tokens["refresh_token"]
    with open(AUTH_JSON_PATH, "w") as f:
        json.dump(full, f, ensure_ascii=False, indent=2)


async def xai_chat(history: list[dict]) -> str:
    token = await get_access_token()
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history
    async with httpx.AsyncClient(timeout=90) as client:
        resp = await client.post(
            f"{XAI_API_BASE}/chat/completions",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"model": CHAT_MODEL, "messages": messages, "max_tokens": 2048},
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]


# ── Auth ─────────────────────────────────────────────────────────────────────

def auth_check(uid: int) -> bool:
    return ALLOWED_USER_ID == 0 or uid == ALLOWED_USER_ID


build_sessions: dict[int, asyncio.subprocess.Process] = {}


# ── Handlers ─────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not auth_check(uid): return
    sid = get_or_create_session(uid)
    name = get_session_name(sid)
    await update.message.reply_text(
        f"👋 Grok Bot 就緒！目前對話：**{name}**\n\n"
        "• 直接傳訊息 → Grok AI 對話\n"
        "• /build <任務> → Grok 執行程式任務\n"
        "• /new [名稱] → 建立新對話\n"
        "• /sessions → 切換對話\n"
        "• /rename 新名稱 → 重新命名\n"
        "• /clear → 清除目前對話紀錄\n"
        "• /delete → 刪除目前對話\n"
        "• /stop → 停止執行中的 build",
        parse_mode="Markdown"
    )


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not auth_check(uid): return
    name = " ".join(context.args) if context.args else f"對話 {datetime.now().strftime('%m/%d %H:%M')}"
    sid = f"sess_{uuid.uuid4().hex[:8]}"
    with db() as con:
        con.execute("INSERT INTO sessions (id, user_id, name, created_at) VALUES (?,?,?,?)",
                    (sid, uid, name, now_iso()))
    set_current_session(uid, sid)
    await update.message.reply_text(f"✅ 已建立新對話：**{name}**", parse_mode="Markdown")


async def cmd_sessions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not auth_check(uid): return
    sessions = list_sessions(uid)
    if not sessions:
        await update.message.reply_text("沒有任何對話，傳 /new 建立一個。")
        return
    current_sid = get_or_create_session(uid)
    buttons = []
    for s in sessions:
        marker = "✅ " if s["id"] == current_sid else ""
        buttons.append([InlineKeyboardButton(
            f"{marker}{s['name']}",
            callback_data=f"switch:{s['id']}"
        )])
    markup = InlineKeyboardMarkup(buttons)
    await update.message.reply_text("選擇要切換的對話：", reply_markup=markup)


async def callback_switch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid = query.from_user.id
    if not auth_check(uid): return
    await query.answer()
    _, sid = query.data.split(":", 1)
    with db() as con:
        row = con.execute("SELECT name FROM sessions WHERE id=? AND user_id=?", (sid, uid)).fetchone()
    if not row:
        await query.edit_message_text("找不到該對話。")
        return
    set_current_session(uid, sid)
    await query.edit_message_text(f"✅ 已切換到：**{row['name']}**", parse_mode="Markdown")


async def cmd_rename(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not auth_check(uid): return
    new_name = " ".join(context.args) if context.args else ""
    if not new_name:
        await update.message.reply_text("用法：/rename 新名稱")
        return
    sid = get_or_create_session(uid)
    with db() as con:
        con.execute("UPDATE sessions SET name=? WHERE id=? AND user_id=?", (new_name, sid, uid))
    await update.message.reply_text(f"✅ 對話已重新命名為：**{new_name}**", parse_mode="Markdown")


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not auth_check(uid): return
    sid = get_or_create_session(uid)
    clear_session_messages(sid)
    await update.message.reply_text("✅ 對話紀錄已清除。")


async def cmd_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not auth_check(uid): return
    sid = get_or_create_session(uid)
    name = get_session_name(sid)
    delete_session(sid, uid)
    await update.message.reply_text(f"🗑 已刪除對話：{name}\n用 /new 建立新對話或 /sessions 切換。")


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not auth_check(uid): return
    proc = build_sessions.pop(uid, None)
    if proc and proc.returncode is None:
        proc.terminate()
        await update.message.reply_text("🛑 Grok 任務已停止。")
    else:
        await update.message.reply_text("沒有執行中的任務。")


async def cmd_build(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not auth_check(uid): return
    task = " ".join(context.args) if context.args else ""
    if not task:
        await update.message.reply_text("用法：/build <任務描述>\n例如：/build 寫一個 Python 計算機腳本")
        return
    if uid in build_sessions and build_sessions[uid].returncode is None:
        await update.message.reply_text("⚠️ 已有執行中的任務，請先 /stop")
        return
    progress = await update.message.reply_text(f"⏳ Grok 執行中：{task[:60]}...")
    try:
        proc = await asyncio.create_subprocess_exec(
            "grok", "build", task,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env={**os.environ, "TERM": "dumb", "NO_COLOR": "1"},
        )
        build_sessions[uid] = proc
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=300)
            output = strip_ansi(stdout.decode("utf-8", errors="replace"))
        except asyncio.TimeoutError:
            proc.kill()
            output = "⚠️ 任務超時（5分鐘）已停止。"
        finally:
            build_sessions.pop(uid, None)
        await context.bot.delete_message(update.effective_chat.id, progress.message_id)
        output = output or "（Grok 完成但沒有輸出）"
        for i in range(0, max(len(output), 1), 4000):
            await update.message.reply_text(output[i:i + 4000])
    except Exception as e:
        await context.bot.delete_message(update.effective_chat.id, progress.message_id)
        await update.message.reply_text(f"❌ 錯誤：{e}")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not auth_check(uid):
        await update.message.reply_text("⛔ 存取拒絕")
        return
    text = update.message.text
    sid = get_or_create_session(uid)
    save_message(sid, "user", text)
    history = get_history(sid)
    progress = await update.message.reply_text("⏳")
    try:
        reply = await xai_chat(history)
        save_message(sid, "assistant", reply)
        await context.bot.delete_message(update.effective_chat.id, progress.message_id)
        for i in range(0, max(len(reply), 1), 4000):
            await update.message.reply_text(reply[i:i + 4000])
    except Exception as e:
        await context.bot.delete_message(update.effective_chat.id, progress.message_id)
        await update.message.reply_text(f"❌ 錯誤：{e}")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("sessions", cmd_sessions))
    app.add_handler(CommandHandler("rename", cmd_rename))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("delete", cmd_delete))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("build", cmd_build))
    app.add_handler(CallbackQueryHandler(callback_switch, pattern=r"^switch:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("Grok Bot started with multi-session support.", flush=True)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
