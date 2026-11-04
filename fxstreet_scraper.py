#!/usr/bin/env python3
"""
FXStreet Economic Calendar Scraper.
Uses Playwright to bypass Cloudflare, extracts events from HTML tables,
scores USD bias, writes JSON for MT5 EA consumption.
Run every 15-30 min via Task Scheduler.
"""

import asyncio, json, os, sys, re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from playwright.async_api import async_playwright

URL = "https://www.fxstreet.com/economic-calendar"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT = os.environ.get("OUTPUT_PATH", os.path.join(SCRIPT_DIR, "fxstreet_calendar.json"))
os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)

MONTH_MAP = {"JANUARY":1,"FEBRUARY":2,"MARCH":3,"APRIL":4,"MAY":5,"JUNE":6,"JULY":7,"AUGUST":8,"SEPTEMBER":9,"OCTOBER":10,"NOVEMBER":11,"DECEMBER":12}

# Keyword-based impact detection (fxstreet free tier shows all Low)
HIGH_KW = ["fomc","interest rate","employment","cpi","gdp","retail sales","trade balance","consumer confidence","ism ","nfp","non-farm","housing starts","durable goods","industrial production"]
MED_KW = ["building permits","philadelphia","jobless claims","minutes","statement","report","housing","consumer credit","weekly employment"]

# USD bias scoring
BULLISH_KW = ["non-farm","nfp","employment change","cpi","ppi","gdp","retail sales","ism ","consumer confidence","industrial production","housing starts","building permits","durable goods","trade balance","philadelphia fed","jobless claims","unemployment claims"]

def guess_impact(t):
    tl = t.lower()
    for k in HIGH_KW:
        if k in tl: return "High"
    for k in MED_KW:
        if k in tl: return "Medium"
    return "Low"

def score(country, title, impact):
    if country != "USD": return (0, "NEUT")
    tl, w = title.lower(), {"High":2.0,"Medium":1.0,"Low":0.3}.get(impact, 0.3)
    for k in BULLISH_KW:
        if k in tl: return (w, "BULL")
    if "fomc" in tl and "minutes" in tl: return (w*0.6, "BULL")
    if "fomc member" in tl or "rate statement" in tl or "monetary policy" in tl: return (w*0.4, "BULL")
    if "unemployment rate" in tl: return (-w*0.5, "BEAR")
    return (0, "NEUT")

def to_utc(tc, day_str):
    if not day_str or not tc: return ""
    tc = tc.strip()
    if re.match(r"\d+'$", tc): return ""
    m = re.search(r"(\w+), (\w+) (\d+)", day_str)
    if not m: return ""
    month, day_num = MONTH_MAP.get(m.group(2).upper(), 1), int(m.group(3))
    year = datetime.now(timezone.utc).year
    up = tc.upper()
    is_pm = "PM" in up
    tclean = up.replace("PM","").replace("AM","").strip()
    parts = tclean.split(":")
    if not parts: return ""
    try:
        hour = int(parts[0]) % 12
        minute = int(parts[1]) if len(parts) > 1 else 0
    except ValueError:
        return ""
    if is_pm: hour += 12
    try:
        local = datetime(year, month, day_num, hour, minute, tzinfo=ZoneInfo("America/New_York"))
        return local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return ""

async def scrape():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=(os.environ.get("HEADED") != "1"), args=["--disable-blink-features=AutomationControlled"])
        ctx = await browser.new_context(timezone_id="America/New_York", user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
        page = await ctx.new_page()
        await page.goto(URL, timeout=45000, wait_until="domcontentloaded")
        try:
            await page.wait_for_selector("table", timeout=15000)
            await page.wait_for_timeout(3000)
        except:
            pass

        print("DEBUG title:", await page.title(), "| url:", page.url)
        try:
            body = (await page.inner_text("body"))[:300].replace("\n", " ")
        except Exception:
            body = ""
        print("DEBUG body:", body)

        rows = await page.eval_on_selector_all("table tbody tr", """
            els => els.map(tr => {
                const cells = Array.from(tr.querySelectorAll('td'));
                return cells.map(td => {
                    const impactSpan = td.querySelector('span.fxs_c_impact-icon');
                    return {
                        text: td.innerText.replace(/\\t/g, ' ').trim(),
                        classList: td.className || '',
                        impactClass: impactSpan ? Array.from(impactSpan.classList).join(' ') : '',
                        colspan: td.getAttribute('colspan') || '',
                    };
                });
            })
        """)
        print("DEBUG table rows:", len(rows))
        await browser.close()

    events, current_day = [], ""

    for cells in rows:
        if not cells or len(cells) < 3:
            continue

        # Day header in its own row with colspan
        c0t = cells[0].get("text", "")
        dm = re.search(r"([A-Z]+DAY, \w+ \d+)", c0t)
        if dm:
            current_day = dm.group(1)
            continue

        # Time cell check
        if "fxs_c_time" not in cells[0].get("classList", ""):
            # Some rows might have a single time cell spanning
            all_text = " ".join(c["text"] for c in cells)
            dm2 = re.search(r"([A-Z]+DAY, \w+ \d+)", all_text)
            if dm2:
                current_day = dm2.group(1)
            continue

        tc = c0t
        if not re.match(r"^\d{1,2}:\d{2}\s*(AM|PM)$", tc, re.IGNORECASE):
            continue

        # Cell layout: [0]time [1]flag [2]currency [3]name [4]impact [5]actual [6]dev [7]consensus [8]previous ...
        country = cells[2]["text"] if len(cells) > 2 else ""
        title = cells[3]["text"] if len(cells) > 3 else ""
        if not title or not country:
            continue

        # Impact from cell[4] class, fallback to keyword
        impact = "Low"
        if len(cells) > 4 and cells[4]["impactClass"]:
            ic = cells[4]["impactClass"]
            if "impact-high" in ic: impact = "High"
            elif "impact-medium" in ic: impact = "Medium"
        impact = guess_impact(title)  # keyword overrides free-tier "Low"

        # Data extraction from cells[5:] which are actual/dev/consensus/previous
        actual = forecast = previous = ""
        has_colspan = cells[5]["colspan"] if len(cells) > 5 else ""

        if has_colspan:
            # Locked/premium event, no data
            pass
        elif len(cells) >= 9:
            # Full data row
            actual = cells[5]["text"] if cells[5]["text"] != "-" else ""
            forecast = cells[7]["text"] if cells[7]["text"] != "-" else ""
            previous = cells[8]["text"] if cells[8]["text"] != "-" else ""
        elif len(cells) >= 7:
            actual = cells[5]["text"] if cells[5]["text"] != "-" else ""
            previous = cells[6]["text"] if cells[6]["text"] != "-" else ""

        b, lbl = score(country, title, impact)
        utc = to_utc(tc, current_day)
        events.append({"title":title,"country":country,"date":utc,"impact":impact,"forecast":forecast,"previous":previous,"bias":b,"label":lbl})

    return events

async def main():
    try:
        raw = await scrape()
        seen, out = set(), []
        for e in raw:
            k = (e["title"], e["country"], e["date"])
            if k in seen: continue
            seen.add(k)
            if e["country"]: out.append(e)
        if not out:
            print("ERROR: 0 events parsed; keeping previous file", file=sys.stderr)
            sys.exit(1)
        usd = [e for e in out if e["country"] == "USD"]
        bias = sum(e["bias"] for e in usd)
        with open(OUTPUT, "w", encoding="utf-8") as f:
            json.dump({"fetched":datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),"total_events":len(out),"usd_events":len(usd),"usd_bias":round(bias,2),"events":out}, f, separators=(",",":"), ensure_ascii=True)
        print(f"OK: {len(out)} events ({len(usd)} USD), bias={bias:.2f}")
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
