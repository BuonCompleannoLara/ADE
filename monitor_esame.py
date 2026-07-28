"""
Monitor disponibilita' esame pratico (categoria B, Camorino) su cariedispoweb.ti.ch

Strategia di navigazione (dalla piu' robusta alla piu' fragile):
  1. va direttamente all'URL del portale prenotazioni
  2. se non funziona, apre la pagina informativa ti.ch, LEGGE l'href del link
     "Appuntamento" e ci naviga (nessun click: niente problemi di overlay,
     banner, elementi nascosti o duplicati mobile/desktop)
  3. solo come ultima risorsa prova a cliccare l'elemento

Poi: login -> scheda "Esame pratico" cat. B -> seleziona sede Camorino ->
scorre le settimane -> confronta con il giro precedente -> avvisa su Telegram
solo per le date che PRIMA non erano libere.

Variabili d'ambiente richieste (file .env):
  TELEGRAM_CHAT_ID
  PORTAL_NUMERO      Nr. candidato (FABER)
  PORTAL_DATA        Data di nascita, formato gg.mm.aaaa
Opzionali:
  TELEGRAM_BOT_TOKEN di default usa il bot @ADEGuida_Bot incluso qui sotto
  HEADLESS=0         browser a schermo (serve xvfb-run su un server)

Uso:
  python3 monitor_esame.py --test-telegram   prova la notifica
  python3 monitor_esame.py --diagnose        esplora il sito e stampa cosa trova
  python3 monitor_esame.py --once            un solo controllo
  python3 monitor_esame.py                   loop continuo
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

import requests
from dotenv import load_dotenv
from playwright.async_api import async_playwright, BrowserContext, Page

load_dotenv()

# --------------------------------------------------------------------------- configurazione

STATE_FILE = Path(__file__).parent / "state.json"

INFO_PAGE_URL = "https://www4.ti.ch/di/sc/conducenti/licenza-allievo-conducente/esami-pratici"
BOOKING_URL = (
    "https://www.cariedispoweb.ti.ch/ecari-dispoweb/ui/app/init/#/conduite/prive/login"
)
BOOKING_DOMAIN_HINT = "cariedispoweb"

LOCATION = "Camorino"

CHECK_INTERVAL_SECONDS = 240   # 4 minuti
MAX_WEEKS_TO_SCAN = 11         # da fine luglio 2026 copre oltre fine settembre
DEADLINE_DATE = datetime(2026, 9, 19)

# Se piu' di questa quota di giorni risulta "libera", quasi certamente la lettura
# della pagina e' sbagliata: mandiamo un solo avviso diagnostico invece di decine
# di falsi allarmi.
IMPLAUSIBLE_AVAILABLE_RATIO = 0.4

UNAVAILABLE_MARKER = "nessuna disponibilit"
DATE_RE = re.compile(r"\b(\d{2}\.\d{2}\.\d{4})\b")

# Token del bot @ADEGuida_Bot (bot di prova dedicato a questo monitor).
DEFAULT_BOT_TOKEN = "8978289517:AAFJy0-42EP6rw8C9kRnsHNo9vcp-UrLdhQ"

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN") or DEFAULT_BOT_TOKEN
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
PORTAL_NUMERO = os.environ.get("PORTAL_NUMERO", "").strip()
PORTAL_DATA = os.environ.get("PORTAL_DATA", "").strip()
HEADLESS = os.environ.get("HEADLESS", "1") != "0"

# Etichette osservate sul portale, con alternative di riserva
FIELD_NUMERO_LABELS = ["Nr. candidato (FABER)", "Nr. candidato", "candidato", "FABER", "Numero"]
FIELD_DATA_LABELS = ["Data di nascita", "Data nascita", "Data"]
SUBMIT_LABELS = ["Login", "Accedi", "Entra", "Continua", "Avanti", "Invia", "Conferma"]
COOKIE_LABELS = ["Accetta", "Accetto", "Accetta tutti", "Accetta tutto", "OK", "Accept", "Chiudi"]
CHOOSE_LABELS = ["Scegliere", "Scegli", "Seleziona", "Selezionare"]
NEXT_WEEK_LABELS = [
    "Caricare la prossima settimana",
    "Prossima settimana",
    "Settimana successiva",
    "Carica la prossima settimana",
]
LOGIN_MARKERS = ["Nr. candidato", "Data di nascita", "Appuntamenti online"]
# pagina di blocco del firewall applicativo del Cantone
BLOCKED_MARKERS = ["Pagina non disponibile", "Support ID", "non e' accessibile"]
LIST_MARKERS = ["Elenco degli appuntamenti", "Appuntamenti esistenti"]
CALENDAR_MARKERS = ["Date disponibili fino al", "Luogo"]

DAY_NAMES = ["Lunedì", "Martedì", "Mercoledì", "Giovedì", "Venerdì", "Sabato", "Domenica"]

BROWSER_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
]


def log(msg: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


# --------------------------------------------------------------------------- telegram / stato

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


# --------------------------------------------------------------------------- utilita' pagina

async def page_has_text(page: Page, markers: list) -> bool:
    """True se almeno uno dei testi indicati compare nella pagina."""
    try:
        body = await page.inner_text("body")
    except Exception:
        return False
    lowered = body.lower()
    return any(m.lower() in lowered for m in markers)


async def wait_for_any_text(page: Page, markers: list, timeout_ms: int = 25000) -> bool:
    """Aspetta che compaia uno qualsiasi dei testi indicati."""
    steps = max(1, timeout_ms // 500)
    for _ in range(steps):
        if await page_has_text(page, markers):
            return True
        await page.wait_for_timeout(500)
    return False


async def click_anything(page: Page, labels: list, timeout: int = 6000) -> bool:
    """Prova a cliccare un elemento con una di queste etichette, usando in
    sequenza: bottone, link, testo semplice; prima quelli visibili, poi con
    scroll, infine con click forzato (utile se qualcosa lo copre)."""
    for label in labels:
        candidates = [
            page.get_by_role("button", name=label, exact=False),
            page.get_by_role("link", name=label, exact=False),
            page.get_by_text(label, exact=False),
        ]
        for locator in candidates:
            try:
                count = await locator.count()
            except Exception:
                continue
            for i in range(count):
                item = locator.nth(i)
                try:
                    if not await item.is_visible():
                        continue
                except Exception:
                    continue
                # 1) click normale
                try:
                    await item.click(timeout=timeout)
                    return True
                except Exception:
                    pass
                # 2) scroll + click
                try:
                    await item.scroll_into_view_if_needed(timeout=3000)
                    await item.click(timeout=timeout)
                    return True
                except Exception:
                    pass
                # 3) click forzato (ignora sovrapposizioni)
                try:
                    await item.click(timeout=timeout, force=True)
                    return True
                except Exception:
                    continue
    return False


async def dismiss_cookie_banner(page: Page) -> None:
    """Chiude il banner cookie se presente. Non blocca mai il flusso."""
    if await click_anything(page, COOKIE_LABELS, timeout=3000):
        await page.wait_for_timeout(500)
        log("Banner cookie chiuso.")


# --------------------------------------------------------------------------- navigazione

async def find_booking_href(page: Page) -> Optional[str]:
    """Cerca fra tutti i link della pagina quello che punta al portale
    prenotazioni. Legge l'href: nessun click, quindi immune a banner,
    sovrapposizioni, elementi nascosti o duplicati mobile/desktop."""
    links = page.locator("a")
    try:
        count = await links.count()
    except Exception:
        return None
    for i in range(count):
        try:
            href = await links.nth(i).get_attribute("href")
        except Exception:
            continue
        if href and BOOKING_DOMAIN_HINT in href:
            # l'href puo' essere relativo: lo rendiamo assoluto rispetto alla pagina
            return urljoin(page.url, href)
    return None


async def _reached_login(page: Page, timeout_ms: int = 15000) -> bool:
    """True se siamo davvero sulla schermata di login del portale."""
    await page.wait_for_timeout(2000)
    await dismiss_cookie_banner(page)
    return await wait_for_any_text(page, LOGIN_MARKERS, timeout_ms)


async def goto_booking_portal(context: BrowserContext, page: Page) -> Page:
    """Porta il browser sulla schermata di login del portale.

    Il portale e' protetto da un firewall applicativo che puo' rispondere
    'Pagina non disponibile'. Proviamo piu' strade, dalla piu' simile a una
    navigazione umana in giu'. La differenza chiave: aprendo un indirizzo di
    colpo NON si invia l'intestazione Referer, mentre cliccando un link SI'.
    Se il firewall pretende che tu arrivi da ti.ch, solo la seconda funziona.
    """

    # Strada 1: dalla pagina informativa, click vero sul link.
    # E' la piu' simile a quello che fa una persona: invia il Referer,
    # mantiene i cookie di sessione ed esegue eventuale JS del sito.
    await page.goto(INFO_PAGE_URL, wait_until="domcontentloaded")
    await dismiss_cookie_banner(page)

    pages_before = len(context.pages)
    if await click_anything(page, ["Appuntamento"]):
        await page.wait_for_timeout(2500)
        booking_page = context.pages[-1] if len(context.pages) > pages_before else page
        try:
            await booking_page.wait_for_load_state("domcontentloaded")
        except Exception:
            pass
        if await _reached_login(booking_page):
            log("Portale raggiunto cliccando il link dalla pagina informativa.")
            return booking_page
        log("Il click ha portato a una pagina che non e' il login.")
        page = booking_page

    # Strada 2: navigazione all'href dichiarando il Referer della pagina ti.ch.
    await page.goto(INFO_PAGE_URL, wait_until="domcontentloaded")
    await dismiss_cookie_banner(page)
    href = await find_booking_href(page)
    if href:
        log(f"Provo l'href dichiarando il Referer: {href[:60]}...")
        try:
            await page.goto(href, wait_until="domcontentloaded", referer=INFO_PAGE_URL)
            if await _reached_login(page):
                log("Portale raggiunto via href con Referer.")
                return page
        except Exception as exc:
            log(f"Navigazione con Referer fallita: {type(exc).__name__}")

    # Strada 3: URL diretto, senza Referer. La meno probabile, ma gratis provarla.
    try:
        await page.goto(BOOKING_URL, wait_until="domcontentloaded")
        if await _reached_login(page, 12000):
            log("Portale raggiunto con URL diretto.")
            return page
    except Exception:
        pass

    # Nessuna strada ha funzionato: diciamo chiaramente perche'.
    if await page_has_text(page, BLOCKED_MARKERS):
        raise RuntimeError(
            "Il portale risponde 'Pagina non disponibile': il firewall del Cantone "
            "sta rifiutando le richieste da questo server. Non e' un problema di "
            "selettori. Prova con HEADLESS=0 (xvfb-run) e, se persiste, e' probabile "
            "un blocco basato sull'indirizzo IP del VPS."
        )
    raise RuntimeError(
        "Non sono riuscito ad aprire il portale prenotazioni "
        "(ne' click, ne' href con Referer, ne' URL diretto)."
    )


# --------------------------------------------------------------------------- login

async def _fill_field(page: Page, labels: list, value: str, index: int) -> None:
    """Riempie un campo provando: etichetta associata, testo vicino,
    placeholder, e infine posizione fra gli input di testo."""
    for label in labels:
        strategies = [
            page.get_by_label(label, exact=False),
            page.locator(
                "//input[preceding::*[contains(normalize-space(text()), "
                + _xpath_literal(label)
                + ")]][1]"
            ),
            page.get_by_placeholder(label, exact=False),
        ]
        for loc in strategies:
            try:
                if await loc.count() == 0:
                    continue
                target = loc.first
                await target.fill(value, timeout=4000)
                if (await target.input_value()).strip():
                    return
            except Exception:
                continue

    # fallback: posizione fra gli input di testo visibili
    inputs = page.locator("input:not([type='hidden']):not([type='submit']):not([type='button'])")
    try:
        visible = []
        for i in range(await inputs.count()):
            if await inputs.nth(i).is_visible():
                visible.append(inputs.nth(i))
        if len(visible) > index:
            await visible[index].fill(value, timeout=4000)
            log(f"Campo '{labels[0]}' riempito per posizione (etichetta non trovata).")
            return
    except Exception:
        pass

    raise RuntimeError(f"Campo '{labels[0]}' non trovato nella pagina di login.")


def _xpath_literal(text: str) -> str:
    """Stringa sicura per XPath, gestendo gli apostrofi."""
    if "'" not in text:
        return f"'{text}'"
    parts = text.split("'")
    return "concat(" + ", \"'\", ".join(f"'{p}'" for p in parts) + ")"


async def login(page: Page) -> None:
    await _fill_field(page, FIELD_NUMERO_LABELS, PORTAL_NUMERO, 0)
    await _fill_field(page, FIELD_DATA_LABELS, PORTAL_DATA, 1)

    # alcuni form Angular abilitano il bottone solo dopo l'evento di uscita dal campo
    try:
        await page.keyboard.press("Tab")
        await page.wait_for_timeout(400)
    except Exception:
        pass

    if not await click_anything(page, SUBMIT_LABELS, timeout=8000):
        log("Nessun bottone di login riconosciuto, provo con Invio.")
        try:
            await page.keyboard.press("Enter")
        except Exception:
            pass

    if await wait_for_any_text(page, LIST_MARKERS, 25000):
        return

    # non siamo arrivati all'elenco: capiamo perche'
    if await page_has_text(page, LOGIN_MARKERS):
        raise RuntimeError(
            "Dopo il login siamo ancora sulla schermata di accesso: "
            "controlla PORTAL_NUMERO e PORTAL_DATA nel file .env "
            "(la data va in formato gg.mm.aaaa)."
        )
    raise RuntimeError("Login effettuato ma l'elenco degli appuntamenti non e' comparso.")


# --------------------------------------------------------------------------- scheda categoria B

async def open_category_b(page: Page) -> None:
    """Apre la scheda dell'esame pratico categoria B."""
    chose = False

    # se ci sono piu' righe, prendiamo quella giusta
    for label in CHOOSE_LABELS:
        items = page.get_by_text(label, exact=False)
        try:
            count = await items.count()
        except Exception:
            continue
        if count == 0:
            continue
        if count == 1:
            chose = await click_anything(page, [label])
            if chose:
                break
            continue
        for i in range(count):
            row = items.nth(i).locator("xpath=ancestor::*[self::tr or self::div][1]")
            try:
                text = await row.inner_text()
            except Exception:
                continue
            if "esame pratico" in text.lower() and re.search(r"\bB\b", text):
                try:
                    await items.nth(i).click(timeout=6000)
                    chose = True
                    break
                except Exception:
                    continue
        if chose:
            break
        # nessuna riga corrispondeva: clicchiamo la prima
        chose = await click_anything(page, [label])
        if chose:
            break

    if not chose:
        raise RuntimeError(
            "Non ho trovato nessun 'Scegliere' nell'elenco appuntamenti "
            "(l'esame potrebbe essere stato fissato dal maestro conducente)."
        )

    if not await wait_for_any_text(page, CALENDAR_MARKERS, 25000):
        raise RuntimeError("Il calendario delle disponibilita' non si e' aperto.")


async def select_location(page: Page, location: str = LOCATION) -> None:
    """Seleziona esplicitamente la sede d'esame. Non diamo per scontato che
    quella giusta sia gia' selezionata di default."""
    selects = page.locator("select")
    try:
        count = await selects.count()
    except Exception:
        count = 0

    for i in range(count):
        sel = selects.nth(i)
        try:
            options = await sel.locator("option").all_inner_texts()
        except Exception:
            continue
        if not any(location.lower() in o.lower() for o in options):
            continue
        current = ""
        try:
            current = (await sel.input_value()) or ""
        except Exception:
            pass
        try:
            await sel.select_option(label=next(o for o in options if location.lower() in o.lower()))
            await page.wait_for_timeout(1500)
            log(f"Sede selezionata: {location}.")
            return
        except Exception:
            try:
                await sel.select_option(index=options.index(
                    next(o for o in options if location.lower() in o.lower())
                ))
                await page.wait_for_timeout(1500)
                log(f"Sede selezionata: {location} (per posizione).")
                return
            except Exception:
                log(f"Non sono riuscito a cambiare la sede (attuale: '{current}').")
                return

    # nessun menu a tendina: verifichiamo almeno che la sede giusta sia a schermo
    if await page_has_text(page, [location]):
        log(f"Nessun menu sede, ma '{location}' risulta gia' selezionata.")
    else:
        log(f"ATTENZIONE: non trovo la sede '{location}' nella pagina. "
            "Le disponibilita' lette potrebbero riferirsi a un'altra sede.")


# --------------------------------------------------------------------------- lettura giorni

async def _day_blocks(page: Page) -> list:
    """Testo di ogni blocco-giorno visibile.

    Per ogni intestazione di giorno risale gli antenati finche' il blocco
    contiene anche la data e lo stato. Si ferma PRIMA di inglobare piu' di una
    data: altrimenti prenderebbe il contenitore dell'intera settimana e un
    solo giorno libero farebbe risultare liberi tutti gli altri.
    """
    day_pattern = "|".join(DAY_NAMES)
    headers = page.locator(f"text=/^\\s*({day_pattern})\\s*$/")
    try:
        count = await headers.count()
    except Exception:
        count = 0

    if count == 0:
        # nessun nome di giorno: proviamo ad ancorarci alle date
        headers = page.locator(r"text=/^\s*\d{2}\.\d{2}\.\d{4}\s*$/")
        try:
            count = await headers.count()
        except Exception:
            count = 0

    blocks = []
    for i in range(count):
        node = headers.nth(i)
        best = ""
        for level in range(1, 7):
            ancestor = node.locator("xpath=" + "/".join([".."] * level))
            try:
                candidate = (await ancestor.inner_text()).strip()
            except Exception:
                break
            dates = set(DATE_RE.findall(candidate))
            if len(dates) > 1:
                break  # abbiamo superato il singolo giorno
            best = candidate
            lines = [l for l in candidate.splitlines() if l.strip()]
            has_status = UNAVAILABLE_MARKER in candidate.lower() or len(lines) >= 3
            if dates and has_status:
                break
        if best and DATE_RE.search(best):
            blocks.append(best)
    return blocks


async def scan_weeks(page: Page):
    """Scorre le settimane. Ritorna (blocchi, settimane_lette)."""
    all_blocks = []
    seen_dates = set()
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
            log(f"Attenzione: la settimana {week + 1} non ha prodotto giorni nuovi.")

        dates_before = set(seen_dates)
        if not await click_anything(page, NEXT_WEEK_LABELS, timeout=5000):
            break

        # aspetta che compaiano date mai viste, invece di un ritardo fisso
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


# --------------------------------------------------------------------------- diagnostica

async def save_debug(page: Page) -> None:
    """Informazioni testuali sulla pagina, leggibili direttamente nel terminale
    di un server senza schermo."""
    try:
        log(f"URL corrente: {page.url}")
        try:
            log(f"Titolo pagina: {await page.title()}")
        except Exception:
            pass

        for tag, name in (("a", "link <a>"), ("button", "<button>"), ("select", "<select>")):
            loc = page.locator(tag)
            try:
                n = await loc.count()
            except Exception:
                continue
            log(f"Trovati {n} {name}. Primi 15 con testo non vuoto:")
            shown = 0
            for i in range(n):
                if shown >= 15:
                    break
                try:
                    txt = (await loc.nth(i).inner_text()).strip().replace("\n", " ")
                    vis = await loc.nth(i).is_visible()
                except Exception:
                    continue
                if txt:
                    log(f"  [{i}] '{txt[:60]}' (visibile: {vis})")
                    shown += 1

        inputs = page.locator("input")
        try:
            n = await inputs.count()
            log(f"Trovati {n} <input>:")
            for i in range(min(n, 10)):
                el = inputs.nth(i)
                log("  [{}] type={} placeholder={} visibile={}".format(
                    i,
                    await el.get_attribute("type"),
                    await el.get_attribute("placeholder"),
                    await el.is_visible(),
                ))
        except Exception:
            pass

        debug_path = Path(__file__).parent / "debug_page.html"
        debug_path.write_text(await page.content(), encoding="utf-8")
        log(f"HTML completo salvato in {debug_path.name}")
    except Exception as exc:
        log(f"Impossibile raccogliere diagnostica: {type(exc).__name__}: {exc}")


async def diagnose() -> None:
    """Percorre il sito passo per passo stampando cosa trova a ogni schermata."""
    require_config()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=HEADLESS, args=BROWSER_ARGS)
        context = await _new_context(browser)
        page = await context.new_page()
        try:
            log("=== PASSO 1: apertura portale ===")
            page = await goto_booking_portal(context, page)
            await save_debug(page)

            log("=== PASSO 2: login ===")
            await login(page)
            await save_debug(page)

            log("=== PASSO 3: scheda categoria B ===")
            await open_category_b(page)
            await select_location(page)
            await save_debug(page)

            log("=== PASSO 4: lettura giorni (solo prima settimana) ===")
            for b in await _day_blocks(page):
                log("  blocco: " + b.replace("\n", " | ")[:100])
        except Exception as exc:
            log(f"Bloccato: {type(exc).__name__}: {exc}")
            await save_debug(page)
        finally:
            await browser.close()


# --------------------------------------------------------------------------- controllo

def build_alert(newly_available: list) -> str:
    newly_available.sort(key=lambda s: s["date"])
    lines = []
    for slot in newly_available:
        urgent = " \U0001F525 ENTRO LA SCADENZA (19.09)" if slot["date"] <= DEADLINE_DATE else ""
        lines.append(f"- {slot['date_str']}{urgent}")
    return (
        f"\U0001F6A8 Nuova disponibilita' esame pratico B - {LOCATION}\n\n"
        + "\n".join(lines)
        + "\n\nPrenota subito: "
        + BOOKING_URL
    )


def require_config() -> None:
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


async def _new_context(browser) -> BrowserContext:
    """Contesto il piu' possibile simile a un browser normale.

    Non impostiamo uno user agent finto: dichiararsi 'Chrome su Windows'
    mentre si e' Chromium su Linux e' un'incoerenza che i firewall
    applicativi riconoscono subito. Meglio quello reale del browser.
    """
    context = await browser.new_context(
        locale="it-CH",
        timezone_id="Europe/Zurich",
        viewport={"width": 1400, "height": 1000},
        extra_http_headers={
            "Accept-Language": "it-CH,it;q=0.9,de;q=0.8,fr;q=0.7,en;q=0.6",
        },
    )
    # nasconde il flag che segnala l'automazione
    await context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
    )
    return context


async def run_check() -> None:
    require_config()
    state = load_state()
    previously_available = set(state.get("available_dates", []))

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=HEADLESS, args=BROWSER_ARGS)
        context = await _new_context(browser)
        page = await context.new_page()
        current_page = page
        try:
            current_page = await goto_booking_portal(context, page)
            await login(current_page)
            await open_category_b(current_page)
            await select_location(current_page)
            raw_blocks, weeks_read = await scan_weeks(current_page)
        except Exception:
            log("Errore durante la navigazione: raccolgo diagnostica prima di chiudere.")
            await save_debug(current_page)
            raise
        finally:
            await browser.close()

    parsed = [b for b in (parse_day_block(r) for r in raw_blocks) if b]
    if not parsed:
        raise RuntimeError("Nessun giorno letto dal calendario: struttura della pagina cambiata?")

    currently_available = {s["date_str"] for s in parsed if s["available"]}
    ratio = len(currently_available) / len(parsed)

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

    state["available_dates"] = sorted(currently_available)
    state["last_check"] = datetime.now().isoformat()
    save_state(state)


async def main_loop(once: bool) -> None:
    if once:
        await run_check()
        return
    log(f"Monitor avviato. Controllo ogni {CHECK_INTERVAL_SECONDS}s, sede {LOCATION}.")
    while True:
        try:
            await run_check()
        except Exception as exc:
            log(f"Errore durante il controllo: {type(exc).__name__}: {exc}")
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Monitor esame pratico B - Camorino")
    parser.add_argument("--once", action="store_true", help="un solo controllo e termina")
    parser.add_argument("--diagnose", action="store_true",
                        help="esplora il sito e stampa cosa trova a ogni passo")
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
                f"Da qui in avanti ricevi un messaggio appena si libera una data a {LOCATION}."
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
            sys.exit(1)
        sys.exit(0)

    try:
        if args.diagnose:
            asyncio.run(diagnose())
        else:
            asyncio.run(main_loop(args.once))
    except KeyboardInterrupt:
        log("Interrotto.")
        sys.exit(0)
