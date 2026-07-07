import os
import time
import json
import logging
import re
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import requests
import feedparser
from bs4 import BeautifulSoup
from telegram import Bot

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
SEC_USER_AGENT = os.getenv("SEC_USER_AGENT", "insider-alert-bot contact@example.com").strip()

MIN_BUY_VALUE = float(os.getenv("MIN_BUY_VALUE", "500000"))
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "300"))
WATCH_TICKERS = {x.strip().upper() for x in os.getenv("WATCH_TICKERS", "").split(",") if x.strip()}

SEEN_FILE = "seen.json"
SEC_ATOM_URL = (
    "https://www.sec.gov/cgi-bin/browse-edgar?"
    "action=getcurrent&type=4&owner=include&count=100&output=atom"
)

HEADERS = {
    "User-Agent": SEC_USER_AGENT,
    "Accept-Encoding": "gzip, deflate",
    "Host": "www.sec.gov",
}


def load_seen() -> set:
    try:
        with open(SEEN_FILE, "r") as f:
            return set(json.load(f))
    except Exception:
        return set()


def save_seen(seen: set) -> None:
    with open(SEEN_FILE, "w") as f:
        json.dump(sorted(list(seen))[-1000:], f)


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def get_json_url(filing_url: str) -> Optional[str]:
    try:
        r = requests.get(filing_url, headers=HEADERS, timeout=20)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if href.endswith(".xml") and "xslF345X05" not in href:
                if href.startswith("/"):
                    return "https://www.sec.gov" + href
                return href
    except Exception as e:
        logging.warning("Could not find XML URL: %s", e)
    return None


def parse_form4_xml(xml_url: str) -> Optional[Dict]:
    try:
        r = requests.get(xml_url, headers=HEADERS, timeout=20)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "xml")

        issuer = soup.find("issuer")
        ticker = clean_text(issuer.find_text("issuerTradingSymbol")) if issuer else ""
        company = clean_text(issuer.find_text("issuerName")) if issuer else ""

        reporting_owner = soup.find("reportingOwner")
        owner_name = ""
        owner_title = ""
        if reporting_owner:
            rel = reporting_owner.find("reportingOwnerRelationship")
            owner = reporting_owner.find("reportingOwnerId")
            owner_name = clean_text(owner.find_text("rptOwnerName")) if owner else ""
            title = rel.find("officerTitle") if rel else None
            is_director = rel.find_text("isDirector") if rel else "0"
            is_officer = rel.find_text("isOfficer") if rel else "0"
            if title:
                owner_title = clean_text(title.get_text())
            elif is_director == "1":
                owner_title = "Director"
            elif is_officer == "1":
                owner_title = "Officer"

        buys = []
        for tx in soup.find_all("nonDerivativeTransaction"):
            code = clean_text(tx.find_text("transactionCode"))
            if code != "P":
                continue

            shares_txt = clean_text(tx.find_text("transactionShares"))
            price_txt = clean_text(tx.find_text("transactionPricePerShare"))
            date_txt = clean_text(tx.find_text("transactionDate"))
            try:
                shares = float(shares_txt.replace(",", ""))
                price = float(price_txt.replace(",", ""))
                value = shares * price
            except Exception:
                continue

            buys.append({
                "date": date_txt,
                "shares": shares,
                "price": price,
                "value": value,
            })

        if not buys:
            return None

        total_value = sum(x["value"] for x in buys)

        return {
            "ticker": ticker.upper(),
            "company": company,
            "owner_name": owner_name,
            "owner_title": owner_title,
            "total_value": total_value,
            "buys": buys,
            "xml_url": xml_url,
        }
    except Exception as e:
        logging.warning("Could not parse XML: %s", e)
        return None


def format_money(x: float) -> str:
    if x >= 1_000_000:
        return f"${x/1_000_000:.2f}M"
    return f"${x:,.0f}"


def format_alert(data: Dict, filing_url: str) -> str:
    ticker = data.get("ticker", "UNKNOWN")
    company = data.get("company", "")
    owner = data.get("owner_name", "")
    title = data.get("owner_title", "")
    total = data.get("total_value", 0)

    lines = [
        "🚨 Insider Purchase Alert",
        f"Ticker: {ticker}",
        f"Company: {company}",
        f"Insider: {owner}" + (f" — {title}" if title else ""),
        f"Total Buy Value: {format_money(total)}",
        "",
        "Transactions:"
    ]

    for b in data.get("buys", [])[:5]:
        lines.append(
            f"- {b['date']}: {b['shares']:,.0f} shares @ ${b['price']:,.2f} = {format_money(b['value'])}"
        )

    lines += ["", f"SEC Filing: {filing_url}"]
    return "\n".join(lines)


def send_message(bot: Bot, text: str) -> None:
    bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text, disable_web_page_preview=True)


def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN")
    if not TELEGRAM_CHAT_ID:
        raise RuntimeError("Missing TELEGRAM_CHAT_ID")

    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    seen = load_seen()

    logging.info("Bot started. Min buy value=%s Watch tickers=%s", MIN_BUY_VALUE, WATCH_TICKERS or "ALL")
    send_message(bot, "✅ Insider alert bot is running.")

    while True:
        try:
            feed = feedparser.parse(SEC_ATOM_URL)
            entries = list(reversed(feed.entries))

            for entry in entries:
                entry_id = entry.get("id") or entry.get("link")
                filing_url = entry.get("link")
                if not entry_id or not filing_url or entry_id in seen:
                    continue

                xml_url = get_json_url(filing_url)
                if not xml_url:
                    seen.add(entry_id)
                    continue

                data = parse_form4_xml(xml_url)
                seen.add(entry_id)

                if not data:
                    continue

                ticker = data.get("ticker", "").upper()
                total_value = data.get("total_value", 0)

                if WATCH_TICKERS and ticker not in WATCH_TICKERS:
                    continue

                if total_value < MIN_BUY_VALUE:
                    continue

                msg = format_alert(data, filing_url)
                send_message(bot, msg)
                logging.info("Sent alert for %s %s", ticker, total_value)

            save_seen(seen)

        except Exception as e:
            logging.exception("Loop error: %s", e)

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
