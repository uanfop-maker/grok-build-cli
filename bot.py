#!/usr/bin/env python3
import asyncio
import json
import os
import re
from datetime import datetime, timezone, timedelta
import httpx
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_USER_ID = int(os.environ.get("ALLOWED_USER_ID", "0"))
AUTH_JSON_PATH = "/root/.grok/auth.json"
OIDC_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
OIDC_TOKEN_URL = "https://auth.x.ai/oauth2/token"
XAI_API_BASE = "https://api.x.ai/v1"
CHAT_MODEL = "grok-3"

ANSI = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')

def strip_ansi(t):
    return ANSI.sub('', t).replace('\r\n', '\n').replace('\r', '\n').strip()

chat_histories: dict[int, list[dict]] = {}
build_sessions: dict[int, asyncio.subprocess.Process] = {}


def auth(uid: int) -> bool:
    return ALLOWED_USER_ID == 0 or uid == ALLOWED_USER_ID


def load_auth() -> dict:
    with open(AUTH_JSON_PATH) as f:
        data = json.load(f)
    for k, v in data.items():
        if "x.ai" in k:
            return k, v
    raise ValueError("No xAI auth entry found in auth.json")


def parse_iso_dt(s: str) -> datetime:
    # Truncate sub-second to 6 digits (microseconds), handle Z suffix
    s = re.sub(r'(\.\d{6})\d+', r'\1', s).replace('Z', '+00:00')
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


async def get_access_token() -> str:
    auth_key, auth_data = load_auth()
    expires_at = auth_data.get("expires_at", "")
    if expires_at:
        try:
            exp = parse_iso_dt(expires_at)
            if datetime.now(timezone.utc) >= exp - timedelta(minutes=5):
                await refresh_access_token(auth_key, auth_data)
                _, auth_data = load_auth()
        except Exception:
            pass  # Use token as-is if we can't parse expiry
    return auth_data["key"]


async def refresh_access_token(auth_key: str, auth_data: dict):
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            OIDC_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": auth_data["refresh_token"],
                "client_id": OIDC_CLIENT_ID,
            }
        )
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


SYSTEM_PROMPT = """你是 @DaDaBiteGrokBot，一個運行在 Zeabur 雲端服務上的 Grok AI 助手。

你具備兩種能力：
1. **對話模式**（目前）：你可以回答問題、聊天、提供建議、解釋技術概念。
2. **程式執行模式**：用戶可以輸入 `/build <任務描述>` 讓 Grok Build CLI 實際去寫程式、建立專案、執行開發任務。

當用戶提出需要「寫程式」、「建立腳本」、「開發功能」、「修改程式碼」等任務時，
請在回覆中主動提示他們可以用 `/build` 指令來實際執行，例如：
「你可以用 `/build 幫我寫一個爬蟲抓股票價格` 讓我直接去執行。」

用繁體中文回覆，除非用戶用其他語言。"""


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


def auth_check(uid): return ALLOWED_USER_ID == 0 or uid == ALLOWED_USER_ID


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not auth_check(uid): return
    chat_histories[uid] = []
    await update.message.reply_text(
        "👋 Grok Bot 已就緒！\n\n"
        "• 直接傳訊息 → 和 Grok AI 對話\n"
        "• /build <任務> → 讓 Grok 執行程式任務\n"
        "• /clear → 清除對話紀錄\n"
        "• /stop → 停止執行中的任務"
    )


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not auth_check(uid): return
    chat_histories[uid] = []
    await update.message.reply_text("✅ 對話紀錄已清除。")


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
    history = chat_histories.setdefault(uid, [])
    history.append({"role": "user", "content": text})
    if len(history) > 40:
        history[:] = history[-40:]

    progress = await update.message.reply_text("⏳")
    try:
        reply = await xai_chat(history)
        history.append({"role": "assistant", "content": reply})
        await context.bot.delete_message(update.effective_chat.id, progress.message_id)
        for i in range(0, max(len(reply), 1), 4000):
            await update.message.reply_text(reply[i:i + 4000])
    except Exception as e:
        await context.bot.delete_message(update.effective_chat.id, progress.message_id)
        await update.message.reply_text(f"❌ 錯誤：{e}")


def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("build", cmd_build))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("Grok Bot started.", flush=True)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
