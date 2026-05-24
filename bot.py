"""
Projet Hermes — Bot Telegram
Architecture : Claude Code + claude-connaissance vault
Multi-client, validation humaine, pas d'auto-modification
"""

import os
import asyncio
import subprocess
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters, ContextTypes
)
from google import genai
from google.genai import types

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN  = os.getenv("TELEGRAM_TOKEN")
GEMINI_KEY      = os.getenv("GEMINI_API_KEY")
VAULT           = Path(os.getenv("VAULT_PATH", os.path.expanduser("~/claude-connaissance")))
WHITELIST       = {int(x) for x in os.getenv("WHITELIST_IDS", "").split(",") if x.strip()}

gemini = genai.Client(api_key=GEMINI_KEY)
GEMINI_MODEL = "gemini-2.5-flash"

# ── Sessions en mémoire : {user_id: {client, history}} ───────────────────────
sessions: dict = {}

def session(uid: int) -> dict:
    if uid not in sessions:
        sessions[uid] = {"client": "_global", "history": []}
    return sessions[uid]

def authorized(uid: int) -> bool:
    return not WHITELIST or uid in WHITELIST

# ── Vault helpers ─────────────────────────────────────────────────────────────
def load_context(client_name: str) -> str:
    """Charge voice.md + memory.md du client actif + voix globale."""
    parts = []

    # Voix globale (Jarlounet)
    global_voice = VAULT / "clients" / "_global" / "voice.md"
    if global_voice.exists():
        parts.append(f"## Voix Jarlounet (globale)\n{global_voice.read_text()}")

    # Contexte client
    if client_name != "_global":
        client_dir = VAULT / "clients" / client_name
        # Fichiers clés en priorité
        priority = ["voice.md", "memory.md", "brief.md", "README.md"]
        loaded = set()
        for fname in priority:
            f = client_dir / fname
            if f.exists():
                parts.append(f"## {fname} — {client_name}\n{f.read_text()[:3000]}")
                loaded.add(fname)
        # Autres .md à la racine (hors sous-dossiers), max 2000 chars chacun
        for f in sorted(client_dir.glob("*.md")):
            if f.name not in loaded and len(parts) < 8:
                parts.append(f"## {f.name} — {client_name}\n{f.read_text()[:2000]}")

    return "\n\n---\n\n".join(parts) if parts else ""

def list_clients() -> list[str]:
    d = VAULT / "clients"
    return sorted(x.name for x in d.iterdir() if x.is_dir()) if d.exists() else []

def append_memory(client_name: str, info: str) -> bool:
    """Écrit dans memory.md et commit+push sur GitHub. Retourne True si push ok."""
    if client_name == "_global":
        target = VAULT / "clients" / "_global" / "memory.md"
    else:
        target = VAULT / "clients" / client_name / "memory.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    with open(target, "a") as f:
        f.write(f"\n- [{ts}] {info}")
    # Sync git
    msg = f"memo [{client_name}] : {info[:60]}"
    try:
        subprocess.run(["git", "add", str(target)], cwd=VAULT, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", msg], cwd=VAULT, check=True, capture_output=True)
        subprocess.run(["git", "push"], cwd=VAULT, check=True, capture_output=True)
        return True
    except subprocess.CalledProcessError:
        return False

# ── Handlers ──────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id):
        await update.message.reply_text("❌ Accès non autorisé.")
        return
    await update.message.reply_text(
        "🤖 *Projet Hermes* — en ligne\n\n"
        "`/client [nom]` — changer de contexte\n"
        "`/clients` — lister les clients\n"
        "`/memo [info]` — ancrer dans le vault\n"
        "`/gold [texte]` — distiller des idées actionnables\n"
        "`/status` — état du système\n"
        "`/reset` — vider l'historique de session\n\n"
        "Ou envoie un message directement.",
        parse_mode="Markdown"
    )

async def cmd_clients(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id):
        return
    s = session(update.effective_user.id)
    clients = list_clients()
    if not clients:
        await update.message.reply_text("Aucun client dans le vault.")
        return
    lines = [f"{'→' if c == s['client'] else ' '} {c}" for c in clients]
    await update.message.reply_text("📁 Clients :\n" + "\n".join(lines))

async def cmd_client(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id):
        return
    s = session(update.effective_user.id)
    if not ctx.args:
        await update.message.reply_text(f"Client actif : `{s['client']}`", parse_mode="Markdown")
        return
    name = ctx.args[0].lower()
    if name not in list_clients() and name != "_global":
        await update.message.reply_text(f"❌ Client `{name}` non trouvé.\n/clients pour la liste.", parse_mode="Markdown")
        return
    s["client"] = name
    s["history"] = []  # reset historique au changement de client
    await update.message.reply_text(f"✅ Contexte chargé : `{name}`", parse_mode="Markdown")

async def cmd_memo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id):
        return
    if not ctx.args:
        await update.message.reply_text("Usage : `/memo [info à ancrer]`", parse_mode="Markdown")
        return
    s = session(update.effective_user.id)
    info = " ".join(ctx.args)
    pushed = append_memory(s["client"], info)
    suffix = " + pushé sur GitHub" if pushed else " (push échoué, check git)"
    await update.message.reply_text(f"✅ Ancré dans `{s['client']}/memory.md`{suffix}", parse_mode="Markdown")

async def cmd_gold(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id):
        return
    if not ctx.args:
        await update.message.reply_text("Usage : `/gold [texte brut]`", parse_mode="Markdown")
        return
    texte = " ".join(ctx.args)
    await update.message.reply_text("⚗️ Distillation...")
    r = gemini.models.generate_content(
        model=GEMINI_MODEL,
        contents=f"Extrais les idées actionnables de ce texte. "
                 f"Format : liste courte, chaque item = une action concrète ou décision à prendre. "
                 f"Sois direct, sans intro.\n\n{texte}"
    )
    await update.message.reply_text(r.text)

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id):
        return
    s = session(update.effective_user.id)
    clients = list_clients()
    exchanges = len(s["history"]) // 2
    await update.message.reply_text(
        f"🤖 *Projet Hermes*\n"
        f"Client actif : `{s['client']}`\n"
        f"Clients dispo : {', '.join(clients) or 'aucun'}\n"
        f"Échanges en session : {exchanges}\n"
        f"Vault : `{VAULT}`",
        parse_mode="Markdown"
    )

async def cmd_reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id):
        return
    session(update.effective_user.id)["history"] = []
    await update.message.reply_text("🔄 Historique vidé.")

MEMO_TRIGGERS = [
    "note ça", "note ca", "ajoute", "sauvegarde", "retiens",
    "mets dans inbox", "rappel", "mémorise", "memorise",
    "add to inbox", "save this", "remember this"
]

def detect_memo_intent(text: str) -> bool:
    t = text.lower()
    return any(trigger in t for trigger in MEMO_TRIGGERS)

def detect_client(text: str) -> str | None:
    """Détecte un nom de client dans le message. Retourne le nom ou None."""
    t = text.lower()
    for client in list_clients():
        if client != "_global" and client in t:
            return client
    return None

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id):
        return
    s = session(update.effective_user.id)
    user_text = update.message.text

    # Détection automatique du client dans le message
    detected = detect_client(user_text)
    if detected and detected != s["client"]:
        s["client"] = detected
        s["history"] = []  # nouveau contexte = reset historique

    vault_ctx = load_context(s["client"])

    # Détection intention memo en langage naturel
    if detect_memo_intent(user_text):
        r = gemini.models.generate_content(
            model=GEMINI_MODEL,
            contents=f"Extrait uniquement l'information à mémoriser de ce message, "
                     f"en une phrase concise. Pas d'intro, juste l'info.\n\n{user_text}"
        )
        info = r.text.strip()
        pushed = append_memory(s["client"], info)
        suffix = " + pushé GitHub ✓" if pushed else " (push échoué)"
        await update.message.reply_text(
            f"📌 Mémorisé dans `{s['client']}`{suffix} :\n_{info}_",
            parse_mode="Markdown"
        )
        return

    system = (
        "Tu es l'assistant de Jarlounet, Brand Designer. "
        "Ton direct, chaleureux, concis. Pas de flatterie. "
        "Réponds en français sauf demande contraire. "
        "Si l'utilisateur te demande de sauvegarder ou noter quelque chose, "
        "dis-lui d'utiliser la commande /memo [info].\n\n"
        + (f"Contexte chargé :\n{vault_ctx}" if vault_ctx else "")
    )

    s["history"].append({"role": "user", "parts": [user_text]})
    history = s["history"][-20:]

    await update.message.chat.send_action("typing")

    # Construire l'historique pour l'API
    contents = [
        types.Content(role="user",  parts=[types.Part(text=system)]),
        types.Content(role="model", parts=[types.Part(text="Compris, je suis prêt.")]),
    ]
    for h in history[:-1]:
        contents.append(types.Content(
            role=h["role"],
            parts=[types.Part(text=h["parts"][0])]
        ))
    contents.append(types.Content(role="user", parts=[types.Part(text=user_text)]))

    r = gemini.models.generate_content(
        model=GEMINI_MODEL,
        contents=contents,
        config=types.GenerateContentConfig(max_output_tokens=1500)
    )

    reply = r.text
    s["history"].append({"role": "model", "parts": [reply]})
    await update.message.reply_text(reply)


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    if not TELEGRAM_TOKEN:
        raise ValueError("TELEGRAM_TOKEN manquant dans .env")
    if not GEMINI_KEY:
        raise ValueError("GEMINI_API_KEY manquant dans .env")

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("clients", cmd_clients))
    app.add_handler(CommandHandler("client",  cmd_client))
    app.add_handler(CommandHandler("memo",    cmd_memo))
    app.add_handler(CommandHandler("gold",    cmd_gold))
    app.add_handler(CommandHandler("status",  cmd_status))
    app.add_handler(CommandHandler("reset",   cmd_reset))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print(f"🚀 Projet Hermes démarré — vault : {VAULT}")
    app.run_polling()

if __name__ == "__main__":
    main()
