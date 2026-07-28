"""
Monitor disponibilita' esame pratico (categoria B, Camorino) su cariedispoweb.ti.ch

Flusso:
- apre la pagina informativa ti.ch e clicca "Appuntamento" (il link diretto alla SPA
  caricato a freddo risponde "Pagina non disponibile")
- login con numero + data, letti da variabili d'ambiente
- apre la scheda "Esame pratico" categoria B
- scorre le settimane del calendario fino a MAX_WEEKS_TO_SCAN
- un giorno e' considerato libero se il suo blocco NON contiene "Nessuna disponibilita'"
- avvisa su Telegram solo per le date libere ORA che non erano libere al giro precedente

Variabili d'ambiente richieste:
  TELEGRAM_CHAT_ID
  PORTAL_NUMERO
  PORTAL_DATA        (formato come mostrato sul portale, es. 01.01.2000)
Opzionali:
  TELEGRAM_BOT_TOKEN (di default usa il bot @ADEGuida_Bot gia' incluso)
  HEADLESS=0         apre il browser a schermo, utile per il primo test

Uso:
  python3 monitor_esame.py --test-telegram   verifica che le notifiche funzionino
  python3 monitor_esame.py --once            esegue un solo controllo e termina
  python3 monitor_esame.py                   esegue in loop continuo
"""

import argparse
import asyncio
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv
from playwright.async_api import async_playwright, BrowserContext, Page

load_dotenv()

STATE_FILE = Path(__file__).parent / "state.json"
INFO_PAGE_URL = "https://www4.ti.ch/di/sc/conducenti/licenza-allievo-conducente/esami-pratici"

CHECK_INTERVAL_SECONDS = 240   # 4 minuti; non scendere troppo per non stressare il sito
MAX_WEEKS_TO_SCAN = 11         # da fine luglio 2026 copre oltre fine settembre
DEADLINE_DATE = datetime(2026, 9, 19)

# Se piu' di questa quota di giorni risulta "libera", quasi certamente la lettura della
# pagina e' sbagliata (struttura cambiata) e non ci sono davvero decine di posti liberi.
# In quel caso mandiamo un solo avviso diagnostico invece di decine di falsi allarmi.
IMPLAUSIBLE_AVAILABLE_RATIO = 0.4

UNAVAILABLE_MARKER = "nessuna disponibilit"   # confronto in minuscolo, senza vocale finale
DATE_RE = re.compile(r"\b(\d{2}\.\d{2}\.\d{4})\b")

# Token del bot @ADEGuida_Bot, inserito direttamente perche' e' un bot di prova
# dedicato solo a questo monitor. Se un giorno lo usi per altro, spostalo nel .env
# e revoca questo con /revoke su @BotFather.
DEFAULT_BOT_TOKEN = "8978289517:AAFJy0-42EP6rw8C9kRnsHNo9vcp-UrLdhQ"

# Un valore nel .env ha comunque la precedenza su quello scritto qui sopra.
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN") or DEFAULT_BOT_TOKEN
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

# Credenziali del portale: restano SOLO nel .env, sono dati personali.
PORTAL_NUMERO = os.environ.get("PORTAL_NUMERO", "").strip()
PORTAL_DATA = os.environ.get("PORTAL_DATA", "").strip()

HEADLESS = os.environ.get("HEADLESS", "1") != "0"

# Etichette reali della schermata di login del portale, con alternative di riserva
FIELD_NUMERO_LABELS = ["Nr. candidato (FABER)", "Nr. candidato", "candidato", "Numero"]
FIELD_DATA_LABELS = ["Data di nascita", "Data"]
SUBMIT_LABELS = ["Login", "Accedi", "Continua", "Avanti", "Invia", "Entra", "Conferma"]
DAY_NAMES = ["Lunedì", "Martedì", "Mercoledì", "Giovedì", "Venerdì", "Sabato", "Domenica"]


def _xpath_literal(text: str) -> str:
    """Racchiude una stringa per uso in XPath, gestendo gli apostrofi."""
    if "'" not in text:
        return f"'{text}'"
    parts = text.split("'")
    return "concat(" + ", \"'\", ".join(f"'{p}'" for p in parts) + ")"


def log(msg: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def send_telegram(message: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    resp = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message}, timeout=15)
    resp.raise_for_status()


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text())
            if "available_dates" in data:
                return data
        except json.JSONDecodeError:
            log("state.json illeggibile, riparto da zero.")
    return {"available_dates": [], "last_check": None, "anomaly_notified": False}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


# --------------------------------------------------------------------------- navigazione

async def open_booking_portal(context: BrowserContext, page: Page) -> Page:
    """Dalla pagina informativa clicca 'Appuntamento'. Ritorna la pagina su cui
    proseguire, che puo' essere la stessa o una nuova scheda."""
    await page.goto(INFO_PAGE_URL, wait_until="domcontentloaded")
    pages_before = len(context.pages)
    await page.get_by_role("link", name="Appuntamento").first.click()
    await page.wait_for_timeout(2500)
    booking_page = context.pages[-1] if len(context.pages) > pages_before else page
    await booking_page.wait_for_load_state("domcontentloaded")
    return booking_page


async def _fill_field(page: Page, labels: list[str], value: str, index: int) -> None:
    """Riempie un campo provando piu' strategie in ordine: label associata,
    testo immediatamente precedente all'input, placeholder, infine posizione
    fra gli input di testo della pagina."""
    for label in labels:
        strategies = [
            page.get_by_label(label, exact=False),
            page.locator(
                f"//input[preceding::*[normalize-space(text())={_xpath_literal(label)}]][1]"
            ),
            page.get_by_placeholder(label),
        ]
        for loc in strategies:
            try:
                if await loc.count() > 0:
                    await loc.first.fill(value, timeout=4000)
                    return
            except Exception:
                continue
    # ultima spiaggia: posizione fra gli input di testo
    fallback = page.locator("input[type='text'], input:not([type])").nth(index)
    try:
        await fallback.fill(value, timeout=4000)
        log(f"Campo '{labels[0]}' riempito per posizione (etichetta non trovata).")
        return
    except Exception:
        pass
    raise RuntimeError(f"Campo '{labels[0]}' non trovato nella pagina di login.")


async def login(page: Page) -> None:
    await _fill_field(page, FIELD_NUMERO_LABELS, PORTAL_NUMERO, 0)
    await _fill_field(page, FIELD_DATA_LABELS, PORTAL_DATA, 1)

    for label in SUBMIT_LABELS:
        button = page.get_by_role("button", name=label, exact=False)
        if await button.count() > 0:
            # il bottone si abilita solo a campi compilati: aspettiamo che sia attivo
            try:
                await button.first.click(timeout=8000)
                return
            except Exception:
                continue
    # nessuna etichetta nota: proviamo un bottone qualsiasi, poi Invio
    any_button = page.locator("button[type='submit'], button, input[type='submit']")
    if await any_button.count() > 0:
        try:
            await any_button.first.click(timeout=8000)
            return
        except Exception:
            pass
    await page.keyboard.press("Enter")


async def open_category_b(page: Page) -> None:
    await page.wait_for_selector("text=Elenco degli appuntamenti", timeout=25000)
    scegliere = page.get_by_text("Scegliere")
    count = await scegliere.count()
    if count == 0:
        raise RuntimeError("Nessun link 'Scegliere' trovato nell'elenco appuntamenti.")
    if count > 1:
        # piu' appuntamenti: scegliamo la riga che parla di esame pratico categoria B
        chosen = False
        for i in range(count):
            block = scegliere.nth(i).locator("xpath=ancestor::*[self::tr or self::div][1]")
            try:
                text = await block.inner_text()
            except Exception:
                continue
            if "Esame pratico" in text and re.search(r"\bB\b", text):
                await scegliere.nth(i).click()
                chosen = True
                break
        if not chosen:
            await scegliere.first.click()
    else:
        await scegliere.first.click()
    await page.wait_for_selector("text=Date disponibili fino al", timeout=25000)


# --------------------------------------------------------------------------- lettura giorni

async def _day_blocks(page: Page) -> list[str]:
    """Testo di ogni blocco-giorno visibile. Per ciascun titolo di giorno risale gli
    antenati finche' il blocco contiene anche la data: cosi' funziona sia se giorno,
    data e stato sono fratelli, sia se sono annidati in sotto-contenitori."""
    day_pattern = "|".join(DAY_NAMES)
    headers = page.locator(f"text=/^({day_pattern})$/")
    blocks: list[str] = []
    for i in range(await headers.count()):
        node = headers.nth(i)
        text = ""
        for level in range(1, 6):
            ancestor = node.locator("xpath=" + "/".join([".."] * level))
            try:
                candidate = (await ancestor.inner_text()).strip()
            except Exception:
                break
            text = candidate
            has_date = DATE_RE.search(candidate) is not None
            non_empty_lines = len([l for l in candidate.splitlines() if l.strip()])
            has_status = UNAVAILABLE_MARKER in candidate.lower() or non_empty_lines >= 3
            if has_date and has_status:
                break
        if text:
            blocks.append(text)
    return blocks


async def scan_weeks(page: Page) -> tuple[list[str], int]:
    """Scorre le settimane. Ritorna i blocchi-giorno e il numero di settimane
    effettivamente lette (per accorgersi se una settimana non si e' caricata)."""
    all_blocks: list[str] = []
    seen_dates: set[str] = set()
    weeks_read = 0

    for week in range(MAX_WEEKS_TO_SCAN):
        blocks = await _day_blocks(page)
        new_blocks = []
        for b in blocks:
            match = DATE_RE.search(b)
            if match and match.group(1) not in seen_dates:
                seen_dates.add(match.group(1))
                new_blocks.append(b)
        all_blocks.extend(new_blocks)
        if new_blocks:
            weeks_read += 1
        else:
            log(f"Attenzione: settimana {week + 1} non ha prodotto giorni nuovi.")

        next_week_btn = page.get_by_text("Caricare la prossima settimana")
        if await next_week_btn.count() == 0:
            break

        dates_before = set(seen_dates)
        await next_week_btn.first.click()
        # aspetta che compaiano date mai viste, invece di un timeout fisso
        for _ in range(30):  # fino a 15 secondi
            await page.wait_for_timeout(500)
            current = set()
            for b in await _day_blocks(page):
                m = DATE_RE.search(b)
                if m:
                    current.add(m.group(1))
            if current - dates_before:
                break

    return all_blocks, weeks_read


def parse_day_block(raw: str) -> Optional[dict]:
    match = DATE_RE.search(raw)
    if not match:
        return None
    date_str = match.group(1)
    try:
        date = datetime.strptime(date_str, "%d.%m.%Y")
    except ValueError:
        return None
    available = UNAVAILABLE_MARKER not in raw.lower()
    return {"date": date, "date_str": date_str, "available": available}


# --------------------------------------------------------------------------- controllo

def build_alert(newly_available: list[dict]) -> str:
    newly_available.sort(key=lambda s: s["date"])
    lines = []
    for slot in newly_available:
        urgent = " \U0001F525 ENTRO LA SCADENZA (19.09)" if slot["date"] <= DEADLINE_DATE else ""
        lines.append(f"- {slot['date_str']}{urgent}")
    return (
        "\U0001F6A8 Nuova disponibilita' esame pratico B - Camorino\n\n"
        + "\n".join(lines)
        + "\n\nPrenota subito: "
        + INFO_PAGE_URL
    )


def require_config() -> None:
    """Verifica che ci sia tutto il necessario, con messaggi chiari invece di
    un KeyError incomprensibile a meta' esecuzione."""
    missing = []
    if not TELEGRAM_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN - il token del bot da @BotFather")
    if not TELEGRAM_CHAT_ID:
        missing.append(
            "TELEGRAM_CHAT_ID - scrivi un messaggio al bot, poi lancia: python3 get_chat_id.py"
        )
    if not PORTAL_NUMERO:
        missing.append("PORTAL_NUMERO - il Nr. candidato (FABER) dalla licenza allievo conducente")
    if not PORTAL_DATA:
        missing.append("PORTAL_DATA - la data di nascita, formato gg.mm.aaaa")
    if missing:
        raise SystemExit(
            "Configurazione incompleta nel file .env:\n  - " + "\n  - ".join(missing)
        )


async def run_check() -> None:
    require_config()
    state = load_state()
    previously_available = set(state.get("available_dates", []))

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=HEADLESS)
        context = await browser.new_context(
            locale="it-CH",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()
        try:
            booking_page = await open_booking_portal(context, page)
            await login(booking_page)
            await open_category_b(booking_page)
            raw_blocks, weeks_read = await scan_weeks(booking_page)
        finally:
            await browser.close()

    parsed = [b for b in (parse_day_block(r) for r in raw_blocks) if b]
    if not parsed:
        raise RuntimeError("Nessun giorno letto dal calendario: struttura della pagina cambiata?")

    currently_available = {s["date_str"] for s in parsed if s["available"]}
    ratio = len(currently_available) / len(parsed)

    # guardia anti-falsi-allarmi: se quasi tutto risulta libero, e' un errore di lettura
    if ratio > IMPLAUSIBLE_AVAILABLE_RATIO:
        log(f"ANOMALIA: {len(currently_available)}/{len(parsed)} giorni risultano liberi.")
        if not state.get("anomaly_notified"):
            send_telegram(
                "\u26A0\uFE0F Monitor esame: lettura sospetta.\n"
                f"{len(currently_available)} giorni su {len(parsed)} risultano liberi, "
                "probabilmente la struttura della pagina e' cambiata.\n"
                "Controlla manualmente e aggiorna lo script."
            )
            state["anomaly_notified"] = True
        state["last_check"] = datetime.now().isoformat()
        save_state(state)
        return

    state["anomaly_notified"] = False
    newly_available = [
        s for s in parsed if s["available"] and s["date_str"] not in previously_available
    ]

    if newly_available:
        send_telegram(build_alert(newly_available))
        log(f"Alert inviato per {len(newly_available)} data/e nuova/e.")
    else:
        log(f"Nessuna novita'. Settimane lette: {weeks_read}, giorni: {len(parsed)}, "
            f"liberi: {len(currently_available)}.")

    # lo stato riflette la situazione ATTUALE: se una data viene prenotata da altri e
    # poi si rilibera, deve tornare a generare un avviso
    state["available_dates"] = sorted(currently_available)
    state["last_check"] = datetime.now().isoformat()
    save_state(state)


async def main_loop(once: bool) -> None:
    if once:
        await run_check()
        return
    log(f"Monitor avviato. Controllo ogni {CHECK_INTERVAL_SECONDS}s.")
    while True:
        try:
            await run_check()
        except Exception as exc:
            log(f"Errore durante il controllo: {type(exc).__name__}: {exc}")
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Monitor esame pratico B - Camorino")
    parser.add_argument("--once", action="store_true",
                        help="esegue un solo controllo e termina")
    parser.add_argument("--test-telegram", action="store_true",
                        help="manda un messaggio di prova e termina")
    args = parser.parse_args()

    if args.test_telegram:
        if not TELEGRAM_TOKEN:
            log("TELEGRAM_BOT_TOKEN mancante: mettilo nel .env")
            sys.exit(1)
        if not TELEGRAM_CHAT_ID:
            log("TELEGRAM_CHAT_ID mancante nel .env.")
            log("Scrivi un messaggio al bot su Telegram, poi lancia: python3 get_chat_id.py")
            sys.exit(1)
        try:
            send_telegram(
                "\u2705 Monitor esame guida: notifiche configurate correttamente.\n"
                "Da qui in avanti ricevi un messaggio appena si libera una data a Camorino."
            )
            log("Messaggio di prova inviato. Controlla Telegram.")
        except requests.HTTPError as exc:
            code = exc.response.status_code if exc.response is not None else "?"
            if code == 401:
                log("Token rifiutato (401): controlla TELEGRAM_BOT_TOKEN nel file .env")
            elif code == 400:
                log("Richiesta rifiutata (400): TELEGRAM_CHAT_ID probabilmente sbagliato, "
                    "oppure non hai ancora scritto un messaggio al bot.")
            else:
                log(f"Invio fallito ({code}): {exc}")
            sys.exit(1)
        except requests.RequestException as exc:
            log(f"Impossibile contattare Telegram: {type(exc).__name__}: {exc}")
            log("Controlla la connessione di rete del VPS verso api.telegram.org")
            sys.exit(1)
        sys.exit(0)

    try:
        asyncio.run(main_loop(args.once))
    except KeyboardInterrupt:
        log("Interrotto.")
        sys.exit(0)
