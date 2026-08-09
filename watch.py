#!/usr/bin/env python3
"""Watch a Cinemark showing for seat openings and newly added dates.

Everything Cinemark serves is plain server-rendered HTML or loaded into the DOM,
so a sweep fetches:
1. Theater page (all sellable dates)
2. Each date's page (showtime ids for the configured movie)
3. Each showtime's seat map (diffing seat availability against previous sweep)

What to watch and which seats qualify comes from config.toml.

State persists in state.json. Alerts append to alerts.log and are sent to a
Discord webhook (if DISCORD_WEBHOOK is set) or ./notify-hook.

Usage:
  python3 watch.py --once             # single sweep (what the CI cron runs)
  python3 watch.py                    # loop forever
  python3 watch.py --report           # print availability from state; no network
  python3 watch.py --dates 2026-08-08 # restrict a sweep (debugging)
  python3 watch.py --debug-ui         # launch browser GUI (headless=False)
"""
from __future__ import annotations

import argparse
import html
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Error as PlaywrightError,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)
from playwright_stealth import Stealth

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore

HERE = Path(__file__).parent
STATE_FILE = HERE / "state.json"
ALERT_LOG = HERE / "alerts.log"

_cfg = tomllib.loads((HERE / "config.toml").read_text())
TARGET, FILTERS, PACING = _cfg["target"], _cfg["filters"], _cfg.get("pacing", {})

THEATER = TARGET["theater"]
MOVIE_ID = str(TARGET["movie_id"])
MOVIE_NAME = TARGET.get("movie_name", f"movie {MOVIE_ID}")
TZ = ZoneInfo(TARGET.get("timezone", "UTC"))
EXCLUDED_ROWS = set(FILTERS.get("excluded_rows", []))
EARLIEST = FILTERS.get("earliest_showtime", "00:00")
LATEST = FILTERS.get("latest_showtime", "23:59")
PARTY_SIZE = int(FILTERS.get("party_size", 1))
REQUEST_GAP = float(PACING.get("request_gap_seconds", 6))
DATE_SCAN_EVERY = int(PACING.get("date_scan_every", 3))
POLL_MINUTES = float(PACING.get("poll_minutes", 5))

BASE = "https://www.cinemark.com"

CLOUDFLARE_MARKERS = [
    "Verify you are human",
    "Just a moment...",
    "cf-mitigated",
    "Attention Required! | Cloudflare",
    "Access denied",
    "Cloudflare Ray ID",
]

DATE_VALUE = re.compile(r'data-datevalue="(\d{4}-\d{2}-\d{2})"')
SHOWTIME_LINK = re.compile(
    r'/TicketSeatMap/\?TheaterId=(\d+)&(?:amp;)?ShowtimeId=(\d+)&(?:amp;)?'
    r'CinemarkMovieId=' + MOVIE_ID + r'&(?:amp;)?Showtime=([\d\-T:]+)'
)
# info="F,12,5,9,635630" = row letter, seat number, physical row, column, showtime
AVAILABLE_SEAT = re.compile(
    r'<button[^>]*class="seatAvailable seatBlock"[^>]*info="([A-Z]+),(\d+),\d+,(\d+),'
)


@dataclass
class Seat:
    row: str
    number: int
    col: int

    @property
    def label(self) -> str:
        return f"{self.row}{self.number}"


def log(msg: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def is_cloudflare_challenge(html: str) -> bool:
    """Check if HTML response matches known Cloudflare bot verification pages."""
    return any(marker.lower() in html.lower() for marker in CLOUDFLARE_MARKERS)


# Global Playwright state & configuration for persistent browser context
HEADLESS_MODE = True
_PLAYWRIGHT_INSTANCE: Playwright | None = None
_BROWSER_INSTANCE: Browser | None = None
_CONTEXT_INSTANCE: BrowserContext | None = None
_PAGE_INSTANCE: Page | None = None


def get_browser_page() -> Page:
    """Initialize or reuse a persistent Playwright Chromium browser page with stealth evasions."""
    global _PLAYWRIGHT_INSTANCE, _BROWSER_INSTANCE, _CONTEXT_INSTANCE, _PAGE_INSTANCE

    if _PAGE_INSTANCE is None or _PAGE_INSTANCE.is_closed():
        if _PLAYWRIGHT_INSTANCE is None:
            _PLAYWRIGHT_INSTANCE = sync_playwright().start()

        if _BROWSER_INSTANCE is None or not _BROWSER_INSTANCE.is_connected():
            _BROWSER_INSTANCE = _PLAYWRIGHT_INSTANCE.chromium.launch(
                headless=HEADLESS_MODE,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-blink-features=AutomationControlled",
                ],
            )

        if _CONTEXT_INSTANCE is None:
            _CONTEXT_INSTANCE = _BROWSER_INSTANCE.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
                ),
                locale="en-US",
                timezone_id=TARGET.get("timezone", "America/Chicago"),
            )

        _PAGE_INSTANCE = _CONTEXT_INSTANCE.new_page()

        # Apply stealth evasions to hide navigator.webdriver & automation flags
        try:
            Stealth().use_sync(_PAGE_INSTANCE)
        except Exception as e:
            log(f"WARN: Failed to apply Stealth evasions: {e!r}")

        _PAGE_INSTANCE.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
        )

    return _PAGE_INSTANCE


def close_browser() -> None:
    """Safely close Playwright browser objects and release resources."""
    global _PLAYWRIGHT_INSTANCE, _BROWSER_INSTANCE, _CONTEXT_INSTANCE, _PAGE_INSTANCE

    if _PAGE_INSTANCE:
        try:
            _PAGE_INSTANCE.close()
        except Exception:
            pass
        _PAGE_INSTANCE = None

    if _CONTEXT_INSTANCE:
        try:
            _CONTEXT_INSTANCE.close()
        except Exception:
            pass
        _CONTEXT_INSTANCE = None

    if _BROWSER_INSTANCE:
        try:
            _BROWSER_INSTANCE.close()
        except Exception:
            pass
        _BROWSER_INSTANCE = None

    if _PLAYWRIGHT_INSTANCE:
        try:
            _PLAYWRIGHT_INSTANCE.stop()
        except Exception:
            pass
        _PLAYWRIGHT_INSTANCE = None


def fetch(url: str, max_retries: int = 5, base_delay: float = 15.0) -> str:
    """Fetch URL using Playwright Chromium with stealth, 30s timeout & exponential backoff."""
    retry_after = 0.0

    for attempt in range(max_retries):
        if attempt > 0:
            exp_wait = base_delay * (2 ** (attempt - 1)) + random.uniform(1.0, 5.0)
            wait = max(exp_wait, retry_after)
            log(f"rate-limited/blocked or error, backing off {wait:.1f}s (attempt {attempt + 1}/{max_retries})")
            time.sleep(wait)

        try:
            page = get_browser_page()
            response = page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(5000)
            status_code = response.status if response else 200

            body = page.content()

            if status_code in (403, 429, 500, 502, 503, 504) or is_cloudflare_challenge(body):
                reason = f"HTTP {status_code}" if status_code != 200 else "Cloudflare Challenge"
                log(f"WARN: fetch attempt {attempt + 1} flagged ({reason}) for {url}")
                close_browser()
                continue

            # Respect request gap pacing
            time.sleep(REQUEST_GAP + (REQUEST_GAP / 2.0) * random.random())
            return body

        except (PlaywrightTimeoutError, PlaywrightError) as e:
            log(f"WARN: Playwright fetch attempt {attempt + 1} for {url} failed: {e!r}")
            close_browser()
            if attempt == max_retries - 1:
                raise RuntimeError(f"Gave up fetching {url} after {max_retries} retries due to Playwright errors.") from e
        except Exception as e:
            log(f"WARN: Unexpected fetch error on attempt {attempt + 1} for {url}: {e!r}")
            close_browser()
            if attempt == max_retries - 1:
                raise RuntimeError(f"Gave up fetching {url} after {max_retries} retries. Last error: {e!r}") from e

    raise RuntimeError(f"Gave up fetching {url} after {max_retries} retries.")


def notify(title: str, message: str, url: str | None = None) -> None:
    log(f"ALERT: {title}: {message}")
    with ALERT_LOG.open("a") as f:
        f.write(f"{datetime.now().isoformat()}  {title}: {message}\n")

    webhook_url = os.environ.get("DISCORD_WEBHOOK")
    if not webhook_url:
        log("WARN: DISCORD_WEBHOOK environment variable not set; skipping Discord notification.")
        return

    book_link = url or f"{BASE}/theatres/{THEATER}"

    payload = {
        "content": "🚨 **CINEMARK TICKET ALERT** 🚨",
        "embeds": [
            {
                "title": f"🎟️ {title}",
                "color": 15158332,  # Crimson Red
                "fields": [
                    {
                        "name": "🎬 Movie",
                        "value": f"**{MOVIE_NAME}**",
                        "inline": True,
                    },
                    {
                        "name": "⏰ Showtime / Event",
                        "value": f"**{title}**",
                        "inline": True,
                    },
                    {
                        "name": "💺 Available Seats / Details",
                        "value": message,
                        "inline": False,
                    },
                    {
                        "name": "🔗 Direct Booking Link",
                        "value": f"[Click Here to Book on Cinemark]({book_link})",
                        "inline": False,
                    },
                ],
                "footer": {
                    "text": "Cinemark Ticket Sniper"
                },
                "timestamp": datetime.now().astimezone().isoformat(),
            }
        ],
    }

    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            webhook_url,
            data=data,
            headers={"Content-Type": "application/json", "User-Agent": "CinemarkTicketSniper/1.0"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status not in (200, 204):
                log(f"WARN: Discord webhook returned HTTP status {resp.status}")
    except Exception as e:  # noqa: BLE001: alerting must never kill the sweep
        log(f"WARN: Failed to send Discord notification: {e!r}")


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"dates": {}, "seats": {}}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=1, sort_keys=True))


def showtimes_for(date: str) -> tuple[str | None, dict[str, str]]:
    """Return (theater_id, {showtime_id: iso_start}) for the movie on a date."""
    page_html = fetch(f"{BASE}/theatres/{THEATER}?showDate={date}")
    theater_id = None
    shows: dict[str, str] = {}

    # 1. Parse data-json-model embedded JSON objects (loosened quote matching & safe ID comparison)
    models = re.findall(r'data-json-model=["\'](.*?)["\']', page_html, re.DOTALL | re.IGNORECASE)
    for model_attr in models:
        try:
            decoded = html.unescape(model_attr)
            data = json.loads(decoded)
            if isinstance(data, dict):
                if str(data.get("cinemarkMovieId", "")) == str(MOVIE_ID):
                    if data.get("theaterId"):
                        theater_id = str(data["theaterId"])
                    for st in data.get("showTimes", []):
                        sid = str(st.get("showtimeId"))
                        iso = st.get("showTime")
                        if sid and iso and sid != "None" and sid != "0":
                            shows[sid] = iso
        except Exception:
            pass

    # 2. Extract active seat map hyperlinks for the targeted movie
    links = SHOWTIME_LINK.findall(page_html)
    for tid, sid, iso in links:
        if not theater_id:
            theater_id = tid
        shows[sid] = iso

    # 3. Global Script & Hydration Fallback regex search
    script_patterns = [
        r'\{[^{}]*?"showtimeId"\s*:\s*(\d+)[^{}]*?"showTime"\s*:\s*"([^"]+)"[^{}]*?\}',
        r'\{[^{}]*?"showTime"\s*:\s*"([^"]+)"[^{}]*?"showtimeId"\s*:\s*(\d+)[^{}]*?\}',
    ]
    for pat in script_patterns:
        for match in re.findall(pat, page_html):
            if pat.startswith(r'\{[^{}]*?"showtimeId"'):
                sid, iso = match[0], match[1]
            else:
                iso, sid = match[0], match[1]
            if sid and iso and sid != "0" and (iso.startswith(date) or iso[:10] == date):
                shows[sid] = iso

    # 4. Fallback theater_id extraction from raw page if not set
    if not theater_id:
        tid_match = re.search(r'TheaterId=(\d+)', page_html) or re.search(r'currentTheaterId\s*=\s*(\d+)', page_html)
        if tid_match:
            theater_id = tid_match.group(1)

    return theater_id, shows


def qualifying(iso: str) -> bool:
    return EARLIEST <= iso[11:16] <= LATEST


def available_seats(theater_id: str, showtime_id: str, iso: str) -> list[Seat]:
    url = (f"{BASE}/TicketSeatMap/?TheaterId={theater_id}&ShowtimeId={showtime_id}"
           f"&CinemarkMovieId={MOVIE_ID}&Showtime={iso}")
    html = fetch(url)
    if "seatBlock" not in html:
        title_match = re.search(r"<title>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
        page_title = title_match.group(1).strip() if title_match else "No Title"
        clean_snippet = re.sub(r"\s+", " ", html[:600]).strip()
        cf_status = "Cloudflare Block" if is_cloudflare_challenge(html) else "DOM change / empty map"

        log(f"WARN: seat map {showtime_id} returned no seat markup ({cf_status}). Title: '{page_title}'")
        log(f"DEBUG HTML Snippet ({len(html)} bytes): {clean_snippet}")
        return []

    return [Seat(row, int(num), int(col))
            for row, num, col in AVAILABLE_SEAT.findall(html)
            if row not in EXCLUDED_ROWS]


def seat_blocks(seats: list[Seat]) -> list[list[Seat]]:
    """Group seats into runs of physically adjacent seats (consecutive columns)."""
    blocks = []
    for row in sorted({s.row for s in seats}):
        run: list[Seat] = []
        for s in sorted((s for s in seats if s.row == row), key=lambda s: s.col):
            if run and s.col != run[-1].col + 1:
                blocks.append(run)
                run = []
            run.append(s)
        blocks.append(run)
    return blocks


def fmt_block(block: list[Seat]) -> str:
    if len(block) == 1:
        return block[0].label
    numbers = sorted(s.number for s in block)
    return f"{block[0].row}{numbers[0]}-{block[0].row}{numbers[-1]}"


def fmt_time(iso: str) -> str:
    return datetime.fromisoformat(iso).strftime("%-I:%M%p").lower()


def prune_past(state: dict) -> None:
    today = datetime.now(TZ).date().isoformat()
    for d in [d for d in state["dates"] if d < today]:
        for sid in state["dates"][d]["showtimes"]:
            state["seats"].pop(sid, None)
        del state["dates"][d]


def sweep(state: dict, scan_dates: bool, only_dates: list[str] | None) -> None:
    first_run = not state["dates"]
    prune_past(state)

    if scan_dates or first_run or only_dates or "theater_id" not in state:
        strip = only_dates or DATE_VALUE.findall(fetch(f"{BASE}/theatres/{THEATER}"))
        for date in sorted(set(strip)):
            existing_entry = state["dates"].get(date, {})
            old_shows = existing_entry.get("showtimes", {})
            try:
                theater_id, shows = showtimes_for(date)
            except Exception as e:  # noqa: BLE001: skip this date, keep sweeping
                log(f"WARN: date probe {date} failed: {e!r}")
                continue

            if theater_id:
                state["theater_id"] = theater_id

            # Detect newly added showtimes (by ISO start time) for notification
            old_isos = set(old_shows.values())
            new_shows_added = [iso for sid, iso in shows.items() if iso not in old_isos]

            # Migrate cached seat records if a showtime ID regenerated for an ISO start
            old_iso_to_sid = {iso: sid for sid, iso in old_shows.items()}
            for new_sid, iso in shows.items():
                old_sid = old_iso_to_sid.get(iso)
                if old_sid and old_sid != new_sid and old_sid in state["seats"]:
                    state["seats"][new_sid] = state["seats"].pop(old_sid)

            # Update state with fresh showtime IDs
            state["dates"][date] = {"showtimes": shows}

            if new_shows_added and not first_run:
                date_url = f"{BASE}/theatres/{THEATER}?showDate={date}"
                notify(f"New date on sale: {date}",
                       f"{MOVIE_NAME} added for {date}: "
                       + ", ".join(sorted(fmt_time(i) for i in new_shows_added)),
                       url=date_url)

        log(f"date scan: tracking "
            f"{sum(1 for d in state['dates'].values() if d['showtimes'])} dates")
        save_state(state)

    watch = [
        (date, sid, iso)
        for date, info in sorted(state["dates"].items())
        for sid, iso in sorted(info["showtimes"].items(), key=lambda kv: kv[1])
        if qualifying(iso) and (not only_dates or date in only_dates)
    ]
    total = 0
    for i, (date, sid, iso) in enumerate(watch):
        try:
            seats = available_seats(state["theater_id"], sid, iso)
        except Exception as e:  # noqa: BLE001: skip this showtime, keep sweeping
            log(f"WARN: seat check {date} {fmt_time(iso)} failed: {e!r}")
            continue
        total += len(seats)
        prev = set(state["seats"].get(sid, []))
        fresh = {s.label for s in seats} - prev
        state["seats"][sid] = sorted(s.label for s in seats)
        openings = [b for b in seat_blocks(seats)
                    if len(b) >= PARTY_SIZE and any(s.label in fresh for s in b)]
        if openings and not first_run:
            seat_map_url = (f"{BASE}/TicketSeatMap/?TheaterId={state['theater_id']}&ShowtimeId={sid}"
                            f"&CinemarkMovieId={MOVIE_ID}&Showtime={iso}")
            notify(f"Seats open {date} {fmt_time(iso)}",
                   f"{MOVIE_NAME}: " + ", ".join(fmt_block(b) for b in openings),
                   url=seat_map_url)
        if i % 10 == 9:
            save_state(state)
    log(f"seat scan: {len(watch)} showtimes checked, {total} qualifying seats")
    if first_run:
        log("first run: baseline recorded, no alerts fired")


def report(state: dict) -> None:
    print(f"\n{MOVIE_NAME} @ {THEATER}")
    print(f"filters: rows {''.join(sorted(EXCLUDED_ROWS)) or 'none'} excluded, "
          f"shows {EARLIEST}-{LATEST}, party of {PARTY_SIZE}\n")
    tracked = {d: v for d, v in sorted(state["dates"].items()) if v["showtimes"]}
    if not tracked:
        print("no dates tracked yet: run a sweep first")
        return
    print(f"on sale: {min(tracked)} to {max(tracked)} ({len(tracked)} dates)\n")
    empty = True
    for d, info in tracked.items():
        for sid, iso in sorted(info["showtimes"].items(), key=lambda kv: kv[1]):
            seats = state["seats"].get(sid, [])
            if qualifying(iso) and seats:
                empty = False
                print(f"  {d} {fmt_time(iso):>8}  {len(seats):>3} seats: "
                      f"{', '.join(seats[:14])}{'...' if len(seats) > 14 else ''}")
    if empty:
        print("no qualifying seats right now: the watcher alerts when one opens")


def main() -> None:
    global HEADLESS_MODE

    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="single sweep, then exit")
    ap.add_argument("--dates", nargs="*", help="restrict to specific YYYY-MM-DD dates")
    ap.add_argument("--report", action="store_true",
                    help="print availability from state.json and exit (no network)")
    ap.add_argument("--debug-ui", action="store_true",
                    help="launch browser with GUI (headless=False) for visual debugging")
    args = ap.parse_args()

    if args.debug_ui:
        HEADLESS_MODE = False
        log("DEBUG: Running with headless=False for visual browser inspection.")

    if args.report:
        report(load_state())
        return

    try:
        while True:
            state = load_state()
            cycle = state.get("cycle", 0)  # persisted so --once runs (CI) keep cadence
            try:
                sweep(state, scan_dates=(cycle % DATE_SCAN_EVERY == 0), only_dates=args.dates)
            except Exception as e:  # noqa: BLE001: keep the loop alive on transient errors
                log(f"ERROR during sweep: {e!r}")
            state["cycle"] = cycle + 1
            save_state(state)
            if args.once:
                return
            time.sleep(POLL_MINUTES * 60)
    finally:
        close_browser()


if __name__ == "__main__":
    main()
