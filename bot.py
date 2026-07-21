#!/usr/bin/env python3
"""
Luisterende Truus (live-modus) — je kunt met haar appen via Telegram.

Draait als een doorlopende sessie op GitHub Actions. Ze "hangt aan de lijn"
met Telegram long-polling en reageert binnen enkele seconden op je berichten.
Elke sessie loopt maximaal ~5u40m; daarna start ze zichzelf opnieuw op, zodat
Truus in de praktijk 24/7 bereikbaar is.

Wat ze kan:
  - je vragen beantwoorden / dingen uitzoeken;
  - je recente mail doorzoeken en samenvatten;
  - een concept-antwoord of nieuwe mail klaarzetten in het juiste account
    (zakelijk -> info@kodesaign.com). Ze verstuurt niks zelf; jij tikt op verzenden.

Herbruikt de bouwstenen uit digest.py. Geen wachtwoorden in dit bestand.
"""

from __future__ import annotations

import json
import os
import smtplib
import sqlite3
import ssl
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from zoneinfo import ZoneInfo

import anthropic
import requests

from digest import (
    Account,
    Mail,
    fetch_recent,
    load_accounts,
    save_draft,
    send_telegram,
)


def send_email(account: Account, to_addr: str, subject: str, body: str) -> None:
    """Verstuur een e-mail via SMTP vanuit het opgegeven account."""
    msg = EmailMessage()
    msg["From"] = account.user
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)
    ctx = ssl.create_default_context()
    # Timeout voorkomt dat de bot bevriest als de mailpoort geblokkeerd is.
    with smtplib.SMTP_SSL(account.smtp_host, account.smtp_port,
                          context=ctx, timeout=20) as s:
        s.login(account.user, account.password)
        s.send_message(msg)


# ---------------------------------------------------------------------------
# Google (Gmail API + Agenda) — werkt via het web, dus geen last van SMTP-blokkade
# ---------------------------------------------------------------------------

GOOGLE_TOKEN_FILE = os.getenv(
    "GOOGLE_TOKEN_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "google_token.json"))
GOOGLE_SCOPES = ["https://www.googleapis.com/auth/gmail.send",
                 "https://www.googleapis.com/auth/calendar"]
TIMEZONE = os.getenv("TRUUS_TZ", "Europe/Amsterdam")
LOCAL_TZ = ZoneInfo(TIMEZONE)  # altijd Amsterdamse tijd, niet de server-tijd (UTC)


def google_enabled() -> bool:
    return os.path.exists(GOOGLE_TOKEN_FILE)


# Verbindingen worden één keer opgebouwd en hergebruikt (scheelt veel tijd).
_google_cache: dict = {}


def _google_creds():
    if "creds" not in _google_cache:
        from google.oauth2.credentials import Credentials
        with open(GOOGLE_TOKEN_FILE) as f:
            d = json.load(f)
        _google_cache["creds"] = Credentials(
            token=None,
            refresh_token=d["refresh_token"],
            client_id=d["client_id"],
            client_secret=d["client_secret"],
            token_uri="https://oauth2.googleapis.com/token",
            scopes=GOOGLE_SCOPES,
        )
    return _google_cache["creds"]


def _service(name: str, version: str):
    key = f"svc:{name}"
    if key not in _google_cache:
        from googleapiclient.discovery import build
        _google_cache[key] = build(name, version, credentials=_google_creds(),
                                   cache_discovery=False)
    return _google_cache[key]


def warm_google() -> None:
    """Bouw de verbindingen alvast op bij de start, zodat de eerste actie snel is."""
    if google_enabled():
        try:
            _service("gmail", "v1")
            _service("calendar", "v3")
        except Exception as e:
            print(f"Google opwarmen faalde: {e}", file=sys.stderr)


def gmail_send(to_addr: str, subject: str, body: str, sender: str | None = None) -> None:
    """Verstuur een mail via de Gmail API (vanuit kokensen80, evt. met alias-afzender)."""
    import base64
    msg = EmailMessage()
    msg["To"] = to_addr
    msg["Subject"] = subject
    if sender:
        msg["From"] = sender
    msg.set_content(body)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    _service("gmail", "v1").users().messages().send(
        userId="me", body={"raw": raw}).execute()


def calendar_add(summary: str, start_iso: str, end_iso: str,
                 description: str = "", location: str = "") -> str:
    """Zet een afspraak in de agenda. Geeft de link naar het event terug."""
    event = {
        "summary": summary,
        "description": description,
        "location": location,
        "start": {"dateTime": start_iso, "timeZone": TIMEZONE},
        "end": {"dateTime": end_iso, "timeZone": TIMEZONE},
    }
    ev = _service("calendar", "v3").events().insert(
        calendarId="primary", body=event).execute()
    return ev.get("htmlLink", "")


def calendar_delete(event_id: str) -> None:
    """Verwijder een afspraak uit de agenda op basis van het event-id."""
    _service("calendar", "v3").events().delete(
        calendarId="primary", eventId=event_id).execute()


def calendar_add_meeting(summary: str, start_iso: str, end_iso: str,
                         attendees: list[str], description: str = "") -> str:
    """Plan een Google Meet-meeting: event met Meet-link + mail-uitnodigingen.

    Geeft de Google Meet-link terug.
    """
    event = {
        "summary": summary,
        "description": description,
        "start": {"dateTime": start_iso, "timeZone": TIMEZONE},
        "end": {"dateTime": end_iso, "timeZone": TIMEZONE},
        "attendees": [{"email": a} for a in attendees],
        "conferenceData": {
            "createRequest": {
                "requestId": str(uuid.uuid4()),
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        },
    }
    ev = _service("calendar", "v3").events().insert(
        calendarId="primary", body=event,
        conferenceDataVersion=1, sendUpdates="all").execute()
    return ev.get("hangoutLink", "")


def calendar_events(time_min_iso: str, time_max_iso: str) -> list[dict]:
    """Haal afspraken op tussen twee tijdstippen (voor context + reminders)."""
    r = _service("calendar", "v3").events().list(
        calendarId="primary", timeMin=time_min_iso, timeMax=time_max_iso,
        singleEvents=True, orderBy="startTime", maxResults=50).execute()
    return r.get("items", [])


def _list_calendars() -> list[str]:
    try:
        r = _service("calendar", "v3").calendarList().list().execute()
        return [c["id"] for c in r.get("items", [])]
    except Exception:
        return ["primary"]


def calendar_all_events(time_min_iso: str, time_max_iso: str) -> list[dict]:
    """Afspraken uit ALLE agenda's (incl. verjaardagen-agenda)."""
    out = []
    for cid in _list_calendars():
        try:
            r = _service("calendar", "v3").events().list(
                calendarId=cid, timeMin=time_min_iso, timeMax=time_max_iso,
                singleEvents=True, orderBy="startTime", maxResults=50).execute()
            for e in r.get("items", []):
                e["_cal"] = cid
                out.append(e)
        except Exception:
            continue
    return out


def _is_birthday(e: dict) -> bool:
    if e.get("eventType") == "birthday":
        return True
    if not e.get("start", {}).get("date"):  # alleen hele-dag-events
        return False
    s = (e.get("summary") or "").lower()
    cal = (e.get("_cal") or "").lower()
    if any(k in s for k in ("verjaardag", "jarig", "birthday", "🎂")):
        return True
    return "birthday" in cal or "contacts" in cal


def _event_start(e: dict):
    """Geef de starttijd als tz-aware datetime, of None bij een hele-dag-event."""
    dt = e.get("start", {}).get("dateTime")
    if not dt:
        return None
    try:
        return datetime.fromisoformat(dt)
    except Exception:
        return None


def _fmt_event(e: dict) -> str:
    summary = e.get("summary", "(geen titel)")
    start = _event_start(e)
    if start:
        return f"{start.astimezone(LOCAL_TZ).strftime('%a %d-%m %H:%M')} — {summary}"
    day = e.get("start", {}).get("date", "?")
    return f"{day} (hele dag) — {summary}"


def build_agenda_context() -> str:
    """Overzicht van de komende ~14 dagen, zodat Truus 'ben ik druk?' kan beantwoorden."""
    if not google_enabled():
        return "(agenda niet gekoppeld)"
    now = datetime.now(timezone.utc)
    later = now + timedelta(days=14)
    try:
        events = calendar_events(now.isoformat(), later.isoformat())
    except Exception as e:
        return f"(agenda ophalen faalde: {e})"
    if not events:
        return "Geen afspraken in de komende 14 dagen."
    # De [id:...] gebruik je om een afspraak te verwijderen; noem het id niet tegen Ko.
    return "Komende afspraken:\n" + "\n".join(
        f"- [id:{e.get('id')}] {_fmt_event(e)}" for e in events)


def check_reminders() -> None:
    """Stuur de dagelijkse agenda-samenvatting en herinneringen ~1 uur vooraf."""
    if not google_enabled():
        return
    now = datetime.now(timezone.utc)
    local = now.astimezone(LOCAL_TZ)
    events = calendar_events(now.isoformat(), (now + timedelta(days=2)).isoformat())

    # Dagelijkse samenvatting: één keer per dag, na 08:00 lokale tijd.
    daily_key = f"daily:{local.date().isoformat()}"
    if 7 <= local.hour <= 10 and not reminder_already_sent(daily_key):
        todays = [e for e in events
                  if (_event_start(e) or now).astimezone(LOCAL_TZ).date() == local.date()
                  and _event_start(e)]
        if todays:
            msg = "🗓️ *Goedemorgen Ko!* Je afspraken voor vandaag:\n" + \
                  "\n".join("• " + _fmt_event(e) for e in todays)
        else:
            msg = "🗓️ Goedemorgen Ko! Je hebt vandaag geen afspraken in je agenda."
        send_telegram(msg)
        mark_reminder_sent(daily_key)

    # Herinnering ~1 uur van tevoren.
    for e in events:
        start = _event_start(e)
        if not start:
            continue
        mins = (start - now).total_seconds() / 60
        key = f"1h:{e.get('id')}"
        if 0 < mins <= 60 and not reminder_already_sent(key):
            send_telegram(f"⏰ Over ~{int(round(mins))} min: *{e.get('summary','afspraak')}* "
                          f"om {start.astimezone(LOCAL_TZ).strftime('%H:%M')}.")
            mark_reminder_sent(key)

    # Verjaardagen: één week van tevoren (scan één keer per ochtend).
    scan_key = f"bdayscan:{local.date().isoformat()}"
    if 7 <= local.hour <= 10 and not reminder_already_sent(scan_key):
        target = local.date() + timedelta(days=7)
        day_start = datetime(target.year, target.month, target.day,
                             tzinfo=local.tzinfo)
        try:
            bdays = calendar_all_events(
                day_start.astimezone(timezone.utc).isoformat(),
                (day_start + timedelta(days=1)).astimezone(timezone.utc).isoformat())
        except Exception as e:
            bdays = []
            print(f"Verjaardag-scan faalde: {e}", file=sys.stderr)
        for e in bdays:
            if _is_birthday(e):
                bkey = f"bday:{e.get('id')}:{target.isoformat()}"
                if not reminder_already_sent(bkey):
                    send_telegram(
                        f"🎂 Over een week ({target.strftime('%d-%m')}) is "
                        f"*{e.get('summary', 'een verjaardag')}*. Denk je aan een "
                        f"kaartje of cadeautje?")
                    mark_reminder_sent(bkey)
        mark_reminder_sent(scan_key)

CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5")
CONTEXT_LOOKBACK_HOURS = int(os.getenv("BOT_CONTEXT_HOURS", "120"))  # 5 dagen
DB_PATH = os.getenv("TRUUS_DB",
                    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "truus_memory.db"))
HISTORY_LOAD = 40   # hoeveel eerdere berichten Truus als geheugen meeneemt
ON_VPS = not os.getenv("GITHUB_REPOSITORY")  # op de VPS draait ze onbeperkt door
CONTEXT_MAX_AGE_S = 180        # mailcontext maximaal 3 min hergebruiken
MAX_CONTEXT_MAILS = 18         # kleinere context = sneller antwoord
LONGPOLL_TIMEOUT = 50          # seconden dat Telegram de verbinding openhoudt
SESSION_BUDGET_S = int(os.getenv("BOT_BUDGET_SECONDS") or "20400")  # ~5u40m
STARTUP_SKIP_OLDER_S = 900     # bij (her)start: berichten ouder dan 15 min overslaan


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

DEBUG = os.getenv("BOT_DEBUG", "").strip() not in ("", "0", "false", "False")


def _api(method: str) -> str:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    return f"https://api.telegram.org/bot{token}/{method}"


def delete_webhook() -> None:
    """Zorg dat getUpdates kan werken (een actieve webhook zou dat blokkeren)."""
    try:
        requests.get(_api("deleteWebhook"),
                     params={"drop_pending_updates": "false"}, timeout=15)
    except Exception as e:
        print(f"deleteWebhook faalde (niet fataal): {e}", file=sys.stderr)


def send_typing() -> None:
    """Toon 'Truus is aan het typen…' in Telegram (verdwijnt na ~5s vanzelf)."""
    try:
        requests.post(_api("sendChatAction"),
                      data={"chat_id": os.environ["TELEGRAM_CHAT_ID"],
                            "action": "typing"}, timeout=10)
    except Exception as e:
        print(f"sendChatAction faalde: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Permanent geheugen (SQLite) — Truus onthoudt gesprekken over herstarts heen
# ---------------------------------------------------------------------------

def init_db() -> None:
    con = sqlite3.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        role TEXT NOT NULL,
        content TEXT NOT NULL)""")
    con.execute("""CREATE TABLE IF NOT EXISTS sent_reminders (
        key TEXT PRIMARY KEY,
        ts TEXT NOT NULL)""")
    con.commit()
    con.close()


def reminder_already_sent(key: str) -> bool:
    try:
        con = sqlite3.connect(DB_PATH)
        row = con.execute("SELECT 1 FROM sent_reminders WHERE key = ?", (key,)).fetchone()
        con.close()
        return row is not None
    except Exception:
        return False


def mark_reminder_sent(key: str) -> None:
    try:
        con = sqlite3.connect(DB_PATH)
        con.execute("INSERT OR IGNORE INTO sent_reminders (key, ts) VALUES (?, ?)",
                    (key, datetime.now(timezone.utc).isoformat()))
        con.commit()
        con.close()
    except Exception as e:
        print(f"Reminder-markering faalde: {e}", file=sys.stderr)


def load_history(limit: int = HISTORY_LOAD) -> list[dict]:
    """Laad de laatste berichten als gespreksgeheugen (oud -> nieuw)."""
    try:
        con = sqlite3.connect(DB_PATH)
        rows = con.execute(
            "SELECT role, content FROM messages ORDER BY id DESC LIMIT ?",
            (limit,)).fetchall()
        con.close()
    except Exception as e:
        print(f"Geheugen laden faalde: {e}", file=sys.stderr)
        return []
    return [{"role": r, "content": c} for r, c in reversed(rows)]


def save_turn(role: str, content: str) -> None:
    try:
        con = sqlite3.connect(DB_PATH)
        con.execute("INSERT INTO messages (ts, role, content) VALUES (?, ?, ?)",
                    (datetime.now(timezone.utc).isoformat(), role, content))
        con.commit()
        con.close()
    except Exception as e:
        print(f"Geheugen opslaan faalde: {e}", file=sys.stderr)


def poll(offset: int | None, skip_before_ts: float | None) -> tuple[list[str], int | None]:
    """Long-poll Telegram. Geeft (nieuwe berichten van Ko, nieuwe offset) terug."""
    chat_id = str(os.environ["TELEGRAM_CHAT_ID"]).strip()
    params = {"timeout": LONGPOLL_TIMEOUT}
    if offset is not None:
        params["offset"] = offset
    r = requests.get(_api("getUpdates"), params=params, timeout=LONGPOLL_TIMEOUT + 15)
    r.raise_for_status()
    updates = r.json().get("result", [])
    if DEBUG and updates:
        ids = [(u.get("message") or u.get("edited_message") or {}).get("chat", {}).get("id")
               for u in updates]
        print(f"[debug] {len(updates)} update(s); chat-ids={ids}; "
              f"verwacht chat-id={chat_id}", file=sys.stderr)

    texts: list[str] = []
    new_offset = offset
    for u in updates:
        new_offset = u["update_id"] + 1
        msg = u.get("message") or u.get("edited_message")
        if not msg:
            continue
        if str(msg.get("chat", {}).get("id")).strip() != chat_id:
            if DEBUG:
                print(f"[debug] bericht overgeslagen: chat-id "
                      f"{msg.get('chat', {}).get('id')} != {chat_id}", file=sys.stderr)
            continue
        # Bij (her)start oude berichten overslaan zodat Truus niet op verouderde
        # appjes reageert.
        if skip_before_ts is not None and msg.get("date", 0) < skip_before_ts:
            continue
        text = msg.get("text")
        if text:
            texts.append(text.strip())
    return texts, new_offset


# ---------------------------------------------------------------------------
# Mailcontext
# ---------------------------------------------------------------------------

def gather_mail_context(accounts) -> list[Mail]:
    since = datetime.now(timezone.utc) - timedelta(hours=CONTEXT_LOOKBACK_HOURS)
    mails: list[Mail] = []
    for acc in accounts:
        try:
            mails.extend(fetch_recent(acc, since))
        except Exception as e:
            print(f"Kon mail niet ophalen bij {acc.name}: {e}", file=sys.stderr)
    mails.sort(key=lambda m: m.date or datetime.min.replace(tzinfo=timezone.utc),
               reverse=True)
    return mails[:MAX_CONTEXT_MAILS]


def _format_context(mails: list[Mail]) -> str:
    if not mails:
        return "(geen recente mail beschikbaar)"
    lines = []
    for i, m in enumerate(mails, 1):
        when = m.date.strftime("%d-%m %H:%M") if m.date else "?"
        lines.append(
            f"[{i}] Account: {m.account} | Van: {m.sender} | {when}\n"
            f"    Onderwerp: {m.subject}\n    Tekst: {m.body[:300]}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Claude: begrijp de opdracht
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """Je bent Able, de persoonlijke assistent van Ko. (Je heette eerder \
Truus; als Ko je Truus noemt, ben jij dat gewoon.) Ko appt je via \
Telegram met vragen en opdrachten. Je bent warm, betrokken en to-the-point, in het \
Nederlands. Je krijgt Ko's recente mail als context, zodat je kunt verwijzen naar \
concrete berichten ("de mail van Jan"). Reageer natuurlijk, alsof je samen aan het \
appen bent.

Antwoord altijd gewoon in natuurlijke taal — je hoeft geen JSON of speciale opmaak te \
gebruiken. Praat als een mens.

Je kunt:
1. Vragen beantwoorden of iets uitzoeken/samenvatten uit de mailcontext.
2. Mails opstellen en versturen. Werkwijze:
   a. Als Ko een mail wil (laten) opstellen of beantwoorden, schrijf je de volledige \
mail en toon je die IN JE ANTWOORD (afzender, aan, onderwerp, tekst), en vraag je: \
"Zal ik 'm zo versturen?".
   b. Pas NADAT Ko duidelijk bevestigt ("ja", "verstuur", "stuur maar", "akkoord") \
roep je de tool 'mail_versturen' aan om de mail ECHT te versturen. Verstuur nooit \
zonder die bevestiging, en nooit op eigen initiatief.
   c. Wil Ko de mail liever alleen als concept (niet versturen)? Gebruik dan \
'concept_klaarzetten'.
3. Ko's Google Agenda beheren:
   - Gewone afspraak toevoegen met 'agenda_afspraak_toevoegen'.
   - Online meeting / videocall met anderen plannen met 'meeting_inplannen': dit maakt \
een Google Meet-link én mailt de deelnemers een uitnodiging. Je hebt hun e-mailadressen \
nodig — vraag ernaar als je ze niet hebt (kijk eerst in de mailcontext).
   - Afspraak verwijderen met 'agenda_afspraak_verwijderen' (gebruik het [id:...] uit het \
agenda-overzicht hieronder). Als Ko zegt "haal de afspraak van morgen eruit", zoek de \
betreffende afspraak in het overzicht en verwijder die.
   - Vragen als "ben ik dinsdag druk?" beantwoord je aan de hand van het agenda-overzicht.
   Bevestig kort wat je deed. Gebruik de datum/tijd hieronder om "morgen", "volgende week" \
enz. correct om te rekenen. Standaardduur van een afspraak is 1 uur tenzij Ko iets \
anders zegt.

Je onthoudt het lopende gesprek (je krijgt de eerdere berichten mee) plus een lijst van \
concepten die je deze sessie al hebt klaargezet. Als Ko vraagt "laat dat concept zien" \
of "wat had je geschreven", toon dan gewoon de tekst van het betreffende concept uit die \
lijst. Maak niet onnodig een nieuw concept aan als er al één is dat Ko bedoelt.

ROUTERING van concepten: is het zakelijk (klant, opdracht, offerte, leverancier, \
factuur, Kodesaign)? Zet 'account' dan op "Kodesaign" (verstuurt vanuit \
info@kodesaign.com). Privé? Gebruik "Gmail privé" of "Gmail studio", afhankelijk van \
waar de oorspronkelijke mail binnenkwam."""


DRAFT_TOOL = {
    "name": "concept_klaarzetten",
    "description": "Zet een concept-mail klaar in de Concepten-map van de juiste "
                   "postbus. Gebruik dit alleen als Ko een mail wil (laten) opstellen "
                   "of beantwoorden. De mail wordt NIET verstuurd, alleen als concept "
                   "klaargezet zodat Ko 'm zelf kan nakijken en versturen.",
    "input_schema": {
        "type": "object",
        "properties": {
            "account": {"type": "string",
                        "enum": ["Kodesaign", "Gmail privé", "Gmail studio"],
                        "description": "Zakelijk -> Kodesaign; privé -> het Gmail-account."},
            "to": {"type": "string", "description": "E-mailadres van de ontvanger."},
            "subject": {"type": "string"},
            "body": {"type": "string", "description": "De volledige concept-tekst."},
        },
        "required": ["account", "to", "subject", "body"],
    },
}

CALENDAR_TOOL = {
    "name": "agenda_afspraak_toevoegen",
    "description": "Zet een afspraak in Ko's Google Agenda. Gebruik ISO-8601 tijden "
                   "met tijdzone-offset, bijv. 2026-07-22T15:00:00+02:00.",
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "Titel van de afspraak."},
            "start": {"type": "string", "description": "Starttijd, ISO-8601."},
            "end": {"type": "string", "description": "Eindtijd, ISO-8601."},
            "description": {"type": "string"},
            "location": {"type": "string"},
        },
        "required": ["summary", "start", "end"],
    },
}

MEETING_TOOL = {
    "name": "meeting_inplannen",
    "description": "Plan een Google Meet-videomeeting: maakt een afspraak in Ko's "
                   "agenda MET een Google Meet-link én mailt de deelnemers automatisch "
                   "een uitnodiging. Gebruik dit als Ko een (online) meeting, videocall "
                   "of gesprek wil plannen met een of meer andere mensen. Vraag naar de "
                   "e-mailadressen van de deelnemers als je die niet hebt.",
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "Titel van de meeting."},
            "start": {"type": "string", "description": "Starttijd, ISO-8601 met offset."},
            "end": {"type": "string", "description": "Eindtijd, ISO-8601 met offset."},
            "attendees": {"type": "array", "items": {"type": "string"},
                          "description": "E-mailadressen van de deelnemers."},
            "description": {"type": "string"},
        },
        "required": ["summary", "start", "end", "attendees"],
    },
}

DELETE_EVENT_TOOL = {
    "name": "agenda_afspraak_verwijderen",
    "description": "Verwijder een afspraak uit de agenda. Gebruik het event_id dat "
                   "tussen [id:...] in het agenda-overzicht staat. Verwijder alleen de "
                   "afspraak die Ko duidelijk bedoelt; twijfel je welke, vraag het na.",
    "input_schema": {
        "type": "object",
        "properties": {
            "event_id": {"type": "string", "description": "Het id uit [id:...]."},
        },
        "required": ["event_id"],
    },
}

SEND_TOOL = {
    "name": "mail_versturen",
    "description": "Verstuur DIRECT een e-mail vanuit de juiste postbus. Gebruik dit "
                   "ALLEEN nadat Ko expliciet heeft bevestigd dat deze specifieke mail "
                   "verstuurd mag worden. Twijfel je? Verstuur niet, maar vraag eerst "
                   "om bevestiging.",
    "input_schema": {
        "type": "object",
        "properties": {
            "account": {"type": "string",
                        "enum": ["Kodesaign", "Gmail privé", "Gmail studio"],
                        "description": "Zakelijk -> Kodesaign; privé -> het Gmail-account."},
            "to": {"type": "string", "description": "E-mailadres van de ontvanger."},
            "subject": {"type": "string"},
            "body": {"type": "string", "description": "De volledige mailtekst."},
        },
        "required": ["account", "to", "subject", "body"],
    },
}


def _build_system(context: str, agenda: str, session_drafts: list[dict]) -> str:
    now = datetime.now(LOCAL_TZ)
    s = (SYSTEM_PROMPT
         + f"\n\nHuidige datum/tijd: {now.strftime('%A %d-%m-%Y %H:%M')} "
         f"({TIMEZONE}, offset {now.strftime('%z')}). Reken afspraaktijden in DEZE "
         f"tijdzone en gebruik deze offset in de ISO-tijden (bijv. ...T15:00:00{now.strftime('%z')[:3]}:00)."
         + "\n\n--- Je agenda (gebruik dit om 'ben ik druk?' te beantwoorden) ---\n"
         + agenda
         + "\n\n--- Recente mail als context ---\n" + context)
    if session_drafts:
        s += "\n\n--- Concepten die je deze sessie al hebt klaargezet ---\n"
        for i, d in enumerate(session_drafts, 1):
            s += (f"{i}. account={d.get('account')} aan={d.get('to')} "
                  f"onderwerp={d.get('subject')}\n   tekst: {d.get('body')}\n")
    return s


def handle(client: anthropic.Anthropic, history: list[dict], message: str,
           context: str, agenda: str, session_drafts: list[dict]) -> dict:
    messages = history + [{"role": "user", "content": message}]
    resp = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1500,
        system=_build_system(context, agenda, session_drafts),
        tools=[DRAFT_TOOL, SEND_TOOL, CALENDAR_TOOL, MEETING_TOOL, DELETE_EVENT_TOOL],
        messages=messages,
    )
    reply = "".join(b.text for b in resp.content if b.type == "text").strip()

    def tool_inputs(name):
        return [b.input for b in resp.content
                if b.type == "tool_use" and b.name == name]

    drafts = tool_inputs("concept_klaarzetten")
    sends = tool_inputs("mail_versturen")
    events = tool_inputs("agenda_afspraak_toevoegen")
    meetings = tool_inputs("meeting_inplannen")
    deletes = tool_inputs("agenda_afspraak_verwijderen")
    if not reply:
        reply = ("Ik heb een concept voor je klaargezet." if drafts
                 else "Verstuurd, Ko." if sends
                 else "Meeting ingepland, Ko." if meetings
                 else "In je agenda gezet, Ko." if events
                 else "Uit je agenda gehaald, Ko." if deletes else "Genoteerd, Ko.")
    return {"reply": reply, "drafts": drafts, "sends": sends,
            "events": events, "meetings": meetings, "deletes": deletes}


def process(client, accounts_by_name, message: str, context: str, agenda: str,
            history: list[dict], session_drafts: list[dict]) -> None:
    # Blijf "aan het typen…" tonen zolang Truus bezig is.
    stop = threading.Event()

    def keep_typing():
        while not stop.is_set():
            send_typing()
            stop.wait(4)

    t = threading.Thread(target=keep_typing, daemon=True)
    t.start()
    try:
        result = handle(client, history, message, context, agenda, session_drafts)
    finally:
        stop.set()

    reply = (result.get("reply") or "").strip() or "Genoteerd, Ko."

    # Echt versturen (alleen als Truus de mail_versturen-tool aanriep na bevestiging).
    for s in result.get("sends", []):
        acc = accounts_by_name.get(s.get("account"))
        to = s.get("to")
        if not to:
            reply += "\n\n⚠️ Kon niet versturen: geen ontvanger."
            continue
        sender = acc.user if acc else None
        try:
            if google_enabled():
                gmail_send(to, s.get("subject", ""), s.get("body", ""), sender=sender)
            elif acc:
                send_email(acc, to, s.get("subject", ""), s.get("body", ""))
            else:
                raise RuntimeError("geen verzendmethode beschikbaar")
            reply += f"\n\n✅ Verstuurd naar {to}" + (f" vanuit {sender}." if sender else ".")
        except Exception as e:
            print(f"Versturen mislukt ({s.get('account')}): {e}", file=sys.stderr)
            reply += f"\n\n⚠️ Het versturen lukte niet ({e})."

    # Afspraken in de agenda zetten.
    for ev in result.get("events", []):
        try:
            calendar_add(ev.get("summary", "Afspraak"), ev["start"], ev["end"],
                         ev.get("description", ""), ev.get("location", ""))
            reply += f"\n\n📅 In je agenda gezet: {ev.get('summary', 'afspraak')}."
        except Exception as e:
            print(f"Agenda toevoegen mislukt: {e}", file=sys.stderr)
            reply += f"\n\n⚠️ Het inplannen lukte niet ({e})."

    # Google Meet-meetings inplannen (met uitnodigingen).
    for m in result.get("meetings", []):
        try:
            link = calendar_add_meeting(
                m.get("summary", "Meeting"), m["start"], m["end"],
                m.get("attendees", []), m.get("description", ""))
            wie = ", ".join(m.get("attendees", [])) or "de deelnemers"
            reply += (f"\n\n🎥 Meeting ingepland en uitnodiging gestuurd naar {wie}."
                      + (f"\nMeet-link: {link}" if link else ""))
        except Exception as e:
            print(f"Meeting inplannen mislukt: {e}", file=sys.stderr)
            reply += f"\n\n⚠️ Het inplannen van de meeting lukte niet ({e})."

    # Afspraken verwijderen.
    for d in result.get("deletes", []):
        try:
            calendar_delete(d["event_id"])
            reply += "\n\n🗑️ Uit je agenda gehaald."
        except Exception as e:
            print(f"Agenda verwijderen mislukt: {e}", file=sys.stderr)
            reply += f"\n\n⚠️ Het verwijderen lukte niet ({e})."

    saved = []
    for d in result.get("drafts", []):
        acc = accounts_by_name.get(d.get("account"))
        if not acc or not d.get("to"):
            continue
        try:
            save_draft(acc, d["to"], d.get("subject", "Re:"), d.get("body", ""))
            saved.append(acc.user)
            session_drafts.append(d)  # onthoud voor later ("laat zien")
        except Exception as e:
            print(f"Concept opslaan mislukt ({d.get('account')}): {e}", file=sys.stderr)
            reply += f"\n\n⚠️ Het concept opslaan lukte niet ({d.get('account')})."
    if saved:
        reply += f"\n\n✍️ Concept klaargezet in: {', '.join(dict.fromkeys(saved))}."

    send_telegram(reply)

    # Gespreksgeheugen bijwerken: in het werkgeheugen én permanent in de database.
    history.append({"role": "user", "content": message})
    history.append({"role": "assistant", "content": reply})
    del history[:-HISTORY_LOAD]
    save_turn("user", message)
    save_turn("assistant", reply)


# ---------------------------------------------------------------------------
# Zichzelf opnieuw starten aan het eind van de sessie (continuïteit)
# ---------------------------------------------------------------------------

def restart_self() -> None:
    repo = os.getenv("GITHUB_REPOSITORY")
    if not repo or not os.getenv("GH_TOKEN"):
        return  # niet in Actions; niks te doen
    try:
        subprocess.run(
            ["gh", "workflow", "run", "truus-bot.yml", "--repo", repo],
            check=False, capture_output=True, timeout=30,
        )
        print("Opvolg-sessie gestart.", file=sys.stderr)
    except Exception as e:
        print(f"Kon opvolg-sessie niet starten: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Main-loop
# ---------------------------------------------------------------------------

def main() -> int:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    accounts = load_accounts()
    accounts_by_name = {a.name: a for a in accounts}

    delete_webhook()  # eventuele oude webhook weg, anders werkt getUpdates niet

    warm_google()  # verbindingen alvast opbouwen zodat de eerste actie snel is

    # Mail- en agenda-context op de achtergrond bijhouden, zodat een antwoord nooit
    # op IMAP of de agenda hoeft te wachten.
    ctx = {"mail": "(mail wordt geladen…)", "agenda": "(agenda wordt geladen…)"}
    ctx_lock = threading.Lock()

    def refresh_context_loop():
        while True:
            try:
                mail = _format_context(gather_mail_context(accounts))
                with ctx_lock:
                    ctx["mail"] = mail
            except Exception as e:
                print(f"Mailcontext faalde: {e}", file=sys.stderr)
            try:
                agenda = build_agenda_context()
                with ctx_lock:
                    ctx["agenda"] = agenda
            except Exception as e:
                print(f"Agendacontext faalde: {e}", file=sys.stderr)
            time.sleep(CONTEXT_MAX_AGE_S)

    threading.Thread(target=refresh_context_loop, daemon=True).start()

    # Reminder-thread: dagelijkse samenvatting + herinnering ~1 uur vooraf.
    def reminder_loop():
        while True:
            try:
                check_reminders()
            except Exception as e:
                print(f"Reminder-check faalde: {e}", file=sys.stderr)
            time.sleep(120)

    if google_enabled():
        threading.Thread(target=reminder_loop, daemon=True).start()

    init_db()
    start = time.time()
    offset: int | None = None
    skip_before = time.time() - STARTUP_SKIP_OLDER_S  # bij start oude appjes negeren
    history: list[dict] = load_history()   # permanent geheugen uit de database
    session_drafts: list[dict] = []        # concepten die deze sessie zijn klaargezet

    print(f"Truus luistert live... (vps={ON_VPS}, debug={DEBUG}, "
          f"geheugen={len(history)} berichten)", file=sys.stderr)
    while ON_VPS or time.time() - start < SESSION_BUDGET_S:
        try:
            texts, offset = poll(offset, skip_before)
        except Exception as e:
            print(f"Poll-fout: {e}", file=sys.stderr)
            time.sleep(5)
            continue
        skip_before = None  # alleen de eerste ronde oude berichten overslaan

        if DEBUG and texts:
            print(f"[debug] {len(texts)} bericht(en) te verwerken", file=sys.stderr)
        for text in texts:
            with ctx_lock:
                mail_ctx = ctx["mail"]
                agenda_ctx = ctx["agenda"]
            try:
                process(client, accounts_by_name, text, mail_ctx, agenda_ctx,
                        history, session_drafts)
            except Exception as e:
                print(f"Verwerken mislukt: {e}", file=sys.stderr)
                try:
                    send_telegram("Sorry Ko, daar ging iets mis aan mijn kant. "
                                  "Probeer het zo nog eens?")
                except Exception:
                    pass

    if not DEBUG:
        restart_self()
    else:
        print("[debug] sessie klaar; geen herstart in debug-modus.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
