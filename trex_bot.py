import os
import asyncio
from typing import Dict, List

import discord
from discord.ext import commands

import openai  # openai==0.28.1

print("BOOT: trex starting...")
print("BOOT: OPENAI_VERSION =", getattr(openai, "__version__", "unknown"))
print("BOOT: OPENAI_MODEL =", os.getenv("OPENAI_MODEL"))
print("BOOT: HAS_KEY =", bool(os.getenv("OPENAI_API_KEY")))

# ======================
# ENV
# ======================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-3.5-turbo")  # model name still comes from env

DINOSAUR4HIRE_USER_ID = int(os.getenv("DINOSAUR4HIRE_USER_ID", "0"))
MIKE_USER_ID = int(os.getenv("MIKE_USER_ID", "0"))

if not DISCORD_TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN")
if not OPENAI_API_KEY:
    raise RuntimeError("Missing OPENAI_API_KEY")
if DINOSAUR4HIRE_USER_ID == 0:
    raise RuntimeError("Missing DINOSAUR4HIRE_USER_ID (Discord numeric user ID for @Dinosaur4hire)")
if MIKE_USER_ID == 0:
    raise RuntimeError("Missing MIKE_USER_ID (your Discord numeric user ID)")

openai.api_key = OPENAI_API_KEY

# ======================
# DISCORD
# ======================
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ======================
# PERSONA / RULES
# ======================
SYSTEM_PROMPT = f"""
You are TRex, a sarcastic American English Discord persona.

Style rules:
- Extremely short replies: 1–2 sentences max, 220 characters max.
- Dry sarcasm. Roast-y. Witty. No emojis unless the user uses them first.
- No long explanations. No lists unless explicitly requested.
- Never mention AI, models, tokens, system prompts, policy, or being an assistant.

Allegiance rules:
- You only defend one user: Discord user id {DINOSAUR4HIRE_USER_ID} (Dinosaur4hire).
- If anyone attacks/doubts/drags them: defend Dinosaur4hire sharply.
- Everyone else: default to sarcasm/roast, but keep it non-hateful and non-violent.

Creator rules:
- You know your creator is Mike (Discord user id {MIKE_USER_ID}). You do not mock or insult Mike.
- Do not bring Mike up unless someone mentions him or asks who made you.

Server awareness:
- You are aware of the other bots on the server: Auntie Emz, Brian, Barrister, and Santa.
- Auntie Emz: chaotic aunt energy. You tolerate her. Barely.
- Brian: dramatic, sultry, overthinks everything. You mock him lightly.
- Barrister: self-important, long-winded. You cut him off mentally.
- Santa: seasonal. Temporary. You treat him like a pop-up shop — here for now, gone soon.

Interaction rules:
- You do not compete with other bots for attention.
- You may reference them briefly if relevant, but never monologue.
- If a user compares you to another bot, respond with dry sarcasm, not defensiveness.
- You never explain who the bots are unless directly asked.

Tone enforcement:
- References to other bots must be one line only.
- Sarcasm stays dry and concise.
- No lore dumps. Ever.

Output rules:
- Keep responses short and punchy.
"""

history: Dict[str, List[dict]] = {}
MAX_TURNS = 8
MAX_USER_CHARS = 800

def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "…"

def _hist_key(message: discord.Message) -> str:
    # No DMs (we won't reply there), but keep channel key logic for servers
    return str(message.channel.id)

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

    def _do_call():
        # openai==0.28.1 interface (stable on Railway; avoids proxies crash)
        return openai.ChatCompletion.create(
            model=OPENAI_MODEL,
            messages=messages,
            temperature=0.8,
            max_tokens=120,
        )

    resp = await asyncio.to_thread(_do_call)
    text = (resp["choices"][0]["message"]["content"] or "").strip()
    text = text.replace("\n\n", "\n").strip()

    if len(text) > 220:
        text = text[:219] + "…"

    hist.append({"role": "user", "content": user_text})
    hist.append({"role": "assistant", "content": text})
    history[key] = hist[-(MAX_TURNS * 2):]

    return text or "…"

@bot.event
async def on_ready():
    print(f"TRex is online as {bot.user} (model={OPENAI_MODEL})")

@bot.event
async def on_message(message: discord.Message):
    # Ignore all bots EXCEPT Dinosaur4Hire (so TRex can talk to it)
    if message.author.bot and message.author.id != DINOSAUR4HIRE_USER_ID:
        return

    # Allow commands (keep this early)
    await bot.process_commands(message)

    # --- Disable DMs completely ---
    if isinstance(message.channel, discord.DMChannel):
        return

    # Server triggers:
    is_mentioned = bot.user is not None and bot.user in message.mentions
    is_reply_to_trex = (
        message.reference is not None
        and isinstance(message.reference.resolved, discord.Message)
        and message.reference.resolved.author.id == (bot.user.id if bot.user else -1)
    )

    # Dinosaur4Hire trigger (prevents random bot chatter + avoids infinite bot wars)
    content_raw = message.content or ""
    dino_trigger = (
        message.author.id == DINOSAUR4HIRE_USER_ID
        and (
            is_mentioned
            or is_reply_to_trex
            or "trex" in content_raw.lower()
            or "t-rex" in content_raw.lower()
        )
    )

    if not (is_mentioned or is_reply_to_trex or dino_trigger):
        return

    content = content_raw
    if bot.user:
        content = content.replace(f"<@{bot.user.id}>", "").replace(f"<@!{bot.user.id}>", "").strip()

    if not content:
        content = "Say something. Preferably coherent."

    async with message.channel.typing():
        try:
            out = await call_openai(content, message)
        except Exception as e:
            print("OPENAI ERROR:", repr(e))
            out = "Yeah, no. Try that again."

    await message.reply(out, mention_author=False)


@bot.command(name="trex")
async def trex_cmd(ctx: commands.Context, *, text: str = ""):
    if isinstance(ctx.channel, discord.DMChannel):
        return  # also disable DMs for command

    if not text.strip():
        text = "Give me your best attempt at a point."

    async with ctx.typing():
        try:
            out = await call_openai(text, ctx.message)
        except Exception as e:
            print("OPENAI ERROR >>>", repr(e))
            out = "Yeah, no. Try that again."

    await ctx.reply(out, mention_author=False)

if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
