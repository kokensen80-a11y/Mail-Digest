#!/usr/bin/env python3
"""
Luisterende Truus — verwerkt je Telegram-berichten en voert taken uit.

Draait periodiek (GitHub Actions). Elke keer:
  1. Haalt nieuwe Telegram-berichten op (getUpdates) van Ko.
  2. Laat Claude begrijpen wat Ko vraagt, met je recente mail als context.
  3. Voert het uit: een vraag beantwoorden, je mail doorzoeken/samenvatten,
     of een concept-antwoord klaarzetten in het juiste account.
  4. Antwoordt terug in Telegram.

Herbruikt de bouwstenen uit digest.py. Geen wachtwoorden in dit bestand —
alles komt uit environment variables (GitHub Secrets).
"""

from __future__ import annotations

import json
import os
import sys
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
MAX_CONTEXT_MAILS = 40


# ---------------------------------------------------------------------------
# Telegram in- en uitvoer
# ---------------------------------------------------------------------------

def _api(method: str) -> str:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    return f"https://api.telegram.org/bot{token}/{method}"


def get_new_messages() -> tuple[list[str], int | None]:
    """Haal nieuwe tekstberichten van Ko op.

    Geeft (berichten, hoogste_update_id) terug. We bevestigen de updates pas
    na verwerking, zodat er niks verloren gaat als een run halverwege faalt.
    """
    chat_id = str(os.environ["TELEGRAM_CHAT_ID"])
    r = requests.get(_api("getUpdates"), params={"timeout": 0}, timeout=30)
    r.raise_for_status()
    updates = r.json().get("result", [])

    messages: list[str] = []
    last_id: int | None = None
    for u in updates:
        last_id = u["update_id"]
        msg = u.get("message") or u.get("edited_message")
        if not msg:
            continue
        # Alleen berichten van Ko zelf verwerken.
        if str(msg.get("chat", {}).get("id")) != chat_id:
            continue
        text = msg.get("text")
        if text:
            messages.append(text.strip())
    return messages, last_id


def confirm_updates(last_id: int) -> None:
    """Bevestig verwerkte updates zodat ze niet opnieuw langskomen."""
    requests.get(_api("getUpdates"),
                 params={"offset": last_id + 1, "timeout": 0}, timeout=30)


# ---------------------------------------------------------------------------
# Mailcontext ophalen (zodat Truus 'de mail van Jan' kan vinden)
# ---------------------------------------------------------------------------

def gather_mail_context() -> list[Mail]:
    accounts = load_accounts()
    since = datetime.now(timezone.utc) - timedelta(hours=CONTEXT_LOOKBACK_HOURS)
    mails: list[Mail] = []
    for acc in accounts:
        try:
            mails.extend(fetch_recent(acc, since))
        except Exception as e:
            print(f"Kon mail niet ophalen bij {acc.name}: {e}", file=sys.stderr)
    # Nieuwste eerst, afkappen.
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
# Claude: begrijp de opdracht en bepaal de actie
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """Je bent Truus, de persoonlijke assistent van Ko. Ko appt je via \
Telegram met vragen en opdrachten. Je bent warm, betrokken en to-the-point, in het \
Nederlands. Je krijgt Ko's recente mail als context, zodat je kunt verwijzen naar \
concrete berichten ("de mail van Jan").

Je kunt drie dingen doen, en je geeft ALTIJD een kort, menselijk antwoord terug in \
'reply' (dat stuur ik naar Telegram):
1. Een vraag beantwoorden of iets uitzoeken/samenvatten uit de mailcontext.
2. Een concept-antwoord of nieuwe mail klaarzetten (zet 'm in 'drafts'). Verstuur \
NOOIT zelf een mail — je zet een concept klaar en Ko verstuurt het met één tik. \
Zeg in 'reply' duidelijk dat het als concept klaarstaat en in welk account.
3. Iets dat nog niet kan (bijv. een afspraak in de agenda zetten): leg vriendelijk \
uit dat die koppeling nog niet actief is.

ROUTERING van concepten: is het zakelijk (klant, opdracht, offerte, leverancier, \
factuur, Kodesaign)? Zet 'account' dan op "Kodesaign" (verstuurt vanuit \
info@kodesaign.com). Privé? Gebruik "Gmail privé" of "Gmail studio", afhankelijk van \
waar de oorspronkelijke mail binnenkwam.

Antwoord UITSLUITEND met geldige JSON:
{
  "reply": "<kort Telegram-antwoord aan Ko>",
  "drafts": [
    {"account": "Kodesaign | Gmail privé | Gmail studio", "to": "<e-mailadres>", \
"subject": "<onderwerp>", "body": "<concept-tekst>"}
  ]
}
Laat 'drafts' leeg ([]) als er niks klaargezet hoeft te worden. Geen tekst buiten de JSON."""


def handle(message: str, context: str) -> dict:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    payload = (
        f"Ko's bericht:\n{message}\n\n"
        f"--- Recente mail als context ---\n{context}"
    )
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


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    try:
        messages, last_id = get_new_messages()
    except Exception as e:
        print(f"Kon Telegram-updates niet ophalen: {e}", file=sys.stderr)
        return 1

    if not messages:
        return 0  # niks te doen, stil afsluiten

    # Mailcontext één keer ophalen voor alle berichten in deze ronde.
    context = _format_context(gather_mail_context())

    accounts = {a.name: a for a in load_accounts()}
    for message in messages:
        result = handle(message, context)
        reply = result.get("reply", "").strip() or "Genoteerd, Ko."

        saved = []
        for d in result.get("drafts", []):
            acc = accounts.get(d.get("account"))
            if not acc or not d.get("to"):
                continue
            try:
                save_draft(acc, d["to"], d.get("subject", "Re:"), d.get("body", ""))
                saved.append(acc.user)
            except Exception as e:
                print(f"Concept opslaan mislukt ({d.get('account')}): {e}",
                      file=sys.stderr)
                reply += f"\n\n⚠️ Het concept opslaan lukte niet ({acc.user})."

        if saved:
            waar = ", ".join(dict.fromkeys(saved))
            reply += f"\n\n✍️ Concept klaargezet in: {waar}."

        send_telegram(reply)

    # Pas ná verwerking bevestigen.
    if last_id is not None:
        try:
            confirm_updates(last_id)
        except Exception as e:
            print(f"Kon updates niet bevestigen: {e}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
