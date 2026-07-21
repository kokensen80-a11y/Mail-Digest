# Dagelijkse mail-digest

Elke ochtend een Telegram-bericht met een samenvatting van de belangrijkste nieuwe
mail uit drie postbussen (2× Gmail + 1× Hostnet), ruis eruit gefilterd. Voor mails
die om een reactie vragen wordt een concept-antwoord klaargezet in je Concepten-map.

Draait gratis in **GitHub Actions** — ook als je Mac uit staat.

---

## Wat het doet

1. Leest via IMAP de nieuwe mail (laatste 24 u) van alle accounts.
2. Filtert nieuwsbrieven/notificaties weg uit de hoofd-samenvatting.
3. Laat Claude het belangrijkste kort samenvatten in het Nederlands.
4. Stuurt de samenvatting naar je Telegram.
5. Zet concept-antwoorden klaar in de Concepten-map van het juiste account.

---

## Eenmalige setup (~20 min)

### 1. Telegram-bot maken
1. Open Telegram, zoek **@BotFather**, stuur `/newbot`, kies een naam.
   → je krijgt een **bot-token** (`123456:ABC...`).
2. Stuur je nieuwe bot zelf één berichtje ("hoi").
3. Open in je browser: `https://api.telegram.org/bot<TOKEN>/getUpdates`
   → zoek `"chat":{"id":123456789...` — dat getal is je **chat-id**.

### 2. Gmail app-wachtwoorden (voor beide Gmail-accounts)
> Werkt alleen met 2-staps-verificatie aan.
1. Ga naar <https://myaccount.google.com/apppasswords>.
2. Maak een app-wachtwoord "Mail digest" → je krijgt 16 tekens.
3. Herhaal ingelogd op het tweede Gmail-account.

### 3. Hostnet-gegevens
- Gebruiker = `info@kodesaign.com`, wachtwoord = het mailbox-wachtwoord.
- Controleer de IMAP/SMTP-server in je Hostnet-webmail-instellingen
  (defaults in de code: `imap.hostnet.nl` / `smtp.hostnet.nl`).

### 4. Claude API-key
- Haal een key bij <https://console.anthropic.com> → **API Keys**.

### 5. Naar GitHub
1. Maak een **nieuwe (privé) repo** op GitHub en push deze map erin.
2. Ga in de repo naar **Settings → Secrets and variables → Actions → New repository secret**
   en zet deze secrets (namen exact overnemen):

   | Secret | Waarde |
   |---|---|
   | `ANTHROPIC_API_KEY` | je Claude-key |
   | `TELEGRAM_BOT_TOKEN` | bot-token |
   | `TELEGRAM_CHAT_ID` | chat-id |
   | `GMAIL1_USER` / `GMAIL1_PASS` | kokensen80@gmail.com + app-wachtwoord |
   | `GMAIL2_USER` / `GMAIL2_PASS` | kensendesaignstudios@gmail.com + app-wachtwoord |
   | `HOSTNET_USER` / `HOSTNET_PASS` | info@kodesaign.com + wachtwoord |
   | `HOSTNET_IMAP_HOST` / `HOSTNET_SMTP_HOST` | Hostnet-servers |

   > 🔒 Wachtwoorden komen **alleen** in GitHub Secrets — nooit in de code of in de chat.

3. Ga naar het tabblad **Actions**, kies **Dagelijkse mail-digest → Run workflow**
   om hem meteen te testen. Daarna draait hij elke ochtend vanzelf.

---

## Lokaal testen (optioneel)
```bash
cd mail-digest
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # vul je gegevens in
export $(grep -v '^#' .env | xargs)
python digest.py
```

## Aanpassen
- **Tijdstip:** `.github/workflows/daily-digest.yml`, regel `cron`. Nu 05:30 UTC
  (= 07:30 NL zomertijd). GitHub gebruikt altijd UTC.
- **Terugkijkperiode:** secret/variabele `LOOKBACK_HOURS` (standaard 24).
- **Concepten uitzetten:** zet `CREATE_DRAFTS=false`.
- **Extra account toevoegen:** kopieer een `add(...)`-regel in `load_accounts()`.

## Fase 2 (later)
Echt "antwoord in Telegram → mail wordt verstuurd" vereist een altijd-aan bot die
naar je berichten luistert. Nu zetten we het antwoord als concept klaar; jij tikt
op verzenden in je mail-app.
