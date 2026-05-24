"""
Projet Hermes — Bot Telegram
Architecture : Claude Code + claude-connaissance vault
Multi-client, validation humaine, pas d'auto-modification
"""

import os
import asyncio
import subprocess
import unicodedata
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

# ── Multi-vault ───────────────────────────────────────────────────────────────
VAULTS = {
    "connaissance": VAULT,
    "musique":      Path(os.path.expanduser("~/claude-musique")),
    "creative":     Path(os.path.expanduser("~/claude-creative-coding")),
    "opendesign":   Path(os.path.expanduser("~/claude-opendesign")),
}

VAULT_TRIGGERS: dict[str, list[str]] = {
    "musique":   ["musique", "instrument", "composition", "accord", "melodie",
                  "chanson", "harmonie", "partition", "theorie musicale",
                  "synthe", "synthetiseur", "moog", "dfam", "eurorack", "modulaire",
                  "ableton", "logic pro", "daw", "mixage", "mastering", "studio",
                  "batterie", "guitare", "basse", "sample", "sampler", "beatmaking",
                  "drum machine", "sequenceur", "arpege", "midi", "vst"],
    "creative":  ["creative coding", "processing", "sketch", "generatif",
                  "p5js", "p5", "openframeworks", "glsl", "shader", "visuel",
                  "touchdesigner", "max msp", "supercollider", "osc"],
    "opendesign":["opendesign", "open design", "figma open"],
}

def detect_vault(text: str) -> Path:
    """Retourne le vault le plus pertinent selon le message."""
    t = strip_accents(text.lower())
    for vault_name, triggers in VAULT_TRIGGERS.items():
        if any(trigger in t for trigger in sorted(triggers, key=len, reverse=True)):
            v = VAULTS[vault_name]
            if v.exists():
                return v
    return VAULT  # défaut : connaissance

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

def search_vault(query: str, max_results: int = 4, vault: Path = None) -> str:
    """RAG basique : cherche les fichiers les plus pertinents par mots-clés."""
    if vault is None:
        vault = VAULT
    EXCLUDE = {"_inbox", "dialogue-archive", "done", "assets"}
    stopwords = {"pour", "dans", "avec", "quoi", "comment", "est", "que", "les", "des", "une", "qui"}
    words = [strip_accents(w) for w in query.lower().split()
             if len(w) > 3 and w not in stopwords]
    if not words:
        return ""

    scores: dict = {}
    for md in vault.rglob("*.md"):
        if any(p in md.parts for p in EXCLUDE):
            continue
        try:
            content = strip_accents(md.read_text().lower())
            score = sum(content.count(w) for w in words)
            if score > 0:
                scores[md] = score
        except Exception:
            pass

    top = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:max_results]
    if not top:
        return ""

    results = []
    for f, _ in top:
        excerpt = f.read_text()[:1500]
        rel = str(f.relative_to(vault))
        results.append(f"### {rel}\n{excerpt}")
    return "\n\n---\n\n".join(results)

def load_topics() -> dict[str, str]:
    """Charge les topics depuis topics.md + auto-découverte des dossiers numérotés non listés."""
    topics = {}
    f = VAULT / "clients" / "_global" / "topics.md"
    if f.exists():
        for line in f.read_text().splitlines():
            if "→" in line and not line.startswith("#"):
                parts = line.split("→", 1)
                if len(parts) == 2:
                    keyword = strip_accents(parts[0].strip().lower())
                    folder = parts[1].strip()
                    topics[keyword] = folder

    # Auto-découverte : dossiers numérotés (ex: 16-nouveau) absents de topics.md
    listed_folders = set(topics.values())
    for d in sorted(VAULT.iterdir()):
        if not d.is_dir() or d.name.startswith("_") or d.name.startswith("."):
            continue
        # Dossier numéroté type "16-nom-du-domaine"
        parts = d.name.split("-", 1)
        if len(parts) == 2 and parts[0].isdigit() and d.name not in listed_folders:
            # Ajouter le nom du dossier (sans numéro) comme keyword de secours
            keyword = strip_accents(parts[1].replace("-", " "))
            topics[keyword] = d.name

    return topics

def detect_topics(text: str) -> list[str]:
    """Retourne les dossiers vault pertinents selon les topics détectés."""
    t = strip_accents(text.lower())
    topics = load_topics()
    found = []
    for keyword in sorted(topics.keys(), key=len, reverse=True):
        if keyword in t and topics[keyword] not in found:
            found.append(topics[keyword])
    return found[:2]  # max 2 domaines à la fois

def load_aliases() -> dict[str, str]:
    """Charge les aliases depuis clients/_global/aliases.md."""
    aliases = {}
    f = VAULT / "clients" / "_global" / "aliases.md"
    if not f.exists():
        return aliases
    for line in f.read_text().splitlines():
        if "→" in line and not line.startswith("#"):
            parts = line.split("→", 1)
            if len(parts) == 2:
                alias = strip_accents(parts[0].strip().lower())
                target = parts[1].strip()
                aliases[alias] = target
    return aliases

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

async def cmd_myid(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.message.reply_text(f"Ton Telegram ID : `{uid}`", parse_mode="Markdown")

SAVE_TRIGGERS = [
    "sauvegarde cette réponse", "sauvegarde la réponse", "garde ça dans inbox",
    "mets ça dans inbox", "enregistre cette réponse", "ajoute à inbox",
    "save to inbox", "keep this", "garde cette conclusion"
]

def save_to_inbox(client_name: str, content: str) -> bool:
    """Sauvegarde une réponse du bot dans _inbox/ pour traitement Claude Code."""
    ts = datetime.now().strftime("%Y-%m-%d-%H-%M")
    filename = f"bot-{ts}-{client_name}.md"
    target = VAULT / "_inbox" / filename
    header = f"# Bot Hermes — {client_name} — {ts}\n\n"
    target.write_text(header + content)
    try:
        subprocess.run(["git", "add", str(target)], cwd=VAULT, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", f"bot → inbox : {client_name} {ts}"],
                       cwd=VAULT, check=True, capture_output=True)
        subprocess.run(["git", "push"], cwd=VAULT, check=True, capture_output=True)
        return True
    except subprocess.CalledProcessError:
        return False

MEMO_TRIGGERS = [
    "note ça", "note ca", "ajoute", "sauvegarde", "retiens",
    "mets dans inbox", "rappel", "mémorise", "memorise",
    "add to inbox", "save this", "remember this"
]

def detect_memo_intent(text: str) -> bool:
    t = text.lower()
    return any(trigger in t for trigger in MEMO_TRIGGERS)

def strip_accents(s: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFD', s) if unicodedata.category(c) != 'Mn')

def detect_client(text: str) -> str | None:
    """Détecte un client via alias ou nom de dossier. Retourne le nom ou None."""
    t = strip_accents(text.lower())
    # Aliases en priorité (plus longs d'abord pour éviter les faux positifs)
    aliases = load_aliases()
    for alias in sorted(aliases.keys(), key=len, reverse=True):
        if alias in t:
            return aliases[alias]
    # Fallback : nom de dossier direct
    for client in list_clients():
        if client != "_global" and strip_accents(client) in t:
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

    # Sauvegarde dernière réponse dans _inbox/
    if any(t in user_text.lower() for t in SAVE_TRIGGERS):
        last_reply = next(
            (h["parts"][0] for h in reversed(s["history"]) if h["role"] == "model"), None
        )
        if last_reply:
            pushed = save_to_inbox(s["client"], last_reply)
            status = "✓ pushé GitHub" if pushed else "(push échoué)"
            await update.message.reply_text(
                f"📥 Réponse sauvegardée dans `_inbox/` {status}\n"
                f"Claude Code la traitera à la prochaine session.",
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text("Pas de réponse récente à sauvegarder.")
        return

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

    active_vault = detect_vault(user_text)
    vault_search = search_vault(user_text, vault=active_vault)

    # Charger les fichiers des dossiers topic détectés
    topic_ctx = []
    for folder in detect_topics(user_text):
        topic_dir = VAULT / folder
        if topic_dir.exists():
            for md in sorted(topic_dir.glob("*.md"))[:2]:
                topic_ctx.append(f"### {folder}/{md.name}\n{md.read_text()[:1500]}")
    topic_content = "\n\n---\n\n".join(topic_ctx)

    system = (
        "Tu es l'assistant de Jarlounet, Brand Designer. "
        "Ton direct, chaleureux, concis. Pas de flatterie. "
        "Réponds en français sauf demande contraire. "
        "Si l'utilisateur te demande de sauvegarder ou noter quelque chose, "
        "dis-lui d'utiliser la commande /memo [info].\n\n"
        + (f"## Contexte client\n{vault_ctx}\n\n" if vault_ctx else "")
        + (f"## Ressources domaine\n{topic_content}\n\n" if topic_content else "")
        + (f"## Ressources pertinentes du vault\n{vault_search}" if vault_search else "")
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

    # Indicateur vault + client (discret, en italique)
    vault_name = next((k for k, v in VAULTS.items() if v == active_vault), "connaissance")
    indicator = f"\n\n📂 {vault_name} · {s['client']}"
    await update.message.reply_text(reply + indicator)


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
    app.add_handler(CommandHandler("myid",    cmd_myid))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print(f"🚀 Projet Hermes démarré — vault : {VAULT}")
    app.run_polling()

if __name__ == "__main__":
    main()
