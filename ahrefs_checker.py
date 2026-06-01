"""
Ahrefs Website Authority Checker — Pull-based local script.

Polls GET /ahref-authority/ for domains, processes in browser, POSTs results back.
Single browser instance reused across all domains.

Usage:
    python ahrefs_checker.py [--api-url URL] [--headless] [--interval 5]
"""

import argparse
import json
import os
import platform
import random
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests

AHREFS_JSON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ahrefs.json")
DEFAULT_API_URL = "http://164.90.252.85/domain-metrics-management-service/api/v1"


def load_ahrefs_js() -> str:
    with open(AHREFS_JSON_PATH, "r", encoding="utf-8") as fh:
        spec = json.load(fh)
    action = next((a for a in spec.get("actions", []) if a.get("type") == "evaluate"), None)
    if not action or "script" not in action:
        raise RuntimeError("ahrefs.json missing the 'evaluate' action / script")
    return action["script"]


def find_chrome_binary() -> Optional[str]:
    system = platform.system()
    candidates: List[str] = []
    if system == "Linux":
        candidates += ["/usr/bin/google-chrome-stable", "/usr/bin/google-chrome", "/usr/bin/chromium-browser"]
    elif system == "Darwin":
        candidates += ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
    elif system == "Windows":
        for env in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            base = os.environ.get(env, "")
            if base:
                candidates.append(os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"))
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return None


def detect_chrome_major(chrome_binary: Optional[str]) -> Optional[int]:
    import subprocess, re
    if not chrome_binary:
        return None
    try:
        out = subprocess.check_output([chrome_binary, "--version"], text=True, timeout=5)
        m = re.search(r"(\d+)\.", out)
        return int(m.group(1)) if m else None
    except Exception:
        return None


def build_driver(headless: bool, chrome_binary: Optional[str], version_main: Optional[int] = None):
    import undetected_chromedriver as uc

    opts = uc.ChromeOptions()
    opts.page_load_strategy = "eager"
    opts.add_argument("--no-first-run")
    opts.add_argument("--no-default-browser-check")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--disable-infobars")
    opts.add_argument("--lang=en-US")
    opts.add_argument("--window-size=1280,900")
    if chrome_binary:
        opts.binary_location = chrome_binary

    profile = os.path.join(tempfile.gettempdir(), "ahrefs_chrome_pull")
    shutil.rmtree(profile, ignore_errors=True)
    os.makedirs(profile, exist_ok=True)

    driver = uc.Chrome(options=opts, headless=headless, use_subprocess=True,
                       version_main=version_main, user_data_dir=profile)
    driver.set_page_load_timeout(30)
    driver.set_script_timeout(120)
    return driver


def scrape_domain(driver, domain: str) -> Dict[str, Any]:
    """Process a single domain in the browser."""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.action_chains import ActionChains
    from selenium.common.exceptions import WebDriverException

    t0 = time.time()
    row: Dict[str, Any] = {"domain_name": domain, "status": "error"}

    try:
        try:
            driver.get("https://ahrefs.com/website-authority-checker/")
        except Exception:
            pass

        time.sleep(2)

        deadline = time.time() + 10
        while time.time() < deadline:
            has_input = driver.execute_script("return !!document.querySelector(\"input[type='text']\")")
            if has_input:
                break
            time.sleep(0.3)

        js_template = load_ahrefs_js()
        js_payload = js_template.replace("${domains}", domain)

        kickoff_js = f"""
            window.addEventListener('message', function(e){{
                if (e && e.data && e.data.type === 'TURNSTILE_FOCUS_REQUEST') {{
                    window.postMessage({{type:'TURNSTILE_FOCUS_RESPONSE',success:true,reason:'no_turnstile'}},'*');
                }}
            }});
            window.__ahrefsResult = undefined;
            window.__ahrefsError = undefined;
            (async function() {{
                try {{
                    var r = await (async function() {{ {js_payload} }})();
                    window.__ahrefsResult = r;
                }} catch (err) {{
                    window.__ahrefsError = JSON.stringify({{error:String(err&&err.message||err)}});
                }}
            }})();
        """
        driver.execute_script(kickoff_js)

        poll_deadline = time.time() + 60
        last_click = 0.0

        while time.time() < poll_deadline:
            try:
                done_raw = driver.execute_script(
                    "return JSON.stringify({r:window.__ahrefsResult,e:window.__ahrefsError})"
                )
                done_payload = json.loads(done_raw) if done_raw else {}
            except Exception:
                time.sleep(1)
                continue

            if done_payload.get("r") is not None:
                raw = done_payload["r"]
                parsed = json.loads(raw) if isinstance(raw, str) else raw
                if isinstance(parsed, dict) and "results" in parsed:
                    results = parsed["results"]
                    if results and isinstance(results, list):
                        r = results[0]
                        row["domain_name"] = r.get("domain_name", domain)
                        row["dr"] = r.get("dr")
                        row["backlinks"] = r.get("backlinks")
                        row["linking_websites"] = r.get("linking_websites")
                        row["backlinks_dofollow_percentage"] = r.get("backlinks_dofollow_percentage")
                        row["linking_websites_dofollow_percentage"] = r.get("linking_websites_dofollow_percentage")
                        row["status"] = "completed"
                break

            if done_payload.get("e") is not None:
                row["error"] = done_payload["e"]
                break

            now = time.time()
            if now - last_click > 4:
                try:
                    for f in driver.find_elements(By.TAG_NAME, "iframe"):
                        if "challenges.cloudflare.com" in (f.get_attribute("src") or ""):
                            ActionChains(driver, duration=100).move_to_element_with_offset(
                                f, random.randint(20, 35), random.randint(15, 25)
                            ).pause(0.15).click().perform()
                            last_click = now
                            break
                except (WebDriverException, Exception):
                    pass

            time.sleep(1.0)

    except Exception as e:
        row["error"] = f"{type(e).__name__}: {e}"
    finally:
        row["elapsed_seconds"] = round(time.time() - t0, 2)
        row["finished_at"] = datetime.now(timezone.utc).isoformat()
    return row


def pull_domain(api_url: str) -> Optional[Dict[str, Any]]:
    """GET /ahref-authority/ to fetch a domain from the queue."""
    try:
        resp = requests.get(f"{api_url}/ahref-authority/", timeout=10)
        if resp.status_code == 204:
            return None  # Queue empty
        resp.raise_for_status()
        data = resp.json()
        if data.get("success"):
            return data["data"]
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 204:
            return None
        print(f"  [ERROR] GET failed: {e.response.status_code} {e.response.text[:100]}")
    except Exception as e:
        print(f"  [ERROR] GET failed: {e}")
    return None


def post_result(api_url: str, execution_record: Dict, result: Dict) -> bool:
    """POST /ahref-authority/ to send results back."""
    try:
        resp = requests.post(
            f"{api_url}/ahref-authority/",
            json={"execution_record": execution_record, "result": result},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("success", False)
    except Exception as e:
        print(f"  [ERROR] POST failed: {e}")
        return False


def main():
    p = argparse.ArgumentParser(description="Ahrefs Checker - Pull Mode")
    p.add_argument("--api-url", default=DEFAULT_API_URL, help="Management service API URL")
    p.add_argument("--headless", action="store_true")
    p.add_argument("--interval", type=int, default=5, help="Seconds to wait when queue is empty")
    p.add_argument("--chrome", help="Path to chrome binary")
    p.add_argument("--max-domains", type=int, default=0, help="Max domains to process (0=unlimited)")
    args = p.parse_args()

    chrome_bin = args.chrome or find_chrome_binary()
    version_main = detect_chrome_major(chrome_bin)

    print(f"[*] Ahrefs Checker - Pull Mode")
    print(f"[*] API: {args.api_url}")
    print(f"[*] Chrome: {chrome_bin}")
    print(f"[*] Poll interval: {args.interval}s")
    print(f"[*] Opening browser...\n")

    driver = None
    processed = 0

    try:
        driver = build_driver(headless=args.headless, chrome_binary=chrome_bin, version_main=version_main)

        # Open a blank tab as keep-alive, work will happen in a second tab
        driver.get("about:blank")
        main_tab = driver.current_window_handle

        print("[*] Browser ready. Polling for domains...\n")

        while True:
            # Check max domains limit
            if args.max_domains > 0 and processed >= args.max_domains:
                print(f"\n[*] Reached max domains limit ({args.max_domains}). Stopping.")
                break

            # Pull a domain from the queue
            record = pull_domain(args.api_url)

            if record is None:
                sys.stdout.write(f"\r[*] Queue empty. Waiting {args.interval}s...")
                sys.stdout.flush()
                time.sleep(args.interval)
                continue

            domain = record.get("domain_name", "unknown")
            execution_id = record.get("execution_id", "?")
            print(f"\n[{processed + 1}] Got domain: {domain} (exec: {execution_id[:8]}...)")

            # Open new tab for this domain
            driver.execute_cdp_cmd("Target.createTarget", {"url": "about:blank"})
            time.sleep(0.5)
            work_tab = [h for h in driver.window_handles if h != main_tab][-1]
            driver.switch_to.window(work_tab)

            # Process in browser
            result = scrape_domain(driver, domain)
            mark = "OK" if result["status"] == "completed" else "FAIL"
            print(f"  [{mark}] DR={result.get('dr', '-')} BL={result.get('backlinks', '-')} "
                  f"LW={result.get('linking_websites', '-')} ({result['elapsed_seconds']:.1f}s)")

            # Close work tab, switch back to main blank tab
            driver.close()
            driver.switch_to.window(main_tab)

            # Post result back
            success = post_result(args.api_url, record, result)
            if success:
                print(f"  [POSTED] Result sent successfully")
            else:
                print(f"  [WARN] Failed to post result")

            processed += 1

    except KeyboardInterrupt:
        print(f"\n\n[*] Interrupted. Processed {processed} domains.")
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass

    print(f"[*] Done. Total processed: {processed}")


if __name__ == "__main__":
    main()
