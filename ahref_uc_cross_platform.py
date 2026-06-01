"""
Cross-platform (Windows + Linux) Ahrefs Website Authority Checker
=================================================================

Uses **undetected-chromedriver** (uc) with a built-in Cloudflare Turnstile
solver pattern (move-mouse-into-iframe + click checkbox + wait-for-token).

Why a separate script?
----------------------
The existing `lambda_handler.py` relies on **nodriver + Xvfb**, which is
Linux-only. This file uses `undetected-chromedriver` + Selenium, which
works natively on both Windows and Linux without Xvfb.

Features
--------
- Auto-detects Chrome on Windows (Program Files) and Linux (/usr/bin/...)
- Uses tempfile.gettempdir() instead of hardcoded /tmp
- Runs N parallel browser windows (configurable)
- Solves Cloudflare Turnstile via mouse-move + click into the iframe
- Streams each result into a JSON array (always-valid file on disk)
- Prints total wall-clock + throughput summary at the end

Install
-------
    pip install undetected-chromedriver selenium

Usage
-----
    # Single domain
    python ahref_uc_cross_platform.py --domain example.com

    # Bulk from file (one domain per line) with 6 parallel windows
    python ahref_uc_cross_platform.py --domains domains_500.txt \
        --workers 6 --out results_uc.json

    # Visible browser (default = visible because Turnstile detects headless)
    python ahref_uc_cross_platform.py --domains domains_500.txt --workers 6

    # Try headless (may fail Turnstile, but supported via uc's --headless=new)
    python ahref_uc_cross_platform.py --domains domains_500.txt --headless

Notes
-----
- Windows: 6 visible Chrome windows will appear (no Xvfb on Windows).
  Minimize them; they keep working in the background.
- Linux desktop: same — windows pop up unless DISPLAY is unset, in which
  case you should still use the Xvfb-based lambda_handler.py for
  best Turnstile bypass.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import random
import shutil
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# --- Heavy deps imported lazily inside workers so --help is fast ---


# ===========================================================================
# Cross-platform helpers
# ===========================================================================
def find_chrome_binary() -> Optional[str]:
    """Return the first existing Chrome/Chromium binary path for this OS."""
    candidates: List[str] = []
    system = platform.system()
    if system == "Windows":
        for env in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            base = os.environ.get(env, "")
            if base:
                candidates += [
                    os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"),
                    os.path.join(base, "Chromium", "Application", "chrome.exe"),
                    os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"),
                ]
    elif system == "Linux":
        candidates += [
            "/usr/bin/google-chrome-stable",
            "/usr/bin/google-chrome",
            "/usr/bin/chromium-browser",
            "/usr/bin/chromium",
            "/opt/google/chrome/chrome",
            "/snap/bin/chromium",
        ]
    elif system == "Darwin":
        candidates += [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return None


def worker_profile_dir(worker_id: int) -> str:
    """Cross-platform per-worker Chrome profile dir."""
    base = tempfile.gettempdir()  # /tmp on Linux, %TEMP% on Windows
    p = os.path.join(base, f"uc_profile_w{worker_id}")
    shutil.rmtree(p, ignore_errors=True)
    os.makedirs(p, exist_ok=True)
    return p


# ===========================================================================
# Streaming JSON sink (thread-safe, file always valid JSON array)
# ===========================================================================
class StreamingJsonArray:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._count = 0
        self.path.write_text("[]")

    def append(self, obj: Dict[str, Any]) -> int:
        with self._lock:
            data = json.loads(self.path.read_text() or "[]")
            data.append(obj)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(data, indent=2, default=str))
            tmp.replace(self.path)
            self._count = len(data)
            return self._count


# ===========================================================================
# Cloudflare Turnstile solver
#   Strategy: find the cf-turnstile iframe, move mouse into it via
#   ActionChains, click the checkbox at a randomized offset, wait for the
#   token <input name="cf-turnstile-response"> to populate.
# ===========================================================================
def solve_turnstile(driver, max_wait_s: int = 40) -> bool:
    """Returns True when Turnstile is solved (token present), else False."""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.action_chains import ActionChains
    from selenium.common.exceptions import (
        NoSuchElementException,
        StaleElementReferenceException,
        WebDriverException,
    )

    deadline = time.time() + max_wait_s

    # First, is there already a token? Sometimes CF auto-passes.
    def token_present() -> bool:
        try:
            tokens = driver.find_elements(
                By.CSS_SELECTOR, 'input[name="cf-turnstile-response"]'
            )
            for t in tokens:
                val = (t.get_attribute("value") or "").strip()
                if val:
                    return True
        except WebDriverException:
            pass
        return False

    if token_present():
        return True

    iframe = None
    while time.time() < deadline and iframe is None:
        try:
            iframes = driver.find_elements(By.TAG_NAME, "iframe")
            for f in iframes:
                src = (f.get_attribute("src") or "")
                if "challenges.cloudflare.com" in src or "turnstile" in src.lower():
                    iframe = f
                    break
        except WebDriverException:
            pass
        if iframe is None:
            time.sleep(0.4)

    if iframe is None:
        # No turnstile present → page might already be open
        return token_present()

    # Move mouse into iframe with small randomised offset (mimics human)
    for _ in range(3):
        if token_present():
            return True
        try:
            ac = ActionChains(driver, duration=120)
            # Move to iframe top-left then randomised offset into checkbox area
            ac.move_to_element_with_offset(
                iframe,
                random.randint(20, 40),
                random.randint(20, 40),
            )
            ac.pause(0.2 + random.random() * 0.4)
            ac.click()
            ac.perform()
        except (StaleElementReferenceException, WebDriverException):
            pass
        # Poll for token
        t0 = time.time()
        while time.time() - t0 < 10 and time.time() < deadline:
            if token_present():
                return True
            time.sleep(0.5)

    return token_present()


# ===========================================================================
# Load the ahrefs.json script (same fast script used by lambda_handler)
# ===========================================================================
AHREFS_JSON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ahrefs.json")


def load_ahrefs_js() -> str:
    """Load the extraction JS from ahrefs.json (uses nativeSetter for instant input)."""
    with open(AHREFS_JSON_PATH, "r", encoding="utf-8") as fh:
        spec = json.load(fh)
    action = next((a for a in spec.get("actions", []) if a.get("type") == "evaluate"), None)
    if not action or "script" not in action:
        raise RuntimeError("ahrefs.json missing the 'evaluate' action / script")
    return action["script"]


# ===========================================================================
# Build a uc driver — works on Windows and Linux
# ===========================================================================
def detect_chrome_major(chrome_binary: Optional[str]) -> Optional[int]:
    """Return the major version of the given Chrome binary, e.g. 147."""
    import subprocess, re
    if not chrome_binary:
        return None
    try:
        out = subprocess.check_output([chrome_binary, "--version"], text=True, timeout=5)
        m = re.search(r"(\d+)\.", out)
        return int(m.group(1)) if m else None
    except Exception:
        return None


def build_driver(worker_id: int, headless: bool, chrome_binary: Optional[str],
                 version_main: Optional[int] = None):
    import undetected_chromedriver as uc

    opts = uc.ChromeOptions()
    # "eager" = don't wait for subresources/iframes (fixes Windows hang
    # where Turnstile iframe keeps page in "loading" state forever)
    opts.page_load_strategy = "eager"
    profile = worker_profile_dir(worker_id)
    opts.add_argument(f"--user-data-dir={profile}")
    opts.add_argument("--no-first-run")
    opts.add_argument("--no-default-browser-check")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--disable-infobars")
    opts.add_argument("--start-maximized")
    opts.add_argument("--lang=en-US")
    # Tile the windows so they don't all stack on top of each other
    col = worker_id % 3
    row = worker_id // 3
    opts.add_argument(f"--window-position={col * 720},{row * 560}")
    opts.add_argument("--window-size=1280,900")

    if chrome_binary:
        opts.binary_location = chrome_binary

    driver = uc.Chrome(options=opts, headless=headless, use_subprocess=True,
                       version_main=version_main)
    driver.set_page_load_timeout(30)
    driver.set_script_timeout(120)
    return driver


# ===========================================================================
# Per-domain scrape — mirrors lambda_handler's parallel approach:
#   1. Load page, wait briefly for input
#   2. Kick off ahrefs.json script (non-blocking via executeScript)
#   3. Poll for result while solving Turnstile in parallel
# ===========================================================================
def scrape_one(worker_id: int, domain: str, headless: bool, chrome_binary: Optional[str],
               version_main: Optional[int] = None) -> Dict[str, Any]:
    t0 = time.time()
    row: Dict[str, Any] = {
        "worker": worker_id,
        "domain": domain,
        "finished_at": None,
        "status": "error",
    }
    driver = None
    try:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.action_chains import ActionChains
        from selenium.common.exceptions import WebDriverException

        driver = build_driver(worker_id, headless=headless, chrome_binary=chrome_binary,
                              version_main=version_main)
        try:
            driver.get("https://ahrefs.com/website-authority-checker/")
        except Exception:
            # On Windows, page load can timeout even with eager strategy
            # if Turnstile blocks DOMContentLoaded — page is still usable
            pass

        # Give the SPA a moment to render (Windows is slower to paint)
        time.sleep(2)

        # Wait for input (max 10s — Windows needs more time)
        deadline = time.time() + 10
        while time.time() < deadline:
            has_input = driver.execute_script(
                "return !!document.querySelector(\"input[type='text']\")"
            )
            if has_input:
                break
            time.sleep(0.3)

        # Load and kick off the ahrefs.json script (non-blocking)
        js_template = load_ahrefs_js()
        js_payload = js_template.replace("${domains}", domain)

        # Stub the TURNSTILE_FOCUS_REQUEST so the script doesn't hang
        turnstile_stub = """
            window.addEventListener('message', function(e){
                if (e && e.data && e.data.type === 'TURNSTILE_FOCUS_REQUEST') {
                    window.postMessage({
                        type: 'TURNSTILE_FOCUS_RESPONSE',
                        success: true,
                        reason: 'no_turnstile'
                    }, '*');
                }
            });
        """

        kickoff_js = f"""
            {turnstile_stub}
            window.__ahrefsResult = undefined;
            window.__ahrefsError = undefined;
            (async function() {{
                try {{
                    var r = await (async function() {{
                        {js_payload}
                    }})();
                    window.__ahrefsResult = r;
                }} catch (err) {{
                    window.__ahrefsError = JSON.stringify({{
                        error: String(err && err.message || err),
                        stack: String(err && err.stack || '')
                    }});
                }}
            }})();
        """
        driver.execute_script(kickoff_js)

        # Poll for result while solving Turnstile in parallel
        poll_deadline = time.time() + 60
        last_click = 0.0
        click_count = 0

        while time.time() < poll_deadline:
            # Check if script finished
            try:
                done_raw = driver.execute_script(
                    "return JSON.stringify({r: window.__ahrefsResult, e: window.__ahrefsError})"
                )
                done_payload = json.loads(done_raw) if done_raw else {}
            except Exception:
                time.sleep(1)
                continue

            if done_payload.get("r") is not None:
                raw = done_payload["r"]
                if isinstance(raw, str):
                    try:
                        parsed = json.loads(raw)
                    except json.JSONDecodeError:
                        parsed = {"raw": raw}
                else:
                    parsed = raw
                # Extract results from the ahrefs.json format
                if isinstance(parsed, dict) and "results" in parsed:
                    results = parsed["results"]
                    if results and isinstance(results, list):
                        r = results[0]
                        row["domain"] = r.get("domain_name", domain)
                        row["dr"] = r.get("dr")
                        row["backlinks"] = r.get("backlinks")
                        row["linking_websites"] = r.get("linking_websites")
                        row["backlinks_dofollow_pct"] = r.get("backlinks_dofollow_percentage")
                        row["linking_websites_dofollow_pct"] = r.get("linking_websites_dofollow_percentage")
                        row["status"] = "completed"
                    else:
                        row["error"] = "no results in response"
                elif isinstance(parsed, dict) and parsed.get("error"):
                    row["error"] = parsed["error"]
                else:
                    row.update(parsed) if isinstance(parsed, dict) else None
                    row["status"] = "completed"
                break

            if done_payload.get("e") is not None:
                row["error"] = done_payload["e"]
                break

            # Solve Turnstile in parallel — click iframe if present
            now = time.time()
            if now - last_click > 4:
                try:
                    iframes = driver.find_elements(By.TAG_NAME, "iframe")
                    for f in iframes:
                        src = (f.get_attribute("src") or "")
                        if "challenges.cloudflare.com" in src:
                            ac = ActionChains(driver, duration=100)
                            ac.move_to_element_with_offset(
                                f, random.randint(20, 35), random.randint(15, 25)
                            )
                            ac.pause(0.15)
                            ac.click()
                            ac.perform()
                            click_count += 1
                            last_click = now
                            break
                except (WebDriverException, Exception):
                    pass

            time.sleep(1.0)

    except Exception as e:
        row["error"] = f"{type(e).__name__}: {e}"
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass
        row["elapsed_seconds"] = round(time.time() - t0, 2)
        row["finished_at"] = datetime.now(timezone.utc).isoformat()
    return row


# ===========================================================================
# Main
# ===========================================================================
def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--domain", help="single domain to scrape")
    p.add_argument("--domains", help="path to newline-separated domain list")
    p.add_argument("--workers", type=int, default=6)
    p.add_argument("--headless", action="store_true",
                   help="run headless (often fails Turnstile)")
    p.add_argument("--out", default="results_uc.json")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--chrome", help="explicit path to chrome.exe / google-chrome")
    p.add_argument("--chrome-version", type=int, default=0,
                   help="force undetected-chromedriver to use this Chrome major version (e.g. 147). 0 = auto-detect")
    args = p.parse_args()

    if not args.domain and not args.domains:
        p.error("provide --domain or --domains")

    here = Path(__file__).parent
    out_path = (here / args.out).resolve() if not os.path.isabs(args.out) else Path(args.out)

    # Build domain list
    if args.domains:
        path = Path(args.domains)
        if not path.is_absolute():
            path = (here / args.domains).resolve()
        if not path.exists():
            sys.exit(f"[!] domains file not found: {path}")
        domains = [d.strip() for d in path.read_text().splitlines() if d.strip()]
    else:
        domains = [args.domain.strip()]
    if args.limit:
        domains = domains[: args.limit]

    chrome_bin = args.chrome or find_chrome_binary()
    if not chrome_bin:
        print("[!] Could not locate Chrome — undetected-chromedriver will try auto-detect.")
    else:
        print(f"[*] Chrome binary: {chrome_bin}")

    version_main = args.chrome_version or detect_chrome_major(chrome_bin)
    if version_main:
        print(f"[*] Chrome major: {version_main} (pinning chromedriver to this version)")

    print(f"[*] OS:        {platform.system()} {platform.release()}")
    print(f"[*] Total:     {len(domains)} domain(s)")
    print(f"[*] Workers:   {args.workers}")
    print(f"[*] Headless:  {args.headless}")
    print(f"[*] Output:    {out_path}")
    print()

    sink = StreamingJsonArray(out_path)
    started_at = time.time()

    def task(idx: int, dom: str) -> Dict[str, Any]:
        worker_id = idx % args.workers
        return scrape_one(worker_id, dom, args.headless, chrome_bin, version_main)

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(task, i, d): d for i, d in enumerate(domains)}
        for fut in as_completed(futures):
            row = fut.result()
            done = sink.append(row)
            mark = "OK" if row["status"] == "completed" else "FAIL"
            wall = time.time() - started_at
            eta = (wall / done) * (len(domains) - done) if done else 0
            print(
                f"[{mark:>4}] w{row['worker']} {row['domain']:32} "
                f"{row['elapsed_seconds']:6.1f}s  DR={row.get('dr','-')!s:>4}  "
                f"BL={row.get('backlinks','-')!s:>10}  "
                f"({done}/{len(domains)}, wall {wall/60:.1f}m, ETA {eta/60:.1f}m)"
            )

    wall = time.time() - started_at
    rows = json.loads(out_path.read_text())
    ok = sum(1 for r in rows if r.get("status") == "completed")
    failed = len(rows) - ok
    print()
    print("#" * 72)
    print(f"#  DONE  total={len(domains)}  ok={ok}  failed={failed}")
    print(f"#  wall  = {wall:.1f}s ({wall/60:.2f} min)")
    print(f"#  avg/domain = {wall/max(1,len(domains)):.2f}s")
    print(f"#  throughput = {len(domains)/(wall/60):.1f} domains/min")
    print(f"#  results    = {out_path}")
    print("#" * 72)

    summary_path = out_path.with_suffix(".summary.json")
    summary_path.write_text(json.dumps({
        "os": f"{platform.system()} {platform.release()}",
        "workers": args.workers,
        "headless": args.headless,
        "total": len(domains),
        "ok": ok,
        "failed": failed,
        "wall_clock_seconds": round(wall, 2),
        "wall_clock_minutes": round(wall / 60, 2),
        "avg_seconds_per_domain": round(wall / max(1, len(domains)), 2),
        "throughput_domains_per_min": round(len(domains) / (wall / 60), 2),
        "started_at": datetime.fromtimestamp(started_at, tz=timezone.utc).isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "results_file": str(out_path),
    }, indent=2))
    print(f"[*] summary written to {summary_path}")


if __name__ == "__main__":
    main()
