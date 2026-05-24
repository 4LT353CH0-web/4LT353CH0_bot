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
import storage

load_dotenv()
storage.init_db()

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

# ── Sessions SQLite persistantes ──────────────────────────────────────────────
_session_cache: dict = {}  # cache in-memory pour pending_skill uniquement

def session(uid: int) -> dict:
    """Charge depuis SQLite. Cache pending_skill en RAM (éphémère ok)."""
    s = storage.get_session(uid)
    if uid in _session_cache:
        s["pending_skill"] = _session_cache[uid].get("pending_skill")
    return s

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
    storage.set_client(update.effective_user.id, name)
    storage.reset_history(update.effective_user.id)
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
    storage.reset_history(update.effective_user.id)
    await update.message.reply_text("🔄 Historique vidé.")

async def cmd_myid(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.message.reply_text(f"Ton Telegram ID : `{uid}`", parse_mode="Markdown")

async def cmd_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Recherche cross-sessions dans les conversations sauvegardées."""
    if not authorized(update.effective_user.id):
        return
    if not ctx.args:
        await update.message.reply_text("Usage : `/search [mot-clé]`", parse_mode="Markdown")
        return
    query = " ".join(ctx.args)
    uid = update.effective_user.id

    # 1. FTS5 SQLite (messages directs)
    fts_hits = storage.search_messages(query, user_id=uid, limit=5)

    # 2. Grep fichiers conversations _inbox/
    words = [strip_accents(w.lower()) for w in query.split() if len(w) > 2]
    inbox = VAULT / "_inbox"
    file_hits = []
    for f in sorted(inbox.glob("conversation-*.md"), reverse=True)[:30]:
        try:
            content = strip_accents(f.read_text().lower())
            score = sum(content.count(w) for w in words)
            if score > 0:
                file_hits.append((score, f))
        except Exception:
            pass
    file_hits.sort(reverse=True)

    if not fts_hits and not file_hits:
        await update.message.reply_text(f"Aucun résultat pour '{query}'.")
        return

    # Construire le contexte pour résumé
    blocks = []
    if fts_hits:
        blocks.append("## Messages directs\n" + "\n".join(
            f"[{h['ts'][:10]} {h['role']}] {h['content'][:300]}" for h in fts_hits
        ))
    if file_hits:
        top_files = file_hits[:2]
        blocks.append("## Conversations sauvegardées\n" + "\n\n---\n\n".join(
            f.read_text()[:600] for _, f in top_files
        ))

    summary_prompt = (
        f"Résume en 3-5 lignes ce que ces échanges disent sur '{query}'. "
        f"Direct, sans intro.\n\n" + "\n\n".join(blocks)
    )
    rs = gemini.models.generate_content(
        model=GEMINI_MODEL,
        contents=summary_prompt,
        config=types.GenerateContentConfig(max_output_tokens=400)
    )
    total = len(fts_hits) + len(file_hits)
    for chunk in split_message(f"🔍 '{query}' — {total} résultat(s)\n\n{rs.text}"):
        await update.message.reply_text(chunk)

async def cmd_monitor(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """État du système Hermes."""
    if not authorized(update.effective_user.id):
        return
    import shutil
    # Vault stats
    vault_files = len(list(VAULT.rglob("*.md")))
    inbox_files = len(list((VAULT / "_inbox").glob("*.md"))) - 1  # -README
    conversations = len(list((VAULT / "_inbox").glob("conversation-*.md")))
    clients = list_clients()
    # Disk (local)
    disk = shutil.disk_usage(str(VAULT))
    disk_free = disk.free // (1024**3)
    status = (
        f"🤖 Hermes — état système\n\n"
        f"📁 Vault : {vault_files} fichiers .md\n"
        f"📥 Inbox : {inbox_files} fichiers en attente\n"
        f"💬 Conversations sauvegardées : {conversations}\n"
        f"👥 Clients : {', '.join(c for c in clients if c != '_global')}\n"
        f"💾 Espace disque libre : {disk_free} Go\n\n"
        f"Vaults actifs :\n"
        + "\n".join(f"  {'✅' if v.exists() else '❌'} {k}" for k, v in VAULTS.items())
    )
    await update.message.reply_text(status)

async def cmd_save(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Sauvegarde la conversation en cours dans _inbox/ sans reset la session."""
    if not authorized(update.effective_user.id):
        return
    s = session(update.effective_user.id)
    if len(s["history"]) < 2:
        await update.message.reply_text("Pas encore assez d'échanges à sauvegarder.")
        return
    await update.message.chat.send_action("typing")
    pushed = extract_and_save_conversation(s["client"], s["history"])
    status = "✓ pushé GitHub" if pushed else "(push local uniquement)"
    await update.message.reply_text(
        f"📥 Conversation sauvegardée dans _inbox/ {status}\n"
        f"Actions extraites — Claude Code les traitera à la prochaine session."
    )

async def handle_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Reçoit un vocal Telegram → transcrit via Gemini → répond + sauvegarde inbox."""
    if not authorized(update.effective_user.id):
        return
    s = session(update.effective_user.id)
    await update.message.chat.send_action("typing")

    # Télécharger le fichier audio
    voice = update.message.voice or update.message.audio
    tg_file = await ctx.bot.get_file(voice.file_id)
    import tempfile, httpx
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        tmp_path = tmp.name

    async with httpx.AsyncClient() as client:
        resp = await client.get(tg_file.file_path)
        Path(tmp_path).write_bytes(resp.content)

    # Transcrire + répondre via Gemini
    try:
        audio_data = Path(tmp_path).read_bytes()
        import base64
        b64 = base64.b64encode(audio_data).decode()
        vault_ctx = load_context(s["client"])
        system = (
            "Tu es l'assistant de Jarlounet, Brand Designer. "
            "Transcris d'abord le message vocal, puis réponds de façon concise. "
            "Format : [Transcription : ...]\n\n[Réponse : ...]\n\n"
            "LIMITES STRICTES — tu ne peux PAS : créer des alertes, programmer des rappels, "
            "accéder à un calendrier, envoyer des emails, mémoriser entre les sessions. "
            "Si on te demande ça, dis-le clairement et propose une alternative réelle "
            "(ex: /memo pour ancrer dans le vault, n8n pour automatiser). "
            "Ne simule JAMAIS une action que tu n'as pas faite.\n\n"
            + (f"Contexte client :\n{vault_ctx}" if vault_ctx else "")
        )
        r = gemini.models.generate_content(
            model=GEMINI_MODEL,
            contents=[
                types.Content(role="user", parts=[types.Part(text=system)]),
                types.Content(role="model", parts=[types.Part(text="Compris.")]),
                types.Content(role="user", parts=[
                    types.Part(inline_data=types.Blob(mime_type="audio/ogg", data=audio_data)),
                    types.Part(text="Transcris ce message vocal et réponds.")
                ])
            ],
            config=types.GenerateContentConfig(max_output_tokens=1500)
        )
        reply = r.text
    except Exception as e:
        reply = f"⚠️ Transcription échouée : {e}"
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    vault_name = next((k for k, v in VAULTS.items() if v == VAULT), "connaissance")
    indicator = f"\n\n🎙️ vocal · {vault_name} · {s['client']}"
    for chunk in split_message(md_to_html(reply) + indicator):
        await update.message.reply_text(chunk, parse_mode="HTML")

    # Sauvegarder dans inbox
    save_to_inbox(s["client"], f"[VOCAL]\n{reply}")

# ── Mots-clés fin de session → auto-save conversation ────────────────────────
SESSION_END_TRIGGERS = [
    "fin", "stop", "bye", "ciao", "à plus", "a plus", "bonne nuit",
    "merci", "on s'arrête", "on s arrete", "pause", "à demain", "a demain",
    "c'est bon pour aujourd'hui", "c bon", "on park", "wrap"
]

SAVE_TRIGGERS = [
    "sauvegarde cette réponse", "sauvegarde la réponse", "garde ça dans inbox",
    "mets ça dans inbox", "enregistre cette réponse", "ajoute à inbox",
    "save to inbox", "keep this", "garde cette conclusion"
]

def extract_and_save_conversation(client_name: str, history: list) -> bool:
    """Analyse la conversation, extrait les actions, sauvegarde dans _inbox/."""
    if len(history) < 2:
        return False

    # Reconstituer le transcript
    transcript = []
    for h in history:
        role = "Jarl" if h["role"] == "user" else "Hermes"
        transcript.append(f"**{role}** : {h['parts'][0]}")
    transcript_text = "\n\n".join(transcript)

    # Gemini extrait les actions
    prompt = (
        "Analyse cette conversation entre Jarlounet et son assistant Hermes. "
        "Extrais UNIQUEMENT les actions concrètes, décisions, ou choses à faire mentionnées. "
        "Format strict :\n"
        "## Actions détectées\n"
        "- [ ] Action 1\n"
        "- [ ] Action 2\n\n"
        "## Résumé de la conversation (2-3 lignes max)\n"
        "[résumé]\n\n"
        "Si aucune action détectée, écris '## Actions détectées\nAucune action identifiée.'\n"
        "Pas d'intro, pas de commentaire, texte brut.\n\n"
        f"--- CONVERSATION ---\n{transcript_text}"
    )

    try:
        r = gemini.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(max_output_tokens=600)
        )
        analysis = r.text.strip()
    except Exception:
        analysis = "## Actions détectées\nAnalyse échouée.\n\n## Résumé\nConversation sauvegardée brute."

    ts = datetime.now().strftime("%Y-%m-%d-%H-%M")
    filename = f"conversation-{ts}-{client_name}.md"
    target = VAULT / "_inbox" / filename
    content = (
        f"# Conversation Hermes — {client_name} — {ts}\n\n"
        f"{analysis}\n\n"
        f"---\n\n## Transcript complet\n\n{transcript_text}\n"
    )
    target.write_text(content)
    try:
        subprocess.run(["git", "add", str(target)], cwd=VAULT, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", f"conversation hermes : {client_name} {ts}"],
                       cwd=VAULT, check=True, capture_output=True)
        subprocess.run(["git", "push"], cwd=VAULT, check=True, capture_output=True)
        return True
    except subprocess.CalledProcessError:
        return False

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

WEB_TRIGGERS = [
    "cherche sur le web", "google ça", "google ca", "recherche en ligne",
    "actualité", "actualites", "derniere version", "dernière version",
    "c'est quoi", "c est quoi", "keski", "qu'est-ce que",
    "prix de", "site de", "trouve moi", "search"
]

def detect_memo_intent(text: str) -> bool:
    t = text.lower()
    return any(trigger in t for trigger in MEMO_TRIGGERS)

def detect_web_intent(text: str) -> bool:
    t = strip_accents(text.lower())
    return any(trigger in t for trigger in [strip_accents(w) for w in WEB_TRIGGERS])

def strip_accents(s: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFD', s) if unicodedata.category(c) != 'Mn')

def md_to_html(text: str) -> str:
    """Convertit Markdown basique → HTML Telegram. Escape les chars dangereux."""
    import re
    # Escape HTML d'abord
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    # **bold** ou __bold__
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'__(.+?)__', r'<b>\1</b>', text)
    # *italic* ou _italic_
    text = re.sub(r'\*(.+?)\*', r'<i>\1</i>', text)
    text = re.sub(r'(?<!\w)_(.+?)_(?!\w)', r'<i>\1</i>', text)
    # `code`
    text = re.sub(r'`([^`]+)`', r'<code>\1</code>', text)
    # ```bloc```
    text = re.sub(r'```[\w]*\n?(.*?)```', r'<pre>\1</pre>', text, flags=re.DOTALL)
    return text

def split_message(text: str, limit: int = 4000) -> list[str]:
    """Découpe un texte long en blocs ≤ limit chars, en coupant aux paragraphes."""
    if len(text) <= limit:
        return [text]
    chunks = []
    current = ""
    for paragraph in text.split("\n\n"):
        candidate = (current + "\n\n" + paragraph).lstrip()
        if len(candidate) <= limit:
            current = candidate
        else:
            # Le paragraphe lui-même est trop long → couper aux phrases
            if current:
                chunks.append(current)
                current = ""
            if len(paragraph) <= limit:
                current = paragraph
            else:
                for i in range(0, len(paragraph), limit):
                    chunks.append(paragraph[i:i+limit])
    if current:
        chunks.append(current)
    return chunks

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
        storage.set_client(uid, detected)
        storage.reset_history(uid)
        s["client"] = detected
        s["history"] = []

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

    t_lower = strip_accents(user_text.lower())

    # Réponse à la suggestion de skill (validation humaine → _inbox/ uniquement)
    if "skill oui" in t_lower and s.get("pending_skill"):
        skill_name = s["pending_skill"]
        skill_prompt = (
            f"Crée un skill Claude Code nommé '{skill_name}' basé sur cette conversation. "
            f"Format Markdown : ## Objectif, ## Quand l'utiliser, ## Étapes, ## Exemples. "
            f"Concis, actionnable, en français.\n\n"
            + "\n".join(h["parts"][0] for h in s["history"][-8:])
        )
        rs = gemini.models.generate_content(
            model=GEMINI_MODEL,
            contents=skill_prompt,
            config=types.GenerateContentConfig(max_output_tokens=800)
        )
        skill_slug = strip_accents(skill_name.lower().replace(" ", "-"))
        ts_skill = datetime.now().strftime("%Y-%m-%d-%H-%M")
        target = VAULT / "_inbox" / f"skill-suggestion-{ts_skill}-{skill_slug}.md"
        target.write_text(
            f"# Skill suggéré : {skill_name}\n\n"
            f"> Valider en session Claude Code → déplacer vers `.claude/commands/{skill_slug}.md`\n\n"
            f"{rs.text}"
        )
        try:
            subprocess.run(["git", "add", str(target)], cwd=VAULT, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", f"skill suggestion : {skill_name}"], cwd=VAULT, check=True, capture_output=True)
            subprocess.run(["git", "push"], cwd=VAULT, check=True, capture_output=True)
        except Exception:
            pass
        s["pending_skill"] = None
        await update.message.reply_text(
            f"✅ Skill '{skill_name}' en attente de validation dans _inbox/\n"
            f"Claude Code te le proposera à la prochaine session locale."
        )
        return
    elif "skill non" in t_lower and s.get("pending_skill"):
        s["pending_skill"] = None
        await update.message.reply_text("Skill ignoré.")
        return

    # Fin de session → sauvegarder la conversation dans _inbox/
    if any(trigger in t_lower for trigger in SESSION_END_TRIGGERS) and len(s["history"]) >= 2:
        pushed = extract_and_save_conversation(s["client"], s["history"])
        status = "✓ pushé GitHub" if pushed else "(push local uniquement)"
        await update.message.reply_text(
            f"📥 Conversation sauvegardée dans _inbox/ {status}\n"
            f"Claude Code la traitera à la prochaine session locale — actions incluses."
        )
        storage.reset_history(uid)
        s["history"] = []
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
        "dis-lui d'utiliser la commande /memo [info].\n"
        "LIMITES STRICTES — tu ne peux PAS : créer des alertes, programmer des rappels, "
        "accéder à un calendrier, envoyer des emails, mémoriser entre les sessions. "
        "Si on te demande ça, dis-le clairement et propose une alternative réelle. "
        "Ne simule JAMAIS une action que tu n'as pas faite.\n\n"
        + (f"## Contexte client\n{vault_ctx}\n\n" if vault_ctx else "")
        + (f"## Ressources domaine\n{topic_content}\n\n" if topic_content else "")
        + (f"## Ressources pertinentes du vault\n{vault_search}" if vault_search else "")
    )

    storage.add_message(uid, "user", user_text)
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

    # Browser : activer Google Search si intent web détecté
    use_web = detect_web_intent(user_text)
    gen_config = types.GenerateContentConfig(
        max_output_tokens=1500,
        tools=[types.Tool(google_search=types.GoogleSearch())] if use_web else []
    )

    r = gemini.models.generate_content(
        model=GEMINI_MODEL,
        contents=contents,
        config=gen_config
    )

    reply = r.text
    storage.add_message(uid, "model", reply)
    s["history"].append({"role": "model", "parts": [reply]})

    # Indicateur vault + client + web si utilisé
    vault_name = next((k for k, v in VAULTS.items() if v == active_vault), "connaissance")
    web_tag = " · 🌐" if use_web else ""
    indicator = f"\n\n📂 {vault_name} · {s['client']}{web_tag}"
    for chunk in split_message(md_to_html(reply) + indicator):
        await update.message.reply_text(chunk, parse_mode="HTML")

    # Suggestion de skill après 4+ échanges
    exchanges = len(s["history"]) // 2
    if exchanges >= 4 and exchanges % 4 == 0:
        skill_prompt = (
            "Analyse cet échange. En une phrase : y a-t-il une bonne pratique, "
            "un process ou une méthode réutilisable qui mérite d'être codifié en skill ? "
            "Réponds UNIQUEMENT par 'OUI : [nom du skill suggéré]' ou 'NON'.\n\n"
            + "\n".join(h["parts"][0] for h in s["history"][-6:])
        )
        try:
            rs = gemini.models.generate_content(
                model=GEMINI_MODEL,
                contents=skill_prompt,
                config=types.GenerateContentConfig(max_output_tokens=50)
            )
            if rs.text.strip().upper().startswith("OUI"):
                skill_name = rs.text.strip()[4:].strip()
                s["pending_skill"] = skill_name
                await update.message.reply_text(
                    f"💡 Skill détecté : {skill_name}\n"
                    f"Répondre 'skill oui' pour le créer dans le vault, 'skill non' pour ignorer."
                )
        except Exception:
            pass


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
    app.add_handler(CommandHandler("save",    cmd_save))
    app.add_handler(CommandHandler("search",  cmd_search))
    app.add_handler(CommandHandler("monitor", cmd_monitor))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))

    print(f"🚀 Projet Hermes démarré — vault : {VAULT}")
    app.run_polling()

if __name__ == "__main__":
    main()
