#!/usr/bin/env python3
"""
Dagelijkse mail-digest.

Leest nieuwe mail van meerdere IMAP-mailboxen (Gmail + Hostnet), filtert ruis,
laat Claude de belangrijkste berichten samenvatten in het Nederlands, stuurt de
samenvatting naar Telegram, en zet voor mails die om een reactie vragen een
concept-antwoord klaar in de Concepten-map van het juiste account.

Alle configuratie komt uit environment variables (GitHub Secrets). Er staan
nooit wachtwoorden in dit bestand.
"""

from __future__ import annotations

import email
import imaplib
import json
import os
import smtplib
import ssl
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import parsedate_to_datetime

import anthropic
import requests

# ---------------------------------------------------------------------------
# Configuratie
# ---------------------------------------------------------------------------

LOOKBACK_HOURS = int(os.getenv("LOOKBACK_HOURS", "24"))
MAX_BODY_CHARS = 1200          # hoeveel tekst per mail we aan Claude geven
MAX_MAILS_TO_MODEL = 60        # veiligheidslimiet
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5")
CREATE_DRAFTS = os.getenv("CREATE_DRAFTS", "true").lower() == "true"

# Afzenders/patronen die we als ruis behandelen (tellen nog wel mee als "overig")
NOISE_HINTS = (
    "no-reply", "noreply", "no_reply", "donotreply", "do-not-reply",
    "nieuwsbrief", "newsletter", "notifications", "notification",
    "mailer-daemon", "postmaster",
)


@dataclass
class Account:
    """Eén mailbox met IMAP-inleg en (optioneel) SMTP voor concepten/verzenden."""
    name: str            # leesbare naam, bijv. "Gmail privé"
    user: str
    password: str
    imap_host: str
    imap_port: int = 993
    smtp_host: str = ""
    smtp_port: int = 465
    drafts_folder: str = "Drafts"


@dataclass
class Mail:
    account: str
    sender: str
    subject: str
    date: datetime | None
    body: str
    is_noise: bool = False


# ---------------------------------------------------------------------------
# Accounts laden
# ---------------------------------------------------------------------------

def load_accounts() -> list[Account]:
    """Bouwt de accountlijst uit environment variables.

    Verwacht per account een set variabelen. Ontbrekende accounts worden
    stilletjes overgeslagen, zodat je klein kunt beginnen (bijv. eerst 1 Gmail).
    """
    accounts: list[Account] = []

    def add(prefix: str, default_name: str, imap_host: str, smtp_host: str,
            drafts: str = "Drafts") -> None:
        user = os.getenv(f"{prefix}_USER")
        pw = os.getenv(f"{prefix}_PASS")
        if not user or not pw:
            return
        accounts.append(Account(
            name=os.getenv(f"{prefix}_NAME", default_name),
            user=user,
            password=pw,
            imap_host=os.getenv(f"{prefix}_IMAP_HOST", imap_host),
            imap_port=int(os.getenv(f"{prefix}_IMAP_PORT", "993")),
            smtp_host=os.getenv(f"{prefix}_SMTP_HOST", smtp_host),
            smtp_port=int(os.getenv(f"{prefix}_SMTP_PORT", "465")),
            drafts_folder=os.getenv(f"{prefix}_DRAFTS", drafts),
        ))

    # Gmail gebruikt "[Gmail]/Drafts" als conceptenmap.
    add("GMAIL1", "Gmail privé", "imap.gmail.com", "smtp.gmail.com", "[Gmail]/Drafts")
    add("GMAIL2", "Gmail studio", "imap.gmail.com", "smtp.gmail.com", "[Gmail]/Drafts")
    # Hostnet-hosts staan hier als default; controleer ze in je Hostnet-mailinstellingen.
    add("HOSTNET", "Kodesaign", "imap.hostnet.nl", "smtp.hostnet.nl", "Drafts")

    return accounts


# ---------------------------------------------------------------------------
# IMAP: mail ophalen
# ---------------------------------------------------------------------------

def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _extract_body(msg: email.message.Message) -> str:
    """Haal platte tekst uit een e-mail (val terug op gestripte HTML)."""
    def payload_to_text(part) -> str:
        try:
            charset = part.get_content_charset() or "utf-8"
            return part.get_payload(decode=True).decode(charset, errors="replace")
        except Exception:
            return ""

    if msg.is_multipart():
        # Eerst text/plain zoeken
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and \
                    "attachment" not in str(part.get("Content-Disposition", "")):
                text = payload_to_text(part)
                if text.strip():
                    return text
        # Anders eerste text/html, ruw gestript
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                return _strip_html(payload_to_text(part))
        return ""
    else:
        text = payload_to_text(msg)
        if msg.get_content_type() == "text/html":
            return _strip_html(text)
        return text


def _strip_html(html: str) -> str:
    import re
    html = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", html)
    html = re.sub(r"(?s)<[^>]+>", " ", html)
    html = re.sub(r"&nbsp;", " ", html)
    html = re.sub(r"\s+", " ", html)
    return html.strip()


def fetch_recent(account: Account, since: datetime) -> list[Mail]:
    """Haal berichten op die na `since` zijn binnengekomen."""
    mails: list[Mail] = []
    ctx = ssl.create_default_context()
    imap = imaplib.IMAP4_SSL(account.imap_host, account.imap_port, ssl_context=ctx)
    try:
        imap.login(account.user, account.password)
        imap.select("INBOX", readonly=True)  # readonly: we markeren niks als gelezen
        date_str = since.strftime("%d-%b-%Y")  # IMAP SINCE is op dagniveau
        status, data = imap.search(None, f'(SINCE "{date_str}")')
        if status != "OK":
            return mails
        ids = data[0].split()
        for num in ids:
            status, msg_data = imap.fetch(num, "(RFC822)")
            if status != "OK" or not msg_data or not msg_data[0]:
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            try:
                mdate = parsedate_to_datetime(msg.get("Date"))
                if mdate and mdate.tzinfo is None:
                    mdate = mdate.replace(tzinfo=timezone.utc)
            except Exception:
                mdate = None
            # Filter op precieze tijd (IMAP SINCE is grover dan uren)
            if mdate and mdate < since:
                continue
            sender = _decode(msg.get("From"))
            subject = _decode(msg.get("Subject"))
            body = _extract_body(msg)[:MAX_BODY_CHARS]
            is_noise = _looks_like_noise(sender, msg)
            mails.append(Mail(
                account=account.name, sender=sender, subject=subject,
                date=mdate, body=body, is_noise=is_noise,
            ))
    finally:
        try:
            imap.logout()
        except Exception:
            pass
    return mails


def _looks_like_noise(sender: str, msg: email.message.Message) -> bool:
    low = sender.lower()
    if any(h in low for h in NOISE_HINTS):
        return True
    # Bulk/nieuwsbrief-headers
    if msg.get("List-Unsubscribe") or msg.get("List-Id"):
        return True
    if str(msg.get("Precedence", "")).lower() in ("bulk", "list", "junk"):
        return True
    return False


# ---------------------------------------------------------------------------
# Claude: samenvatten + concept-antwoorden
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """Je bent een persoonlijke mail-assistent. Je krijgt de nieuwe \
e-mails van de afgelopen 24 uur van meerdere postbussen. Vat het belangrijkste \
kort en zakelijk samen in het Nederlands.

Regels:
- Filter ruis (nieuwsbrieven, notificaties, marketing) weg uit de hoofd-samenvatting; \
noem die hooguit als één tellend regeltje ("+ 7 nieuwsbrieven/notificaties").
- Groepeer de belangrijke mails en zet per item: afzender, onderwerp, en in één zin \
waar het over gaat.
- Markeer duidelijk welke mails om een reactie of actie vragen.
- Voor elke mail die om een antwoord vraagt, schrijf een kort, professioneel \
concept-antwoord in het Nederlands.

Antwoord UITSLUITEND met geldige JSON in dit schema:
{
  "summary_markdown": "<de samenvatting als korte Telegram-markdown tekst>",
  "drafts": [
    {"account": "<accountnaam>", "to": "<e-mailadres afzender>", \
"subject": "<Re: ...>", "body": "<concept-antwoord>"}
  ]
}
Geen tekst buiten de JSON."""


def summarize(mails: list[Mail]) -> dict:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    # Belangrijke mails eerst, ruis onderaan, en afkappen op limiet.
    mails_sorted = sorted(mails, key=lambda m: (m.is_noise, m.date or datetime.min.replace(tzinfo=timezone.utc)))
    trimmed = mails_sorted[:MAX_MAILS_TO_MODEL]

    lines = []
    for i, m in enumerate(trimmed, 1):
        tag = " [RUIS]" if m.is_noise else ""
        when = m.date.strftime("%d-%m %H:%M") if m.date else "?"
        lines.append(
            f"--- Mail {i}{tag} ---\n"
            f"Account: {m.account}\nVan: {m.sender}\nDatum: {when}\n"
            f"Onderwerp: {m.subject}\nTekst: {m.body}\n"
        )
    noise_count = sum(1 for m in mails if m.is_noise)
    payload = (
        f"Aantal mails totaal: {len(mails)} (waarvan {noise_count} vermoedelijk ruis).\n\n"
        + "\n".join(lines)
    )

    resp = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=2000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": payload}],
    )
    text = "".join(block.text for block in resp.content if block.type == "text").strip()
    # Claude kan per ongeluk ```json wrappers meesturen; strip die.
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"summary_markdown": text, "drafts": []}


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def send_telegram(text: str) -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    # Telegram-limiet is 4096 tekens per bericht; splits netjes.
    for chunk in _chunk(text, 4000):
        r = requests.post(url, data={
            "chat_id": chat_id,
            "text": chunk,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }, timeout=30)
        if not r.ok:
            # Val terug op platte tekst als markdown de parser breekt
            requests.post(url, data={"chat_id": chat_id, "text": chunk}, timeout=30)


def _chunk(text: str, size: int):
    lines = text.split("\n")
    buf = ""
    for line in lines:
        if len(buf) + len(line) + 1 > size:
            if buf:
                yield buf
            buf = line
        else:
            buf = f"{buf}\n{line}" if buf else line
    if buf:
        yield buf


# ---------------------------------------------------------------------------
# Concept-antwoorden opslaan in de Concepten-map (IMAP APPEND)
# ---------------------------------------------------------------------------

def save_draft(account: Account, to_addr: str, subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["From"] = account.user
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)

    ctx = ssl.create_default_context()
    imap = imaplib.IMAP4_SSL(account.imap_host, account.imap_port, ssl_context=ctx)
    try:
        imap.login(account.user, account.password)
        imap.append(account.drafts_folder, "(\\Draft)",
                    imaplib.Time2Internaldate(datetime.now().timestamp()),
                    msg.as_bytes())
    finally:
        try:
            imap.logout()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    accounts = load_accounts()
    if not accounts:
        print("Geen accounts geconfigureerd. Zet minstens GMAIL1_USER/PASS.", file=sys.stderr)
        send_safe("⚠️ Mail-digest: geen accounts geconfigureerd.")
        return 1

    since = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    all_mails: list[Mail] = []
    errors: list[str] = []

    for acc in accounts:
        try:
            fetched = fetch_recent(acc, since)
            all_mails.extend(fetched)
            print(f"{acc.name}: {len(fetched)} mails")
        except Exception as e:
            errors.append(f"{acc.name}: {e}")
            print(f"FOUT bij {acc.name}: {e}", file=sys.stderr)

    header = f"📬 *Mail-digest {datetime.now().strftime('%d-%m-%Y')}*\n"

    if not all_mails:
        msg = header + "\nGeen nieuwe mail in de afgelopen 24 uur. 🎉"
        if errors:
            msg += "\n\n⚠️ Problemen:\n" + "\n".join(errors)
        send_telegram(msg)
        return 0

    result = summarize(all_mails)
    summary = result.get("summary_markdown", "(geen samenvatting)")
    telegram_msg = header + "\n" + summary
    if errors:
        telegram_msg += "\n\n⚠️ Kon niet bij: " + "; ".join(errors)

    send_telegram(telegram_msg)

    # Concept-antwoorden klaarzetten
    drafts = result.get("drafts", []) if CREATE_DRAFTS else []
    saved = 0
    by_name = {a.name: a for a in accounts}
    for d in drafts:
        acc = by_name.get(d.get("account"))
        if not acc or not d.get("to"):
            continue
        try:
            save_draft(acc, d["to"], d.get("subject", "Re:"), d.get("body", ""))
            saved += 1
        except Exception as e:
            print(f"Kon concept niet opslaan ({acc.name}): {e}", file=sys.stderr)

    if saved:
        send_telegram(f"✍️ {saved} concept-antwoord(en) klaargezet in je Concepten-map.")

    return 0


def send_safe(text: str) -> None:
    try:
        send_telegram(text)
    except Exception as e:
        print(f"Telegram mislukt: {e}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
