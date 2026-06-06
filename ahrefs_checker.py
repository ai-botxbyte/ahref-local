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
from requests.adapters import HTTPAdapter

try:
    from urllib3.util.retry import Retry
except ImportError:  # pragma: no cover
    from requests.packages.urllib3.util.retry import Retry  # type: ignore

AHREFS_JSON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ahrefs.json")
PROXIES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "proxies.txt")
DEFAULT_API_URL = "https://b-domain.articleinnovator.com/domain-metrics-management-service/api/v1"

# Cloudflare auto-click extension path (solves Cloudflare Turnstile captchas)
CF_AUTOCLICK_EXTENSION_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "cf-autoclick-master")


# ----------------------------------------------------------------------------
# HTTP resilience — retries on DNS / 5xx
# ----------------------------------------------------------------------------

def _make_resilient_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=5, connect=5, read=5, status=5,
        backoff_factor=1.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST", "PUT", "PATCH", "DELETE"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(pool_connections=10, pool_maxsize=20, max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


_HTTP = _make_resilient_session()


# ----------------------------------------------------------------------------
# Driver-build serialization — undetected_chromedriver patches its own
# binary at runtime; concurrent uc.Chrome() calls cause the binary to vanish
# (FileNotFoundError on /home/.../undetected_chromedriver). We serialize the
# patching with a file lock and pre-cache the binary on first use.
# ----------------------------------------------------------------------------

_DRIVER_BUILD_LOCK_PATH = "/tmp/uc_driver_build.lock"
_driver_build_lock = threading.Lock()  # within-process

UC_CACHE_DIR = os.path.expanduser("~/.local/share/undetected_chromedriver")


def _acquire_driver_build_lock(timeout: float = 60.0) -> Optional[Any]:
    """Cross-process lock around uc.Chrome() construction.

    Returns the file handle (caller must release) or None on timeout.
    Falls back gracefully if `fcntl` isn't available (Windows).
    """
    try:
        import fcntl
    except ImportError:
        return None  # OS doesn't support flock — best effort
    deadline = time.time() + timeout
    fh = open(_DRIVER_BUILD_LOCK_PATH, "w")
    while time.time() < deadline:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fh
        except BlockingIOError:
            time.sleep(0.5)
    fh.close()
    return None


def _release_driver_build_lock(fh: Optional[Any]) -> None:
    if fh is None:
        return
    try:
        import fcntl
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        fh.close()
    except Exception:
        pass


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
    """Build a uc.Chrome driver. Serialized cross-process via flock so
    undetected_chromedriver's binary patching doesn't race between workers."""
    import undetected_chromedriver as uc

    # 1. Cross-process lock — undetected_chromedriver writes to a shared
    #    binary in ~/.local/share/undetected_chromedriver. Two workers
    #    patching at the same time can leave one with FileNotFoundError.
    file_lock = _acquire_driver_build_lock(timeout=120.0)

    # 2. In-process lock as a second layer (cheap)
    with _driver_build_lock:
        try:
            return _build_driver_locked(
                worker_id, headless=headless, chrome_binary=chrome_binary,
                version_main=version_main, proxy=proxy, uc=uc,
            )
        finally:
            _release_driver_build_lock(file_lock)


def _build_driver_locked(worker_id: int, headless: bool, chrome_binary: Optional[str],
                         version_main: Optional[int], proxy: Optional[str],
                         uc):
    opts = uc.ChromeOptions()
    opts.page_load_strategy = "eager"
    opts.add_argument("--no-first-run")
    opts.add_argument("--no-default-browser-check")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--disable-infobars")
    opts.add_argument("--lang=en-US")
    opts.add_argument("--window-size=1280,900")
    opts.add_argument(f"--remote-debugging-port={_free_port()}")

    # Persistent profile for this worker (extension stays loaded after first install)
    profile = os.path.join(tempfile.gettempdir(), f"ahrefs_chrome_w{worker_id}")
    os.makedirs(profile, exist_ok=True)

    # Copy cf-autoclick extension to profile only if not already present
    cf_ext_path = os.path.abspath(CF_AUTOCLICK_EXTENSION_PATH)
    ext_dest = os.path.join(profile, "Extensions", "cf_autoclick")
    if os.path.isdir(cf_ext_path) and not os.path.isdir(ext_dest):
        os.makedirs(os.path.dirname(ext_dest), exist_ok=True)
        shutil.copytree(cf_ext_path, ext_dest)
        print(f"  [worker-{worker_id}] Installed cf-autoclick extension", flush=True)

    # Load extension from profile
    if os.path.isdir(ext_dest):
        opts.add_argument(f"--load-extension={ext_dest}")

    if chrome_binary:
        opts.binary_location = chrome_binary

    # Proxy auth extension if needed
    if proxy:
        parts = proxy.split(":")
        if len(parts) == 4:
            ip, port, user, passwd = parts
            ext_zip = os.path.join(tempfile.gettempdir(), f"proxy_ext_w{worker_id}.zip")
            with zipfile.ZipFile(ext_zip, 'w') as zp:
                zp.writestr("manifest.json", json.dumps({
                    "version": "1.0.0", "manifest_version": 2, "name": "Proxy Auth",
                    "permissions": ["proxy", "tabs", "unlimitedStorage", "storage",
                                    "<all_urls>", "webRequest", "webRequestBlocking"],
                    "background": {"scripts": ["background.js"]},
                    "minimum_chrome_version": "22.0.0"
                }))
                zp.writestr("background.js", f"""
                    var config = {{mode: "fixed_servers", rules: {{
                        singleProxy: {{scheme: "http", host: "{ip}", port: parseInt({port})}},
                        bypassList: ["localhost"]
                    }}}};
                    chrome.proxy.settings.set({{value: config, scope: "regular"}}, function(){{}});
                    chrome.webRequest.onAuthRequired.addListener(
                        function(details) {{ return {{authCredentials: {{username: "{user}", password: "{passwd}"}}}}; }},
                        {{urls: ["<all_urls>"]}}, ['blocking']
                    );
                """)
            opts.add_extension(ext_zip)

    _ensure_uc_binary_present()

    driver = uc.Chrome(options=opts, headless=headless, use_subprocess=True,
                       version_main=version_main, user_data_dir=profile)
    driver.set_page_load_timeout(15)
    driver.set_script_timeout(45)

    _snapshot_uc_binary()
    return driver


# ----------------------------------------------------------------------------
# undetected_chromedriver binary cache management
# ----------------------------------------------------------------------------

_UC_BIN_BACKUP = "/tmp/uc_chromedriver.backup"


def _ensure_uc_binary_present() -> None:
    """If our backup exists but the canonical UC binary is missing, restore it."""
    try:
        os.makedirs(UC_CACHE_DIR, exist_ok=True)
    except Exception:
        return
    canonical = os.path.join(UC_CACHE_DIR, "undetected_chromedriver")
    if os.path.exists(canonical):
        return
    if os.path.exists(_UC_BIN_BACKUP):
        try:
            shutil.copy2(_UC_BIN_BACKUP, canonical)
            os.chmod(canonical, 0o755)
            print(f"  [uc-cache] restored chromedriver from backup", flush=True)
        except Exception as e:  # pragma: no cover
            print(f"  [uc-cache] restore failed: {e}", flush=True)


def _snapshot_uc_binary() -> None:
    """Snapshot the patched UC binary to /tmp so we can restore on next run."""
    canonical = os.path.join(UC_CACHE_DIR, "undetected_chromedriver")
    if not os.path.exists(canonical):
        return
    try:
        # Only copy if the source has changed (avoid wasted I/O)
        if os.path.exists(_UC_BIN_BACKUP):
            try:
                if os.path.getsize(canonical) == os.path.getsize(_UC_BIN_BACKUP):
                    return
            except Exception:
                pass
        shutil.copy2(canonical, _UC_BIN_BACKUP)
    except Exception:
        pass


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
    """Pull a single domain from the queue. Uses resilient session for retries."""
    try:
        resp = _HTTP.get(f"{api_url}/ahref-authority/", timeout=(10, 30))
        if resp.status_code == 204:
            return None
        resp.raise_for_status()
        data = resp.json()
        if data.get("success"):
            return data["data"]
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 204:
            return None
    except Exception as e:
        # After 5 retries this still failed — log so the watchdog catches it.
        tprint(f"  [pull] error after retries: {e}")
    return None


def post_result(api_url: str, execution_record: Dict, result: Dict) -> bool:
    """Post a single result. Failed results are buffered to disk."""
    try:
        resp = _HTTP.post(
            f"{api_url}/ahref-authority/",
            json={"execution_record": execution_record, "result": result},
            timeout=(10, 60),
        )
        resp.raise_for_status()
        ok = resp.json().get("success", False)
        if not ok:
            _buffer_ahref_failed_post(execution_record, result)
        return ok
    except Exception as e:
        tprint(f"  [post] error after retries: {e}; buffering result for {result.get('domain_name')}")
        _buffer_ahref_failed_post(execution_record, result)
        return False


# ----------------------------------------------------------------------------
# Persistent post buffer — never lose a result
# ----------------------------------------------------------------------------

AHREF_POST_BUFFER = os.environ.get(
    "AHREF_POST_BUFFER", "/tmp/ahref-local_pending_posts.jsonl"
)
_ahref_buffer_lock = threading.Lock()


def _buffer_ahref_failed_post(execution_record: Dict, result: Dict) -> None:
    try:
        with _ahref_buffer_lock:
            with open(AHREF_POST_BUFFER, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "execution_record": execution_record,
                    "result": result,
                }) + "\n")
    except Exception as e:  # pragma: no cover
        print(f"  [buffer] FATAL: cannot write ahref post buffer: {e}", flush=True)


def _flush_pending_ahref_posts(api_url: str, max_per_run: int = 50) -> int:
    if not os.path.exists(AHREF_POST_BUFFER):
        return 0
    flushed = 0
    remaining: List[str] = []
    with _ahref_buffer_lock:
        try:
            with open(AHREF_POST_BUFFER, "r", encoding="utf-8") as fh:
                lines = fh.readlines()
        except Exception:
            return 0
        for line in lines:
            line = line.strip()
            if not line:
                continue
            if flushed >= max_per_run:
                remaining.append(line)
                continue
            try:
                obj = json.loads(line)
                resp = _HTTP.post(
                    f"{api_url}/ahref-authority/",
                    json={"execution_record": obj["execution_record"],
                          "result": obj["result"]},
                    timeout=(10, 60),
                )
                if resp.status_code < 400 and resp.json().get("success"):
                    flushed += 1
                    continue
            except Exception:
                pass
            remaining.append(line)
        try:
            with open(AHREF_POST_BUFFER, "w", encoding="utf-8") as fh:
                for ln in remaining:
                    fh.write(ln + "\n")
        except Exception:  # pragma: no cover
            pass
    return flushed


def _start_ahref_buffer_flusher(api_url: str) -> threading.Thread:
    def loop():
        while True:
            time.sleep(30)
            try:
                n = _flush_pending_ahref_posts(api_url)
                if n:
                    tprint(f"  [flusher] re-sent {n} buffered ahref posts")
            except Exception as e:
                tprint(f"  [flusher] error: {e}")
    t = threading.Thread(target=loop, daemon=True, name="ahref-post-flusher")
    t.start()
    return t


# ----------------------------------------------------------------------------
# Browser healthcheck — detect dead drivers proactively
# ----------------------------------------------------------------------------

def _is_driver_alive(driver) -> bool:
    """Return True if `driver` can execute a trivial script."""
    if driver is None:
        return False
    try:
        return driver.execute_script("return 1") == 1
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
        """Build the driver with up to 10 retries with exponential backoff.
        Chromedriver port races and undetected_chromedriver binary corruption
        can cause early attempts to fail; the file lock + binary cache make
        later attempts succeed."""
        nonlocal driver
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
            time.sleep(2)
        last_err = None
        for attempt in range(10):
            try:
                driver = build_driver(worker_id, headless=headless, chrome_binary=chrome_bin,
                                      version_main=version_main, proxy=proxy)
                # Sanity: a live session can run a trivial script
                if _is_driver_alive(driver):
                    break
                raise RuntimeError("driver built but is_alive() returned False")
            except Exception as e:
                last_err = e
                tprint(f"  [W{worker_id}] driver build attempt {attempt+1}/10 failed: {e}")
                try:
                    if driver:
                        driver.quit()
                except Exception:
                    pass
                driver = None
                # Exponential backoff: 5, 10, 20, 40, 60, 60, 60, 60, 60, 60
                sleep_s = min(5 * (2 ** attempt), 60)
                time.sleep(sleep_s)
        if driver is None:
            raise RuntimeError(f"driver build failed after 10 attempts: {last_err}")
        try:
            driver.get("about:blank")
        except Exception:
            pass
        tprint(f"  [W{worker_id}] Browser ready (proxy: {proxy_short})")

    try:
        start_browser()
        last_healthcheck = time.time()

        while True:
            # Periodic healthcheck: every 60s, probe the driver. If it's
            # silently dead (rare but possible after Chrome OOM), rebuild
            # before the next batch instead of after a failed scrape.
            now = time.time()
            if now - last_healthcheck > 60:
                if not _is_driver_alive(driver):
                    tprint(f"  [W{worker_id}] healthcheck failed — rebuilding browser")
                    start_browser()
                last_healthcheck = now

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
                try:
                    driver.get("about:blank")
                except Exception:
                    # If even about:blank fails, browser is dead — rebuild.
                    tprint(f"  [W{worker_id}] post-scrape navigation failed; rebuilding browser")
                    start_browser()
            except Exception as e:
                tprint(f"  [W{worker_id}] Error: {e.__class__.__name__}: {e}. Restarting browser...")
                try:
                    start_browser()
                except Exception as e2:
                    # Re-raise to let the watchdog restart the whole process
                    tprint(f"  [W{worker_id}] FATAL: cannot rebuild browser: {e2}")
                    raise
                result = {"domain_name": domain, "status": "error", "error": str(e)}

            # Post result — buffered to disk on failure so we never lose it
            post_result(api_url, record, result)
            processed += 1

            # Proactive recycle: undetected_chromedriver leaks file handles
            # over time. Recycle every 50 domains to stay healthy.
            if processed > 0 and processed % 50 == 0:
                tprint(f"  [W{worker_id}] processed {processed} — recycling browser")
                try:
                    start_browser()
                except Exception as e:
                    tprint(f"  [W{worker_id}] recycle failed (continuing): {e}")

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

    # Flush any results buffered from a previous crashed run, then start the
    # background flusher to handle ongoing buffer drains.
    initial = _flush_pending_ahref_posts(args.api_url, max_per_run=200)
    if initial:
        print(f"[*] Recovered {initial} buffered posts from previous run")
    _start_ahref_buffer_flusher(args.api_url)

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
