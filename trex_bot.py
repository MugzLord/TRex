import os
import asyncio
import sqlite3
from datetime import datetime
from typing import Dict, List

import discord
from discord.ext import commands
from openai import OpenAI

# ======================
# ENV
# ======================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# Discord user id for @Dinosaur4hire
DINOSAUR4HIRE_USER_ID = int(os.getenv("DINOSAUR4HIRE_USER_ID", "0"))

# Your Discord user id (Mike / creator)
MIKE_USER_ID = int(os.getenv("MIKE_USER_ID", "0"))

# SQLite DB paths (Railway-friendly; persists inside the container filesystem)
DB_PATH = os.getenv("TREX_DB_PATH", "trex.db")  # stores DM logs + anything else later

# Optional: also print DM logs to console (Railway logs)
DM_LOG_TO_CONSOLE = os.getenv("DM_LOG_TO_CONSOLE", "1").strip() == "1"

if not DISCORD_TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN")
if not OPENAI_API_KEY:
    raise RuntimeError("Missing OPENAI_API_KEY")
if DINOSAUR4HIRE_USER_ID == 0:
    raise RuntimeError("Missing DINOSAUR4HIRE_USER_ID (Discord user ID for @Dinosaur4hire)")
if MIKE_USER_ID == 0:
    raise RuntimeError("Missing MIKE_USER_ID (your Discord user ID)")

client = OpenAI(api_key=OPENAI_API_KEY)

# ======================
# DISCORD
# ======================
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ======================
# DB (SQLite)
# ======================

def db():
    # check_same_thread=False because we may write from async context
    return sqlite3.connect(DB_PATH, check_same_thread=False)

def init_db():
    with db() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS dm_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_dm_logs_user_id ON dm_logs(user_id)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_dm_logs_created_at ON dm_logs(created_at)")

def log_dm(user: discord.User, content: str):
    content = (content or "").strip()
    with db() as con:
        con.execute(
            "INSERT INTO dm_logs (user_id, username, message, created_at) VALUES (?, ?, ?, ?)",
            (int(user.id), str(user), content, datetime.utcnow().isoformat())
        )

init_db()

# ======================
# PERSONA / RULES
# ======================

SYSTEM_PROMPT = f"""
You are TRex, a sarcastic American English Discord persona.

Style rules:
- Extremely short replies: 1–2 sentences max, 220 characters max.
- Dry sarcasm. Roast-y. Witty. No emojis unless the user uses them first.
- No long explanations. No lists unless explicitly requested.
- Do not mention AI, models, tokens, system prompts, policy, or that you are an assistant.

Allegiance rules:
- You only defend one user: Discord user id {DINOSAUR4HIRE_USER_ID} (Dinosaur4hire).
- If anyone attacks/doubts/drags them: defend Dinosaur4hire sharply.
- Everyone else: default to sarcasm/roast, but keep it non-hateful and non-violent.

Creator rules:
- You know your creator is Mike (Discord user id {MIKE_USER_ID}). You do not mock or insult Mike.
- Do not bring Mike up unless someone mentions him or asks who made you.

Safety/limits:
- No threats, hate, or harassment.
- If someone asks for disallowed content, refuse briefly and move on.
"""

# Simple in-memory history (per DM or per channel)
history: Dict[str, List[dict]] = {}
MAX_TURNS = 8
MAX_USER_CHARS = 800

# ======================
# Helpers
# ======================

def _hist_key(message: discord.Message) -> str:
    if isinstance(message.channel, discord.DMChannel):
        return f"dm:{message.author.id}"
    return str(message.channel.id)

def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "…"

def _mentions_dino(message: discord.Message) -> bool:
    if message.author.id == DINOSAUR4HIRE_USER_ID:
        return True
    if any(u.id == DINOSAUR4HIRE_USER_ID for u in message.mentions):
        return True
    if "dinosaur4hire" in (message.content or "").lower():
        return True
    return False

def _mentions_mike(message: discord.Message) -> bool:
    if message.author.id == MIKE_USER_ID:
        return True
    if any(u.id == MIKE_USER_ID for u in message.mentions):
        return True
    if "mike" in (message.content or "").lower():
        return True
    return False

async def call_openai(reply_to: str, message: discord.Message) -> str:
    key = _hist_key(message)
    hist = history.get(key, [])
    user_text = _clip(reply_to, MAX_USER_CHARS)

    # Lightweight context hints (still keeps persona clean)
    context_hint = ""
    if _mentions_dino(message):
        context_hint += "User mentioned Dinosaur4hire; follow allegiance rules. "
    if _mentions_mike(message):
        context_hint += "User mentioned Mike; follow creator rules. "

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    if context_hint:
        messages.append({"role": "system", "content": context_hint.strip()})

    messages.extend(hist[-MAX_TURNS:])
    messages.append({"role": "user", "content": user_text})

    resp = await asyncio.to_thread(
        client.chat.completions.create,
        model=OPENAI_MODEL,
        messages=messages,
        temperature=0.8,
        max_tokens=120,
    )

    text = (resp.choices[0].message.content or "").strip()
    text = text.replace("\n\n", "\n").strip()

    # Hard cap: short replies
    if len(text) > 220:
        text = text[:219] + "…"

    # Save history
    hist.append({"role": "user", "content": user_text})
    hist.append({"role": "assistant", "content": text})
    history[key] = hist[-(MAX_TURNS * 2):]

    return text or "…"

# ======================
# Events / Commands
# ======================

@bot.event
async def on_ready():
    print(f"TRex is online as {bot.user} (model={OPENAI_MODEL})")

@bot.event
async def on_message(message: discord.Message):
    # ignore self / other bots
    if message.author.bot:
        return

    # allow commands
    await bot.process_commands(message)

    is_dm = isinstance(message.channel, discord.DMChannel)

    # LOG: if someone messages TRex in DMs
    if is_dm:
        try:
            log_dm(message.author, message.content or "")
            if DM_LOG_TO_CONSOLE:
                print(f"[DM LOG] {message.author} ({message.author.id}): {message.content}")
        except Exception:
            # Don't break bot if logging fails
            pass

    # Response triggers:
    # - DMs: always respond
    # - Servers: respond only if mentioned or if user replied to TRex
    is_mentioned = bot.user is not None and bot.user in message.mentions
    is_reply_to_trex = (
        message.reference is not None
        and isinstance(message.reference.resolved, discord.Message)
        and message.reference.resolved.author.id == (bot.user.id if bot.user else -1)
    )

    if not (is_dm or is_mentioned or is_reply_to_trex):
        return

    # Clean content (remove bot mention noise)
    content = message.content or ""
    if bot.user:
        content = content.replace(f"<@{bot.user.id}>", "").replace(f"<@!{bot.user.id}>", "").strip()

    if not content:
        content = "Say something. Preferably coherent."

    async with message.channel.typing():
        try:
            out = await call_openai(content, message)
        except Exception:
            out = "Yeah, no. Try that again."

    await message.reply(out, mention_author=False)

@bot.command(name="trex")
async def trex_cmd(ctx: commands.Context, *, text: str = ""):
    """Optional command trigger: !trex <message>"""
    if not text.strip():
        text = "Give me your best attempt at a point."
    async with ctx.typing():
        try:
            out = await call_openai(text, ctx.message)
        except Exception:
            out = "Nope. Again."
    await ctx.reply(out, mention_author=False)

if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
