#!/usr/bin/env python3
import asyncio
import os
import re
import sys
from telegram import Update, BotCommand
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_USER_ID = int(os.environ.get("ALLOWED_USER_ID", "0"))
XAI_API_KEY = os.environ.get("XAI_API_KEY", "")

ANSI_ESCAPE = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')

def strip_ansi(text: str) -> str:
    return ANSI_ESCAPE.sub('', text)

def clean_output(text: str) -> str:
    text = strip_ansi(text)
    # Remove carriage returns
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    # Collapse excessive blank lines
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()

class GrokSession:
    def __init__(self):
        self.process: asyncio.subprocess.Process | None = None
        self._output_buf = ''
        self._lock = asyncio.Lock()

    async def start(self) -> str:
        env = {**os.environ}
        if XAI_API_KEY:
            env['XAI_API_KEY'] = XAI_API_KEY
        env['TERM'] = 'dumb'
        env['NO_COLOR'] = '1'

        self.process = await asyncio.create_subprocess_exec(
            'grok',
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
        return await self._collect_output(timeout=6.0)

    async def _collect_output(self, timeout: float = 10.0) -> str:
        """Read output until no new data for `timeout` seconds after first byte."""
        chunks = []
        deadline = asyncio.get_event_loop().time() + timeout
        got_first = False

        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                break
            try:
                data = await asyncio.wait_for(
                    self.process.stdout.read(2048),
                    timeout=min(remaining, 0.3),
                )
                if data:
                    chunks.append(data.decode('utf-8', errors='replace'))
                    if not got_first:
                        got_first = True
                        # Reset deadline from first byte: 4 more seconds of quiet
                        deadline = asyncio.get_event_loop().time() + 4.0
                    else:
                        deadline = asyncio.get_event_loop().time() + 4.0
                else:
                    # EOF
                    break
            except asyncio.TimeoutError:
                if got_first:
                    break

        return clean_output(''.join(chunks))

    async def send(self, text: str) -> str:
        if not self.process or self.process.returncode is not None:
            return '❌ Session ended. Use /start to restart.'
        self.process.stdin.write((text + '\n').encode())
        await self.process.stdin.drain()
        return await self._collect_output(timeout=60.0)

    async def stop(self):
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=3)
            except asyncio.TimeoutError:
                self.process.kill()
        self.process = None

    @property
    def alive(self) -> bool:
        return self.process is not None and self.process.returncode is None


sessions: dict[int, GrokSession] = {}


def auth(user_id: int) -> bool:
    return ALLOWED_USER_ID == 0 or user_id == ALLOWED_USER_ID


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not auth(uid):
        await update.message.reply_text('⛔ 存取拒絕')
        return

    if uid in sessions:
        await sessions[uid].stop()

    await update.message.reply_text('⏳ 啟動 Grok 中...')
    session = GrokSession()
    output = await session.start()
    sessions[uid] = session

    msg = output if output else '✅ Grok 已啟動，請傳入你的任務。'
    await update.message.reply_text(msg)


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not auth(uid):
        return
    if uid in sessions:
        await sessions[uid].stop()
        del sessions[uid]
        await update.message.reply_text('🛑 Grok 工作階段已結束。')
    else:
        await update.message.reply_text('目前沒有進行中的工作階段。')


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not auth(uid):
        return
    if uid in sessions and sessions[uid].alive:
        await update.message.reply_text('✅ Grok 執行中')
    else:
        await update.message.reply_text('❌ 沒有進行中的工作階段，使用 /start 開始')


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not auth(uid):
        await update.message.reply_text('⛔ 存取拒絕')
        return

    if uid not in sessions or not sessions[uid].alive:
        await update.message.reply_text('沒有進行中的工作階段，請先使用 /start')
        return

    text = update.message.text
    progress = await update.message.reply_text('⏳ Grok 處理中...')

    output = await sessions[uid].send(text)

    if not output:
        output = '（Grok 仍在執行中，或沒有輸出）'

    await context.bot.delete_message(update.effective_chat.id, progress.message_id)

    # Telegram message limit is 4096 chars
    for i in range(0, max(len(output), 1), 4000):
        await update.message.reply_text(output[i:i + 4000])


def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler('start', cmd_start))
    app.add_handler(CommandHandler('stop', cmd_stop))
    app.add_handler(CommandHandler('status', cmd_status))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print('Bot started.', flush=True)
    app.run_polling(drop_pending_updates=True)


if __name__ == '__main__':
    main()
