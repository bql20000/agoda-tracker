"""Entry point. Run via `python -m src.check_prices` from the repo root."""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import date
from pathlib import Path

import yaml

from src.notifier import TelegramNotifier
from src.scraper import PriceResult, scrape_prices
from src.state import (
    append_history,
    get_last_price,
    load_last_prices,
    now_iso_utc,
    save_last_prices,
    update_last_price,
)

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


def _load_yaml(name: str) -> dict:
    with (CONFIG_DIR / name).open() as f:
        return yaml.safe_load(f)


def _setup_logging(debug: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _format_price(price: float | None, currency: str) -> str:
    return f"{currency} {price:,.2f}" if price is not None else "—"


def _alert_message(
    hotel_name: str,
    hotel_id: int,
    check_in: str,
    old_price: float,
    new_price: float,
    currency: str,
    desired_max: float | None,
) -> str:
    delta = new_price - old_price
    pct = (delta / old_price) * 100 if old_price else 0.0
    arrow = "⬇️" if delta < 0 else "⬆️"

    lines = [
        f"{arrow} *{hotel_name}*",
        f"Check-in: `{check_in}`",
        f"Price: {_format_price(old_price, currency)} → *{_format_price(new_price, currency)}*",
        f"Change: {pct:+.1f}% ({delta:+,.2f} {currency})",
    ]

    if desired_max is not None:
        if new_price <= desired_max:
            lines.append(f"✅ Within budget (max {desired_max:,.0f} {currency})")
        else:
            lines.append(f"⚠️ Above budget (max {desired_max:,.0f} {currency})")

    booking_url = (
        f"https://www.agoda.com/partners/partnersearch.aspx"
        f"?hid={hotel_id}&checkIn={check_in}"
    )
    lines.append(f"[Book on Agoda]({booking_url})")
    return "\n".join(lines)


async def main() -> int:
    settings = _load_yaml("settings.yaml")
    hotels_cfg = _load_yaml("hotels.yaml")
    bookings_cfg = _load_yaml("bookings.yaml")

    _setup_logging(bool(settings.get("debug")))
    log = logging.getLogger("check_prices")

    hotels = hotels_cfg["hotels"]
    bookings = bookings_cfg["bookings"]

    # Filter out past dates
    today = date.today()
    upcoming = [b for b in bookings if date.fromisoformat(b["date"]) >= today]
    skipped = len(bookings) - len(upcoming)
    if skipped:
        log.info("Skipped %d past booking date(s)", skipped)

    if not upcoming:
        log.warning("No upcoming bookings — nothing to do.")
        return 0

    # Build the work list: every hotel × every upcoming booking date
    targets: list[tuple[int, str]] = []
    for booking in upcoming:
        for hotel in hotels:
            targets.append((hotel["id"], booking["date"]))

    log.info(
        "Checking %d hotel × date combinations (%d hotels × %d dates)",
        len(targets),
        len(hotels),
        len(upcoming),
    )

    # Run the scraper
    results: list[PriceResult] = await scrape_prices(
        targets,
        currency=settings["currency"],
        adults=settings["adults"],
        rooms=settings["rooms"],
        inter_hotel_delay=float(settings["inter_hotel_delay_seconds"]),
        xhr_timeout=float(settings["xhr_timeout_seconds"]),
        retry_count=int(settings["retry_count"]),
        concurrency=int(settings.get("concurrency", 10)),
    )

    # Compare against last seen, fire alerts, persist state
    last_prices = load_last_prices()
    notifier = TelegramNotifier()
    threshold = float(settings["alert_threshold_pct"])

    hotel_lookup = {h["id"]: h for h in hotels}
    history_rows: list[dict] = []
    alerts_sent = 0
    successes = 0

    for r in results:
        hotel = hotel_lookup[r.hotel_id]
        history_rows.append(
            {
                "timestamp_utc": now_iso_utc(),
                "hotel_id": r.hotel_id,
                "hotel_name": hotel["name"],
                "check_in": r.check_in,
                "price": f"{r.price:.2f}" if r.price is not None else "",
                "currency": r.currency,
                "source": r.source,
                "rooms_seen": r.raw_room_count,
                "error": r.error or "",
            }
        )

        if r.price is None:
            log.warning(
                "No price for hotel=%s (%s) date=%s: %s — recording as sold out (9999)",
                r.hotel_id,
                hotel["name"],
                r.check_in,
                r.error,
            )
            update_last_price(last_prices, r.hotel_id, r.check_in, 9999.0, hotel_name=hotel["name"])
            continue

        successes += 1
        previous = get_last_price(last_prices, r.hotel_id, r.check_in)
        update_last_price(last_prices, r.hotel_id, r.check_in, r.price, hotel_name=hotel["name"])

        if previous is None:
            log.info(
                "First observation for %s on %s: %s %.2f",
                hotel["name"],
                r.check_in,
                r.currency,
                r.price,
            )
            continue

        pct_change = abs(r.price - previous) / previous * 100 if previous else 0
        if pct_change >= threshold:
            msg = _alert_message(
                hotel_name=hotel["name"],
                hotel_id=r.hotel_id,
                check_in=r.check_in,
                old_price=previous,
                new_price=r.price,
                currency=r.currency,
                desired_max=hotel.get("desired_price_max"),
            )
            log.info("ALERT: %s %s %.1f%%", hotel["name"], r.check_in, pct_change)
            if notifier.send(msg):
                alerts_sent += 1

    # Persist
    save_last_prices(last_prices)
    append_history(history_rows)

    log.info(
        "Done. successes=%d/%d alerts_sent=%d",
        successes,
        len(results),
        alerts_sent,
    )

    # Exit non-zero only if we got zero successes — that's a real failure mode
    # worth surfacing in GitHub Actions.
    return 0 if successes > 0 else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
