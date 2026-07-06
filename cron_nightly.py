#!/usr/bin/env python3
"""
Hermes — Digest nightly
Collecte les signaux du jour → Gemini → Discord
"""

import os
import subprocess
import urllib.request
import json
from pathlib import Path
from datetime import datetime, timedelta
from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()

VAULT            = Path(os.getenv("VAULT_PATH", os.path.expanduser("~/claude-connaissance")))
GEMINI_KEY       = os.getenv("GEMINI_API_KEY")
DISCORD_WEBHOOK  = os.getenv("DISCORD_WEBHOOK", "")

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

def send_digest():
    if not GEMINI_KEY:
        print("Clé Gemini manquante — abort")
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
        "📌 Ce qui s'est passé — 1-2 lignes max\n"
        "→ Propositions pour demain — 2 ou 3 actions concrètes, pas plus\n"
        "⚠️ Point de vigilance — une seule chose si tu en vois une, sinon omets cette section\n\n"
        "IMPORTANT : texte brut uniquement, pas de Markdown, pas d'astérisques, pas de #. "
        "Utilise des tirets ou des émojis pour structurer. "
        "Ton direct, sans intro, sans flatterie, en français.\n\n"
        + "\n\n".join(blocks)
    )

    gemini_client = genai.Client(api_key=GEMINI_KEY)
    r = gemini_client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(max_output_tokens=1500)
    )

    date_str = datetime.now().strftime("%d %b")
    message  = f"🌙 Digest Hermes — {date_str}\n\n{r.text}"

    # Envoyer sur Discord
    try:
        url = DISCORD_WEBHOOK.strip()
        payload = json.dumps({"content": message}).encode("utf-8")
        req = urllib.request.Request(url, data=payload, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", "HermesBot/1.0")
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(f"[{datetime.now():%Y-%m-%d %H:%M}] ✓ Discord → HTTP {resp.status}")
    except urllib.error.HTTPError as e:
        print(f"[{datetime.now():%Y-%m-%d %H:%M}] ⚠ Discord HTTP {e.code} : {e.read().decode()}")
    except Exception as e:
        print(f"[{datetime.now():%Y-%m-%d %H:%M}] ⚠ Discord échoué : {e}")

    # 2. Sauvegarder dans _inbox/ pour digestion Claude Code
    ts       = datetime.now().strftime("%Y-%m-%d-%H-%M")
    filename = f"digest-nightly-{ts}.md"
    target   = VAULT / "_inbox" / filename
    target.write_text(f"# Digest Hermes — {date_str}\n\n{r.text}\n")
    try:
        subprocess.run(["git", "add", str(target)], cwd=VAULT, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", f"digest nightly : {ts}"],
                       cwd=VAULT, check=True, capture_output=True)
        subprocess.run(["git", "pull", "--rebase"], cwd=VAULT, check=True, capture_output=True)
        subprocess.run(["git", "push"], cwd=VAULT, check=True, capture_output=True)
        print(f"[{datetime.now():%Y-%m-%d %H:%M}] ✓ Digest → _inbox/{filename} + pushé GitHub")
    except subprocess.CalledProcessError as e:
        print(f"[{datetime.now():%Y-%m-%d %H:%M}] ⚠ Digest sauvegardé localement mais push échoué : {e}")

if __name__ == "__main__":
    send_digest()
