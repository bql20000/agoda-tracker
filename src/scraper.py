"""
Agoda price scraper.

Strategy:
  1. Navigate to https://www.agoda.com/partners/partnersearch.aspx?... or the
     direct hotel page with check-in/out URL params.
  2. Listen for the XHR to `GetSecondaryData` (Agoda's internal room-grid API)
     and capture the JSON response.
  3. Parse out the lowest available nightly rate.
  4. Fallback: if the XHR doesn't fire (endpoint changed / blocked), parse
     the embedded JSON in the page's <script> tags or visible price elements.

The XHR endpoint name is undocumented and may change. Both extractors are
defensive: anything unparseable returns None so the caller logs and moves on.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Optional

from playwright.async_api import (
    Browser,
    Page,
    Response,
    TimeoutError as PlaywrightTimeout,
    async_playwright,
)

log = logging.getLogger(__name__)


@dataclass
class PriceResult:
    hotel_id: int
    check_in: str   # ISO date
    price: Optional[float]
    currency: str
    source: str     # "xhr" | "html" | "none"
    raw_room_count: int = 0
    error: Optional[str] = None


# Agoda's internal endpoints we care about. Multiple candidates because the
# name has shifted historically (GetSecondaryData, getSecondaryData, etc.).
_XHR_PATTERNS = [
    re.compile(r"/api/.*[Ss]econdary[Dd]ata", re.IGNORECASE),
    re.compile(r"GetRoomGridData", re.IGNORECASE),
    re.compile(r"/Hotel/.*Rooms", re.IGNORECASE),
]


def _build_hotel_url(
    hotel_id: int,
    check_in: str,
    check_out: str,
    currency: str,
    adults: int,
    rooms: int,
) -> str:
    """Build the Agoda hotel URL with search parameters baked in.

    Using the SG locale and explicit cid to skip locale-detection redirects.
    """
    return (
        f"https://www.agoda.com/partners/partnersearch.aspx"
        f"?cid=1844104"
        f"&hl=en-us"
        f"&hid={hotel_id}"
        f"&checkIn={check_in}"
        f"&checkOut={check_out}"
        f"&rooms={rooms}"
        f"&adults={adults}"
        f"&children=0"
        f"&currencyCode={currency}"
    )


def _extract_min_price_from_xhr(payload: Any) -> tuple[Optional[float], int]:
    """Walk the GetSecondaryData JSON and find the cheapest nightly rate.

    Agoda's response shape varies but consistently has rooms with a price
    object containing an 'exclusive' or 'inclusive' nightly amount. We
    recursively walk and collect any number that looks like a rate.

    Returns (min_price, room_count_seen).
    """
    prices: list[float] = []
    room_count = 0

    def walk(node: Any) -> None:
        nonlocal room_count
        if isinstance(node, dict):
            # Heuristic: a "room" object usually has a name + a price block.
            if (
                ("roomName" in node or "masterRoomTypeName" in node)
                and any(k in node for k in ("price", "displayPrice", "perNight"))
            ):
                room_count += 1
            for k, v in node.items():
                if k in ("perNight", "exclusive", "inclusive", "displayPrice", "price"):
                    if isinstance(v, (int, float)) and v > 0:
                        prices.append(float(v))
                    elif isinstance(v, dict):
                        # Nested {"perNight": {"exclusive": 350.0, ...}} style
                        for nested in v.values():
                            if isinstance(nested, (int, float)) and nested > 0:
                                prices.append(float(nested))
                walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    if not prices:
        return None, room_count

    # Reasonable sanity filter — drop outliers below SGD 30 (probably fees,
    # taxes, or per-point amounts) and above 50,000 (probably IDR/VND mixed in).
    sane = [p for p in prices if 30 <= p <= 50_000]
    return (min(sane) if sane else min(prices)), room_count


def _extract_min_price_from_html(html: str) -> tuple[Optional[float], int]:
    """Fallback: parse Agoda's embedded JSON or visible price text from HTML.

    Agoda ships a window-attached state object for SSR. We look for it first,
    then fall back to regex on visible price tags.
    """
    # 1. Try the embedded JSON in script tags
    state_match = re.search(
        r"window\.__INITIAL_STATE__\s*=\s*(\{.+?\})\s*;\s*</script>",
        html,
        re.DOTALL,
    )
    if state_match:
        try:
            state = json.loads(state_match.group(1))
            price, count = _extract_min_price_from_xhr(state)
            if price is not None:
                return price, count
        except json.JSONDecodeError:
            pass

    # 2. Visible price elements: data-element-name="final-price" etc.
    price_re = re.compile(
        r'data-element-name="(?:final-price|display-price)"[^>]*>[^0-9<]*([\d,]+(?:\.\d+)?)',
        re.IGNORECASE,
    )
    matches = [float(m.replace(",", "")) for m in price_re.findall(html)]
    if matches:
        sane = [p for p in matches if 30 <= p <= 50_000]
        return (min(sane) if sane else min(matches)), len(matches)

    return None, 0


async def _scrape_one(
    browser: Browser,
    hotel_id: int,
    check_in: str,
    *,
    currency: str,
    adults: int,
    rooms: int,
    xhr_timeout: float,
) -> PriceResult:
    check_in_date = date.fromisoformat(check_in)
    check_out = (check_in_date + timedelta(days=1)).isoformat()

    url = _build_hotel_url(hotel_id, check_in, check_out, currency, adults, rooms)
    log.debug("Loading %s", url)

    captured_payload: dict[str, Any] = {}

    context = await browser.new_context(
        viewport={"width": 1366, "height": 850},
        locale="en-SG",
        timezone_id="Asia/Singapore",
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0.0.0 Safari/537.36"
        ),
    )
    page: Page = await context.new_page()

    async def on_response(resp: Response) -> None:
        if captured_payload.get("data") is not None:
            return
        if any(pat.search(resp.url) for pat in _XHR_PATTERNS):
            try:
                if resp.ok:
                    captured_payload["data"] = await resp.json()
                    captured_payload["url"] = resp.url
            except Exception as exc:  # noqa: BLE001
                log.debug("Failed to read XHR body for %s: %s", resp.url, exc)

    page.on("response", on_response)

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=45_000)

        # Give the room grid time to render — that's when GetSecondaryData fires.
        try:
            await page.wait_for_load_state("networkidle", timeout=int(xhr_timeout * 1000))
        except PlaywrightTimeout:
            log.debug("networkidle timeout for hotel %s — proceeding anyway", hotel_id)

        # Path 1: XHR was captured
        if captured_payload.get("data") is not None:
            price, count = _extract_min_price_from_xhr(captured_payload["data"])
            if price is not None:
                return PriceResult(
                    hotel_id=hotel_id,
                    check_in=check_in,
                    price=price,
                    currency=currency,
                    source="xhr",
                    raw_room_count=count,
                )

        # Path 2: parse the rendered HTML
        html = await page.content()
        price, count = _extract_min_price_from_html(html)
        if price is not None:
            return PriceResult(
                hotel_id=hotel_id,
                check_in=check_in,
                price=price,
                currency=currency,
                source="html",
                raw_room_count=count,
            )

        return PriceResult(
            hotel_id=hotel_id,
            check_in=check_in,
            price=None,
            currency=currency,
            source="none",
            error="No price found in XHR or HTML — endpoint may have changed or hotel sold out",
        )

    except Exception as exc:  # noqa: BLE001
        return PriceResult(
            hotel_id=hotel_id,
            check_in=check_in,
            price=None,
            currency=currency,
            source="none",
            error=f"{type(exc).__name__}: {exc}",
        )
    finally:
        await context.close()


async def scrape_prices(
    targets: list[tuple[int, str]],
    *,
    currency: str,
    adults: int,
    rooms: int,
    inter_hotel_delay: float,
    xhr_timeout: float,
    retry_count: int,
) -> list[PriceResult]:
    """Scrape prices for every (hotel_id, check_in) pair sequentially."""
    results: list[PriceResult] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
            ],
        )

        for i, (hotel_id, check_in) in enumerate(targets):
            attempt = 0
            result: Optional[PriceResult] = None
            while attempt <= retry_count:
                attempt += 1
                result = await _scrape_one(
                    browser,
                    hotel_id,
                    check_in,
                    currency=currency,
                    adults=adults,
                    rooms=rooms,
                    xhr_timeout=xhr_timeout,
                )
                if result.price is not None:
                    break
                log.warning(
                    "Attempt %d failed for hotel=%s date=%s: %s",
                    attempt,
                    hotel_id,
                    check_in,
                    result.error,
                )
                if attempt <= retry_count:
                    await asyncio.sleep(5)

            assert result is not None
            results.append(result)
            log.info(
                "hotel=%s date=%s price=%s source=%s rooms_seen=%s",
                hotel_id,
                check_in,
                result.price,
                result.source,
                result.raw_room_count,
            )

            # Polite delay between hotels (skip after the last one)
            if i < len(targets) - 1:
                await asyncio.sleep(inter_hotel_delay)

        await browser.close()

    return results
