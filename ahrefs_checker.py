"""
Ahrefs Website Authority Checker — Pull-based, 5 parallel proxy instances.

Each worker gets its own proxy and browser instance, all pulling from the same queue.

Usage:
    python ahrefs_checker.py [--api-url URL] [--headless] [--workers 5]
"""

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
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

AHREFS_JSON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ahrefs.json")
PROXIES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "proxies.txt")
DEFAULT_API_URL = "http://164.90.252.85/domain-metrics-management-service/api/v1"


def load_ahrefs_js() -> str:
    with open(AHREFS_JSON_PATH, "r", encoding="utf-8") as fh:
        spec = json.load(fh)
    action = next((a for a in spec.get("actions", []) if a.get("type") == "evaluate"), None)
    if not action or "script" not in action:
        raise RuntimeError("ahrefs.json missing the 'evaluate' action / script")
    return action["script"]


def load_proxies() -> List[str]:
    if not os.path.exists(PROXIES_PATH):
        return []
    with open(PROXIES_PATH) as f:
        return [line.strip() for line in f if line.strip()]


def find_chrome_binary() -> Optional[str]:
    system = platform.system()
    candidates = []
    if system == "Linux":
        candidates = ["/usr/bin/google-chrome-stable", "/usr/bin/google-chrome", "/usr/bin/chromium-browser"]
    elif system == "Darwin":
        candidates = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
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


def _free_port() -> int:
    """Pick an unused TCP port to avoid chromedriver port races between concurrent instances."""
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def build_driver(worker_id: int, headless: bool, chrome_binary: Optional[str],
                 version_main: Optional[int], proxy: Optional[str] = None):
    import undetected_chromedriver as uc

    opts = uc.ChromeOptions()
    opts.page_load_strategy = "eager"
    opts.add_argument("--no-first-run")
    opts.add_argument("--no-default-browser-check")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--disable-infobars")
    opts.add_argument("--lang=en-US")
    opts.add_argument("--window-size=1280,900")
    # Unique debugging port per instance — prevents collision when running
    # alongside other UC scripts (e.g. bing-local) which also pick ports.
    opts.add_argument(f"--remote-debugging-port={_free_port()}")

    if chrome_binary:
        opts.binary_location = chrome_binary

    # Create proxy auth extension if proxy has credentials
    if proxy:
        parts = proxy.split(":")
        if len(parts) == 4:
            ip, port, user, passwd = parts
            ext_dir = os.path.join(tempfile.gettempdir(), f"proxy_ext_w{worker_id}")
            os.makedirs(ext_dir, exist_ok=True)
            manifest = json.dumps({
                "version": "1.0.0",
                "manifest_version": 2,
                "name": "Proxy Auth",
                "permissions": ["proxy", "tabs", "unlimitedStorage", "storage",
                                "<all_urls>", "webRequest", "webRequestBlocking"],
                "background": {"scripts": ["background.js"]},
                "minimum_chrome_version": "22.0.0"
            })
            background = f"""
            var config = {{
                mode: "fixed_servers",
                rules: {{
                    singleProxy: {{scheme: "http", host: "{ip}", port: parseInt({port})}},
                    bypassList: ["localhost"]
                }}
            }};
            chrome.proxy.settings.set({{value: config, scope: "regular"}}, function(){{}});
            function callbackFn(details) {{
                return {{authCredentials: {{username: "{user}", password: "{passwd}"}}}};
            }}
            chrome.webRequest.onAuthRequired.addListener(callbackFn,
                {{urls: ["<all_urls>"]}}, ['blocking']);
            """
            ext_zip = os.path.join(tempfile.gettempdir(), f"proxy_ext_w{worker_id}.zip")
            with zipfile.ZipFile(ext_zip, 'w') as zp:
                zp.writestr("manifest.json", manifest)
                zp.writestr("background.js", background)
            opts.add_extension(ext_zip)

    profile = os.path.join(tempfile.gettempdir(), f"ahrefs_chrome_w{worker_id}")
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


# Thread-safe print
_print_lock = threading.Lock()
def tprint(msg):
    with _print_lock:
        print(msg)


def pull_domain(api_url: str) -> Optional[Dict[str, Any]]:
    try:
        resp = requests.get(f"{api_url}/ahref-authority/", timeout=10)
        if resp.status_code == 204:
            return None
        resp.raise_for_status()
        data = resp.json()
        if data.get("success"):
            return data["data"]
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 204:
            return None
    except Exception:
        pass
    return None


def post_result(api_url: str, execution_record: Dict, result: Dict) -> bool:
    try:
        resp = requests.post(
            f"{api_url}/ahref-authority/",
            json={"execution_record": execution_record, "result": result},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json().get("success", False)
    except Exception:
        return False


def worker_loop(worker_id: int, proxy: Optional[str], api_url: str, headless: bool,
                chrome_bin: Optional[str], version_main: Optional[int]):
    """Single worker: opens browser with proxy, pulls and processes domains forever."""
    proxy_short = proxy.split(":")[0] if proxy else "local"
    tprint(f"  [W{worker_id}] Starting with proxy {proxy_short}...")

    driver = None
    processed = 0

    def start_browser():
        """Build the driver with up to 3 retries — chromedriver port races
        can leave the first attempt with a dead session, especially when
        another UC script (bing-local) is launching at the same time."""
        nonlocal driver
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
            time.sleep(2)
        last_err = None
        for attempt in range(3):
            try:
                driver = build_driver(worker_id, headless=headless, chrome_binary=chrome_bin,
                                      version_main=version_main, proxy=proxy)
                # Sanity: a live session can run a trivial script
                driver.execute_script("return 1")
                break
            except Exception as e:
                last_err = e
                tprint(f"  [W{worker_id}] driver build attempt {attempt+1} failed: {e}")
                try:
                    if driver:
                        driver.quit()
                except Exception:
                    pass
                driver = None
                time.sleep(5 + attempt * 5)
        if driver is None:
            raise RuntimeError(f"driver build failed after 3 attempts: {last_err}")
        driver.get("about:blank")
        tprint(f"  [W{worker_id}] Browser ready (proxy: {proxy_short})")

    try:
        start_browser()

        while True:
            record = pull_domain(api_url)

            if record is None:
                time.sleep(3 + random.random() * 2)
                continue

            domain = record.get("domain_name", "unknown")
            execution_id = record.get("execution_id", "?")[:8]
            tprint(f"  [W{worker_id}] Got: {domain} (exec: {execution_id})")

            try:
                result = scrape_domain(driver, domain)
                mark = "OK" if result["status"] == "completed" else "FAIL"
                tprint(f"  [W{worker_id}] [{mark}] {domain} DR={result.get('dr', '-')} "
                       f"BL={result.get('backlinks', '-')} ({result['elapsed_seconds']:.1f}s)")
                # Navigate back to blank to reset state
                driver.get("about:blank")
            except Exception as e:
                tprint(f"  [W{worker_id}] Error: {e.__class__.__name__}. Restarting browser...")
                start_browser()
                result = {"domain_name": domain, "status": "error", "error": str(e)}

            # Post result
            post_result(api_url, record, result)
            processed += 1

    except KeyboardInterrupt:
        pass
    except Exception as e:
        tprint(f"  [W{worker_id}] FATAL: {e}")
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
        tprint(f"  [W{worker_id}] Stopped. Processed: {processed}")


def main():
    p = argparse.ArgumentParser(description="Ahrefs Checker - Parallel Pull Mode")
    p.add_argument("--api-url", default=DEFAULT_API_URL)
    p.add_argument("--headless", action="store_true")
    p.add_argument("--workers", type=int, default=5, help="Number of parallel browser instances")
    p.add_argument("--chrome", help="Path to chrome binary")
    p.add_argument("--proxies", default=PROXIES_PATH, help="Path to proxies file")
    p.add_argument("--no-proxy", action="store_true", help="Disable proxies, run all instances on local IP")
    args = p.parse_args()

    proxies = [] if args.no_proxy else load_proxies()
    num_workers = args.workers

    if not args.no_proxy and not proxies:
        print("[WARN] No proxies found. Running all instances on local IP.")

    chrome_bin = args.chrome or find_chrome_binary()
    version_main = detect_chrome_major(chrome_bin)

    print(f"[*] Ahrefs Checker - Parallel Pull Mode")
    print(f"[*] API: {args.api_url}")
    print(f"[*] Chrome: {chrome_bin}")
    print(f"[*] Workers: {num_workers} | Proxy: {'disabled' if args.no_proxy or not proxies else f'{len(proxies)} loaded'}")
    print(f"[*] Launching {num_workers} browser instances...\n")

    with ThreadPoolExecutor(max_workers=num_workers) as executor:
        futures = []
        for i in range(num_workers):
            proxy = proxies[i % len(proxies)] if proxies else None
            f = executor.submit(
                worker_loop, i, proxy, args.api_url, args.headless,
                chrome_bin, version_main
            )
            futures.append(f)
            time.sleep(10)  # Stagger launches to avoid resource contention / chromedriver port races

        try:
            for f in futures:
                f.result()
        except KeyboardInterrupt:
            print("\n[*] Shutting down...")


if __name__ == "__main__":
    main()
