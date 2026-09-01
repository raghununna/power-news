#!/usr/bin/env python3
"""
LinkedIn Feed Capture (OPTIONAL module — runs on YOUR computer)
================================================================
Reads YOUR LinkedIn home feed with your own logged-in session using a
dedicated Playwright browser profile, keeps only posts that match your
topic keywords, and saves them to data/linkedin_posts.json. The next
`agent.py` run merges them into the digest under
"From your LinkedIn feed".

⚠️  IMPORTANT — READ BEFORE USING
  * Automated access to LinkedIn violates their Terms of Service and can
    lead to account restriction or suspension. Use at your own risk.
  * The script is deliberately gentle (one visit, human-like delays,
    no clicking, limited scrolling) — but the risk is never zero.
  * If you prefer zero risk, simply don't use this module; the public
    sources already cover the industry news.

SETUP (only once)
    pip install playwright
    playwright install chromium

FIRST RUN — log in once
    python3 linkedin_reader.py
    A browser window opens -> log into LinkedIn manually (2FA included),
    then press Enter in the terminal. Your session is stored in
    .li-profile/ so future runs are automatic.

AUTOMATE — chain it before the agent, e.g. in cron / Task Scheduler:
    python3 linkedin_reader.py && python3 agent.py
"""

import datetime as dt
import json
import os
import random
import re
import sys
import time

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.exit("Playwright is not installed. Run:\n  pip install playwright\n  playwright install chromium")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROFILE_DIR = os.path.join(BASE_DIR, ".li-profile")
OUT_PATH = os.path.join(BASE_DIR, "data", "linkedin_posts.json")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

# Keywords used to filter your feed down to the topics you care about.
KEYWORD_RE = re.compile(
    r"\b(power electronics|power module|SiC|GaN|IGBT|MOSFET|wide[- ]?bandgap|"
    r"substrate|ceramic|AMB\b|DCB|silicon nitride|aluminium nitride|aluminum nitride|"
    r"sinter|sintering|solder|bonding wire|bond wire|die[- ]attach|lead[- ]?frame|"
    r"thermal interface|encapsulan|molding compound|packaging|"
    r"on[- ]board charger|onboard charger|OBC\b|traction inverter|e[- ]axle|e[- ]drive|"
    r"electric vehicle|\bEV\b|\bBEV\b|powertrain|electrification|e[- ]mobility|"
    r"800\s?v|volt|thermal|Wolfspeed|onsemi|Infineon|ROHM|"
    r"STMicroelectronics|Navitas|Semikron|Danfoss|Vincotech|Curamik|Rogers|Heraeus|"
    r"Indium Corporation|Henkel|Ferrotec|NGK|Toshiba Materials|Denka|Maruwa|Tanaka)\b",
    re.IGNORECASE,
)

MAX_POSTS = 40


def human_pause(a, b):
    time.sleep(random.uniform(a, b))


def is_logged_in(page):
    return page.locator("div.feed-shared-update-v2, div.feed-identity-module").first.is_visible(
        timeout=8000
    )


def collect_posts(page, scroll_times):
    posts = []
    seen_texts = set()
    for _ in range(scroll_times):
        page.mouse.wheel(0, random.randint(1800, 3200))
        human_pause(2.5, 6.0)
        for art in page.locator("div.feed-shared-update-v2").all():
            try:
                text = (art.inner_text(timeout=3000) or "").strip()
            except Exception:  # noqa: BLE001
                continue
            # first 3 lines usually: author / headline / post text
            lines = [l.strip() for l in text.splitlines() if l.strip()]
            if not lines:
                continue
            author = lines[0][:120]
            body = " ".join(lines[1:])[:900]
            key = body[:120]
            if key in seen_texts:
                continue
            if not KEYWORD_RE.search(body):
                continue
            seen_texts.add(key)
            url = ""
            try:
                href = art.locator("a[href*='/feed/update/'], a[href*='/posts/']").first.get_attribute(
                    "href", timeout=2000
                )
                url = href or ""
            except Exception:  # noqa: BLE001
                pass
            posts.append({"author": author, "time": dt.datetime.now().isoformat(timespec="minutes"),
                          "text": body, "url": url})
            if len(posts) >= MAX_POSTS:
                return posts
    return posts


def main():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    li = cfg.get("linkedin", {})
    scroll_times = li.get("scroll_times", 15)

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            PROFILE_DIR,
            headless=False,
            viewport={"width": 1280, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=60000)
        human_pause(2, 4)

        logged_in = False
        try:
            logged_in = is_logged_in(page)
        except Exception:  # noqa: BLE001
            logged_in = False

        if not logged_in:
            print("\n>> Please log in to LinkedIn in the browser window (incl. 2FA).")
            input(">> Press ENTER here once your feed is visible... ")
            human_pause(2, 4)

        print(f">> Scrolling feed ({scroll_times} rounds, human-like pacing)...")
        posts = collect_posts(page, scroll_times)
        print(f">> Kept {len(posts)} on-topic posts.")
        human_pause(1.5, 3)
        ctx.close()

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump({"captured_at": dt.datetime.now(dt.timezone.utc).isoformat(), "posts": posts}, f,
                  ensure_ascii=False, indent=1)
    print(f">> Saved -> {OUT_PATH}")
    print(">> Next `python3 agent.py` run will include these under 'From your LinkedIn feed'.")


if __name__ == "__main__":
    main()
