"""Persistent state. Files live under data/ and are committed back to the repo."""

from __future__ import annotations

import csv
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
LAST_PRICES_FILE = DATA_DIR / "last_prices.json"
HISTORY_FILE = DATA_DIR / "history.csv"


def _key(hotel_id: int, check_in: str) -> str:
    return f"{hotel_id}|{check_in}"


def load_last_prices() -> dict[str, float]:
    """Returns a mapping of '<hotel_id>|<check_in>' -> last observed price."""
    if not LAST_PRICES_FILE.exists():
        return {}
    try:
        with LAST_PRICES_FILE.open() as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Failed to load last prices: %s — starting fresh", exc)
        return {}


def get_last_price(state: dict[str, float], hotel_id: int, check_in: str) -> Optional[float]:
    return state.get(_key(hotel_id, check_in))


def update_last_price(state: dict[str, float], hotel_id: int, check_in: str, price: float) -> None:
    state[_key(hotel_id, check_in)] = price


def save_last_prices(state: dict[str, float]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with LAST_PRICES_FILE.open("w") as f:
        json.dump(state, f, indent=2, sort_keys=True)


def append_history(rows: list[dict]) -> None:
    """Append observations to history.csv (creates with header on first run)."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "timestamp_utc",
        "hotel_id",
        "hotel_name",
        "check_in",
        "price",
        "currency",
        "source",
        "rooms_seen",
        "error",
    ]
    new_file = not HISTORY_FILE.exists()
    with HISTORY_FILE.open("a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if new_file:
            writer.writeheader()
        for row in rows:
            writer.writerow(row)


def now_iso_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
