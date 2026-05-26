"""
Module Google Calendar pour Hermes Bot
Auth : OAuth2 — flow initial en local, token.json copié sur VPS

Setup (une seule fois) :
  1. Google Cloud Console → créer projet → activer Calendar API
  2. Credentials → OAuth 2.0 → Desktop App → télécharger → renommer credentials-gcal.json
  3. En local : python3 calendar_google.py --auth
  4. Copier token-gcal.json sur le VPS : scp -P 2222 token-gcal.json kingofthedesert@87.106.218.213:~/bot-hermes/
  5. Ajouter dans .env : GOOGLE_CALENDAR_ID=primary (ou l'ID exact du calendar)
"""

import os
import sys
import json
import zoneinfo
from pathlib import Path
from datetime import datetime, timedelta, timezone

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/calendar"]
BASE_DIR = Path(__file__).parent
CREDENTIALS_FILE = BASE_DIR / "credentials-gcal.json"
TOKEN_FILE        = BASE_DIR / "token-gcal.json"
CALENDAR_ID       = os.getenv("GOOGLE_CALENDAR_ID", "primary")
TZ                = zoneinfo.ZoneInfo("Europe/Paris")


def get_service():
    """Retourne un service Google Calendar authentifié."""
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDENTIALS_FILE.exists():
                raise FileNotFoundError(
                    f"credentials-gcal.json manquant dans {BASE_DIR}\n"
                    "Voir setup dans le header du fichier."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json())
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


# ── Lecture ───────────────────────────────────────────────────────────────────

def get_upcoming_events(n: int = 5) -> list[dict]:
    """Retourne les N prochains événements."""
    service = get_service()
    now = datetime.now(tz=timezone.utc).isoformat()
    result = service.events().list(
        calendarId=CALENDAR_ID,
        timeMin=now,
        maxResults=n,
        singleEvents=True,
        orderBy="startTime",
    ).execute()
    return result.get("items", [])


def get_events_for_day(date: datetime) -> list[dict]:
    """Retourne les événements d'une journée donnée."""
    service = get_service()
    start = datetime(date.year, date.month, date.day, 0, 0, tzinfo=TZ)
    end   = start + timedelta(days=1)
    result = service.events().list(
        calendarId=CALENDAR_ID,
        timeMin=start.isoformat(),
        timeMax=end.isoformat(),
        singleEvents=True,
        orderBy="startTime",
    ).execute()
    return result.get("items", [])


def format_event(event: dict) -> str:
    """Formate un événement pour affichage HTML Telegram."""
    title    = event.get("summary", "(Sans titre)")
    location = event.get("location", "")
    desc     = event.get("description", "")

    start_raw = event["start"].get("dateTime") or event["start"].get("date", "")
    end_raw   = event["end"].get("dateTime")   or event["end"].get("date", "")

    # Meet link
    meet_link = ""
    for ep in event.get("conferenceData", {}).get("entryPoints", []):
        if ep.get("entryPointType") == "video":
            meet_link = ep.get("uri", "")

    # Formater les datetimes
    try:
        if "T" in start_raw:
            dt_s = datetime.fromisoformat(start_raw).astimezone(TZ)
            dt_e = datetime.fromisoformat(end_raw).astimezone(TZ)
            time_str = f"{dt_s.strftime('%a %d/%m %H:%M')}–{dt_e.strftime('%H:%M')}"
        else:
            # Journée entière
            dt_s = datetime.fromisoformat(start_raw)
            time_str = dt_s.strftime("%a %d/%m (journée entière)")
    except Exception:
        time_str = start_raw

    parts = [f"📅 <b>{title}</b>", f"🕐 {time_str}"]
    if location:
        parts.append(f"📍 {location}")
    if meet_link:
        parts.append(f"🎥 <a href='{meet_link}'>Lien Meet</a>")
    return "\n".join(parts)


def format_events_list(events: list[dict]) -> str:
    """Formate une liste d'événements."""
    if not events:
        return "📅 Aucun événement à venir."
    return "\n\n".join(format_event(e) for e in events)


def events_to_context(events: list[dict]) -> str:
    """Formate les événements pour injection dans le system prompt (texte brut)."""
    if not events:
        return "Aucun événement à venir."
    lines = []
    for e in events:
        title    = e.get("summary", "(Sans titre)")
        start    = e["start"].get("dateTime") or e["start"].get("date", "")
        location = e.get("location", "")
        meet     = any(
            ep.get("entryPointType") == "video"
            for ep in e.get("conferenceData", {}).get("entryPoints", [])
        )
        try:
            if "T" in start:
                dt = datetime.fromisoformat(start).astimezone(TZ)
                time_str = dt.strftime("%a %d/%m à %H:%M")
            else:
                dt = datetime.fromisoformat(start)
                time_str = dt.strftime("%a %d/%m (journée entière)")
        except Exception:
            time_str = start
        line = f"- {time_str} : {title}"
        if location: line += f" ({location})"
        if meet:     line += " [Meet]"
        lines.append(line)
    return "\n".join(lines)


# ── Création ──────────────────────────────────────────────────────────────────

def create_event(
    title: str,
    start_dt: datetime,
    duration_min: int = 60,
    description: str = "",
    location: str = "",
    with_meet: bool = False,
) -> dict:
    """Crée un événement. Retourne l'événement créé (avec htmlLink)."""
    import uuid
    service = get_service()
    end_dt  = start_dt + timedelta(minutes=duration_min)

    body: dict = {
        "summary":     title,
        "description": description,
        "location":    location,
        "start": {"dateTime": start_dt.isoformat(), "timeZone": "Europe/Paris"},
        "end":   {"dateTime": end_dt.isoformat(),   "timeZone": "Europe/Paris"},
    }
    if with_meet:
        body["conferenceData"] = {
            "createRequest": {
                "requestId": str(uuid.uuid4()),
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        }

    kwargs: dict = {
        "calendarId": CALENDAR_ID,
        "body": body,
        "sendUpdates": "all",
    }
    if with_meet:
        kwargs["conferenceDataVersion"] = 1

    return service.events().insert(**kwargs).execute()


def format_created_event(event: dict) -> str:
    """Formate la confirmation de création d'un événement."""
    title = event.get("summary", "(Sans titre)")
    link  = event.get("htmlLink", "")
    start = event["start"].get("dateTime", "")
    meet  = ""
    for ep in event.get("conferenceData", {}).get("entryPoints", []):
        if ep.get("entryPointType") == "video":
            meet = ep.get("uri", "")
    try:
        dt = datetime.fromisoformat(start).astimezone(TZ)
        time_str = dt.strftime("%a %d/%m à %H:%M")
    except Exception:
        time_str = start
    parts = [f"✅ <b>{title}</b> créé", f"🕐 {time_str}"]
    if meet:
        parts.append(f"🎥 <a href='{meet}'>Rejoindre Meet</a>")
    if link:
        parts.append(f"<a href='{link}'>Voir dans Google Calendar</a>")
    return "\n".join(parts)


# ── Parsing date/heure (via Gemini, appelé depuis bot.py) ─────────────────────

def parse_event_args(text: str, gemini_fn) -> dict | None:
    """
    Utilise Gemini pour parser une expression naturelle en dict structuré.
    gemini_fn : callable(prompt) → str  (wrappé depuis bot.py)

    Retourne : {title, date (YYYY-MM-DD), time (HH:MM), duration_min, with_meet} ou None
    """
    import re
    now_str = datetime.now(tz=TZ).strftime("%Y-%m-%d %H:%M (%A %d %B %Y)")
    prompt = (
        f"Aujourd'hui : {now_str}, fuseau Europe/Paris.\n"
        f"Extrait les infos de cet événement et retourne UNIQUEMENT un objet JSON valide "
        f"avec ces clés exactes :\n"
        f'  "title": string,\n'
        f'  "date": "YYYY-MM-DD",\n'
        f'  "time": "HH:MM",\n'
        f'  "duration_min": int (défaut 60),\n'
        f'  "with_meet": bool\n'
        f"Si la date ou l'heure est absente, utilise une valeur raisonnable. "
        f"Si le texte ne décrit pas un événement, retourne null.\n\n"
        f"Texte : {text}"
    )
    raw = gemini_fn(prompt)
    m = re.search(r"\{.*?\}", raw, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group())
    except json.JSONDecodeError:
        return None


def build_event_datetime(parsed: dict) -> datetime | None:
    """Construit un datetime aware depuis un dict parsé."""
    try:
        date_str = parsed["date"]
        time_str = parsed.get("time", "09:00")
        dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
        return dt.replace(tzinfo=TZ)
    except (KeyError, ValueError):
        return None


# ── Entrypoint auth (python3 calendar_google.py --auth) ──────────────────────

if __name__ == "__main__":
    if "--auth" in sys.argv:
        print("Lancement du flow OAuth2...")
        get_service()
        print(f"✅ token-gcal.json créé dans {BASE_DIR}")
        print(f"Copier sur VPS : scp -P 2222 {TOKEN_FILE} kingofthedesert@87.106.218.213:~/bot-hermes/")
    else:
        events = get_upcoming_events(5)
        print(format_events_list(events))
