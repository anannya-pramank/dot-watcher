"""
dot_watcher.py  –  DoT document scraper (fixed)
================================================
Fixes vs original
-----------------
1. year_stop: no longer does `return rows` mid-card-loop; accumulates
   full page then stops pagination — avoids dropping same-page 2026 items.
2. networkidle replaced with explicit selector wait — GoI SPAs have
   continuous background XHRs that prevent networkidle from firing.
3. Stealth headless instead of off-screen headful — works reliably in CI.
4. Bot-detection bypass: AutomationControlled disabled + real UA.
5. force_english isolated from page state — failure never corrupts a load.
6. Per-card errors logged with title snippet, not silently swallowed.
7. Retry on navigation failure (up to 3 attempts per page).
8. Optional Supabase upsert via SUPABASE_URL + SUPABASE_KEY env vars.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

# ── optional Supabase ────────────────────────────────────────────────────────
try:
    from supabase import create_client, Client as SupabaseClient
    _SUPABASE_AVAILABLE = True
except ImportError:
    _SUPABASE_AVAILABLE = False

SUPABASE_URL   = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY   = os.getenv("SUPABASE_KEY", "")
SUPABASE_TABLE = os.getenv("SUPABASE_TABLE", "dot_documents")

# ── test-run controls (set by CI workflow) ────────────────────────────────────
# DOT_MAX_PAGES=1  → scrape only the first page of every section (fast smoke test)
# DOT_SKIP_SUPABASE=true → parse + write CSV/JSON but never touch Supabase
MAX_PAGES     : int | None = int(os.getenv("DOT_MAX_PAGES", "0")) or None  # None = unlimited
SKIP_SUPABASE : bool       = os.getenv("DOT_SKIP_SUPABASE", "").lower() in ("1", "true")

# ── config ───────────────────────────────────────────────────────────────────

DATA_DIR = Path("dot")
CSV_FILE = DATA_DIR / "dot_master.csv"
JSON_FILE = DATA_DIR / "dot_new.json"
DEBUG_DIR = DATA_DIR / "debug"   # screenshots land here

SECTIONS = [
    ("ORDERS_AND_NOTICES",      "https://www.dot.gov.in/documents/orders-and-notices?page=",  True),
    ("REPORTS",                  "https://www.dot.gov.in/documents?page=",                     True),
    ("ACTS_AND_POLICIES",        "https://www.dot.gov.in/documents/acts-and-policies?page=",   True),
    ("PUBLICATIONS",             "https://www.dot.gov.in/documents/publications?page=",        True),
    ("PRESS_RELEASE",            "https://www.dot.gov.in/documents/press-release?page=",       True),
    ("GUIDELINES",               "https://www.dot.gov.in/documents/guidelines?page=",          True),
    ("GAZETTES_NOTIFICATIONS",   "https://www.dot.gov.in/documents/gazettes-notifications?page=", False),
]

VIEWPORT = {"width": 1400, "height": 900}
NAV_TIMEOUT  = 90_000   # ms
CARD_WAIT    = 30_000   # ms – how long to wait for first card after load
INTER_PAGE   = 2_000    # ms – pause between pages
NAV_RETRIES  = 3

# Real browser UA so Cloudflare doesn't finger-print headless
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Ordered selector sets – try each in turn for resilience against CSS renames
CARD_SELECTORS  = ["div.announcementbox", "div.announcement-box", "div.document-card",
                   "article.document", "div.views-row"]
TITLE_SELECTORS = ["p.mb-0", "h3.document-title", "span.title", "p", "h3", "h4"]
DATE_SELECTORS  = ["small.ptype", "span.date", "small.date", "span.ptype", "time"]
PDF_SELECTORS   = ["a.download-btn", "a[href$='.pdf']", "a.btn-download", "a.pdf-link",
                   "a[href*='/static/uploads/']"]


# ── helpers ──────────────────────────────────────────────────────────────────

def normalize_date(s: str) -> str:
    for fmt in ("%d.%m.%Y", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(s.strip(), fmt).strftime("%m/%d/%Y")
        except Exception:
            pass
    return s.strip() if s else ""


def extract_year(s: str) -> int | None:
    m = re.search(r"(20\d{2})", s or "")
    return int(m.group(1)) if m else None


def make_id(pdf_url: str) -> str:
    return hashlib.sha1(pdf_url.encode()).hexdigest()[:16]


def _try_select(ctx, selectors: list[str], method: str = "query_selector"):
    """Try a list of selectors, return first match or None."""
    for sel in selectors:
        try:
            el = getattr(ctx, method)(sel)
            if el:
                return el
        except Exception:
            pass
    return None


def _try_select_all(ctx, selectors: list[str]) -> list:
    for sel in selectors:
        try:
            els = ctx.query_selector_all(sel)
            if els:
                return els
        except Exception:
            pass
    return []


def save_debug_screenshot(page, label: str) -> None:
    try:
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.utcnow().strftime("%H%M%S")
        path = DEBUG_DIR / f"{label}_{ts}.png"
        page.screenshot(path=str(path), full_page=False)
        print(f"📸 Screenshot saved: {path}")
    except Exception as e:
        print(f"Screenshot failed: {e}")


# ── language forcing ─────────────────────────────────────────────────────────

def force_english(page) -> None:
    """
    Try to set Bhashini widget to English.
    Completely isolated – any exception is logged and ignored;
    page state is never corrupted by this function.
    """
    try:
        btn = page.locator("button.bhashini-dropdown-btn")
        if btn.count() == 0:
            return  # widget not present on this page
        btn.click(timeout=4_000)
        page.wait_for_selector("li.language-option[data-value='en']", timeout=4_000)
        page.locator("li.language-option[data-value='en']").click()
        page.wait_for_timeout(1_500)
        print("🌐 Language set to English")
    except Exception as e:
        print(f"Language switch skipped ({e})")


# ── content context finder ────────────────────────────────────────────────────

def find_ctx(page, timeout_ms: int = CARD_WAIT):
    """
    Wait for announcement cards to appear anywhere on the page or in frames.
    Returns the context (page or frame) where cards are found, or None.
    """
    deadline = time.monotonic() + timeout_ms / 1000

    while time.monotonic() < deadline:
        for ctx in [page] + list(page.frames):
            try:
                for sel in CARD_SELECTORS:
                    if ctx.query_selector(sel):
                        return ctx
            except Exception:
                pass
        page.wait_for_timeout(800)

    return None


# ── navigation with retry ─────────────────────────────────────────────────────

def safe_goto(page, url: str) -> bool:
    for attempt in range(1, NAV_RETRIES + 1):
        try:
            page.goto(url, timeout=NAV_TIMEOUT, wait_until="domcontentloaded")
            page.wait_for_timeout(INTER_PAGE)
            force_english(page)
            return True
        except Exception as e:
            print(f"  Nav attempt {attempt}/{NAV_RETRIES} failed: {e}")
            if attempt == NAV_RETRIES:
                return False
            page.wait_for_timeout(3_000)

    return False


# ── section scraper ──────────────────────────────────────────────────────────

def scrape_section(page, category: str, base_url: str, year_stop: bool) -> list[dict]:
    rows: list[dict] = []
    page_no = 1

    print(f"\n{'='*50}")
    print(f"  {category}")
    print(f"{'='*50}")

    while True:
        url = base_url + str(page_no)
        print(f"\n── page {page_no} ── {url}")

        if not safe_goto(page, url):
            print("Navigation failed after retries – stopping section")
            save_debug_screenshot(page, f"{category}_nav_fail_p{page_no}")
            break

        ctx = find_ctx(page)

        if ctx is None:
            print("No card context found – stopping section")
            save_debug_screenshot(page, f"{category}_no_ctx_p{page_no}")
            break

        cards = _try_select_all(ctx, CARD_SELECTORS)
        print(f"Cards found: {len(cards)}")

        if not cards:
            print("Empty page – end of section")
            break

        stop_after_this_page = False   # FIX: don't return mid-loop

        for idx, card in enumerate(cards):
            try:
                # ── title ──
                title_el = _try_select(card, TITLE_SELECTORS)
                title = title_el.inner_text().strip() if title_el else ""

                # ── date ──
                date_el = _try_select(card, DATE_SELECTORS)
                date_raw = date_el.inner_text().strip() if date_el else ""

                # ── PDF link ──
                pdf_el = _try_select(card, PDF_SELECTORS)
                pdf = pdf_el.get_attribute("href") if pdf_el else ""

            except Exception as e:
                print(f"  [card {idx}] parse error: {e}")
                continue

            if not pdf:
                continue

            # Ensure absolute URL
            if pdf.startswith("/"):
                pdf = "https://www.dot.gov.in" + pdf

            # ── year gate ──
            # FIX: set flag, don't return early — process rest of page first
            if year_stop and date_raw:
                y = extract_year(date_raw)
                if y and y < 2026:
                    print(f"  🛑 Found {y} entry – will stop pagination after this page")
                    stop_after_this_page = True
                    continue   # skip this card but keep looping

            row = {
                "id":           make_id(pdf),
                "title":        title,
                "publish_date": normalize_date(date_raw),
                "pdf_url":      pdf,
                "category":     category,
                "scraped_at":   datetime.utcnow().strftime("%m/%d/%Y"),
            }
            rows.append(row)
            print(f"  ✓ {title[:80]}")

        if stop_after_this_page:
            print("Stopping pagination (hit pre-2026 entry)")
            break

        if MAX_PAGES and page_no >= MAX_PAGES:
            print(f"[TEST] MAX_PAGES={MAX_PAGES} reached – stopping section")
            break

        page_no += 1

    return rows


# ── CSV helpers ──────────────────────────────────────────────────────────────

FIELDNAMES = ["id", "title", "publish_date", "pdf_url", "category", "scraped_at"]


def ensure_csv() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    if not CSV_FILE.exists():
        with CSV_FILE.open("w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(FIELDNAMES)


def load_existing_ids() -> set[str]:
    if not CSV_FILE.exists():
        return set()
    with CSV_FILE.open(encoding="utf-8") as f:
        return {r["id"] for r in csv.DictReader(f)}


def append_csv(rows: list[dict]) -> None:
    with CSV_FILE.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDNAMES)
        for r in rows:
            w.writerow(r)


# ── Supabase helpers ──────────────────────────────────────────────────────────

def get_supabase_client() -> "SupabaseClient | None":
    if not _SUPABASE_AVAILABLE:
        print("⚠  supabase-py not installed – skipping Supabase upsert")
        return None
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("⚠  SUPABASE_URL / SUPABASE_KEY not set – skipping Supabase upsert")
        return None
    return create_client(SUPABASE_URL, SUPABASE_KEY)


def upsert_supabase(client: "SupabaseClient", rows: list[dict]) -> None:
    if not rows:
        return
    try:
        result = (
            client.table(SUPABASE_TABLE)
            .upsert(rows, on_conflict="id")
            .execute()
        )
        print(f"✅ Supabase upsert: {len(rows)} rows")
    except Exception as e:
        print(f"❌ Supabase upsert failed: {e}")


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    if MAX_PAGES or SKIP_SUPABASE:
        print("=" * 50)
        print("  🧪 TEST RUN")
        if MAX_PAGES:
            print(f"     MAX_PAGES      = {MAX_PAGES} per section")
        if SKIP_SUPABASE:
            print("     SKIP_SUPABASE  = true")
        print("=" * 50)

    ensure_csv()
    existing_ids = load_existing_ids()
    sb = get_supabase_client()

    all_scraped: list[dict] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                # Bot-detection bypass
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
            ],
        )

        context = browser.new_context(
            viewport=VIEWPORT,
            user_agent=USER_AGENT,
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
            # Mask automation signals
            java_script_enabled=True,
        )

        # Remove `navigator.webdriver` flag
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        page = context.new_page()

        for category, base_url, year_stop in SECTIONS:
            section_rows = scrape_section(page, category, base_url, year_stop)
            all_scraped.extend(section_rows)

        browser.close()

    # ── dedupe & persist ──────────────────────────────────────────────────────
    new_rows = [r for r in all_scraped if r["id"] not in existing_ids]

    print(f"\n{'='*50}")
    print(f"Total scraped : {len(all_scraped)}")
    print(f"New (deduped) : {len(new_rows)}")

    if new_rows:
        append_csv(new_rows)
        print(f"CSV updated   : {CSV_FILE}")

        if sb and not SKIP_SUPABASE:
            upsert_supabase(sb, new_rows)
        elif SKIP_SUPABASE:
            print("⏭  Supabase upsert skipped (DOT_SKIP_SUPABASE=true)")

    JSON_FILE.write_text(
        json.dumps(
            {
                "generated_at": datetime.utcnow().isoformat(),
                "count": len(new_rows),
                "items": new_rows,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"JSON updated  : {JSON_FILE}")


if __name__ == "__main__":
    main()
