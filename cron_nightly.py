#!/usr/bin/env python3
"""
Hermes — Digest nightly
Collecte les signaux du jour → Gemini → Telegram
"""

import os
import asyncio
from pathlib import Path
from datetime import datetime, timedelta
from dotenv import load_dotenv
from google import genai
from google.genai import types
import telegram

load_dotenv()

VAULT       = Path(os.getenv("VAULT_PATH", os.path.expanduser("~/claude-connaissance")))
GEMINI_KEY  = os.getenv("GEMINI_API_KEY")
TOKEN       = os.getenv("TELEGRAM_TOKEN")
CHAT_ID     = os.getenv("TELEGRAM_CHAT_ID", "")

GEMINI_MODEL = "gemini-2.5-flash"

# ── Collecte ──────────────────────────────────────────────────────────────────

def collect_memos(hours: int = 24) -> list[str]:
    """Entrées ajoutées aux memory.md dans les dernières N heures."""
    cutoff = datetime.now() - timedelta(hours=hours)
    found = []
    for memory_file in VAULT.rglob("memory.md"):
        try:
            for line in memory_file.read_text().splitlines():
                if not line.startswith("- ["):
                    continue
                try:
                    ts_str = line[3:line.index("]")]
                    ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M")
                    if ts >= cutoff:
                        client = memory_file.parent.name
                        found.append(f"[{client}] {line.split('] ', 1)[-1]}")
                except (ValueError, IndexError):
                    pass
        except Exception:
            pass
    return found

def collect_inbox(hours: int = 24) -> list[str]:
    """Fichiers _inbox/ créés dans les dernières N heures."""
    inbox = VAULT / "_inbox"
    cutoff = datetime.now() - timedelta(hours=hours)
    found = []
    if not inbox.exists():
        return found
    for f in sorted(inbox.glob("*.md")):
        try:
            if datetime.fromtimestamp(f.stat().st_mtime) >= cutoff:
                excerpt = f.read_text()[:600]
                found.append(f"[{f.name}]\n{excerpt}")
        except Exception:
            pass
    return found

# ── Digest ────────────────────────────────────────────────────────────────────

async def send_digest():
    if not CHAT_ID:
        print("TELEGRAM_CHAT_ID non défini dans .env — digest ignoré")
        return
    if not TOKEN or not GEMINI_KEY:
        print("Token ou clé Gemini manquant — abort")
        return

    memos  = collect_memos()
    inbox  = collect_inbox()

    if not memos and not inbox:
        print(f"[{datetime.now():%Y-%m-%d %H:%M}] Rien à analyser — pas de digest")
        return

    # Construire le contexte pour Gemini
    blocks = []
    if memos:
        blocks.append("## Memos du jour\n" + "\n".join(memos))
    if inbox:
        blocks.append("## Fichiers entrants (inbox)\n" + "\n\n---\n\n".join(inbox))

    prompt = (
        "Tu es l'assistant de Jarlounet, Brand Designer. "
        "Voici les signaux captés aujourd'hui dans son système de connaissance. "
        "Génère un digest de fin de journée en 3 parties, dans cet ordre :\n"
        "1. **Ce qui s'est passé** — 1-2 lignes max\n"
        "2. **Propositions pour demain** — 2 ou 3 actions concrètes, pas plus\n"
        "3. **Point de vigilance** — une seule chose si tu en vois une, sinon rien\n\n"
        "Ton direct, sans intro, sans flatterie, en français.\n\n"
        + "\n\n".join(blocks)
    )

    gemini_client = genai.Client(api_key=GEMINI_KEY)
    r = gemini_client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(max_output_tokens=500)
    )

    date_str = datetime.now().strftime("%d %b")
    message  = f"🌙 Digest Hermes — {date_str}\n\n{r.text}"

    bot = telegram.Bot(token=TOKEN)
    await bot.send_message(chat_id=int(CHAT_ID), text=message)
    print(f"[{datetime.now():%Y-%m-%d %H:%M}] ✓ Digest envoyé → {CHAT_ID}")

if __name__ == "__main__":
    asyncio.run(send_digest())
