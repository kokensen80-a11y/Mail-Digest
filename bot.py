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

import contextvars
import json
import os
import re
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
                 "https://www.googleapis.com/auth/calendar",
                 "https://www.googleapis.com/auth/contacts.readonly",
                 "https://www.googleapis.com/auth/contacts.other.readonly"]
TIMEZONE = os.getenv("TRUUS_TZ", "Europe/Amsterdam")
LOCAL_TZ = ZoneInfo(TIMEZONE)  # altijd Amsterdamse tijd, niet de server-tijd (UTC)


def google_enabled(uid: int | None = None) -> bool:
    return bool(get_user_google_token(cur_uid() if uid is None else uid))


# Verbindingen worden één keer opgebouwd en hergebruikt (scheelt veel tijd).
# Per gebruiker een eigen cache (multi-user): _google_cache[uid] = {...}
_google_cache: dict = {}

# "Huidige gebruiker" voor de webverzoeken. Telegram/digest laten dit op 1 (Ko).
_CUR_UID: contextvars.ContextVar = contextvars.ContextVar("uid", default=1)


def cur_uid() -> int:
    return _CUR_UID.get()


def set_uid(uid: int) -> None:
    _CUR_UID.set(int(uid))


# --- Gebruikers (multi-user) ------------------------------------------------

def get_user(uid: int) -> dict | None:
    try:
        con = sqlite3.connect(DB_PATH)
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        con.close()
        return dict(row) if row else None
    except Exception:
        return None


def get_user_by_username(username: str) -> dict | None:
    try:
        con = sqlite3.connect(DB_PATH)
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT * FROM users WHERE username=?",
                          (username.strip().lower(),)).fetchone()
        con.close()
        return dict(row) if row else None
    except Exception:
        return None


def list_users() -> list[dict]:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT id, username, name, is_admin, "
                       "(google_token IS NOT NULL) AS has_google FROM users "
                       "ORDER BY id").fetchall()
    con.close()
    return [dict(r) for r in rows]


def create_user(username: str, name: str, pw_hash: str, is_admin: int = 0) -> int:
    con = sqlite3.connect(DB_PATH)
    cur = con.execute("INSERT INTO users (username, name, pw_hash, is_admin, created_ts) "
                      "VALUES (?,?,?,?,?)",
                      (username.strip().lower(), name.strip(), pw_hash, is_admin,
                       datetime.now(LOCAL_TZ).isoformat()))
    uid = cur.lastrowid
    con.commit()
    con.close()
    return uid


def delete_user(uid: int) -> None:
    """Verwijder een gebruiker én al zijn data (geheugen, taken, follow-ups,
    instellingen, spraakverbruik). Gebruiker 1 (Ko) kan niet worden verwijderd."""
    if int(uid) == 1:
        return
    con = sqlite3.connect(DB_PATH)
    for tbl in ("messages", "todos", "followups", "settings", "voice_usage"):
        try:
            con.execute(f"DELETE FROM {tbl} WHERE user_id=?", (uid,))
        except Exception:
            pass
    con.execute("DELETE FROM users WHERE id=?", (uid,))
    con.commit()
    con.close()
    _google_cache.pop(uid, None)


def set_user_password(uid: int, pw_hash: str) -> None:
    con = sqlite3.connect(DB_PATH)
    con.execute("UPDATE users SET pw_hash=? WHERE id=?", (pw_hash, uid))
    con.commit()
    con.close()


def set_user_name(uid: int, name: str) -> None:
    con = sqlite3.connect(DB_PATH)
    con.execute("UPDATE users SET name=? WHERE id=?", (name, uid))
    con.commit()
    con.close()


def set_user_avatar(uid: int, avatar: str | None) -> None:
    con = sqlite3.connect(DB_PATH)
    con.execute("UPDATE users SET avatar=? WHERE id=?", (avatar, uid))
    con.commit()
    con.close()


def delete_user(uid: int) -> None:
    """Verwijder een gebruiker én al zijn data. Gebruiker 1 (Ko) kan niet weg."""
    if int(uid) == 1:
        raise ValueError("De beheerder kan niet verwijderd worden.")
    con = sqlite3.connect(DB_PATH)
    for tbl in ("messages", "todos", "followups", "settings", "voice_usage"):
        con.execute(f"DELETE FROM {tbl} WHERE user_id=?", (uid,))
    con.execute("DELETE FROM users WHERE id=?", (uid,))
    con.commit()
    con.close()
    _google_cache.pop(uid, None)


def get_user_google_token(uid: int) -> str | None:
    con = sqlite3.connect(DB_PATH)
    row = con.execute("SELECT google_token FROM users WHERE id=?", (uid,)).fetchone()
    con.close()
    tok = row[0] if row else None
    # Terugval voor Ko (1): het oude tokenbestand.
    if not tok and uid == 1 and os.path.exists(GOOGLE_TOKEN_FILE):
        try:
            tok = open(GOOGLE_TOKEN_FILE).read()
        except Exception:
            tok = None
    return tok


def set_user_google_token(uid: int, token_json: str) -> None:
    con = sqlite3.connect(DB_PATH)
    con.execute("UPDATE users SET google_token=? WHERE id=?", (token_json, uid))
    con.commit()
    con.close()
    _google_cache.pop(uid, None)  # cache verversen


def _ucache(uid: int) -> dict:
    return _google_cache.setdefault(uid, {})


def _google_creds():
    uid = cur_uid()
    cache = _ucache(uid)
    if "creds" not in cache:
        from google.oauth2.credentials import Credentials
        tok = get_user_google_token(uid)
        if not tok:
            raise RuntimeError("Geen Google-koppeling voor deze gebruiker.")
        d = json.loads(tok)
        cache["creds"] = Credentials(
            token=None,
            refresh_token=d["refresh_token"],
            client_id=d["client_id"],
            client_secret=d["client_secret"],
            token_uri="https://oauth2.googleapis.com/token",
            scopes=GOOGLE_SCOPES,
        )
    return cache["creds"]


def _service(name: str, version: str):
    cache = _ucache(cur_uid())
    key = f"svc:{name}"
    if key not in cache:
        from googleapiclient.discovery import build
        cache[key] = build(name, version, credentials=_google_creds(),
                           cache_discovery=False)
    return cache[key]


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


def _person(p: dict) -> dict | None:
    names = p.get("names", [])
    emails = p.get("emailAddresses", [])
    if not emails:
        return None
    name = names[0].get("displayName") if names else emails[0].get("value")
    return {"name": name or emails[0].get("value"), "email": emails[0].get("value")}


def contact_search(name: str) -> list[dict]:
    """Zoek e-mailadressen op naam, uit opgeslagen contacten én iedereen die Ko
    ooit heeft gemaild (Google 'other contacts')."""
    svc = _service("people", "v1")
    found: list[dict] = []
    mask = "names,emailAddresses"
    try:
        r = svc.otherContacts().search(query=name, readMask=mask).execute()
        for item in r.get("results", []):
            p = _person(item.get("person", {}))
            if p:
                found.append(p)
    except Exception as e:
        print(f"otherContacts zoeken faalde: {e}", file=sys.stderr)
    try:
        r = svc.people().searchContacts(query=name, readMask=mask).execute()
        for item in r.get("results", []):
            p = _person(item.get("person", {}))
            if p:
                found.append(p)
    except Exception as e:
        print(f"contacten zoeken faalde: {e}", file=sys.stderr)
    # Dedupe op e-mailadres.
    seen, unique = set(), []
    for p in found:
        key = p["email"].lower()
        if key not in seen:
            seen.add(key)
            unique.append(p)
    return unique[:8]


def calendar_events(time_min_iso: str, time_max_iso: str) -> list[dict]:
    """Haal afspraken op tussen twee tijdstippen (voor context + reminders)."""
    r = _service("calendar", "v3").events().list(
        calendarId="primary", timeMin=time_min_iso, timeMax=time_max_iso,
        singleEvents=True, orderBy="startTime", maxResults=50).execute()
    return r.get("items", [])


def find_free_slots(date_from_iso: str, date_to_iso: str, duration_min: int = 60,
                    work_start: int = 9, work_end: int = 18) -> list[datetime]:
    """Vind vrije momenten (binnen werktijd) tussen twee datums."""
    d_from = datetime.fromisoformat(date_from_iso).date()
    d_to = datetime.fromisoformat(date_to_iso).date()
    span_start = datetime(d_from.year, d_from.month, d_from.day, 0, 0, tzinfo=LOCAL_TZ)
    span_end = datetime(d_to.year, d_to.month, d_to.day, 23, 59, tzinfo=LOCAL_TZ)
    events = calendar_events(span_start.astimezone(timezone.utc).isoformat(),
                             span_end.astimezone(timezone.utc).isoformat())
    busy = []
    for e in events:
        s = _event_start(e)
        end_dt = e.get("end", {}).get("dateTime")
        if s and end_dt:
            busy.append((s.astimezone(LOCAL_TZ),
                         datetime.fromisoformat(end_dt).astimezone(LOCAL_TZ)))

    slots: list[datetime] = []
    day = d_from
    while day <= d_to:
        if day.weekday() < 5:  # ma-vr
            cursor = datetime(day.year, day.month, day.day, work_start, 0, tzinfo=LOCAL_TZ)
            day_end = datetime(day.year, day.month, day.day, work_end, 0, tzinfo=LOCAL_TZ)
            for bstart, bend in sorted(b for b in busy if b[0].date() == day):
                if (bstart - cursor) >= timedelta(minutes=duration_min):
                    slots.append(cursor)
                cursor = max(cursor, bend)
            if (day_end - cursor) >= timedelta(minutes=duration_min):
                slots.append(cursor)
        day += timedelta(days=1)
    return slots[:12]


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
    if (feature_on("dagelijkse_brief") and 7 <= local.hour <= 10
            and not reminder_already_sent(daily_key)):
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
    for e in events if feature_on("herinnering_1u") else []:
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
    if (feature_on("verjaardagen") and 7 <= local.hour <= 10
            and not reminder_already_sent(scan_key)):
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


def check_todos() -> None:
    """Herinner aan taken waarvan de deadline (bijna) is bereikt."""
    if not feature_on("taken"):
        return
    today = datetime.now(LOCAL_TZ).date().isoformat()
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        "SELECT id, text, due FROM todos WHERE user_id = 1 AND done = 0 AND reminded = 0 "
        "AND due IS NOT NULL AND due <= ?", (today,)).fetchall()
    for tid, text, due in rows:
        send_telegram(f"✅ Herinnering: *{text}* (deadline {due}).")
        con.execute("UPDATE todos SET reminded = 1 WHERE id = ?", (tid,))
    con.commit()
    con.close()


def check_followups(accounts) -> None:
    """Follow-up radar: por Ko over mails waar nog geen antwoord op kwam."""
    if not feature_on("followup_radar"):
        return
    now = datetime.now(timezone.utc)
    due = [f for f in followup_list_open()
           if datetime.fromisoformat(f["remind_after"]) <= now]
    if not due:
        return
    since = now - timedelta(days=21)
    inbox = []
    for acc in accounts:
        try:
            inbox.extend(fetch_recent(acc, since))
        except Exception:
            continue
    for f in due:
        sent = datetime.fromisoformat(f["sent_ts"])
        replied = any(f["to_email"].lower() in (m.sender or "").lower()
                      and m.date and m.date > sent for m in inbox)
        if replied:
            followup_close(f["id"])
            continue
        days = max((now - sent).days, 1)
        send_telegram(
            f"📮 Je wacht al {days} dagen op antwoord van {f['to_email']} over "
            f"'{f['subject']}'. Zal ik een vriendelijke reminder sturen? "
            f"(Of zeg 'klaar' om te stoppen met volgen.)")
        followup_mark_nudged(f["id"])

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
    con.execute("""CREATE TABLE IF NOT EXISTS todos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        text TEXT NOT NULL,
        due TEXT,
        done INTEGER DEFAULT 0,
        reminded INTEGER DEFAULT 0)""")
    con.execute("""CREATE TABLE IF NOT EXISTS followups (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        to_email TEXT NOT NULL,
        subject TEXT,
        sent_ts TEXT NOT NULL,
        remind_after TEXT NOT NULL,
        done INTEGER DEFAULT 0,
        nudged INTEGER DEFAULT 0)""")
    con.execute("""CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL)""")
    # Spraakverbruik per gebruiker per maand (voor het maandbudget).
    con.execute("""CREATE TABLE IF NOT EXISTS voice_usage (
        user_id INTEGER NOT NULL DEFAULT 1,
        month TEXT NOT NULL,
        seconds INTEGER NOT NULL DEFAULT 0,
        cents INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (user_id, month))""")

    # --- Multi-user migratie (idempotent): user_id op de datatabellen ---
    def _cols(tbl):
        return [r[1] for r in con.execute(f"PRAGMA table_info({tbl})")]
    for tbl in ("messages", "todos", "followups"):
        if "user_id" not in _cols(tbl):
            con.execute(f"ALTER TABLE {tbl} ADD COLUMN user_id INTEGER NOT NULL DEFAULT 1")
        con.execute(f"CREATE INDEX IF NOT EXISTS idx_{tbl}_user ON {tbl}(user_id)")
    # settings -> samengestelde sleutel (user_id, key); bestaande rijen = Ko (1)
    if "user_id" not in _cols("settings"):
        con.execute("""CREATE TABLE settings_new (
            user_id INTEGER NOT NULL DEFAULT 1,
            key TEXT NOT NULL, value TEXT NOT NULL,
            PRIMARY KEY (user_id, key))""")
        con.execute("INSERT INTO settings_new (user_id, key, value) "
                    "SELECT 1, key, value FROM settings")
        con.execute("DROP TABLE settings")
        con.execute("ALTER TABLE settings_new RENAME TO settings")

    # Gebruikers (multi-user). Ko is altijd gebruiker 1.
    con.execute("""CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        name TEXT NOT NULL,
        pw_hash TEXT,
        google_token TEXT,
        is_admin INTEGER DEFAULT 0,
        created_ts TEXT NOT NULL)""")
    row = con.execute("SELECT id FROM users WHERE id=1").fetchone()
    if not row:
        pw = con.execute("SELECT value FROM settings WHERE user_id=1 AND key='web_pw_hash'").fetchone()
        gtok = None
        try:
            if os.path.exists(GOOGLE_TOKEN_FILE):
                gtok = open(GOOGLE_TOKEN_FILE).read()
        except Exception:
            gtok = None
        con.execute("INSERT INTO users (id, username, name, pw_hash, google_token, is_admin, created_ts) "
                    "VALUES (1, 'ko', 'Ko', ?, ?, 1, ?)",
                    (pw[0] if pw else None, gtok,
                     datetime.now(LOCAL_TZ).isoformat()))
    # Profielfoto per gebruiker (data-URL). Idempotente migratie.
    if "avatar" not in _cols("users"):
        con.execute("ALTER TABLE users ADD COLUMN avatar TEXT")
    con.commit()
    con.close()


# --- Functie-schakelaars (aan/uit per feature) -----------------------------

# Alle schakelbare functies (standaard aan). De toekomstige app leest/schrijft
# dezelfde tabel, dus de knopjes daar werken meteen op deze instellingen.
FEATURES = {
    "dagelijkse_brief": "Dagelijkse ochtend-samenvatting van je agenda",
    "herinnering_1u": "Herinnering ~1 uur voor een afspraak",
    "verjaardagen": "Verjaardagen een week van tevoren",
    "taken": "Herinneringen voor taken met een deadline",
    "followup_radar": "Automatisch volgen of er antwoord komt op je mails",
}


def _get_setting(key: str, default: str | None = None, uid: int | None = None) -> str | None:
    if uid is None:
        uid = cur_uid()
    try:
        con = sqlite3.connect(DB_PATH)
        row = con.execute("SELECT value FROM settings WHERE user_id = ? AND key = ?",
                          (uid, key)).fetchone()
        con.close()
        return row[0] if row else default
    except Exception:
        return default


def _set_setting(key: str, value: str, uid: int | None = None) -> None:
    if uid is None:
        uid = cur_uid()
    con = sqlite3.connect(DB_PATH)
    con.execute("INSERT INTO settings (user_id, key, value) VALUES (?, ?, ?) "
                "ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value",
                (uid, key, value))
    con.commit()
    con.close()


def feature_on(key: str, uid: int | None = None) -> bool:
    return _get_setting(f"feat:{key}", "1", uid) != "0"


# --- Spraakbudget (max € per gebruiker per maand) --------------------------

def voice_cap_cents() -> int:
    """Maandbudget in centen (standaard €3). Instelbaar via settings."""
    try:
        return int(_get_setting("voice_cap_cents", "300"))
    except Exception:
        return 300


def voice_cents_per_min() -> float:
    """Geschatte kosten per spraakminuut in centen (instelbaar, want de
    OpenAI-prijs is een schatting)."""
    try:
        return float(_get_setting("voice_cents_per_min", "15"))
    except Exception:
        return 15.0


def _voice_month() -> str:
    return datetime.now(LOCAL_TZ).strftime("%Y-%m")


def voice_spend_cents(uid: int = 1) -> int:
    try:
        con = sqlite3.connect(DB_PATH)
        row = con.execute(
            "SELECT cents FROM voice_usage WHERE user_id=? AND month=?",
            (uid, _voice_month())).fetchone()
        con.close()
        return int(row[0]) if row else 0
    except Exception:
        return 0


def voice_add_seconds(uid: int, seconds: float) -> None:
    """Boek gebruikte spraakseconden bij en reken ze om naar centen."""
    seconds = max(0, min(int(seconds), 3600))  # max 1 uur per sessie
    if seconds <= 0:
        return
    cents = int(round(seconds / 60.0 * voice_cents_per_min()))
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "INSERT INTO voice_usage (user_id, month, seconds, cents) VALUES (?,?,?,?) "
        "ON CONFLICT(user_id, month) DO UPDATE SET "
        "seconds = seconds + excluded.seconds, cents = cents + excluded.cents",
        (uid, _voice_month(), seconds, cents))
    con.commit()
    con.close()


def voice_over_cap(uid: int = 1) -> bool:
    return voice_spend_cents(uid) >= voice_cap_cents()


def voice_usage_info(uid: int = 1) -> dict:
    spent = voice_spend_cents(uid)
    cap = voice_cap_cents()
    pct = 0 if cap <= 0 else min(100, round(spent / cap * 100))
    return {"spent_cents": spent, "cap_cents": cap,
            "remaining_cents": max(0, cap - spent), "pct": pct}


def set_feature(key: str, on: bool, uid: int | None = None) -> None:
    _set_setting(f"feat:{key}", "1" if on else "0", uid)


def features_status() -> str:
    return "Functie-schakelaars (Ko kan ze aan/uit zetten):\n" + "\n".join(
        f"- {k}: {'AAN' if feature_on(k) else 'UIT'} — {d}"
        for k, d in FEATURES.items())


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


# --- To-do's ---------------------------------------------------------------

def todo_add(text: str, due: str | None = None) -> None:
    con = sqlite3.connect(DB_PATH)
    con.execute("INSERT INTO todos (ts, text, due, user_id) VALUES (?, ?, ?, ?)",
                (datetime.now(timezone.utc).isoformat(), text, due or None, cur_uid()))
    con.commit()
    con.close()


def todo_list_open() -> list[dict]:
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        "SELECT id, text, due FROM todos WHERE user_id = ? AND done = 0 ORDER BY id",
        (cur_uid(),)).fetchall()
    con.close()
    return [{"id": i, "text": t, "due": d} for i, t, d in rows]


def todo_complete(text_fragment: str) -> str:
    con = sqlite3.connect(DB_PATH)
    row = con.execute(
        "SELECT id, text FROM todos WHERE user_id = ? AND done = 0 AND text LIKE ? "
        "ORDER BY id LIMIT 1",
        (cur_uid(), f"%{text_fragment}%")).fetchone()
    if row:
        con.execute("UPDATE todos SET done = 1 WHERE id = ?", (row[0],))
        con.commit()
    con.close()
    return row[1] if row else ""


def todos_context() -> str:
    todos = todo_list_open()
    if not todos:
        return "Geen openstaande taken."
    lines = []
    for t in todos:
        due = f" (voor {t['due']})" if t.get("due") else ""
        lines.append(f"- [id:{t['id']}] {t['text']}{due}")
    return "Openstaande taken:\n" + "\n".join(lines)


# --- Geheugen doorzoeken ---------------------------------------------------

def memory_search(query: str, limit: int = 8) -> list[dict]:
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        "SELECT ts, role, content FROM messages WHERE user_id = ? AND content LIKE ? "
        "ORDER BY id DESC LIMIT ?", (cur_uid(), f"%{query}%", limit)).fetchall()
    con.close()
    return [{"ts": ts, "role": r, "content": c} for ts, r, c in rows]


# --- Follow-ups (wachten-op-antwoord) --------------------------------------

def followup_add(to_email: str, subject: str, days: int = 3) -> None:
    now = datetime.now(timezone.utc)
    remind_after = (now + timedelta(days=days)).isoformat()
    con = sqlite3.connect(DB_PATH)
    con.execute("INSERT INTO followups (ts, to_email, subject, sent_ts, remind_after, user_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (now.isoformat(), to_email, subject, now.isoformat(), remind_after, cur_uid()))
    con.commit()
    con.close()


def followup_list_open() -> list[dict]:
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        "SELECT id, to_email, subject, sent_ts, remind_after, nudged "
        "FROM followups WHERE user_id = ? AND done = 0 ORDER BY id",
        (cur_uid(),)).fetchall()
    con.close()
    return [{"id": i, "to_email": e, "subject": s, "sent_ts": st,
             "remind_after": ra, "nudged": n} for i, e, s, st, ra, n in rows]


def followup_close(id_: int) -> None:
    con = sqlite3.connect(DB_PATH)
    con.execute("UPDATE followups SET done = 1 WHERE id = ?", (id_,))
    con.commit()
    con.close()


def followup_mark_nudged(id_: int) -> None:
    con = sqlite3.connect(DB_PATH)
    con.execute("UPDATE followups SET nudged = 1, remind_after = ? WHERE id = ?",
                ((datetime.now(timezone.utc) + timedelta(days=3)).isoformat(), id_))
    con.commit()
    con.close()


def load_history(limit: int = HISTORY_LOAD) -> list[dict]:
    """Laad de laatste berichten als gespreksgeheugen (oud -> nieuw) van de
    huidige gebruiker."""
    try:
        con = sqlite3.connect(DB_PATH)
        rows = con.execute(
            "SELECT role, content FROM messages WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (cur_uid(), limit)).fetchall()
        con.close()
    except Exception as e:
        print(f"Geheugen laden faalde: {e}", file=sys.stderr)
        return []
    return [{"role": r, "content": c} for r, c in reversed(rows)]


def save_turn(role: str, content: str) -> None:
    try:
        con = sqlite3.connect(DB_PATH)
        con.execute("INSERT INTO messages (ts, role, content, user_id) VALUES (?, ?, ?, ?)",
                    (datetime.now(timezone.utc).isoformat(), role, content, cur_uid()))
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

ZEER BELANGRIJK — geen loze beloftes: beweer NOOIT dat je iets hebt gedaan (mail \
verstuurd, afspraak ingepland of verwijderd, meeting gepland, taak toegevoegd, contact \
gevonden) tenzij je in DEZE beurt daadwerkelijk de bijbehorende tool hebt aangeroepen en \
een geslaagd resultaat terugkreeg. Verzin geen voltooide acties. Wil Ko iets dat een \
actie vereist, voer die dan uit via de juiste tool — niet alleen zeggen dat je het doet. \
Lukt een tool niet of ontbreekt er iets (bijv. een e-mailadres), zeg dat eerlijk en vraag \
door.

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
4. Meedenken en organiseren:
   - To-do's: 'taak_toevoegen' bij "onthoud dat ik...", "ik moet nog...". 'taak_afronden' \
als iets klaar is. Ko's openstaande taken staan hieronder in je context.
   - Geheugen: 'geheugen_doorzoeken' bij "wat had ik ook alweer gezegd/afgesproken over...".
   - Slim plannen: 'vrije_tijd_zoeken' om een vrij moment te vinden; stel dat voor en plan \
daarna eventueel de afspraak of meeting in.
   - Follow-ups: elke mail die je verstuurt volg je automatisch op antwoord; je port Ko als \
er te lang niks komt. Zegt Ko dat iets is afgehandeld, gebruik dan 'followup_sluiten'.
5. Functies aan/uit: Ko kan elke automatische functie uitzetten ("zet de follow-up radar \
uit", "geen ochtend-brief meer"). Gebruik dan 'functie_instellen'. De huidige stand staat \
in je context; als een functie UIT staat, doe je 'm niet en bied je 'm ook niet ongevraagd aan.

Je onthoudt het lopende gesprek (je krijgt de eerdere berichten mee) plus een lijst van \
concepten die je deze sessie al hebt klaargezet. Als Ko vraagt "laat dat concept zien" \
of "wat had je geschreven", toon dan gewoon de tekst van het betreffende concept uit die \
lijst. Maak niet onnodig een nieuw concept aan als er al één is dat Ko bedoelt.

ROUTERING van concepten: is het zakelijk (klant, opdracht, offerte, leverancier, \
factuur, Kodesaign)? Zet 'account' dan op "Kodesaign" (verstuurt vanuit \
info@kodesaign.com). Privé? Gebruik "Gmail privé" of "Gmail studio", afhankelijk van \
waar de oorspronkelijke mail binnenkwam."""


CONTACT_TOOL = {
    "name": "contact_opzoeken",
    "description": "Zoek het e-mailadres van iemand op naam, uit Ko's opgeslagen "
                   "contacten én iedereen die Ko ooit heeft gemaild. Gebruik dit "
                   "ZODRA Ko iemand alleen bij voornaam of naam noemt (bijv. 'mail Cas' "
                   "of 'plan een meeting met Cas') en je het adres nog niet hebt. Krijg "
                   "je meerdere treffers, vraag dan aan Ko welke hij bedoelt.",
    "input_schema": {
        "type": "object",
        "properties": {"naam": {"type": "string", "description": "De naam om te zoeken."}},
        "required": ["naam"],
    },
}

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

TODO_ADD_TOOL = {
    "name": "taak_toevoegen",
    "description": "Voeg een taak/to-do toe aan Ko's lijst. Gebruik dit als Ko zegt "
                   "'onthoud dat ik ...', 'ik moet nog ...', 'zet op mijn lijst ...'. "
                   "Geef een deadline (due) mee als Ko een datum noemt.",
    "input_schema": {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "De taak."},
            "due": {"type": "string", "description": "Deadline als YYYY-MM-DD (optioneel)."},
        },
        "required": ["text"],
    },
}

TODO_DONE_TOOL = {
    "name": "taak_afronden",
    "description": "Markeer een taak als klaar. Geef een stukje van de taaktekst mee.",
    "input_schema": {
        "type": "object",
        "properties": {"tekst": {"type": "string"}},
        "required": ["tekst"],
    },
}

MEMORY_SEARCH_TOOL = {
    "name": "geheugen_doorzoeken",
    "description": "Doorzoek eerdere gesprekken met Ko op een trefwoord. Gebruik dit "
                   "als Ko vraagt 'wat had ik ook alweer gezegd/afgesproken over ...'.",
    "input_schema": {
        "type": "object",
        "properties": {"zoekterm": {"type": "string"}},
        "required": ["zoekterm"],
    },
}

FREESLOT_TOOL = {
    "name": "vrije_tijd_zoeken",
    "description": "Vind vrije momenten in Ko's agenda (binnen werktijd, ma-vr 9-18u) "
                   "tussen twee datums. Gebruik dit voor 'wanneer ben ik vrij?' of om een "
                   "meeting in te plannen op een moment dat Ko vrij is.",
    "input_schema": {
        "type": "object",
        "properties": {
            "van": {"type": "string", "description": "Begindatum YYYY-MM-DD."},
            "tot": {"type": "string", "description": "Einddatum YYYY-MM-DD."},
            "duur_minuten": {"type": "integer", "description": "Gewenste duur (standaard 60)."},
        },
        "required": ["van", "tot"],
    },
}

FOLLOWUP_CLOSE_TOOL = {
    "name": "followup_sluiten",
    "description": "Stop met het volgen van een openstaande follow-up (wachten op "
                   "antwoord). Gebruik dit als Ko zegt dat het klaar/afgehandeld is.",
    "input_schema": {
        "type": "object",
        "properties": {"to_email": {"type": "string",
                       "description": "Het e-mailadres van de follow-up die weg mag."}},
        "required": ["to_email"],
    },
}

SETTINGS_TOOL = {
    "name": "functie_instellen",
    "description": "Zet een automatische functie AAN of UIT als Ko dat vraagt. Geldige "
                   "functies: dagelijkse_brief (ochtend-agenda), herinnering_1u (1 uur "
                   "vooraf), verjaardagen (week vooraf), taken (deadline-herinneringen), "
                   "followup_radar (mails opvolgen). De huidige stand staat in je context.",
    "input_schema": {
        "type": "object",
        "properties": {
            "functie": {"type": "string",
                        "enum": ["dagelijkse_brief", "herinnering_1u", "verjaardagen",
                                 "taken", "followup_radar"]},
            "aan": {"type": "boolean", "description": "true = aanzetten, false = uitzetten."},
        },
        "required": ["functie", "aan"],
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


def _dynamic_context(context: str, agenda: str, session_drafts: list[dict]) -> str:
    """Het wisselende deel van de systeemprompt (tijd, agenda, taken, mail).
    Staat los van SYSTEM_PROMPT zodat dat constante deel gecachet kan worden."""
    now = datetime.now(LOCAL_TZ)
    _u = get_user(cur_uid())
    _name = _u["name"] if _u else "Ko"
    s = (f"Je praat nu met {_name}. Noem de gebruiker bij deze naam en ga uit van "
         f"ZIJN/HAAR eigen agenda, mail, contacten en taken (niet die van iemand anders).\n\n"
         f"Huidige datum/tijd: {now.strftime('%A %d-%m-%Y %H:%M')} "
         f"({TIMEZONE}, offset {now.strftime('%z')}). Reken afspraaktijden in DEZE "
         f"tijdzone en gebruik deze offset in de ISO-tijden (bijv. ...T15:00:00{now.strftime('%z')[:3]}:00)."
         + "\n\n--- De agenda (gebruik dit om 'ben ik druk?' te beantwoorden) ---\n"
         + agenda
         + f"\n\n--- Openstaande taken van {_name} ---\n" + todos_context()
         + "\n\n--- " + features_status()
         + "\n\n--- Recente mail als context ---\n" + context)
    if session_drafts:
        s += "\n\n--- Concepten die je deze sessie al hebt klaargezet ---\n"
        for i, d in enumerate(session_drafts, 1):
            s += (f"{i}. account={d.get('account')} aan={d.get('to')} "
                  f"onderwerp={d.get('subject')}\n   tekst: {d.get('body')}\n")
    if _get_setting("lang", "nl") == "en":
        s += (f"\n\n--- LANGUAGE ---\n{_name} has set the app language to English. Write all "
              f"your replies to {_name} in English. Assume {_name} writes to you in English; "
              "never translate to Dutch. Keep the same helpful, concise style.")
    return s


def _current_name() -> str:
    u = get_user(cur_uid())
    return (u["name"] if u and u["name"] else "Ko")


def _personalize(text: str, name: str) -> str:
    """Vervang de hardcoded 'Ko' in de prompt door de naam van de huidige gebruiker.
    Voor Ko (naam == 'Ko') verandert er niets, zodat zijn prompt gecachet blijft."""
    if name == "Ko":
        return text
    return re.sub(r"\bKo\b", name, text.replace("Ko's", name + "'s"))


def _build_system(context: str, agenda: str, session_drafts: list[dict]) -> str:
    return (_personalize(SYSTEM_PROMPT, _current_name()) + "\n\n"
            + _dynamic_context(context, agenda, session_drafts))


def _system_blocks(context: str, agenda: str, session_drafts: list[dict]) -> list[dict]:
    """Systeemprompt als blokken: constante kop gecachet, wisselend deel niet."""
    return [
        {"type": "text", "text": _personalize(SYSTEM_PROMPT, _current_name()),
         "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": _dynamic_context(context, agenda, session_drafts)},
    ]


def _cached_tools() -> list[dict]:
    """ALL_TOOLS met een cache-marker op het laatste blok, zodat het (constante,
    grote) tool-schema gecachet wordt en niet elke beurt opnieuw verwerkt hoeft."""
    if not ALL_TOOLS:
        return ALL_TOOLS
    name = _current_name()
    base = ALL_TOOLS
    if name != "Ko":
        # Ook de tool-beschrijvingen ("Ko's agenda" enz.) naar de juiste naam.
        base = json.loads(_personalize(json.dumps(ALL_TOOLS, ensure_ascii=False), name))
    tools = list(base)
    last = dict(tools[-1])
    last["cache_control"] = {"type": "ephemeral"}
    tools[-1] = last
    return tools


def _messages_create(client, **kwargs):
    """Roep Claude aan met een paar herhalingen bij tijdelijke overbelasting."""
    last = None
    for attempt in range(4):
        try:
            return client.messages.create(**kwargs)
        except Exception as e:
            last = e
            m = str(e).lower()
            transient = any(k in m for k in
                            ("overloaded", "529", "429", "rate", "timeout", "503", "502"))
            if transient and attempt < 3:
                time.sleep(2 * (attempt + 1))
                continue
            raise
    raise last


def execute_tool(name: str, inp: dict, accounts_by_name: dict,
                 session_drafts: list[dict]) -> str:
    """Voer één tool uit en geef een korte tekst-uitkomst terug voor het model."""
    if name == "contact_opzoeken":
        matches = contact_search(inp.get("naam", ""))
        if not matches:
            return f"Geen contact gevonden voor '{inp.get('naam')}'."
        return "Gevonden:\n" + "\n".join(f"- {m['name']} <{m['email']}>" for m in matches)

    if name == "concept_klaarzetten":
        acc = accounts_by_name.get(inp.get("account"))
        if not acc or not inp.get("to"):
            return "Kon concept niet klaarzetten: onbekend account of ontvanger."
        try:
            save_draft(acc, inp["to"], inp.get("subject", "Re:"), inp.get("body", ""))
            session_drafts.append(inp)
            return f"Concept klaargezet in {acc.user}."
        except Exception as e:
            return f"Concept opslaan mislukt: {e}"

    if name == "mail_versturen":
        acc = accounts_by_name.get(inp.get("account"))
        to = inp.get("to")
        if not to:
            return "Geen ontvanger opgegeven."
        sender = acc.user if acc else None
        try:
            if google_enabled():
                gmail_send(to, inp.get("subject", ""), inp.get("body", ""), sender=sender)
            elif acc:
                send_email(acc, to, inp.get("subject", ""), inp.get("body", ""))
            else:
                return "Geen verzendmethode beschikbaar."
            # Follow-up radar: houd bij dat we op antwoord wachten (indien aan).
            note = ""
            if feature_on("followup_radar"):
                try:
                    followup_add(to, inp.get("subject", "(geen onderwerp)"))
                    note = " (Ik hou in de gaten of er antwoord komt.)"
                except Exception as e:
                    print(f"Follow-up toevoegen faalde: {e}", file=sys.stderr)
            return f"Verstuurd naar {to}.{note}"
        except Exception as e:
            return f"Versturen mislukt: {e}"

    if name == "agenda_afspraak_toevoegen":
        try:
            calendar_add(inp.get("summary", "Afspraak"), inp["start"], inp["end"],
                         inp.get("description", ""), inp.get("location", ""))
            return "Afspraak toegevoegd aan de agenda."
        except Exception as e:
            return f"Inplannen mislukt: {e}"

    if name == "meeting_inplannen":
        try:
            link = calendar_add_meeting(
                inp.get("summary", "Meeting"), inp["start"], inp["end"],
                inp.get("attendees", []), inp.get("description", ""))
            return (f"Meeting ingepland en uitnodigingen verstuurd. "
                    f"Google Meet-link: {link or '(volgt)'}")
        except Exception as e:
            return f"Meeting inplannen mislukt: {e}"

    if name == "agenda_afspraak_verwijderen":
        try:
            calendar_delete(inp["event_id"])
            return "Afspraak verwijderd uit de agenda."
        except Exception as e:
            return f"Verwijderen mislukt: {e}"

    if name == "taak_toevoegen":
        try:
            todo_add(inp.get("text", ""), inp.get("due"))
            return "Taak toegevoegd aan je lijst."
        except Exception as e:
            return f"Taak toevoegen mislukt: {e}"

    if name == "taak_afronden":
        done = todo_complete(inp.get("tekst", ""))
        return f"Afgevinkt: {done}" if done else "Geen bijpassende open taak gevonden."

    if name == "geheugen_doorzoeken":
        hits = memory_search(inp.get("zoekterm", ""))
        if not hits:
            return "Niks gevonden in eerdere gesprekken."
        return "Uit eerdere gesprekken:\n" + "\n".join(
            f"- ({h['role']}) {h['content'][:200]}" for h in hits)

    if name == "vrije_tijd_zoeken":
        try:
            slots = find_free_slots(inp["van"], inp["tot"],
                                    int(inp.get("duur_minuten", 60)))
            if not slots:
                return "Geen vrije momenten gevonden in die periode (binnen werktijd)."
            return "Vrije momenten:\n" + "\n".join(
                "- " + s.strftime("%a %d-%m %H:%M") for s in slots)
        except Exception as e:
            return f"Vrije tijd zoeken mislukt: {e}"

    if name == "followup_sluiten":
        target = (inp.get("to_email") or "").lower()
        closed = 0
        for f in followup_list_open():
            if target in f["to_email"].lower():
                followup_close(f["id"])
                closed += 1
        return f"{closed} follow-up(s) afgesloten." if closed else "Geen follow-up gevonden."

    if name == "functie_instellen":
        key = inp.get("functie")
        if key not in FEATURES:
            return f"Onbekende functie: {key}."
        set_feature(key, bool(inp.get("aan")))
        return (f"'{FEATURES[key]}' staat nu "
                f"{'AAN' if inp.get('aan') else 'UIT'}.")

    return f"Onbekende tool: {name}"


ALL_TOOLS = [CONTACT_TOOL, DRAFT_TOOL, SEND_TOOL, CALENDAR_TOOL,
             MEETING_TOOL, DELETE_EVENT_TOOL, TODO_ADD_TOOL, TODO_DONE_TOOL,
             MEMORY_SEARCH_TOOL, FREESLOT_TOOL, FOLLOWUP_CLOSE_TOOL, SETTINGS_TOOL]


def handle(client: anthropic.Anthropic, history: list[dict], message: str,
           context: str, agenda: str, session_drafts: list[dict],
           accounts_by_name: dict) -> str:
    """Verwerk Ko's bericht; Able mag tools ketenen (bijv. eerst contact opzoeken,
    dan meeting plannen). Geeft de uiteindelijke tekst voor Telegram terug."""
    messages = history + [{"role": "user", "content": message}]
    system = _system_blocks(context, agenda, session_drafts)
    tools = _cached_tools()
    final_text = ""
    for _ in range(6):
        resp = _messages_create(
            client, model=CLAUDE_MODEL, max_tokens=900, system=system,
            tools=tools, messages=messages)
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        if text:
            final_text = text
        tool_uses = [b for b in resp.content if b.type == "tool_use"]
        if not tool_uses:
            break
        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for tu in tool_uses:
            out = execute_tool(tu.name, tu.input, accounts_by_name, session_drafts)
            results.append({"type": "tool_result", "tool_use_id": tu.id, "content": out})
        messages.append({"role": "user", "content": results})
    return final_text or "Genoteerd, Ko."


def handle_stream(client: anthropic.Anthropic, history: list[dict], message: str,
                  context: str, agenda: str, session_drafts: list[dict],
                  accounts_by_name: dict):
    """Zelfde als handle(), maar levert de tekst stukje-bij-beetje (streaming) op
    zodat de app het antwoord al ziet terwijl Able nog typt. Yield't tekst-deltas."""
    messages = history + [{"role": "user", "content": message}]
    system = _system_blocks(context, agenda, session_drafts)
    tools = _cached_tools()
    for _ in range(6):
        with client.messages.stream(
                model=CLAUDE_MODEL, max_tokens=900, system=system,
                tools=tools, messages=messages) as stream:
            for delta in stream.text_stream:
                yield delta
            final = stream.get_final_message()
        tool_uses = [b for b in final.content if b.type == "tool_use"]
        if not tool_uses:
            return
        messages.append({"role": "assistant", "content": final.content})
        results = []
        for tu in tool_uses:
            out = execute_tool(tu.name, tu.input, accounts_by_name, session_drafts)
            results.append({"type": "tool_result", "tool_use_id": tu.id, "content": out})
        messages.append({"role": "user", "content": results})


def process(client, accounts_by_name, message: str, context: str, agenda: str,
            history: list[dict], session_drafts: list[dict]) -> None:
    # Blijf "aan het typen…" tonen zolang Able bezig is.
    stop = threading.Event()

    def keep_typing():
        while not stop.is_set():
            send_typing()
            stop.wait(4)

    threading.Thread(target=keep_typing, daemon=True).start()
    try:
        reply = handle(client, history, message, context, agenda,
                       session_drafts, accounts_by_name)
    finally:
        stop.set()

    reply = (reply or "").strip() or "Genoteerd, Ko."
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

    # Reminder-thread: agenda-samenvatting, 1u-vooraf, verjaardagen, taken, follow-ups.
    def reminder_loop():
        last_followup = 0.0
        while True:
            try:
                if google_enabled():
                    check_reminders()
            except Exception as e:
                print(f"Reminder-check faalde: {e}", file=sys.stderr)
            try:
                check_todos()
            except Exception as e:
                print(f"Taak-check faalde: {e}", file=sys.stderr)
            # Follow-up radar hooguit elke ~20 min (checkt inbox).
            if time.time() - last_followup > 1200:
                try:
                    check_followups(accounts)
                except Exception as e:
                    print(f"Follow-up-check faalde: {e}", file=sys.stderr)
                last_followup = time.time()
            time.sleep(120)

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
