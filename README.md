# Agoda Price Tracker

Tracks the lowest available room price on Agoda for a hardcoded list of Singapore hotels across a list of desired check-in dates. Sends a Telegram alert when the price moves more than a configurable percentage (default 10%).

Runs entirely on **GitHub Actions** — no server, no database. Price history is committed back to the repo as CSV.

## How it works

1. Cron triggers `.github/workflows/check-prices.yml` at `:05` past every hour.
2. The workflow runs `src/check_prices.py`, which:
   - Loads `config/hotels.yaml`, `config/bookings.yaml`, `config/settings.yaml`.
   - Launches headless Chromium via Playwright.
   - For each `(hotel_id, check_in)` pair, navigates to the Agoda hotel page and intercepts the internal `GetSecondaryData` XHR (with an HTML fallback).
   - Compares the new price to `data/last_prices.json` and sends a Telegram alert if `|Δ%| ≥ alert_threshold_pct`.
   - Appends the observation to `data/history.csv`.
3. The workflow commits `data/` back to the repo so state survives between runs.

## Setup

### 1. Fork or push this repo to GitHub

Make sure the repo has Actions enabled (Settings → Actions → General → Allow all actions).

### 2. Create a Telegram bot

1. Message [@BotFather](https://t.me/BotFather) → `/newbot` → follow prompts → save the **bot token**.
2. Send any message to your new bot.
3. Open `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser. Look for `chat.id` in the JSON. Save the **chat ID**.

### 3. Add GitHub secrets

In your repo: **Settings → Secrets and variables → Actions → New repository secret**

| Name | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Token from BotFather |
| `TELEGRAM_CHAT_ID` | Your chat ID |

### 4. Configure hotels and dates

Edit:
- `config/hotels.yaml` — list of Agoda hotel IDs and your desired price ranges.
- `config/bookings.yaml` — list of check-in dates you're tracking.
- `config/settings.yaml` — alert threshold, currency, retry counts.

To find a hotel ID: open the Agoda page for the hotel, then check the URL or page source for `hotelid=` / `hid=`.

### 5. Trigger the first run

Go to **Actions → Check Agoda Prices → Run workflow**. The first run won't fire alerts (no baseline) but will populate `data/last_prices.json`. Subsequent hourly runs will compare against it.

## Local testing

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install --with-deps chromium

# Optional — alerts go to Telegram if these are set, otherwise just logged
export TELEGRAM_BOT_TOKEN=...
export TELEGRAM_CHAT_ID=...

python -m src.check_prices
```

## Files

```
.github/workflows/check-prices.yml   Hourly cron + commit-back
config/
  hotels.yaml                        Hardcoded hotel list + desired ranges
  bookings.yaml                      Dates to track
  settings.yaml                      Threshold, currency, timeouts
src/
  check_prices.py                    Main orchestrator
  scraper.py                         Playwright + XHR intercept
  notifier.py                        Telegram sender (no SDK)
  state.py                           Last-price + history persistence
data/
  last_prices.json                   Most recent price per (hotel, date)
  history.csv                        Append-only log of every observation
```

## Operational notes

### Cost

GitHub Actions gives 2,000 free minutes/month for private repos and unlimited for public repos. A single run takes ~2–3 minutes for ~10 (hotel × date) pairs. Hourly = 720 runs/month → ~1,800 min/month → fits within the private-repo free tier with little headroom. **If you push past it, make the repo public** — same workflow, no minute cap.

### What can go wrong

| Failure mode | Symptom | Mitigation |
|---|---|---|
| Agoda renames the XHR endpoint | All results have `source=html` or `source=none` | Update `_XHR_PATTERNS` in `scraper.py`. Inspect the network tab on a real visit. |
| GitHub runner IP gets soft-blocked by Agoda | Many `Timeout` / `403` errors | Add a residential proxy via `BROWSER_PROXY` env var (extension left as TODO) |
| HTML structure changes | `source=html` returns garbage | Update regexes in `_extract_min_price_from_html` |
| Hotel sold out for that date | `source=none`, no rooms | Expected — log only, no alert |
| Cron delay (Actions can be 5–15 min late under load) | Run starts at `:18` instead of `:05` | Acceptable for hourly cadence; raise frequency if matters |

### Two-run rule for first alert

The first observation for any (hotel, date) **never alerts** — there's no baseline. So expect alerts to start from the second hour onward.

### Past dates

`bookings.yaml` entries with dates earlier than today are silently skipped. Remove old ones during repo housekeeping.

### Adjusting the schedule

The cron `5 * * * *` means "minute 5 of every hour, UTC". For different cadence, edit `.github/workflows/check-prices.yml`. Examples:
- `*/30 * * * *` — every 30 minutes
- `5 */2 * * *` — every 2 hours, at :05
- `5 0,8,16 * * *` — three times a day

## Limitations / honesty

- Agoda's ToS prohibits automated scraping. This is a personal-use tool at low volume; that doesn't make it allowed, just unlikely to attract attention.
- The XHR endpoint is undocumented and can change without notice. Maintenance is on you.
- Prices are the lowest of any room type. If you want a specific room, change the heuristic in `_extract_min_price_from_xhr`.
- "Member-only" Agoda deals visible when logged in won't be captured. We scrape as an anonymous visitor.
