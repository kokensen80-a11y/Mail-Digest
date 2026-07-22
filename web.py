#!/usr/bin/env python3
"""
Web-app backend voor Able (kompas-7x2.kodesaign.com).

Serveert het chatscherm en koppelt het aan Able's brein uit bot.py — zelfde
geheugen, dezelfde tools (mail, agenda, contacten). Achter een inlogscherm.
Stap 1: getypt chatten. Stem (STT/TTS) komt in latere stappen.
"""

import hashlib
import hmac
import json
import os
import threading
import time
from datetime import datetime

import anthropic
import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (FileResponse, JSONResponse, Response,
                               StreamingResponse)

import bot  # Able's brein (zelfde DB, tools en geheugen)

# Stem (ElevenLabs) — professioneel & rustig, Nederlands via multilingual model.
ELEVEN_VOICE = os.environ.get("ELEVENLABS_VOICE_ID", "EXAVITQu4vr4xnSDxMaL")  # "Sarah"
ELEVEN_MODEL = os.environ.get("ELEVENLABS_MODEL", "eleven_multilingual_v2")

HERE = os.path.dirname(os.path.abspath(__file__))
WEB_PASSWORD = os.environ.get("WEB_PASSWORD", "")
WEB_SECRET = os.environ.get("WEB_SECRET", "onveilig-dev-secret")
COOKIE = "able_auth"
MAX_AGE = 120 * 86400  # ~4 maanden ingelogd blijven

# Google "Web application" koppeling (voor de Koppel-Google-knop per gebruiker).
GOOGLE_WEB_CLIENT_ID = os.environ.get("GOOGLE_WEB_CLIENT_ID", "")
GOOGLE_WEB_CLIENT_SECRET = os.environ.get("GOOGLE_WEB_CLIENT_SECRET", "")
OAUTH_REDIRECT = os.environ.get(
    "OAUTH_REDIRECT", "https://kompas-7x2.kodesaign.com/oauth/callback")

app = FastAPI()

_client = None
def client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _client

# Mail-/agenda-context op de achtergrond bijhouden zodat antwoorden snel zijn.
_ctx = {"mail": "(wordt geladen…)", "agenda": "(wordt geladen…)"}
_ctx_lock = threading.Lock()

def _refresh_ctx():
    while True:
        try:
            accounts = bot.load_accounts()
            m = bot._format_context(bot.gather_mail_context(accounts))
            with _ctx_lock:
                _ctx["mail"] = m
        except Exception as e:
            print(f"mailctx: {e}")
        try:
            a = bot.build_agenda_context()
            with _ctx_lock:
                _ctx["agenda"] = a
        except Exception as e:
            print(f"agendactx: {e}")
        time.sleep(180)


def _user_context(uid: int) -> tuple[str, str]:
    """(mail, agenda) voor deze gebruiker. Ko (1) uit de snelle cache; anderen
    live uit hun eigen Google-agenda. Mail-lezen voor gasten komt later."""
    if uid == 1:
        with _ctx_lock:
            return _ctx["mail"], _ctx["agenda"]
    try:
        agenda = bot.build_agenda_context()
    except Exception:
        agenda = ""
    return "", agenda


@app.on_event("startup")
def _startup():
    bot.init_db()
    try:
        bot.warm_google()
    except Exception as e:
        print(f"warm_google: {e}")
    threading.Thread(target=_refresh_ctx, daemon=True).start()


# --- Inlog (ondertekend cookie, geen wachtwoord-opslag nodig) ---------------

def _make_token(uid: int) -> str:
    payload = f"{int(time.time())}.{int(uid)}"
    sig = hmac.new(WEB_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return payload + "." + sig


def _token_uid(tok: str) -> int | None:
    """Geef de gebruiker-id uit een geldig cookie, anders None."""
    try:
        ts, uid, sig = tok.split(".")
        exp = hmac.new(WEB_SECRET.encode(), f"{ts}.{uid}".encode(),
                       hashlib.sha256).hexdigest()
        if hmac.compare_digest(sig, exp) and (time.time() - int(ts) < MAX_AGE):
            return int(uid)
    except Exception:
        pass
    return None


def _uid(req: Request) -> int:
    """Huidige gebruiker-id uit het cookie (0 = niet ingelogd)."""
    return _token_uid(req.cookies.get(COOKIE, "")) or 0


def _authed(req: Request) -> bool:
    """Ingelogd? Zet meteen de 'huidige gebruiker' voor dit verzoek zodat alle
    bot-functies de juiste persoon gebruiken."""
    uid = _token_uid(req.cookies.get(COOKIE, ""))
    if uid:
        bot.set_uid(uid)
        return True
    return False


def _hash_pw(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()


def _authenticate(username: str, password: str) -> dict | None:
    """Controleer gebruikersnaam + wachtwoord. Terugval voor Ko (1) op de oude
    web_pw_hash-instelling of het WEB_PASSWORD uit de omgeving."""
    user = bot.get_user_by_username(username)
    if not user:
        return None
    ph = user.get("pw_hash")
    if ph:
        return user if _hash_pw(password) == ph else None
    # Geen pw_hash: alleen mogelijk voor Ko (1) via de oude fallback.
    if user["id"] == 1:
        legacy = bot._get_setting("web_pw_hash", uid=1)
        if legacy and _hash_pw(password) == legacy:
            return user
        if WEB_PASSWORD and password == WEB_PASSWORD:
            return user
    return None


@app.post("/api/login")
async def login(req: Request):
    data = await req.json()
    username = (data.get("username") or "").strip().lower()
    user = _authenticate(username, data.get("password", ""))
    if not user:
        raise HTTPException(401, "Onjuiste gebruikersnaam of wachtwoord")
    resp = JSONResponse({"ok": True})
    resp.set_cookie(COOKIE, _make_token(user["id"]), httponly=True, secure=True,
                    samesite="lax", max_age=MAX_AGE)
    return resp


@app.post("/api/password")
async def change_password(req: Request):
    if not _authed(req):
        raise HTTPException(401, "Niet ingelogd")
    uid = _uid(req)
    user = bot.get_user(uid)
    data = await req.json()
    # Huidig wachtwoord controleren (via dezelfde route als inloggen).
    if not _authenticate(user["username"], data.get("current", "")):
        raise HTTPException(400, "Huidig wachtwoord klopt niet")
    new = (data.get("new") or "").strip()
    if len(new) < 4:
        raise HTTPException(400, "Nieuw wachtwoord is te kort (min. 4 tekens)")
    bot.set_user_password(uid, _hash_pw(new))
    return {"ok": True}


@app.post("/api/memory/clear")
async def clear_memory(req: Request):
    if not _authed(req):
        raise HTTPException(401, "Niet ingelogd")
    con = sqlite3.connect(bot.DB_PATH)
    n = con.execute("SELECT COUNT(*) FROM messages WHERE user_id=?", (_uid(req),)).fetchone()[0]
    con.execute("DELETE FROM messages WHERE user_id=?", (_uid(req),))
    con.commit()
    con.close()
    return {"ok": True, "removed": n}


# --- Gebruikersbeheer (alleen admin = Ko) ----------------------------------

def _require_admin(req: Request) -> int:
    if not _authed(req):
        raise HTTPException(401, "Niet ingelogd")
    uid = _uid(req)
    u = bot.get_user(uid)
    if not u or not u.get("is_admin"):
        raise HTTPException(403, "Alleen de beheerder mag dit")
    return uid


@app.get("/api/users")
async def api_users(req: Request):
    _require_admin(req)
    return {"users": bot.list_users()}


@app.post("/api/users")
async def api_users_add(req: Request):
    _require_admin(req)
    data = await req.json()
    username = (data.get("username") or "").strip().lower()
    name = (data.get("name") or "").strip()
    pw = (data.get("password") or "").strip()
    if not username or not name or len(pw) < 4:
        raise HTTPException(400, "Naam, gebruikersnaam en wachtwoord (min. 4) zijn verplicht")
    if not username.isalnum():
        raise HTTPException(400, "Gebruikersnaam mag alleen letters/cijfers bevatten")
    if bot.get_user_by_username(username):
        raise HTTPException(400, "Die gebruikersnaam bestaat al")
    uid = bot.create_user(username, name, _hash_pw(pw), is_admin=0)
    return {"ok": True, "id": uid}


@app.post("/api/users/delete")
async def api_users_delete(req: Request):
    me = _require_admin(req)
    data = await req.json()
    try:
        target = int(data.get("id"))
    except (TypeError, ValueError):
        raise HTTPException(400, "Ongeldige gebruiker")
    if target == 1 or target == me:
        raise HTTPException(400, "Je kunt de beheerder niet verwijderen")
    if not bot.get_user(target):
        raise HTTPException(404, "Gebruiker bestaat niet")
    bot.delete_user(target)
    return {"ok": True}


@app.post("/api/users/delete")
async def api_users_delete(req: Request):
    admin = _require_admin(req)
    data = await req.json()
    try:
        target = int(data.get("id"))
    except Exception:
        raise HTTPException(400, "Ongeldige gebruiker")
    if target == 1:
        raise HTTPException(400, "De beheerder kan niet worden verwijderd")
    if target == admin:
        raise HTTPException(400, "Je kunt jezelf niet verwijderen")
    if not bot.get_user(target):
        raise HTTPException(404, "Gebruiker niet gevonden")
    bot.delete_user(target)
    return {"ok": True}


@app.get("/api/integrations")
async def integrations(req: Request):
    if not _authed(req):
        raise HTTPException(401, "Niet ingelogd")
    uid = _uid(req)
    # IMAP-mailboxen zijn (nog) alleen die van Ko; gasten zien ze niet.
    accounts = bot.load_accounts() if uid == 1 else []
    con = sqlite3.connect(bot.DB_PATH)
    mem = con.execute("SELECT COUNT(*) FROM messages WHERE user_id=?", (uid,)).fetchone()[0]
    con.close()
    # Nettere labels voor Ko's mailboxen; anders de geconfigureerde naam.
    relabel = {
        "kokensen80@gmail.com": "Gmail privé",
        "kensendesaignstudios@gmail.com": "Gmail Zakelijk",
        "info@kodesaign.com": "Anders Zakelijk",
    }

    def mb(a):
        email = (a.user or "").lower()
        return {"name": relabel.get(email, a.name), "user": a.user,
                "type": "gmail" if email.endswith("@gmail.com") else "other",
                "on": True}

    return {
        "google": bot.google_enabled(),
        "telegram": bool(os.environ.get("TELEGRAM_BOT_TOKEN")),
        "mailboxes": [mb(a) for a in accounts],
        "voice_model": OPENAI_REALTIME_MODEL,
        "openai": bool(os.environ.get("OPENAI_API_KEY")),
        "memory_count": mem,
        "server": "kompas-7x2.kodesaign.com",
    }


@app.get("/api/me")
async def me(req: Request):
    if not _authed(req):
        return {"auth": False}
    u = bot.get_user(_uid(req)) or {}
    return {"auth": True, "name": u.get("name", ""), "username": u.get("username", ""),
            "is_admin": bool(u.get("is_admin")), "google": bot.google_enabled()}


# --- "Koppel Google": web-OAuth per gebruiker ------------------------------

def _oauth_state(uid: int) -> str:
    sig = hmac.new(WEB_SECRET.encode(), f"g{uid}".encode(), hashlib.sha256).hexdigest()[:24]
    return f"{uid}.{sig}"


def _oauth_state_uid(state: str) -> int | None:
    try:
        uid, sig = state.split(".")
        exp = hmac.new(WEB_SECRET.encode(), f"g{uid}".encode(), hashlib.sha256).hexdigest()[:24]
        return int(uid) if hmac.compare_digest(sig, exp) else None
    except Exception:
        return None


@app.get("/oauth/start")
async def oauth_start(req: Request):
    if not _authed(req):
        raise HTTPException(401, "Niet ingelogd")
    if not GOOGLE_WEB_CLIENT_ID:
        raise HTTPException(400, "Google-webkoppeling nog niet ingesteld op de server.")
    from urllib.parse import urlencode
    params = {
        "client_id": GOOGLE_WEB_CLIENT_ID,
        "redirect_uri": OAUTH_REDIRECT,
        "response_type": "code",
        "scope": " ".join(bot.GOOGLE_SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": _oauth_state(_uid(req)),
    }
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params)
    return JSONResponse({"url": url})


@app.get("/oauth/callback")
async def oauth_callback(req: Request):
    err = req.query_params.get("error")
    if err:
        return Response(_oauth_page("Koppeling geannuleerd. Je kunt dit tabblad sluiten."),
                        media_type="text/html")
    code = req.query_params.get("code", "")
    uid = _oauth_state_uid(req.query_params.get("state", ""))
    if not code or not uid:
        return Response(_oauth_page("Er ging iets mis met de koppeling."),
                        media_type="text/html", status_code=400)
    try:
        r = requests.post("https://oauth2.googleapis.com/token", data={
            "code": code,
            "client_id": GOOGLE_WEB_CLIENT_ID,
            "client_secret": GOOGLE_WEB_CLIENT_SECRET,
            "redirect_uri": OAUTH_REDIRECT,
            "grant_type": "authorization_code",
        }, timeout=20)
        d = r.json()
        refresh = d.get("refresh_token")
        if not refresh:
            return Response(_oauth_page(
                "Google gaf geen blijvende toegang terug. Verwijder Able bij "
                "je Google-account-machtigingen en probeer opnieuw."),
                media_type="text/html", status_code=400)
        token_json = json.dumps({
            "refresh_token": refresh,
            "client_id": GOOGLE_WEB_CLIENT_ID,
            "client_secret": GOOGLE_WEB_CLIENT_SECRET,
        })
        bot.set_user_google_token(uid, token_json)
    except Exception as e:  # noqa: BLE001
        print(f"oauth-fout: {e}")
        return Response(_oauth_page("Er ging iets mis bij het koppelen."),
                        media_type="text/html", status_code=500)
    return Response(_oauth_page("Google is gekoppeld! Je kunt dit tabblad sluiten "
                                "en terug naar Able."), media_type="text/html")


def _oauth_page(msg: str) -> str:
    return (
        "<!doctype html><meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Able</title>"
        "<div style=\"font-family:-apple-system,system-ui,sans-serif;max-width:420px;"
        "margin:22vh auto;padding:0 24px;text-align:center;color:#111\">"
        "<div style=\"font-size:34px;font-weight:800;letter-spacing:.01em;margin-bottom:14px\">able</div>"
        f"<p style=\"font-size:17px;line-height:1.5;color:#444\">{msg}</p>"
        "<a href='/' style=\"display:inline-block;margin-top:18px;background:#111;color:#fff;"
        "text-decoration:none;font-weight:600;padding:12px 22px;border-radius:999px\">Terug naar Able</a>"
        "</div>")


@app.post("/api/chat")
async def chat(req: Request):
    if not _authed(req):
        raise HTTPException(401, "Niet ingelogd")
    data = await req.json()
    msg = (data.get("message") or "").strip()
    if not msg:
        return {"reply": ""}
    uid = _uid(req)
    accounts = bot.load_accounts() if uid == 1 else []
    abn = {a.name: a for a in accounts}
    history = bot.load_history()
    mail, agenda = _user_context(uid)
    try:
        reply = bot.handle(client(), history, msg, mail, agenda, [], abn)
    except Exception as e:
        print(f"chat-fout: {e}")
        reply = "Sorry Ko, daar ging iets mis aan mijn kant. Probeer het zo nog eens?"
    reply = _strip_emoji(reply)
    bot.save_turn("user", msg)
    bot.save_turn("assistant", reply)
    return {"reply": reply}


@app.post("/api/chat/stream")
async def chat_stream(req: Request):
    """Streamt Able's antwoord stukje-bij-beetje, zodat de app het meteen ziet."""
    if not _authed(req):
        raise HTTPException(401, "Niet ingelogd")
    data = await req.json()
    msg = (data.get("message") or "").strip()
    if not msg:
        return Response("", media_type="text/plain")
    uid = _uid(req)
    accounts = bot.load_accounts() if uid == 1 else []
    abn = {a.name: a for a in accounts}
    history = bot.load_history()
    mail, agenda = _user_context(uid)

    def gen():
        bot.set_uid(uid)  # pin de juiste gebruiker in de worker-thread
        parts = []
        try:
            for chunk in bot.handle_stream(client(), history, msg, mail, agenda, [], abn):
                if not chunk:
                    continue
                parts.append(chunk)
                # Alleen emoji weghalen; spaties/indeling intact laten tijdens streamen.
                clean = _EMOJI_RE.sub("", chunk)
                if clean:
                    yield clean
        except Exception as e:  # noqa: BLE001
            print(f"chat-stream-fout: {e}")
            if not parts:
                # Streaming lukte niet → val terug op het gewone (niet-streamende) antwoord.
                try:
                    reply = bot.handle(client(), history, msg, mail, agenda, [], abn)
                except Exception as e2:  # noqa: BLE001
                    print(f"chat-fallback-fout: {e2}")
                    reply = "Sorry Ko, daar ging iets mis aan mijn kant. Probeer het zo nog eens?"
                reply = _strip_emoji(reply)
                yield reply
                bot.save_turn("user", msg)
                bot.save_turn("assistant", reply)
                return
        reply = _strip_emoji("".join(parts)).strip() or "Genoteerd, Ko."
        bot.save_turn("user", msg)
        bot.save_turn("assistant", reply)

    return StreamingResponse(gen(), media_type="text/plain; charset=utf-8",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


import re as _re

_EMOJI_RE = _re.compile(
    "[\U0001F000-\U0001FAFF☀-➿⬀-⯿️‍]+")


def _strip_emoji(text: str) -> str:
    """De app-UI is emoji-vrij; Telegram houdt zijn eigen stijl."""
    return _EMOJI_RE.sub("", text).replace("  ", " ").strip()


@app.post("/api/tts")
async def tts(req: Request):
    if not _authed(req):
        raise HTTPException(401, "Niet ingelogd")
    key = os.environ.get("ELEVENLABS_API_KEY", "")
    if not key:
        raise HTTPException(400, "Geen ElevenLabs-sleutel ingesteld")
    data = await req.json()
    text = (data.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "Geen tekst")
    try:
        r = requests.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_VOICE}",
            headers={"xi-api-key": key, "Accept": "audio/mpeg",
                     "Content-Type": "application/json"},
            json={"text": text, "model_id": ELEVEN_MODEL,
                  "voice_settings": {"stability": 0.5, "similarity_boost": 0.75,
                                     "style": 0.0, "use_speaker_boost": True}},
            timeout=45)
    except Exception as e:
        raise HTTPException(502, f"Stem-fout: {e}")
    if r.status_code != 200:
        raise HTTPException(502, f"Stem-fout {r.status_code}: {r.text[:200]}")
    return Response(content=r.content, media_type="audio/mpeg")


# --- Tab-data (Upcoming, Taken, Mail, Instellingen) ------------------------

import sqlite3
from datetime import timedelta, timezone as _tz

DAYS_NL = ["ma", "di", "wo", "do", "vr", "za", "zo"]


@app.get("/api/agenda")
async def api_agenda(req: Request):
    if not _authed(req):
        raise HTTPException(401, "Niet ingelogd")
    try:
        now = datetime.now(_tz.utc)
        events = bot.calendar_events(now.isoformat(),
                                     (now + timedelta(days=14)).isoformat())
    except Exception as e:
        return {"items": [], "error": str(e)}
    items = []
    for e in events:
        s = bot._event_start(e)
        title = e.get("summary", "(geen titel)")
        if s:
            loc = s.astimezone(bot.LOCAL_TZ)
            items.append({"day": f"{DAYS_NL[loc.weekday()]} {loc.day} {loc.strftime('%b').lower()}",
                          "time": loc.strftime("%H:%M"), "title": title})
        else:
            d = e.get("start", {}).get("date", "?")
            items.append({"day": d, "time": "hele dag", "title": title})
    return {"items": items}


@app.get("/api/history")
async def api_history(req: Request):
    if not _authed(req):
        raise HTTPException(401, "Niet ingelogd")
    return {"items": bot.load_history(60)}


@app.get("/api/todos")
async def api_todos(req: Request):
    if not _authed(req):
        raise HTTPException(401, "Niet ingelogd")
    return {"items": bot.todo_list_open()}


@app.post("/api/todos/done")
async def api_todo_done(req: Request):
    if not _authed(req):
        raise HTTPException(401, "Niet ingelogd")
    data = await req.json()
    con = sqlite3.connect(bot.DB_PATH)
    con.execute("UPDATE todos SET done = 1 WHERE id = ?", (int(data.get("id", 0)),))
    con.commit()
    con.close()
    return {"ok": True}


@app.post("/api/todos/add")
async def api_todo_add(req: Request):
    if not _authed(req):
        raise HTTPException(401, "Niet ingelogd")
    data = await req.json()
    text = (data.get("text") or "").strip()
    if text:
        bot.todo_add(text, (data.get("due") or "").strip() or None)
    return {"ok": True}


@app.get("/api/mailtab")
async def api_mailtab(req: Request):
    if not _authed(req):
        raise HTTPException(401, "Niet ingelogd")
    now = datetime.now(_tz.utc)
    items = []
    for f in bot.followup_list_open():
        try:
            days = max((now - datetime.fromisoformat(f["sent_ts"])).days, 0)
        except Exception:
            days = 0
        items.append({"to": f["to_email"], "subject": f["subject"] or "(geen onderwerp)",
                      "days": days})
    return {"followups": items}


# Menselijke namen voor de OpenAI-stemmen (m = man, v = vrouw).
VOICE_META = [
    {"id": "marin",   "name": "Saar",  "gender": "v"},
    {"id": "coral",   "name": "Emma",  "gender": "v"},
    {"id": "sage",    "name": "Fleur", "gender": "v"},
    {"id": "shimmer", "name": "Lisa",  "gender": "v"},
    {"id": "cedar",   "name": "Daan",  "gender": "m"},
    {"id": "alloy",   "name": "Bram",  "gender": "m"},
]
VOICES = [v["id"] for v in VOICE_META]


@app.get("/api/settings")
async def api_settings(req: Request):
    if not _authed(req):
        raise HTTPException(401, "Niet ingelogd")
    feats = [{"key": k, "label": lbl, "on": bot.feature_on(k)}
             for k, lbl in bot.FEATURES.items()]
    voice = bot._get_setting("voice", OPENAI_REALTIME_VOICE)
    lang = bot._get_setting("lang", "nl")
    return {"features": feats, "voice": voice, "voices": VOICES,
            "voices_meta": VOICE_META, "lang": lang}


@app.post("/api/settings")
async def api_settings_set(req: Request):
    if not _authed(req):
        raise HTTPException(401, "Niet ingelogd")
    data = await req.json()
    if "key" in data:
        bot.set_feature(data["key"], bool(data.get("on")))
    if data.get("voice") in VOICES:
        bot._set_setting("voice", data["voice"])
    if data.get("lang") in ("nl", "en"):
        bot._set_setting("lang", data["lang"])
    return {"ok": True}


@app.post("/api/logout")
async def api_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE)
    return resp


# --- OpenAI Realtime (top-of-the-bill spraak) ------------------------------

OPENAI_REALTIME_MODEL = os.environ.get("OPENAI_REALTIME_MODEL", "gpt-realtime")
OPENAI_REALTIME_VOICE = os.environ.get("OPENAI_REALTIME_VOICE", "cedar")


def _realtime_tools():
    tools = []
    for t in bot.ALL_TOOLS:
        tools.append({"type": "function", "name": t["name"],
                      "description": t.get("description", ""),
                      "parameters": t["input_schema"]})
    return tools


def _realtime_context():
    """Agenda, taken en recente geschiedenis — gedeeld door beide talen."""
    ctx = []
    try:
        _mail, _agenda = _user_context(bot.cur_uid())
        ctx.append("Je agenda (komende afspraken):\n" + _agenda)
    except Exception:
        pass
    try:
        ctx.append("Openstaande taken:\n" + bot.todos_context())
    except Exception:
        pass
    try:
        hist = bot.load_history(12)
        if hist:
            h = "\n".join((("Ko: " if m["role"] == "user" else "Able: ")
                           + m["content"][:280]) for m in hist)
            ctx.append("Recent gesprek (je geheugen):\n" + h)
    except Exception:
        pass
    return ctx


def _realtime_instructions():
    now = datetime.now(bot.LOCAL_TZ)
    lang = bot._get_setting("lang", "nl")
    _u = bot.get_user(bot.cur_uid())
    name = _u["name"] if _u else "Ko"
    if lang == "en":
        parts = [
            f"You are Able, {name}'s personal assistant. You are now talking with {name} by voice.",
            "Speak English, calm and professional, in short natural sentences — you are "
            "speaking, not writing. No bullet lists, no emoji, no long paragraphs.",
            f"{name} speaks English to you. Always assume the speech is English; never treat "
            "it as another language and never translate it.",
            "Talk like a real conversation: do NOT end every answer with a greeting or a "
            "written sign-off. Say exactly what you speak — nothing more, nothing less.",
            "Speak times and numbers naturally in English. Say '16:00' as 'four o'clock', "
            "'09:30' as 'half past nine'. Never read a time out literally digit by digit.",
            "You can really do things through your functions: draft and send email, manage "
            "the calendar (add/remove/free time), schedule Meet meetings, look up contacts "
            "by name, manage tasks, search your memory.",
            f"Only send an email AFTER {name} clearly confirms. NEVER claim you did something "
            "without actually calling the matching function.",
            f"STRICTLY FORBIDDEN: you have NO camera, no image, no video, no screen and no "
            f"location. You CANNOT see {name} — you only hear the voice through the microphone. "
            "Never say you can see them or that you have access to camera, photos or "
            "location. If asked, honestly say you only have audio.",
            f"NEVER start talking on your own. Always wait until {name} says something and only "
            "respond to what is actually said. Do not react to your own voice, to silence "
            "or to background noise. If you hear nothing or something unintelligible, stay "
            "silent.",
            f"It is now {now.strftime('%A %d-%m-%Y %H:%M')} ({bot.TIMEZONE}).",
        ]
        return "\n\n".join(parts + _realtime_context())
    parts = [
        f"Je bent Able, de persoonlijke assistent van {name}. Je praat nu met {name} via spraak.",
        "Spreek Nederlands, professioneel en rustig, in korte natuurlijke zinnen — je "
        "praat, je schrijft niet. Geen opsommingen, geen emoji's, geen lange lappen.",
        f"{name} spreekt Nederlands tegen je. Ga er altijd van uit dat wat je hoort Nederlands "
        "is; behandel het nooit als een andere taal en vertaal het nooit.",
        "Praat als in een echt gesprek: sluit NIET elk antwoord af met 'fijne dag' of een "
        "groet, en voeg geen geschreven afsluiting toe. Zeg precies wat je uitspreekt — "
        "niets meer, niets minder.",
        "Spreek tijden en getallen natuurlijk uit in het Nederlands. Zeg '16:00' als "
        "'vier uur', '09:30' als 'half tien', '13:15' als 'kwart over één'. Lees een tijd "
        "NOOIT letterlijk voor (dus niet 'zestien nul nul' of '16 dubbelepunt 00').",
        "Je kunt echt dingen doen via je functies: mail opstellen en versturen, agenda "
        "beheren (toevoegen/verwijderen/vrije tijd), meetings met Meet plannen, contacten "
        "opzoeken op naam, taken beheren, je geheugen doorzoeken.",
        f"Verstuur een mail PAS nadat {name} duidelijk bevestigt. Beweer NOOIT dat je iets "
        "gedaan hebt zonder de bijbehorende functie echt aan te roepen.",
        f"STRIKT VERBODEN: je hebt GEEN camera, geen beeld, geen video, geen scherm en "
        f"geen locatie. Je kunt {name} NIET zien — je hoort uitsluitend de stem via de "
        "microfoon. Zeg nooit dat je iemand kunt zien of dat je toegang hebt tot camera, "
        "foto's of locatie. Word je ernaar gevraagd, zeg dan eerlijk dat je alleen "
        "audio hebt.",
        f"Begin NOOIT uit jezelf te praten. Wacht altijd tot {name} iets zegt en reageer "
        "alleen op wat er daadwerkelijk gezegd wordt. Reageer niet op je eigen stem, op stilte "
        "of op achtergrondgeluid. Hoor je niets of iets onverstaanbaars, blijf dan stil.",
        f"Nu is het {now.strftime('%A %d-%m-%Y %H:%M')} ({bot.TIMEZONE}).",
    ]
    return "\n\n".join(parts + _realtime_context())


@app.get("/api/voice-usage")
async def voice_usage_get(req: Request):
    if not _authed(req):
        raise HTTPException(401, "Niet ingelogd")
    return JSONResponse(bot.voice_usage_info(_uid(req)))


@app.post("/api/voice-usage")
async def voice_usage_add(req: Request):
    if not _authed(req):
        raise HTTPException(401, "Niet ingelogd")
    data = await req.json()
    try:
        seconds = float(data.get("seconds") or 0)
    except (TypeError, ValueError):
        seconds = 0
    bot.voice_add_seconds(_uid(req), seconds)
    return JSONResponse(bot.voice_usage_info(_uid(req)))


@app.post("/api/realtime-session")
async def realtime_session(req: Request):
    if not _authed(req):
        raise HTTPException(401, "Niet ingelogd")
    # Maandbudget voor spraak (standaard €3 per gebruiker).
    if bot.voice_over_cap(_uid(req)):
        raise HTTPException(402, "Je spraaktegoed voor deze maand is op.")
    key = os.environ.get("OPENAI_API_KEY", "")
    if not key:
        raise HTTPException(400, "Geen OpenAI-sleutel ingesteld")
    lang = bot._get_setting("lang", "nl")
    payload = {"session": {
        "type": "realtime",
        "model": OPENAI_REALTIME_MODEL,
        "instructions": _realtime_instructions(),
        "tools": _realtime_tools(),
        "tool_choice": "auto",
        "audio": {
            "input": {
                # Forceer de taal zodat de spraakherkenning niet 'omslaat' naar
                # een andere taal (dat gaf rare vertalingen).
                "transcription": {"model": "whisper-1", "language": lang},
                "turn_detection": {"type": "semantic_vad", "eagerness": "high"},
            },
            "output": {"voice": bot._get_setting("voice", OPENAI_REALTIME_VOICE)},
        },
    }}
    try:
        r = requests.post("https://api.openai.com/v1/realtime/client_secrets",
                          headers={"Authorization": f"Bearer {key}",
                                   "Content-Type": "application/json"},
                          json=payload, timeout=20)
    except Exception as e:
        raise HTTPException(502, f"Realtime-fout: {e}")
    if r.status_code not in (200, 201):
        raise HTTPException(502, f"Realtime-fout {r.status_code}: {r.text[:300]}")
    d = r.json()
    return JSONResponse({"value": d.get("value"), "model": OPENAI_REALTIME_MODEL})


@app.post("/api/tool")
async def run_tool(req: Request):
    if not _authed(req):
        raise HTTPException(401, "Niet ingelogd")
    data = await req.json()
    name = data.get("name", "")
    args = data.get("arguments") or {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except Exception:
            args = {}
    accounts = bot.load_accounts() if _uid(req) == 1 else []
    abn = {a.name: a for a in accounts}
    try:
        output = bot.execute_tool(name, args, abn, [])
    except Exception as e:
        output = f"Fout bij uitvoeren: {e}"
    return {"output": output}


@app.get("/")
async def root():
    return FileResponse(os.path.join(HERE, "static", "index.html"))


@app.get("/icon.png")
async def icon():
    return FileResponse(os.path.join(HERE, "static", "icon.png"))


@app.get("/favicon.ico")
async def favicon_ico():
    return FileResponse(os.path.join(HERE, "static", "favicon.ico"))


@app.get("/favicon-{size}.png")
async def favicon_png(size: str):
    if size not in {"16", "32", "180", "192", "512"}:
        return Response(status_code=404)
    return FileResponse(os.path.join(HERE, "static", f"favicon-{size}.png"))


@app.get("/manifest.webmanifest")
async def manifest():
    return JSONResponse({
        "name": "Able", "short_name": "Able",
        "display": "standalone", "background_color": "#FFFFFF",
        "theme_color": "#FFFFFF", "start_url": "/",
        "icons": [
            {"src": "/favicon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/favicon-512.png", "sizes": "512x512", "type": "image/png"},
            {"src": "/icon.png", "sizes": "1254x1254", "type": "image/png"},
        ],
    })
