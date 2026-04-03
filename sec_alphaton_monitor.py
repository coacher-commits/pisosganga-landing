#!/usr/bin/env python3
"""
SEC AlphaTon Alert Monitor
==========================
Polls the SEC EDGAR full-text search API and SEC news feeds for any mention
of "AlphaTon". Sends a Telegram message when new results are detected.

Usage:
    python3 sec_alphaton_monitor.py

Environment variables (required):
    TELEGRAM_BOT_TOKEN   - Telegram bot token (from @BotFather)
    TELEGRAM_CHAT_ID     - Target chat/channel ID

Environment variables (optional):
    CHECK_INTERVAL_SECS  - Polling interval in seconds (default: 3600 = 1h)
    STATE_FILE           - Path to JSON file for persisting seen IDs
                           (default: ./sec_alphaton_state.json)
    LOOKBACK_DAYS        - How many days back to look on first run (default: 30)
"""

import os
import sys
import json
import time
import logging
import datetime
import hashlib
from pathlib import Path

import requests

# ── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
CHECK_INTERVAL     = int(os.environ.get("CHECK_INTERVAL_SECS", "3600"))
STATE_FILE         = Path(os.environ.get("STATE_FILE", "sec_alphaton_state.json"))
LOOKBACK_DAYS      = int(os.environ.get("LOOKBACK_DAYS", "30"))

SEARCH_TERM = "AlphaTon"

# SEC requires a descriptive User-Agent with contact info.
# See: https://www.sec.gov/developer
SEC_HEADERS = {
    "User-Agent": "AlphaTon-Monitor sec-alerts@pisosganga.com",
    "Accept":     "application/json",
}

# ── State helpers ─────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception as exc:
            log.warning("Could not read state file (%s). Starting fresh.", exc)
    return {"seen_edgar": [], "seen_news": []}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ── Telegram ──────────────────────────────────────────────────────────────────

def send_telegram(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set — cannot send alert.")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id":    TELEGRAM_CHAT_ID,
        "text":       text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    try:
        resp = requests.post(url, json=payload, timeout=15)
        if resp.ok:
            log.info("Telegram alert sent.")
            return True
        log.error("Telegram error %s: %s", resp.status_code, resp.text[:200])
    except Exception as exc:
        log.error("Telegram request failed: %s", exc)
    return False


# ── SEC EDGAR full-text search ────────────────────────────────────────────────

def fetch_edgar_filings(start_date: str) -> list[dict]:
    """
    Query EDGAR EFTS (full-text search) for documents mentioning AlphaTon.
    Returns a list of hit dicts.
    """
    url = "https://efts.sec.gov/LATEST/search-index"
    params = {
        "q":          f'"{SEARCH_TERM}"',
        "dateRange":  "custom",
        "startdt":    start_date,
        "enddt":      datetime.date.today().isoformat(),
        "_source":    "entity_name,file_date,form_type,period_of_report,file_num,id",
        "hits.hits.total.value": 40,
    }

    try:
        resp = requests.get(url, params=params, headers=SEC_HEADERS, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        return data.get("hits", {}).get("hits", [])
    except requests.HTTPError as exc:
        log.warning("EDGAR search HTTP error: %s", exc)
    except Exception as exc:
        log.warning("EDGAR search failed: %s", exc)
    return []


def filing_to_message(hit: dict) -> str:
    src  = hit.get("_source", {})
    eid  = hit.get("_id", "")
    name = src.get("entity_name", "N/A")
    date = src.get("file_date", "N/A")
    form = src.get("form_type", "N/A")

    # Build EDGAR filing URL from accession number (the _id field)
    # Format: 0001234567-24-000001  →  Archives/edgar/data/CIK/0001234567-24-000001
    accession = eid.replace("-", "")
    edgar_url = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&filenum={src.get('file_num', '')}&type={form}&dateb=&owner=include&count=10&search_text="

    # Simpler direct link to the filing index
    cik_part  = accession[:10].lstrip("0") if accession else ""
    filing_url = (
        f"https://www.sec.gov/Archives/edgar/data/{cik_part}/{eid.replace('-','')}-index.htm"
        if accession else "https://efts.sec.gov/LATEST/search-index?q=%22AlphaTon%22"
    )

    return (
        f"🔔 <b>Nueva presentación SEC sobre {SEARCH_TERM}</b>\n\n"
        f"<b>Entidad:</b> {name}\n"
        f"<b>Formulario:</b> {form}\n"
        f"<b>Fecha:</b> {date}\n"
        f"<b>ID:</b> {eid}\n\n"
        f'🔗 <a href="{filing_url}">Ver presentación en EDGAR</a>\n'
        f'🔍 <a href="https://efts.sec.gov/LATEST/search-index?q=%22AlphaTon%22">Buscar más en EDGAR</a>'
    )


def check_edgar(state: dict) -> int:
    """Check EDGAR for new filings. Returns count of new alerts sent."""
    start_date = (
        datetime.date.today() - datetime.timedelta(days=LOOKBACK_DAYS)
    ).isoformat()

    hits = fetch_edgar_filings(start_date)
    log.info("EDGAR: %d hit(s) found for '%s'.", len(hits), SEARCH_TERM)

    seen  = set(state["seen_edgar"])
    new_alerts = 0

    for hit in hits:
        hit_id = hit.get("_id", "")
        if not hit_id or hit_id in seen:
            continue

        log.info("  → New EDGAR filing: %s", hit_id)
        msg = filing_to_message(hit)
        if send_telegram(msg):
            seen.add(hit_id)
            new_alerts += 1
        time.sleep(1)  # rate-limit

    state["seen_edgar"] = list(seen)
    return new_alerts


# ── SEC News / Press releases ─────────────────────────────────────────────────

def fetch_sec_news_rss() -> list[dict]:
    """
    Parse the SEC press-release RSS feed and return items whose title or
    description mentions SEARCH_TERM (case-insensitive).
    """
    import xml.etree.ElementTree as ET

    rss_urls = [
        "https://www.sec.gov/rss/news/pressreleases.xml",
        "https://www.sec.gov/litigation/litreleases.xml",
        "https://www.sec.gov/litigation/admin.xml",
    ]

    items = []
    term_lower = SEARCH_TERM.lower()

    for rss_url in rss_urls:
        try:
            resp = requests.get(rss_url, headers=SEC_HEADERS, timeout=20)
            resp.raise_for_status()
            root = ET.fromstring(resp.text)
            channel = root.find("channel")
            if channel is None:
                continue
            for item in channel.findall("item"):
                title = item.findtext("title", "")
                desc  = item.findtext("description", "")
                link  = item.findtext("link", "")
                date  = item.findtext("pubDate", "")
                if term_lower in title.lower() or term_lower in desc.lower():
                    items.append({
                        "id":    hashlib.sha1(link.encode()).hexdigest(),
                        "title": title,
                        "link":  link,
                        "date":  date,
                        "feed":  rss_url.split("/")[-1],
                    })
        except Exception as exc:
            log.warning("RSS fetch failed (%s): %s", rss_url, exc)

    return items


def news_to_message(item: dict) -> str:
    feed_label = {
        "pressreleases.xml":  "Nota de prensa",
        "litreleases.xml":    "Acción legal (Litigation Release)",
        "admin.xml":          "Acción administrativa",
    }.get(item.get("feed", ""), "Noticia SEC")

    return (
        f"🚨 <b>SEC – {feed_label} sobre {SEARCH_TERM}</b>\n\n"
        f"<b>{item['title']}</b>\n"
        f"<i>{item['date']}</i>\n\n"
        f'🔗 <a href="{item["link"]}">Leer noticia completa</a>'
    )


def check_sec_news(state: dict) -> int:
    """Check SEC RSS news feeds. Returns count of new alerts sent."""
    items = fetch_sec_news_rss()
    log.info("SEC News RSS: %d match(es) for '%s'.", len(items), SEARCH_TERM)

    seen = set(state["seen_news"])
    new_alerts = 0

    for item in items:
        if item["id"] in seen:
            continue

        log.info("  → New SEC news: %s", item["title"])
        msg = news_to_message(item)
        if send_telegram(msg):
            seen.add(item["id"])
            new_alerts += 1
        time.sleep(1)

    state["seen_news"] = list(seen)
    return new_alerts


# ── EDGAR company-level search (backup) ──────────────────────────────────────

def check_edgar_company_search(state: dict) -> int:
    """
    Fallback: query the EDGAR company search Atom feed for 'AlphaTon'.
    Useful if full-text search is unavailable.
    """
    import xml.etree.ElementTree as ET

    url = "https://www.sec.gov/cgi-bin/browse-edgar"
    params = {
        "action":      "getcompany",
        "company":     SEARCH_TERM,
        "type":        "",
        "dateb":       "",
        "owner":       "include",
        "count":       "40",
        "search_text": "",
        "output":      "atom",
    }
    NS = "http://www.w3.org/2005/Atom"

    seen = set(state.get("seen_company", []))
    new_alerts = 0

    try:
        resp = requests.get(url, params=params, headers=SEC_HEADERS, timeout=20)
        resp.raise_for_status()
        root = ET.fromstring(resp.text)

        for entry in root.findall(f"{{{NS}}}entry"):
            entry_id   = entry.findtext(f"{{{NS}}}id", "")
            entry_title = entry.findtext(f"{{{NS}}}title", "")
            entry_link_el = entry.find(f"{{{NS}}}link")
            entry_link = entry_link_el.get("href", "") if entry_link_el is not None else ""
            updated    = entry.findtext(f"{{{NS}}}updated", "")

            uid = hashlib.sha1(entry_id.encode()).hexdigest()
            if uid in seen:
                continue

            log.info("  → New EDGAR company entry: %s", entry_title)
            msg = (
                f"📋 <b>Empresa en EDGAR relacionada con {SEARCH_TERM}</b>\n\n"
                f"<b>{entry_title}</b>\n"
                f"<i>Actualizado: {updated}</i>\n\n"
                f'🔗 <a href="{entry_link}">Ver en EDGAR</a>'
            )
            if send_telegram(msg):
                seen.add(uid)
                new_alerts += 1
            time.sleep(1)

    except Exception as exc:
        log.warning("EDGAR company search failed: %s", exc)

    state["seen_company"] = list(seen)
    return new_alerts


# ── Main loop ─────────────────────────────────────────────────────────────────

def run_once():
    """Run a single check cycle across all sources."""
    state = load_state()

    total = 0
    total += check_edgar(state)
    total += check_sec_news(state)
    total += check_edgar_company_search(state)

    save_state(state)
    log.info("Cycle done. %d new alert(s) sent.", total)
    return total


def main():
    if not TELEGRAM_BOT_TOKEN:
        log.error("Set TELEGRAM_BOT_TOKEN environment variable before running.")
        sys.exit(1)
    if not TELEGRAM_CHAT_ID:
        log.error("Set TELEGRAM_CHAT_ID environment variable before running.")
        sys.exit(1)

    log.info("SEC AlphaTon Monitor started. Interval: %ds", CHECK_INTERVAL)
    log.info("Monitoring search term: '%s'", SEARCH_TERM)
    log.info("State file: %s", STATE_FILE)

    while True:
        try:
            run_once()
        except KeyboardInterrupt:
            log.info("Stopped by user.")
            break
        except Exception as exc:
            log.error("Unexpected error in cycle: %s", exc, exc_info=True)

        log.info("Sleeping %d seconds until next check…", CHECK_INTERVAL)
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    # Allow a one-shot mode: python3 sec_alphaton_monitor.py --once
    if len(sys.argv) > 1 and sys.argv[1] == "--once":
        run_once()
    else:
        main()
