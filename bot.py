"""
Projet Hermes — Bot Telegram
Architecture : Claude Code + claude-connaissance vault
Multi-client, validation humaine, pas d'auto-modification
"""
from __future__ import annotations

import os
import asyncio
import time
import subprocess
import unicodedata
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv
load_dotenv()  # ← avant tout import qui lit les env vars

from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters, ContextTypes
)
from google import genai
from google.genai import types
import storage
import calendar_google as gcal
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
_last_discord_activity: dict[int, float] = {}

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
        subprocess.run(["git", "pull", "--rebase"], cwd=VAULT, check=True, capture_output=True)
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
        "📅 *Calendrier*\n"
        "`/agenda [n]` — prochains événements\n"
        "`/tache [texte]` — créer un événement\n"
        "`/meet [texte]` — créer + lien Meet\n"
        "`/campagne [planning]` — créer plusieurs events d'un coup\n"
        "`/annule [description]` — supprimer un événement\n"
        "`/modif [event] | [modification]` — modifier\n\n"
        "🗃️ *Vault*\n"
        "`/client [nom]` — changer de contexte\n"
        "`/memo [info]` — ancrer dans le vault\n"
        "`/gold [texte]` — distiller des idées\n\n"
        "🔧 *Système*\n"
        "`/status` · `/reset` · `/search` · `/monitor`\n\n"
        "Ou écris directement — 'annule le rdv de lundi', 'planifie une réunion jeudi 10h'…",
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

def _gcal_available() -> bool:
    """Vérifie si le token Google Calendar est disponible."""
    return (gcal.TOKEN_FILE.exists() or gcal.CREDENTIALS_FILE.exists())

def _gemini_fn(prompt: str) -> str:
    """Wrappeur synchrone Gemini pour le parsing de dates/events.
    max_output_tokens élevé : Gemini 2.5 Flash consomme des tokens sur sa réflexion interne."""
    r = gemini.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(max_output_tokens=1024)
    )
    return r.text

async def cmd_agenda(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Affiche les prochains événements Google Calendar."""
    if not authorized(update.effective_user.id):
        return
    if not _gcal_available():
        await update.message.reply_text(
            "⚠️ Google Calendar non configuré.\n"
            "Setup : <code>python3 calendar_google.py --auth</code> en local,\n"
            "puis copier <code>token-gcal.json</code> sur le VPS.",
            parse_mode="HTML"
        )
        return
    n = int(ctx.args[0]) if ctx.args and ctx.args[0].isdigit() else 5
    await update.message.chat.send_action("typing")
    try:
        events = gcal.get_upcoming_events(n)
        text   = gcal.format_events_list(events)
    except Exception as e:
        text = f"⚠️ Erreur Calendar : {e}"
    for chunk in split_message(text):
        await update.message.reply_text(chunk, parse_mode="HTML")

async def cmd_rdv(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Crée un événement. Usage : /rdv Titre du RDV | demain à 14h | 45min"""
    if not authorized(update.effective_user.id):
        return
    if not _gcal_available():
        await update.message.reply_text("⚠️ Google Calendar non configuré. Voir /agenda.")
        return
    if not ctx.args:
        await update.message.reply_text(
            "Usage : <code>/rdv Titre | demain à 14h | 45min</code>\n"
            "Ou simplement : <code>/rdv Appel client lundi 10h</code>",
            parse_mode="HTML"
        )
        return
    raw = " ".join(ctx.args)
    await update.message.chat.send_action("typing")
    parsed = gcal.parse_event_args(raw, _gemini_fn)
    if not parsed:
        await update.message.reply_text("❌ Impossible de parser la date/heure. Essaie : /rdv Titre | 2026-05-28 | 14:00")
        return
    dt = gcal.build_event_datetime(parsed)
    if not dt:
        await update.message.reply_text("❌ Date invalide dans la réponse parsée.")
        return
    try:
        event = gcal.create_event(
            title        = parsed.get("title", raw),
            start_dt     = dt,
            duration_min = parsed.get("duration_min", 60),
            with_meet    = parsed.get("with_meet", False),
        )
        await update.message.reply_text(gcal.format_created_event(event), parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Erreur création : {e}")

async def cmd_meet(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Crée un événement avec lien Meet. Usage : /meet Titre | demain à 15h"""
    if not authorized(update.effective_user.id):
        return
    if not _gcal_available():
        await update.message.reply_text("⚠️ Google Calendar non configuré. Voir /agenda.")
        return
    if not ctx.args:
        await update.message.reply_text(
            "Usage : <code>/meet Titre de la réunion | demain à 15h</code>",
            parse_mode="HTML"
        )
        return
    raw = " ".join(ctx.args)
    await update.message.chat.send_action("typing")
    parsed = gcal.parse_event_args(raw, _gemini_fn)
    if not parsed:
        await update.message.reply_text("❌ Impossible de parser la date/heure.")
        return
    parsed["with_meet"] = True
    dt = gcal.build_event_datetime(parsed)
    if not dt:
        await update.message.reply_text("❌ Date invalide.")
        return
    try:
        event = gcal.create_event(
            title        = parsed.get("title", raw),
            start_dt     = dt,
            duration_min = parsed.get("duration_min", 60),
            with_meet    = True,
        )
        await update.message.reply_text(gcal.format_created_event(event), parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Erreur création Meet : {e}")

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
            "LIMITES STRICTES — tu ne peux PAS : créer des alertes, envoyer des emails, "
            "mémoriser entre les sessions. "
            "Pour le calendrier Google : utilise /agenda, /rdv, /meet. "
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
                    config=types.GenerateContentConfig(max_output_tokens=4096)
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
    inbox_dir = VAULT / "_inbox" / "4LT353CH0-bot"
    inbox_dir.mkdir(parents=True, exist_ok=True)
    target = inbox_dir / filename
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
        subprocess.run(["git", "pull", "--rebase"], cwd=VAULT, check=True, capture_output=True)
        subprocess.run(["git", "push"], cwd=VAULT, check=True, capture_output=True)
        return True
    except subprocess.CalledProcessError:
        return False

def save_to_inbox(client_name: str, content: str) -> bool:
    """Sauvegarde une réponse du bot dans _inbox/ pour traitement Claude Code."""
    ts = datetime.now().strftime("%Y-%m-%d-%H-%M")
    filename = f"bot-{ts}-{client_name}.md"
    inbox_dir = VAULT / "_inbox" / "4LT353CH0-bot"
    inbox_dir.mkdir(parents=True, exist_ok=True)
    target = inbox_dir / filename
    header = f"# Bot Hermes — {client_name} — {ts}\n\n"
    target.write_text(header + content)
    try:
        subprocess.run(["git", "add", str(target)], cwd=VAULT, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", f"bot → inbox : {client_name} {ts}"],
                       cwd=VAULT, check=True, capture_output=True)
        subprocess.run(["git", "pull", "--rebase"], cwd=VAULT, check=True, capture_output=True)
        subprocess.run(["git", "push"], cwd=VAULT, check=True, capture_output=True)
        return True
    except subprocess.CalledProcessError:
        return False

CALENDAR_TRIGGERS = [
    "agenda", "calendrier", "planning", "reunion", "réunion", "rendez-vous",
    "suis libre", "je suis dispo", "disponible", "ce soir", "cette semaine",
    "prochain meeting", "prochaine reunion", "quand est", "à quelle heure",
    "mon planning", "mes rendez", "prochains events", "qu'est-ce que j'ai",
]

NL_DELETE_WORDS = ["annule", "supprime", "cancel", "efface", "retire du calendrier"]
NL_MODIFY_WORDS = [
    "decale", "reporte", "modifie", "change l heure", "repousse", "deplace",
    "trop tot", "trop tard", "pas le temps", "arrive trop", "ca ne va pas",
    "probleme avec", "conflit", "indisponible",
]
NL_CREATE_WORDS = [
    "cree un rdv", "ajoute un rdv", "planifie", "mets au calendrier",
    "ajoute au calendrier", "note un rdv", "prevois", "bloque le creneau",
]

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

def detect_calendar_action(text: str) -> str | None:
    """Retourne 'delete', 'modify', 'create', ou None."""
    t = strip_accents(text.lower())
    if any(w in t for w in [strip_accents(x) for x in NL_DELETE_WORDS]):
        return "delete"
    if any(w in t for w in [strip_accents(x) for x in NL_MODIFY_WORDS]):
        return "modify"
    if any(w in t for w in [strip_accents(x) for x in NL_CREATE_WORDS]):
        return "create"
    return None

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

def _derive_campaign_name(events: list[dict]) -> str:
    """Dérive un nom court de campagne depuis la liste d'événements parsés."""
    from datetime import datetime as _dt
    if not events:
        return "Campagne"
    titles = [ev.get("title", "") for ev in events]
    first_words = [t.split()[0] if t.split() else "" for t in titles]
    common = first_words[0] if first_words and all(w == first_words[0] for w in first_words) else ""
    try:
        dt = _dt.strptime(events[0]["date"], "%Y-%m-%d")
        month_year = dt.strftime("%B %Y")
    except Exception:
        month_year = ""
    if common:
        return f"Campagne {common} {month_year}".strip()
    return f"Planning {month_year}".strip() or "Campagne"


def _build_campagne_table(events: list[dict], campaign_name: str) -> str:
    """Formate le tableau de validation d'une campagne pour Telegram HTML."""
    from datetime import datetime as _dt
    n = len(events)
    lines = [f"📅 <b>{n} événement(s) détecté(s) :</b>"]
    for i, ev in enumerate(events, 1):
        title    = ev.get("title", "Sans titre")
        date_str = ev.get("date", "")
        time_str = ev.get("time", "09:00")
        dur      = ev.get("duration_min", 60)
        try:
            dt_s = _dt.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
            dt_e = dt_s + timedelta(minutes=dur)
            day_label  = dt_s.strftime("%a %d/%m/%Y")
            time_label = f"{dt_s.strftime('%H:%M')}–{dt_e.strftime('%H:%M')}"
        except Exception:
            day_label  = date_str
            time_label = time_str
        desc = f"Tâche N°{i}/{n} — {campaign_name}"
        lines.append(
            f"\n<b>{i}.</b> {title}\n"
            f"   📆 {day_label}  🕐 {time_label}\n"
            f"   📝 <i>{desc}</i>"
        )
    lines.append(
        "\n<i>C'est conforme ? Réponds <b>oui</b> pour créer, <b>non</b> pour annuler,\n"
        "ou dis-moi ce que tu veux changer (ex: \"décale le 2 à 14h\").</i>"
    )
    return "\n".join(lines)


def _fmt_gcal_start(start: str) -> str:
    """Formate une datetime ISO Calendar en chaîne lisible."""
    try:
        if "T" in start:
            dt = datetime.fromisoformat(start).astimezone(gcal.TZ)
            return dt.strftime("%a %d/%m à %H:%M")
        return start
    except Exception:
        return start

def _apply_event_update(event: dict, parsed: dict) -> dict | None:
    """Applique les modifications parsées et retourne l'événement mis à jour, ou None."""
    title    = parsed.get("title") or None
    start_dt = None
    if parsed.get("date") or parsed.get("time"):
        orig_start = event["start"].get("dateTime") or event["start"].get("date", "")
        try:
            orig_dt = datetime.fromisoformat(orig_start).astimezone(gcal.TZ)
            d = parsed.get("date") or orig_dt.strftime("%Y-%m-%d")
            t = parsed.get("time") or orig_dt.strftime("%H:%M")
            start_dt = datetime.strptime(f"{d} {t}", "%Y-%m-%d %H:%M").replace(tzinfo=gcal.TZ)
        except Exception:
            pass
    try:
        return gcal.update_event(
            event["id"],
            title        = title,
            start_dt     = start_dt,
            duration_min = parsed.get("duration_min"),
        )
    except Exception:
        return None

async def _handle_calendar_nl(update: Update, action: str, text: str, uid: int) -> bool:
    """Gère une action calendrier détectée en NL. Retourne True si gérée."""
    await update.message.chat.send_action("typing")

    if action == "delete":
        event = gcal.find_event_by_description(text, _gemini_fn)
        if not event:
            await update.message.reply_text("❌ Aucun événement trouvé pour cette description.")
            return True
        title = event.get("summary", "(Sans titre)")
        ts    = _fmt_gcal_start(event["start"].get("dateTime") or event["start"].get("date", ""))
        try:
            gcal.delete_event(event["id"])
            await update.message.reply_text(
                f"🗑️ <b>{title}</b> supprimé\n🕐 {ts}", parse_mode="HTML"
            )
        except Exception as e:
            await update.message.reply_text(f"⚠️ Erreur suppression : {e}")
        return True

    if action == "modify":
        event = gcal.find_event_by_description(text, _gemini_fn)
        if not event:
            return False  # Passer au flow normal si on ne trouve pas l'event
        parsed = gcal.parse_modify_args(text, event, _gemini_fn)
        if parsed:
            # Modification claire → on applique directement
            updated = _apply_event_update(event, parsed)
            if updated:
                ts = _fmt_gcal_start(updated["start"].get("dateTime", ""))
                await update.message.reply_text(
                    f"✏️ <b>{updated.get('summary', '(Sans titre)')}</b> mis à jour\n🕐 {ts}",
                    parse_mode="HTML",
                )
            else:
                await update.message.reply_text("⚠️ Modification impossible.")
        else:
            # Modification vague → poser la question, stocker l'état
            title = event.get("summary", "(Sans titre)")
            ts    = _fmt_gcal_start(event["start"].get("dateTime") or event["start"].get("date", ""))
            if uid not in _session_cache:
                _session_cache[uid] = {}
            _session_cache[uid]["pending_modif"] = {
                "event_id":    event["id"],
                "event_title": title,
            }
            await update.message.reply_text(
                f"📅 <b>{title}</b> — {ts}\n\n"
                f"Qu'est-ce qui pose problème ? Le jour, l'heure, la durée, ou autre chose ?",
                parse_mode="HTML",
            )
        return True

    if action == "create":
        parsed = gcal.parse_event_args(text, _gemini_fn)
        if not parsed:
            return False
        dt = gcal.build_event_datetime(parsed)
        if not dt:
            return False
        try:
            event = gcal.create_event(
                title        = parsed.get("title", text),
                start_dt     = dt,
                duration_min = parsed.get("duration_min", 60),
                with_meet    = parsed.get("with_meet", False),
            )
            await update.message.reply_text(gcal.format_created_event(event), parse_mode="HTML")
        except Exception as e:
            await update.message.reply_text(f"⚠️ Erreur création : {e}")
        return True

    return False

async def cmd_tache(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Alias de /rdv — crée un événement en NL."""
    return await cmd_rdv(update, ctx)

async def cmd_annule(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Annule un événement. Usage : /annule réunion de lundi"""
    if not authorized(update.effective_user.id):
        return
    if not _gcal_available():
        await update.message.reply_text("⚠️ Google Calendar non configuré.")
        return
    if not ctx.args:
        await update.message.reply_text(
            "Usage : <code>/annule [description de l'événement]</code>\n"
            "Ex : <code>/annule réunion lundi matin</code>",
            parse_mode="HTML",
        )
        return
    raw = " ".join(ctx.args)
    await update.message.chat.send_action("typing")
    event = gcal.find_event_by_description(raw, _gemini_fn)
    if not event:
        await update.message.reply_text("❌ Aucun événement trouvé pour cette description.")
        return
    title = event.get("summary", "(Sans titre)")
    ts    = _fmt_gcal_start(event["start"].get("dateTime") or event["start"].get("date", ""))
    try:
        gcal.delete_event(event["id"])
        await update.message.reply_text(
            f"🗑️ <b>{title}</b> supprimé\n🕐 {ts}", parse_mode="HTML"
        )
    except Exception as e:
        await update.message.reply_text(f"⚠️ Erreur suppression : {e}")

async def cmd_modif(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Modifie un événement. Usage : /modif réunion lundi | décale à 15h"""
    if not authorized(update.effective_user.id):
        return
    if not _gcal_available():
        await update.message.reply_text("⚠️ Google Calendar non configuré.")
        return
    if not ctx.args:
        await update.message.reply_text(
            "Usage : <code>/modif [événement] | [modification]</code>\n"
            "Ex : <code>/modif réunion lundi | décale à 15h</code>\n"
            "Ex : <code>/modif appel client | renomme en Call Idéallis</code>",
            parse_mode="HTML",
        )
        return
    raw = " ".join(ctx.args)
    await update.message.chat.send_action("typing")
    # Séparer description de l'event et modification si | présent
    if "|" in raw:
        event_desc, change_desc = [x.strip() for x in raw.split("|", 1)]
    else:
        event_desc = change_desc = raw
    event = gcal.find_event_by_description(event_desc, _gemini_fn)
    if not event:
        await update.message.reply_text("❌ Aucun événement trouvé pour cette description.")
        return
    parsed = gcal.parse_modify_args(change_desc, event, _gemini_fn)
    if not parsed:
        await update.message.reply_text("❌ Impossible de parser la modification.")
        return
    updated = _apply_event_update(event, parsed)
    if updated:
        ts = _fmt_gcal_start(updated["start"].get("dateTime", ""))
        await update.message.reply_text(
            f"✏️ <b>{updated.get('summary', '(Sans titre)')}</b> mis à jour\n🕐 {ts}",
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text("⚠️ Modification impossible.")

async def cmd_campagne(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Parse un planning et demande validation avant de créer les events.
    Usage : /campagne Interview Éric 9 juin 10h, Hervé 16 juin 10h…"""
    if not authorized(update.effective_user.id):
        return
    if not _gcal_available():
        await update.message.reply_text("⚠️ Google Calendar non configuré.")
        return
    if not ctx.args:
        await update.message.reply_text(
            "Usage : <code>/campagne [planning]</code>\n"
            "Ex : <code>/campagne Interview Éric 9 juin 10h, Hervé 16 juin 10h, Karine 23 juin 10h</code>",
            parse_mode="HTML",
        )
        return
    uid = update.effective_user.id
    raw = " ".join(ctx.args)
    await update.message.chat.send_action("typing")
    events = gcal.parse_event_list(raw, _gemini_fn)
    if not events:
        await update.message.reply_text("❌ Aucun événement détecté dans ce texte.")
        return
    campaign_name = _derive_campaign_name(events)
    _session_cache.setdefault(uid, {})["pending_campagne"] = {
        "events": events,
        "campaign_name": campaign_name,
    }
    reply = _build_campagne_table(events, campaign_name)
    for chunk in split_message(reply):
        await update.message.reply_text(chunk, parse_mode="HTML")


async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id):
        return
    uid = update.effective_user.id
    s = session(uid)
    user_text = update.message.text

    # Résolution pending_campagne (validation campagne en attente)
    pending_campagne = _session_cache.get(uid, {}).get("pending_campagne")
    if pending_campagne and _gcal_available():
        t_lower = user_text.lower().strip()
        CONFIRM = {"oui", "ok", "go", "c'est bon", "vas-y", "crée", "cree", "yes", "ouais"}
        CANCEL  = {"non", "annule", "cancel", "stop", "laisse tomber"}
        is_confirm = any(t_lower == kw or t_lower.startswith(kw + " ") for kw in CONFIRM)
        is_cancel  = any(t_lower == kw or t_lower.startswith(kw + " ") for kw in CANCEL)

        if is_confirm:
            events        = pending_campagne["events"]
            campaign_name = pending_campagne["campaign_name"]
            _session_cache[uid]["pending_campagne"] = None
            await update.message.chat.send_action("typing")
            created, errors = [], []
            n = len(events)
            for i, ev in enumerate(events, 1):
                dt = gcal.build_event_datetime(ev)
                if not dt:
                    errors.append(ev.get("title", "?"))
                    continue
                description = f"Tâche N°{i}/{n} — {campaign_name}"
                try:
                    c = gcal.create_event(
                        title        = ev.get("title", "Sans titre"),
                        start_dt     = dt,
                        duration_min = ev.get("duration_min", 60),
                        description  = description,
                        with_meet    = ev.get("with_meet", False),
                    )
                    ts = _fmt_gcal_start(c["start"].get("dateTime", ""))
                    created.append(f"✅ <b>{c.get('summary', '?')}</b> — {ts}")
                except Exception as e:
                    errors.append(f"{ev.get('title', '?')} ({e})")
            reply = f"📅 <b>Campagne créée — {len(created)} événement(s)</b>\n\n" + "\n".join(created)
            if errors:
                reply += "\n\n⚠️ Échecs : " + ", ".join(errors)
            for chunk in split_message(reply):
                await update.message.reply_text(chunk, parse_mode="HTML")
            return

        elif is_cancel:
            _session_cache[uid]["pending_campagne"] = None
            await update.message.reply_text("❌ Campagne annulée.")
            return

        else:
            # Modifier la liste via Gemini puis réafficher
            await update.message.chat.send_action("typing")
            updated = gcal.parse_campagne_modif(
                pending_campagne["events"], user_text, _gemini_fn
            )
            _session_cache[uid]["pending_campagne"]["events"] = updated
            reply = _build_campagne_table(updated, pending_campagne["campaign_name"])
            for chunk in split_message(reply):
                await update.message.reply_text(chunk, parse_mode="HTML")
            return

    # Résolution pending_modif (modification conversationnelle en attente)
    pending_modif = _session_cache.get(uid, {}).get("pending_modif")
    if pending_modif and _gcal_available():
        await update.message.chat.send_action("typing")
        try:
            event = gcal.get_event_by_id(pending_modif["event_id"])
        except Exception:
            event = {
                "id": pending_modif["event_id"],
                "summary": pending_modif["event_title"],
                "start": {"dateTime": ""},
                "end":   {"dateTime": ""},
            }
        _session_cache[uid]["pending_modif"] = None  # toujours effacer
        parsed = gcal.parse_modify_args(user_text, event, _gemini_fn)
        if parsed:
            updated = _apply_event_update(event, parsed)
            if updated:
                ts = _fmt_gcal_start(updated["start"].get("dateTime", ""))
                await update.message.reply_text(
                    f"✏️ <b>{updated.get('summary', '(Sans titre)')}</b> mis à jour\n🕐 {ts}",
                    parse_mode="HTML",
                )
                return
        await update.message.reply_text(
            "❌ Pas compris la modification. Reformule ou utilise /modif."
        )
        return

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

    # Détection action calendrier en NL (annule / modifie / crée)
    if _gcal_available():
        cal_action = detect_calendar_action(user_text)
        if cal_action:
            handled = await _handle_calendar_nl(update, cal_action, user_text, uid)
            if handled:
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
            subprocess.run(["git", "pull", "--rebase"], cwd=VAULT, check=True, capture_output=True)
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

    # Injection agenda si sujet calendrier détecté
    calendar_ctx = ""
    t_cal = strip_accents(user_text.lower())
    if _gcal_available() and any(trigger in t_cal for trigger in CALENDAR_TRIGGERS):
        try:
            events = gcal.get_upcoming_events(7)
            calendar_ctx = gcal.events_to_context(events)
        except Exception:
            pass

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
        "LIMITES STRICTES — tu ne peux PAS : créer des alertes, envoyer des emails, "
        "mémoriser entre les sessions. "
        "Pour le calendrier Google : utilise /agenda, /rdv, /meet. "
        "Ne simule JAMAIS une action que tu n'as pas faite.\n\n"
        + (f"## Contexte client\n{vault_ctx}\n\n" if vault_ctx else "")
        + (f"## Agenda Google Calendar (prochains événements)\n{calendar_ctx}\n\n" if calendar_ctx else "")
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
        max_output_tokens=4096,
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

    async def error_handler(update, context):
        from telegram.error import TimedOut, NetworkError
        if isinstance(context.error, (TimedOut, NetworkError)):
            return  # retry automatique, pas de crash
        print(f"⚠️ Erreur Telegram : {context.error}")

    # Telegram désactivé — Discord uniquement

    # Gateway Discord (bidirectionnel) en thread parallèle
    import threading, gateway_discord

    async def discord_handler(text: str, user_id: int, reply_fn):
        """Même logique que handle_message mais pour Discord."""
        _last_discord_activity[user_id] = time.time()
        s = session(user_id)
        detected = detect_client(text)
        if detected and detected != s["client"]:
            storage.set_client(user_id, detected)
            storage.reset_history(user_id)
            s["client"] = detected
        vault_ctx  = load_context(s["client"])
        active_vault = detect_vault(text)
        vault_search = search_vault(text, vault=active_vault)
        topic_ctx  = []
        for folder in detect_topics(text):
            topic_dir = VAULT / folder
            if topic_dir.exists():
                for md in sorted(topic_dir.glob("*.md"))[:2]:
                    topic_ctx.append(f"### {folder}/{md.name}\n{md.read_text()[:1500]}")
        system = (
            "Tu es l'assistant de Jarlounet, Brand Designer. "
            "Ton direct, chaleureux, concis. Réponds en français sauf demande contraire.\n"
            + (f"## Contexte client\n{vault_ctx}\n\n" if vault_ctx else "")
            + (f"## Ressources domaine\n" + "\n\n---\n\n".join(topic_ctx) + "\n\n" if topic_ctx else "")
            + (f"## Vault\n{vault_search}" if vault_search else "")
        )
        storage.add_message(user_id, "user", text)
        s["history"].append({"role": "user", "parts": [text]})
        contents = [
            types.Content(role="user",  parts=[types.Part(text=system)]),
            types.Content(role="model", parts=[types.Part(text="Compris.")]),
        ]
        for h in s["history"][-20:]:
            contents.append(types.Content(role=h["role"], parts=[types.Part(text=h["parts"][0])]))
        r = gemini.models.generate_content(
            model=GEMINI_MODEL, contents=contents,
                    config=types.GenerateContentConfig(max_output_tokens=4096)
        )
        reply = r.text
        storage.add_message(user_id, "model", reply)
        vault_name = next((k for k, v in VAULTS.items() if v == active_vault), "connaissance")
        await reply_fn(reply + f"\n\n📂 {vault_name} · {s['client']}")

    INACTIVITY_TIMEOUT = 600  # 10 min sans activite -> auto-save

    async def discord_auto_saver():
        while True:
            await asyncio.sleep(60)
            now = time.time()
            to_save = [uid for uid, ts in list(_last_discord_activity.items())
                       if now - ts >= INACTIVITY_TIMEOUT]
            for uid in to_save:
                s = session(uid)
                if len(s["history"]) >= 2:
                    extract_and_save_conversation(s["client"], s["history"])
                    storage.reset_history(uid)
                    s["history"] = []
                del _last_discord_activity[uid]

    threading.Thread(
        target=gateway_discord.run_discord_gateway,
        args=(discord_handler, discord_auto_saver),
        daemon=True
    ).start()

    print(f"🚀 Hermes Discord démarré — vault : {VAULT}")
    import threading
    threading.Event().wait()

if __name__ == "__main__":
    main()
