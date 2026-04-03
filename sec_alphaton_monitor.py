#!/usr/bin/env python3
"""
AlphaTon Alert Monitor — SEC + Etherscan
=========================================
Monitors two independent sources and sends Telegram alerts:

  1. SEC (EDGAR full-text search, press-release / litigation RSS, company feed)
       → any document mentioning "AlphaTon"
       → checked every SEC_CHECK_INTERVAL_SECS (default 3600 s / 1 h)

  2. Etherscan ERC-20 token transfers
       → contract 0xd9016a907dc0ecfa3ca425ab20b6b785b42f2373
       → only transfers >= MIN_TOKEN_AMOUNT tokens (default 500 000)
       → checked every ETH_CHECK_INTERVAL_SECS (default 300 s / 5 min)

Usage:
    python3 sec_alphaton_monitor.py          # continuous daemon
    python3 sec_alphaton_monitor.py --once   # single cycle (for cron)

Required environment variables:
    TELEGRAM_BOT_TOKEN       Telegram bot token (from @BotFather)
    TELEGRAM_CHAT_ID         Target chat / channel ID
    ETHERSCAN_API_KEY        Etherscan API key (https://etherscan.io/myapikey)

Optional environment variables:
    SEC_CHECK_INTERVAL_SECS  How often to poll SEC  (default: 3600)
    ETH_CHECK_INTERVAL_SECS  How often to poll Etherscan (default: 300)
    LOOKBACK_DAYS            Days back to look on first SEC run (default: 30)
    ETH_LOOKBACK_BLOCKS      Blocks back to scan on first Etherscan run (default: 7200 ≈ 1 day)
    MIN_TOKEN_AMOUNT         Minimum transfer size to alert on (default: 500000)
    STATE_FILE               Path to state JSON file (default: ./alphaton_state.json)
"""

import os
import sys
import json
import time
import logging
import datetime
import hashlib
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────

TELEGRAM_BOT_TOKEN      = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID        = os.environ.get("TELEGRAM_CHAT_ID", "")
ETHERSCAN_API_KEY       = os.environ.get("ETHERSCAN_API_KEY", "")

SEC_CHECK_INTERVAL      = int(os.environ.get("SEC_CHECK_INTERVAL_SECS", "3600"))
ETH_CHECK_INTERVAL      = int(os.environ.get("ETH_CHECK_INTERVAL_SECS", "300"))
LOOKBACK_DAYS           = int(os.environ.get("LOOKBACK_DAYS", "30"))
ETH_LOOKBACK_BLOCKS     = int(os.environ.get("ETH_LOOKBACK_BLOCKS", "7200"))
MIN_TOKEN_AMOUNT        = float(os.environ.get("MIN_TOKEN_AMOUNT", "500000"))
STATE_FILE              = Path(os.environ.get("STATE_FILE", "alphaton_state.json"))

SEC_SEARCH_TERM   = "AlphaTon"
TOKEN_CONTRACT    = "0xd9016a907dc0ecfa3ca425ab20b6b785b42f2373"
TRANSFER_SIG      = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

# SEC requires a descriptive User-Agent with contact info
# https://www.sec.gov/developer
SEC_HEADERS = {
    "User-Agent": "AlphaTon-Monitor alerts@pisosganga.com",
    "Accept":     "application/json, application/xml, text/xml",
}

# ── State helpers ─────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception as exc:
            log.warning("Could not read state file (%s). Starting fresh.", exc)
    return {
        "seen_edgar":   [],
        "seen_news":    [],
        "seen_company": [],
        "eth_last_block": 0,
        "eth_seen_tx":  [],
        "token_decimals": None,
        "token_symbol":   None,
    }


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ── Telegram ──────────────────────────────────────────────────────────────────

def send_telegram(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set — cannot send alert.")
        return False

    url     = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id":                  TELEGRAM_CHAT_ID,
        "text":                     text,
        "parse_mode":               "HTML",
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


# ══════════════════════════════════════════════════════════════════════════════
#  SEC EDGAR
# ══════════════════════════════════════════════════════════════════════════════

def _edgar_fetch_filings(start_date: str) -> list:
    url    = "https://efts.sec.gov/LATEST/search-index"
    params = {
        "q":         f'"{SEC_SEARCH_TERM}"',
        "dateRange": "custom",
        "startdt":   start_date,
        "enddt":     datetime.date.today().isoformat(),
        "_source":   "entity_name,file_date,form_type,period_of_report,file_num,id",
    }
    try:
        resp = requests.get(url, params=params, headers=SEC_HEADERS, timeout=20)
        resp.raise_for_status()
        return resp.json().get("hits", {}).get("hits", [])
    except Exception as exc:
        log.warning("EDGAR full-text search error: %s", exc)
    return []


def _edgar_filing_msg(hit: dict) -> str:
    src  = hit.get("_source", {})
    eid  = hit.get("_id", "")
    name = src.get("entity_name", "N/A")
    date = src.get("file_date", "N/A")
    form = src.get("form_type", "N/A")
    search_url = "https://efts.sec.gov/LATEST/search-index?q=%22AlphaTon%22"
    return (
        f"🔔 <b>Nueva presentación SEC — {SEC_SEARCH_TERM}</b>\n\n"
        f"<b>Entidad:</b> {name}\n"
        f"<b>Formulario:</b> {form}\n"
        f"<b>Fecha:</b> {date}\n"
        f"<b>ID:</b> <code>{eid}</code>\n\n"
        f'🔗 <a href="{search_url}">Ver en EDGAR</a>'
    )


def check_edgar(state: dict) -> int:
    start = (datetime.date.today() - datetime.timedelta(days=LOOKBACK_DAYS)).isoformat()
    hits  = _edgar_fetch_filings(start)
    log.info("EDGAR: %d hit(s) for '%s'.", len(hits), SEC_SEARCH_TERM)

    seen = set(state["seen_edgar"])
    sent = 0
    for hit in hits:
        hid = hit.get("_id", "")
        if not hid or hid in seen:
            continue
        log.info("  → New EDGAR filing: %s", hid)
        if send_telegram(_edgar_filing_msg(hit)):
            seen.add(hid)
            sent += 1
        time.sleep(1)

    state["seen_edgar"] = list(seen)
    return sent


# ── SEC RSS feeds (press releases, litigation, admin actions) ─────────────────

_RSS_FEEDS = [
    ("pressreleases.xml",  "https://www.sec.gov/newsroom/pressreleases.rss",       "Nota de prensa"),
    ("litreleases.xml",    "https://www.sec.gov/litigation/litreleases.xml",        "Acción legal"),
    ("admin.xml",          "https://www.sec.gov/litigation/admin.xml",              "Acción administrativa"),
]


def _rss_news_msg(item: dict) -> str:
    label = item.get("label", "Noticia SEC")
    return (
        f"🚨 <b>SEC — {label} sobre {SEC_SEARCH_TERM}</b>\n\n"
        f"<b>{item['title']}</b>\n"
        f"<i>{item['date']}</i>\n\n"
        f'🔗 <a href="{item["link"]}">Leer noticia completa</a>'
    )


def check_sec_news(state: dict) -> int:
    term  = SEC_SEARCH_TERM.lower()
    seen  = set(state["seen_news"])
    sent  = 0

    for _fname, rss_url, label in _RSS_FEEDS:
        try:
            resp = requests.get(rss_url, headers=SEC_HEADERS, timeout=20)
            resp.raise_for_status()
            root    = ET.fromstring(resp.text)
            channel = root.find("channel")
            if channel is None:
                continue
            for item_el in channel.findall("item"):
                title = item_el.findtext("title", "")
                desc  = item_el.findtext("description", "")
                link  = item_el.findtext("link", "")
                date  = item_el.findtext("pubDate", "")
                if term not in title.lower() and term not in desc.lower():
                    continue
                uid = hashlib.sha1(link.encode()).hexdigest()
                if uid in seen:
                    continue
                log.info("  → New SEC news (%s): %s", label, title)
                item = {"title": title, "link": link, "date": date, "label": label}
                if send_telegram(_rss_news_msg(item)):
                    seen.add(uid)
                    sent += 1
                time.sleep(1)
        except Exception as exc:
            log.warning("RSS fetch failed (%s): %s", rss_url, exc)

    log.info("SEC News: %d new alert(s).", sent)
    state["seen_news"] = list(seen)
    return sent


# ── EDGAR company-level search (Atom feed) ────────────────────────────────────

_ATOM_NS = "http://www.w3.org/2005/Atom"


def check_edgar_company(state: dict) -> int:
    url    = "https://www.sec.gov/cgi-bin/browse-edgar"
    params = {
        "action": "getcompany", "company": SEC_SEARCH_TERM,
        "type": "", "dateb": "", "owner": "include",
        "count": "40", "search_text": "", "output": "atom",
    }
    seen = set(state.get("seen_company", []))
    sent = 0
    try:
        resp = requests.get(url, params=params, headers=SEC_HEADERS, timeout=20)
        resp.raise_for_status()
        root = ET.fromstring(resp.text)
        for entry in root.findall(f"{{{_ATOM_NS}}}entry"):
            eid   = entry.findtext(f"{{{_ATOM_NS}}}id", "")
            title = entry.findtext(f"{{{_ATOM_NS}}}title", "")
            link_el = entry.find(f"{{{_ATOM_NS}}}link")
            link  = link_el.get("href", "") if link_el is not None else ""
            upd   = entry.findtext(f"{{{_ATOM_NS}}}updated", "")
            uid   = hashlib.sha1(eid.encode()).hexdigest()
            if uid in seen:
                continue
            log.info("  → New EDGAR company: %s", title)
            msg = (
                f"📋 <b>Empresa en EDGAR — {SEC_SEARCH_TERM}</b>\n\n"
                f"<b>{title}</b>\n<i>{upd}</i>\n\n"
                f'🔗 <a href="{link}">Ver en EDGAR</a>'
            )
            if send_telegram(msg):
                seen.add(uid)
                sent += 1
            time.sleep(1)
    except Exception as exc:
        log.warning("EDGAR company search failed: %s", exc)

    state["seen_company"] = list(seen)
    return sent


def run_sec_checks(state: dict) -> int:
    log.info("── SEC check ────────────────────────────────")
    total  = check_edgar(state)
    total += check_sec_news(state)
    total += check_edgar_company(state)
    log.info("SEC check done. %d new alert(s).", total)
    return total


# ══════════════════════════════════════════════════════════════════════════════
#  ETHERSCAN — ERC-20 token transfer monitor
# ══════════════════════════════════════════════════════════════════════════════

_ETHERSCAN_BASE = "https://api.etherscan.io/v2/api"
_ETHERSCAN_CHAIN_ID = "1"


def _eth_get(params: dict) -> dict:
    """Make an Etherscan API call and return the parsed JSON."""
    params.setdefault("apikey", ETHERSCAN_API_KEY)
    params.setdefault("chainid", _ETHERSCAN_CHAIN_ID)
    resp = requests.get(_ETHERSCAN_BASE, params=params, timeout=20)
    resp.raise_for_status()
    return resp.json()


def _eth_latest_block() -> int:
    data = _eth_get({"module": "proxy", "action": "eth_blockNumber"})
    return int(data["result"], 16)


def _eth_token_info(state: dict) -> tuple[str, int]:
    """Return (symbol, decimals), cached in state."""
    if state.get("token_decimals") and state.get("token_symbol"):
        return state["token_symbol"], state["token_decimals"]

    try:
        data = _eth_get({
            "module":          "token",
            "action":          "tokeninfo",
            "contractaddress": TOKEN_CONTRACT,
        })
        if data.get("status") == "1" and data.get("result"):
            info = data["result"]
            if isinstance(info, list):
                info = info[0]
            symbol   = info.get("symbol", "TOKEN")
            decimals = int(info.get("divisor", info.get("decimals", "18")))
            state["token_symbol"]   = symbol
            state["token_decimals"] = decimals
            log.info("Token info: %s, %d decimals", symbol, decimals)
            return symbol, decimals
    except Exception as exc:
        log.warning("Could not fetch token info: %s. Assuming 18 decimals.", exc)

    state["token_symbol"]   = "TOKEN"
    state["token_decimals"] = 18
    return "TOKEN", 18


def _decode_transfer(log_entry: dict, decimals: int) -> dict:
    """Decode a raw Transfer event log entry."""
    topics    = log_entry.get("topics", [])
    from_addr = "0x" + topics[1][-40:] if len(topics) > 1 else "0x?"
    to_addr   = "0x" + topics[2][-40:] if len(topics) > 2 else "0x?"
    raw_val   = int(log_entry.get("data", "0x0"), 16)
    amount    = raw_val / (10 ** decimals)
    tx_hash   = log_entry.get("transactionHash", "")
    block_num = int(log_entry.get("blockNumber", "0x0"), 16)
    return {
        "from":   from_addr,
        "to":     to_addr,
        "amount": amount,
        "tx":     tx_hash,
        "block":  block_num,
    }


def _transfer_msg(t: dict, symbol: str) -> str:
    amount_fmt = f"{t['amount']:,.0f}"
    short_from = t["from"][:6] + "…" + t["from"][-4:]
    short_to   = t["to"][:6]   + "…" + t["to"][-4:]
    tx_url     = f"https://etherscan.io/tx/{t['tx']}"
    from_url   = f"https://etherscan.io/address/{t['from']}"
    to_url     = f"https://etherscan.io/address/{t['to']}"
    token_url  = f"https://etherscan.io/token/{TOKEN_CONTRACT}"

    return (
        f"💸 <b>Gran transferencia {symbol}</b> — {amount_fmt} tokens\n\n"
        f"<b>De:</b>  <a href=\"{from_url}\">{short_from}</a>\n"
        f"<b>A:</b>   <a href=\"{to_url}\">{short_to}</a>\n"
        f"<b>Bloque:</b> {t['block']:,}\n\n"
        f'🔗 <a href="{tx_url}">Ver tx en Etherscan</a>\n'
        f'📊 <a href="{token_url}">Ver token en Etherscan</a>'
    )


def check_etherscan(state: dict) -> int:
    if not ETHERSCAN_API_KEY:
        log.warning("ETHERSCAN_API_KEY not set — skipping Etherscan check.")
        return 0

    symbol, decimals = _eth_token_info(state)

    try:
        latest_block = _eth_latest_block()
    except Exception as exc:
        log.warning("Could not get latest block: %s", exc)
        return 0

    last_block = state.get("eth_last_block", 0)
    if last_block == 0:
        last_block = latest_block - ETH_LOOKBACK_BLOCKS

    if latest_block <= last_block:
        log.info("Etherscan: no new blocks (latest=%d).", latest_block)
        return 0

    # Etherscan getLogs supports max ~10 000 blocks per call
    from_block = last_block + 1
    to_block   = min(latest_block, from_block + 9999)

    log.info("Etherscan: scanning blocks %d → %d for %s transfers >= %s.",
             from_block, to_block, symbol, f"{MIN_TOKEN_AMOUNT:,.0f}")

    try:
        data = _eth_get({
            "module":    "logs",
            "action":    "getLogs",
            "address":   TOKEN_CONTRACT,
            "topic0":    TRANSFER_SIG,
            "fromBlock": from_block,
            "toBlock":   to_block,
        })
    except Exception as exc:
        log.warning("Etherscan getLogs error: %s", exc)
        return 0

    if data.get("status") not in ("1", 1) and not data.get("result"):
        log.info("Etherscan: no Transfer events in range.")
        state["eth_last_block"] = to_block
        return 0

    raw_logs = data.get("result", [])
    log.info("Etherscan: %d Transfer event(s) in range.", len(raw_logs))

    seen = set(state.get("eth_seen_tx", []))
    sent = 0

    for entry in raw_logs:
        t = _decode_transfer(entry, decimals)

        if t["tx"] in seen:
            continue
        if t["amount"] < MIN_TOKEN_AMOUNT:
            continue

        log.info("  → Large transfer: %.0f %s  tx=%s", t["amount"], symbol, t["tx"])
        if send_telegram(_transfer_msg(t, symbol)):
            seen.add(t["tx"])
            sent += 1
        time.sleep(0.5)

    state["eth_last_block"] = to_block
    # Keep seen list bounded (last 2000 tx hashes is plenty)
    state["eth_seen_tx"] = list(seen)[-2000:]
    log.info("Etherscan check done. %d new alert(s).", sent)
    return sent


# ══════════════════════════════════════════════════════════════════════════════
#  Main loop
# ══════════════════════════════════════════════════════════════════════════════

def run_once():
    """Single cycle: run all checks once and save state."""
    state = load_state()
    total  = run_sec_checks(state)
    total += check_etherscan(state)
    save_state(state)
    log.info("Full cycle done. %d new alert(s) sent.", total)
    return total


def main():
    if not TELEGRAM_BOT_TOKEN:
        log.error("Set TELEGRAM_BOT_TOKEN before running.")
        sys.exit(1)
    if not TELEGRAM_CHAT_ID:
        log.error("Set TELEGRAM_CHAT_ID before running.")
        sys.exit(1)
    if not ETHERSCAN_API_KEY:
        log.warning("ETHERSCAN_API_KEY not set — Etherscan monitoring disabled.")

    log.info("AlphaTon Monitor started (SEC + Etherscan).")
    log.info("  SEC check every %ds, Etherscan every %ds.", SEC_CHECK_INTERVAL, ETH_CHECK_INTERVAL)
    log.info("  Token contract : %s", TOKEN_CONTRACT)
    log.info("  Min transfer   : %s tokens", f"{MIN_TOKEN_AMOUNT:,.0f}")

    last_sec = 0.0
    last_eth = 0.0

    while True:
        now   = time.monotonic()
        state = load_state()

        if now - last_sec >= SEC_CHECK_INTERVAL:
            run_sec_checks(state)
            save_state(state)
            last_sec = now

        if now - last_eth >= ETH_CHECK_INTERVAL:
            check_etherscan(state)
            save_state(state)
            last_eth = now

        try:
            time.sleep(60)   # wake up every minute to check which timer fired
        except KeyboardInterrupt:
            log.info("Stopped by user.")
            break


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--once":
        run_once()
    else:
        main()
