"""
Process Telegram bot commands and update config/ YAML files accordingly.

Supported commands (only accepted from the configured TELEGRAM_CHAT_ID):

  Date management:
    /adddate YYYY-MM-DD [label]       Start tracking a check-in date (past dates rejected)
    /removedate YYYY-MM-DD            Stop tracking a date
    /listdates                        Show all currently tracked dates
    /cleardates                       Remove ALL tracked dates

  Hotel management:
    /listhotels                       Show all hotels with their index and target max price
    /setprice <index> <max>           Update target max price for a hotel by its index
                                      Example: /setprice 1 700
    /lastprices                       Show the last observed price for every hotel × date

  General:
    /help                             Show the command reference

Run via:
  python -m src.telegram_commands

Returns exit code 0 always; errors are logged but not fatal.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from src.state import load_last_prices as _load_last_prices

log = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
OFFSET_FILE = DATA_DIR / "telegram_offset.json"
BOOKINGS_FILE = CONFIG_DIR / "bookings.yaml"
HOTELS_FILE = CONFIG_DIR / "hotels.yaml"

SOLD_OUT_PRICE = 9999.0


# ---------------------------------------------------------------------------
# Offset state (tracks last-processed Telegram update_id)
# ---------------------------------------------------------------------------

def _load_offset() -> int:
    if not OFFSET_FILE.exists():
        return 0
    try:
        with OFFSET_FILE.open() as f:
            return int(json.load(f).get("offset", 0))
    except Exception as exc:
        log.warning("Could not read offset file: %s — starting from 0", exc)
        return 0


def _save_offset(offset: int) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with OFFSET_FILE.open("w") as f:
        json.dump({"offset": offset}, f)


# ---------------------------------------------------------------------------
# Telegram HTTP helpers
# ---------------------------------------------------------------------------

def _tg_post(token: str, method: str, params: dict[str, Any]) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def _send(token: str, chat_id: str, text: str) -> None:
    try:
        _tg_post(token, "sendMessage", {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": "true",
        })
    except Exception as exc:
        log.error("Failed to send Telegram reply: %s", exc)


# ---------------------------------------------------------------------------
# Bookings YAML read/write
# ---------------------------------------------------------------------------

def _load_bookings() -> list[dict]:
    if not BOOKINGS_FILE.exists():
        return []
    with BOOKINGS_FILE.open() as f:
        cfg = yaml.safe_load(f) or {}
    return cfg.get("bookings") or []


def _save_bookings(bookings: list[dict]) -> None:
    """Rewrite bookings.yaml cleanly, sorted by date."""
    bookings = sorted(bookings, key=lambda b: b["date"])
    lines = [
        "# Booking dates managed via Telegram bot commands.",
        "# Use /adddate YYYY-MM-DD [label] to add, /removedate YYYY-MM-DD to remove.",
        "",
        "bookings:",
    ]
    if bookings:
        for b in bookings:
            lines.append(f'  - date: "{b["date"]}"')
            if b.get("label"):
                lines.append(f'    label: "{b["label"]}"')
    else:
        lines.append("  []")
    lines.append("")  # trailing newline
    BOOKINGS_FILE.write_text("\n".join(lines))


# ---------------------------------------------------------------------------
# Hotels YAML read/write
# ---------------------------------------------------------------------------

def _load_hotels() -> list[dict]:
    if not HOTELS_FILE.exists():
        return []
    with HOTELS_FILE.open() as f:
        cfg = yaml.safe_load(f) or {}
    return cfg.get("hotels") or []


def _save_hotels(hotels: list[dict]) -> None:
    """Rewrite hotels.yaml cleanly, preserving all fields except desired_price_min."""
    lines = [
        "# Hotel list managed via Telegram bot commands.",
        "# Use /setprice <index> <max> to update the target max price.",
        "# To add or remove hotels, edit this file directly.",
        "",
        "hotels:",
    ]
    for h in hotels:
        lines.append(f'  - id: {h["id"]}')
        lines.append(f'    name: "{h["name"]}"')
        if "desired_price_max" in h:
            lines.append(f'    desired_price_max: {h["desired_price_max"]}')
    lines.append("")  # trailing newline
    HOTELS_FILE.write_text("\n".join(lines))


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

def _handle_adddate(
    token: str, chat_id: str, args: list[str]
) -> bool:
    """Returns True if bookings.yaml was modified."""
    if not args:
        _send(token, chat_id, "Usage: `/adddate YYYY-MM-DD [label]`\nExample: `/adddate 2026-12-31 New Year Eve`")
        return False

    date_str = args[0]
    label = " ".join(args[1:]) if len(args) > 1 else ""

    # Validate format
    try:
        d = date.fromisoformat(date_str)
    except ValueError:
        _send(token, chat_id, f"❌ Invalid date: `{date_str}`\nPlease use *YYYY-MM-DD* format.")
        return False

    # Hard-reject past dates — no point tracking them.
    if d < date.today():
        _send(
            token, chat_id,
            f"❌ `{date_str}` is in the past. Please provide a future date."
        )
        return False

    bookings = _load_bookings()
    existing_dates = {b["date"] for b in bookings}

    if date_str in existing_dates:
        _send(token, chat_id, f"ℹ️ `{date_str}` is already being tracked.")
        return False

    entry: dict = {"date": date_str}
    if label:
        entry["label"] = label
    bookings.append(entry)

    # Auto-purge any dates that have already passed while we're here.
    today = date.today()
    cleaned = [b for b in bookings if date.fromisoformat(b["date"]) >= today]
    purged = len(bookings) - len(cleaned)
    _save_bookings(cleaned)

    label_str = f" _({label})_" if label else ""
    purge_note = f"\n_Also removed {purged} past date(s) from the list._" if purged else ""
    _send(
        token, chat_id,
        f"✅ Added `{date_str}`{label_str} to tracking.{purge_note}\n"
        "_Prices for all hotels will be checked on the next hourly run._"
    )
    return True


def _handle_removedate(
    token: str, chat_id: str, args: list[str]
) -> bool:
    if not args:
        _send(token, chat_id, "Usage: `/removedate YYYY-MM-DD`")
        return False

    date_str = args[0]
    bookings = _load_bookings()
    existing_dates = {b["date"] for b in bookings}

    if date_str not in existing_dates:
        _send(token, chat_id, f"❌ `{date_str}` is not in your tracked dates.\nUse /listdates to see what's tracked.")
        return False

    bookings = [b for b in bookings if b["date"] != date_str]
    _save_bookings(bookings)
    _send(token, chat_id, f"🗑 Removed `{date_str}` from tracking.")
    return True


def _handle_listdates(token: str, chat_id: str) -> None:
    bookings = _load_bookings()
    today = date.today()

    if not bookings:
        _send(
            token, chat_id,
            "No dates tracked yet.\n\nUse `/adddate YYYY-MM-DD [label]` to start tracking a check-in date."
        )
        return

    upcoming = [b for b in bookings if date.fromisoformat(b["date"]) >= today]
    past = [b for b in bookings if date.fromisoformat(b["date"]) < today]

    lines = ["*📅 Tracked check-in dates:*", ""]
    for b in upcoming:
        label_str = f" — {b['label']}" if b.get("label") else ""
        lines.append(f"  `{b['date']}`{label_str}")

    if not upcoming:
        lines.append("  _(none upcoming)_")

    if past:
        lines.append(f"\n_{len(past)} past date(s) are being auto-skipped._")

    _send(token, chat_id, "\n".join(lines))


def _handle_cleardates(token: str, chat_id: str) -> bool:
    """Remove ALL tracked dates. Returns True if bookings.yaml was modified."""
    bookings = _load_bookings()
    if not bookings:
        _send(token, chat_id, "ℹ️ No dates to clear — the list is already empty.")
        return False

    count = len(bookings)
    _save_bookings([])
    _send(token, chat_id, f"🗑 Cleared all {count} tracked date(s).\nUse `/adddate YYYY-MM-DD [label]` to add new ones.")
    return True


def _handle_listhotels(token: str, chat_id: str) -> None:
    hotels = _load_hotels()
    if not hotels:
        _send(token, chat_id, "No hotels configured.\nEdit `config/hotels.yaml` to add hotels.")
        return

    lines = ["*🏨 Tracked hotels:*", ""]
    for i, h in enumerate(hotels, start=1):
        price_max = h.get("desired_price_max")
        price_str = f"max SGD {price_max:,}" if price_max is not None else "_(no max set)_"
        lines.append(f"*{i}.* {h['name']}")
        lines.append(f"     ID `{h['id']}` · Budget: {price_str}")

    lines.append("")
    lines.append("_Use `/setprice <index> <max>` to update a budget._")
    lines.append("_Example: `/setprice 1 700`_")
    _send(token, chat_id, "\n".join(lines))


def _handle_lastprices(token: str, chat_id: str) -> None:
    """Show the last recorded price for every tracked hotel × date combination."""
    try:
        last_prices = _load_last_prices()
    except Exception as exc:
        log.error("Failed to read last_prices.json: %s", exc)
        _send(token, chat_id, "❌ Could not read price data.")
        return

    if not last_prices:
        _send(token, chat_id, "ℹ️ No price data yet — run a price check first.")
        return

    hotels = _load_hotels()
    hotel_names = {str(h["id"]): h["name"] for h in hotels}

    # Group entries by hotel_id; each value is a PriceEntry dict {"name": ..., "price": float}
    by_hotel: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for key, entry in last_prices.items():
        hotel_id, check_in = key.split("|", 1)
        price: float = entry["price"] if isinstance(entry, dict) else float(entry)
        by_hotel[hotel_id].append((check_in, price))

    lines = ["*💰 Last observed prices:*", ""]
    for hotel_id, entries in sorted(by_hotel.items(), key=lambda kv: hotel_names.get(kv[0], kv[0])):
        name = hotel_names.get(hotel_id, f"Hotel {hotel_id}")
        lines.append(f"*{name}*")
        for check_in, price in sorted(entries):
            if price >= SOLD_OUT_PRICE:
                price_str = "🚫 Sold out"
            else:
                price_str = f"SGD {price:,.0f}"
            lines.append(f"  `{check_in}` — {price_str}")
        lines.append("")

    _send(token, chat_id, "\n".join(lines))


def _handle_setprice(token: str, chat_id: str, args: list[str]) -> bool:
    """Update desired_price_max for a hotel by 1-based index.

    Returns True if hotels.yaml was modified.
    """
    if len(args) != 2:
        _send(
            token, chat_id,
            "Usage: `/setprice <index> <max>`\n"
            "Example: `/setprice 1 700`\n\n"
            "Use /listhotels to see hotel indexes."
        )
        return False

    # Validate index
    try:
        idx = int(args[0])
    except ValueError:
        _send(token, chat_id, f"❌ Index must be a number, got: `{args[0]}`")
        return False

    # Validate max price
    try:
        price_max = float(args[1])
    except ValueError:
        _send(token, chat_id, f"❌ Max price must be a number.\nExample: `/setprice {args[0]} 700`")
        return False

    if price_max < 0:
        _send(token, chat_id, "❌ Price cannot be negative.")
        return False

    hotels = _load_hotels()
    if idx < 1 or idx > len(hotels):
        _send(
            token, chat_id,
            f"❌ Index {idx} is out of range. You have {len(hotels)} hotel(s).\n"
            "Use /listhotels to see valid indexes."
        )
        return False

    hotel = hotels[idx - 1]
    old_max = hotel.get("desired_price_max", "—")

    # Store as int if whole number, float otherwise (keeps YAML tidy)
    hotel["desired_price_max"] = int(price_max) if price_max == int(price_max) else price_max
    _save_hotels(hotels)

    _send(
        token, chat_id,
        f"✅ Updated budget for *{hotel['name']}*\n"
        f"Before: SGD {old_max}\n"
        f"After:  SGD {price_max:,.0f}"
    )
    return True


def _handle_help(token: str, chat_id: str) -> None:
    _send(
        token, chat_id,
        "*🏨 Agoda Price Tracker — commands:*\n\n"
        "*Dates*\n"
        "`/adddate YYYY-MM-DD [label]`\n"
        "  Start tracking a check-in date\n\n"
        "`/removedate YYYY-MM-DD`\n"
        "  Stop tracking a specific date\n\n"
        "`/listdates`\n"
        "  Show all tracked check-in dates\n\n"
        "`/cleardates`\n"
        "  Remove *all* tracked dates at once\n\n"
        "*Hotels*\n"
        "`/listhotels`\n"
        "  Show all hotels with their index and target price ranges\n\n"
        "`/setprice <index> <max>`\n"
        "  Update the budget max price for a hotel\n"
        "  Example: `/setprice 1 700`\n\n"
        "`/lastprices`\n"
        "  Show the last observed price for every hotel × date\n"
        "  (🚫 Sold out shown when no rooms were available)\n\n"
        "`/help`\n"
        "  Show this message\n\n"
        "_Prices are checked hourly for all hotels × tracked dates. "
        "You'll get an alert when any price moves by ≥10%._"
    )


# ---------------------------------------------------------------------------
# Main poll loop
# ---------------------------------------------------------------------------

def process_commands() -> bool:
    """
    Poll Telegram for new messages, dispatch commands, persist offset.

    Returns True if config/bookings.yaml was modified (so the caller
    can decide whether to commit).
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")

    if not token or not chat_id:
        log.error(
            "TELEGRAM_BOT_TOKEN and/or TELEGRAM_CHAT_ID not set — "
            "cannot process commands."
        )
        return False

    offset = _load_offset()
    log.debug("Polling Telegram from offset %d", offset)

    try:
        result = _tg_post(token, "getUpdates", {
            "offset": offset,
            "timeout": 5,
            "allowed_updates": "message",
        })
    except Exception as exc:
        log.error("Telegram getUpdates failed: %s", exc)
        return False

    updates = result.get("result", [])
    if not updates:
        log.debug("No new Telegram updates.")
        return False

    log.info("Processing %d Telegram update(s)", len(updates))
    bookings_changed = False

    for update in updates:
        update_id: int = update["update_id"]
        offset = update_id + 1

        msg = update.get("message") or {}
        if not msg:
            continue

        # Security: only accept commands from the configured chat ID.
        from_chat = str(msg.get("chat", {}).get("id", ""))
        if from_chat != str(chat_id):
            log.warning("Ignoring message from unknown chat_id=%s", from_chat)
            continue

        text: str = (msg.get("text") or "").strip()
        if not text.startswith("/"):
            continue  # ignore non-commands

        # Strip optional @botname suffix (e.g. /adddate@mybot → /adddate)
        parts = text.split()
        cmd = parts[0].lower().split("@")[0]
        args = parts[1:]

        log.info("Command: %s args=%s", cmd, args)

        if cmd == "/adddate":
            bookings_changed |= _handle_adddate(token, chat_id, args)
        elif cmd == "/removedate":
            bookings_changed |= _handle_removedate(token, chat_id, args)
        elif cmd in ("/listdates", "/list"):
            _handle_listdates(token, chat_id)
        elif cmd == "/cleardates":
            bookings_changed |= _handle_cleardates(token, chat_id)
        elif cmd in ("/listhotels", "/hotels"):
            _handle_listhotels(token, chat_id)
        elif cmd in ("/lastprices", "/prices"):
            _handle_lastprices(token, chat_id)
        elif cmd == "/setprice":
            bookings_changed |= _handle_setprice(token, chat_id, args)
        elif cmd == "/help":
            _handle_help(token, chat_id)
        else:
            _send(
                token, chat_id,
                f"Unknown command: `{cmd}`\nSend /help to see available commands."
            )

    _save_offset(offset)
    return bookings_changed


if __name__ == "__main__":
    import sys
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    process_commands()
    sys.exit(0)
