#!/usr/bin/env python3
import asyncio
import os
import re
import httpx
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_USER_ID = int(os.environ.get("ALLOWED_USER_ID", "0"))
XAI_API_KEY = os.environ.get("XAI_API_KEY", "")
XAI_BASE_URL = "https://api.x.ai/v1"
CHAT_MODEL = "grok-3"

ANSI = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')

def strip_ansi(t): return ANSI.sub('', t).replace('\r\n', '\n').replace('\r', '\n').strip()

# Per-user chat history for conversational AI
chat_histories: dict[int, list[dict]] = {}

# Per-user active grok CLI sessions
build_sessions: dict[int, asyncio.subprocess.Process] = {}


def auth(uid: int) -> bool:
    return ALLOWED_USER_ID == 0 or uid == ALLOWED_USER_ID


async def xai_chat(history: list[dict]) -> str:
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{XAI_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {XAI_API_KEY}", "Content-Type": "application/json"},
            json={"model": CHAT_MODEL, "messages": history, "max_tokens": 2048},
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not auth(uid): return
    chat_histories[uid] = []
    await update.message.reply_text(
        "👋 Grok Bot 已就緒！\n\n"
        "• 直接傳訊息 → 和 Grok AI 對話\n"
        "• /build <任務> → 讓 Grok 執行程式工作\n"
        "• /clear → 清除對話紀錄\n"
        "• /stop → 停止執行中的任務"
    )


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not auth(uid): return
    chat_histories[uid] = []
    await update.message.reply_text("✅ 對話紀錄已清除。")


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not auth(uid): return
    proc = build_sessions.pop(uid, None)
    if proc and proc.returncode is None:
        proc.terminate()
        await update.message.reply_text("🛑 Grok 任務已停止。")
    else:
        await update.message.reply_text("沒有執行中的任務。")


async def cmd_build(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not auth(uid): return

    task = " ".join(context.args) if context.args else ""
    if not task:
        await update.message.reply_text("用法：/build <任務描述>\n例如：/build 寫一個 Python 爬蟲腳本")
        return

    if uid in build_sessions and build_sessions[uid].returncode is None:
        await update.message.reply_text("⚠️ 已有執行中的任務，請先 /stop")
        return

    progress = await update.message.reply_text(f"⏳ Grok 執行中：{task[:50]}...")

    try:
        proc = await asyncio.create_subprocess_exec(
            "grok", "build", task,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env={**os.environ, "TERM": "dumb", "NO_COLOR": "1",
                 "XAI_API_KEY": XAI_API_KEY},
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

        if not output:
            output = "（Grok 完成但沒有輸出）"

        for i in range(0, max(len(output), 1), 4000):
            await update.message.reply_text(output[i:i + 4000])

    except Exception as e:
        await context.bot.delete_message(update.effective_chat.id, progress.message_id)
        await update.message.reply_text(f"❌ 錯誤：{e}")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not auth(uid):
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
