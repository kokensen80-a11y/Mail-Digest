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
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

import anthropic
import requests

from digest import (
    Mail,
    fetch_recent,
    load_accounts,
    save_draft,
    send_telegram,
)

CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5")
CONTEXT_LOOKBACK_HOURS = int(os.getenv("BOT_CONTEXT_HOURS", "120"))  # 5 dagen
CONTEXT_MAX_AGE_S = 180        # mailcontext maximaal 3 min hergebruiken
MAX_CONTEXT_MAILS = 40
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


def poll(offset: int | None, skip_before_ts: float | None) -> tuple[list[str], int | None]:
    """Long-poll Telegram. Geeft (nieuwe berichten van Ko, nieuwe offset) terug."""
    chat_id = str(os.environ["TELEGRAM_CHAT_ID"])
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
        if str(msg.get("chat", {}).get("id")) != chat_id:
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
            f"    Onderwerp: {m.subject}\n    Tekst: {m.body[:600]}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Claude: begrijp de opdracht
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """Je bent Truus, de persoonlijke assistent van Ko. Ko appt je via \
Telegram met vragen en opdrachten. Je bent warm, betrokken en to-the-point, in het \
Nederlands. Je krijgt Ko's recente mail als context, zodat je kunt verwijzen naar \
concrete berichten ("de mail van Jan"). Reageer natuurlijk, alsof je samen aan het \
appen bent.

Je kunt:
1. Een vraag beantwoorden of iets uitzoeken/samenvatten uit de mailcontext.
2. Een concept-antwoord of nieuwe mail klaarzetten (in 'drafts'). Verstuur NOOIT zelf \
een mail — je zet een concept klaar en Ko verstuurt het met één tik. Zeg in 'reply' \
duidelijk dat het als concept klaarstaat en in welk account.
3. Iets dat nog niet kan (bijv. een afspraak in de agenda zetten): leg vriendelijk uit \
dat die koppeling nog niet actief is.

ROUTERING van concepten: is het zakelijk (klant, opdracht, offerte, leverancier, \
factuur, Kodesaign)? Zet 'account' dan op "Kodesaign" (verstuurt vanuit \
info@kodesaign.com). Privé? Gebruik "Gmail privé" of "Gmail studio", afhankelijk van \
waar de oorspronkelijke mail binnenkwam.

Antwoord UITSLUITEND met geldige JSON:
{
  "reply": "<kort, menselijk Telegram-antwoord aan Ko>",
  "drafts": [
    {"account": "Kodesaign | Gmail privé | Gmail studio", "to": "<e-mailadres>", \
"subject": "<onderwerp>", "body": "<concept-tekst>"}
  ]
}
Laat 'drafts' leeg ([]) als er niks klaargezet hoeft te worden. Geen tekst buiten de JSON."""


def handle(client: anthropic.Anthropic, message: str, context: str) -> dict:
    payload = f"Ko's bericht:\n{message}\n\n--- Recente mail als context ---\n{context}"
    resp = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1500,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": payload}],
    )
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"reply": text or "Sorry Ko, dat ging even mis. Probeer 't nog eens.",
                "drafts": []}


def process(client, accounts_by_name, message: str, context: str) -> None:
    send_typing()  # laat meteen "Truus is aan het typen…" zien
    result = handle(client, message, context)
    reply = (result.get("reply") or "").strip() or "Genoteerd, Ko."

    saved = []
    for d in result.get("drafts", []):
        acc = accounts_by_name.get(d.get("account"))
        if not acc or not d.get("to"):
            continue
        try:
            save_draft(acc, d["to"], d.get("subject", "Re:"), d.get("body", ""))
            saved.append(acc.user)
        except Exception as e:
            print(f"Concept opslaan mislukt ({d.get('account')}): {e}", file=sys.stderr)
            reply += f"\n\n⚠️ Het concept opslaan lukte niet ({d.get('account')})."
    if saved:
        reply += f"\n\n✍️ Concept klaargezet in: {', '.join(dict.fromkeys(saved))}."

    send_telegram(reply)


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

    start = time.time()
    offset: int | None = None
    skip_before = time.time() - STARTUP_SKIP_OLDER_S  # bij start oude appjes negeren
    context = "(nog niet geladen)"
    context_ts = 0.0

    print(f"Truus luistert live... (debug={DEBUG}, budget={SESSION_BUDGET_S}s)",
          file=sys.stderr)
    while time.time() - start < SESSION_BUDGET_S:
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
            if time.time() - context_ts > CONTEXT_MAX_AGE_S:
                context = _format_context(gather_mail_context(accounts))
                context_ts = time.time()
            try:
                process(client, accounts_by_name, text, context)
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
