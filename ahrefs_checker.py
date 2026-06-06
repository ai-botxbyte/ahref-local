"""
Ahrefs Pull-based Checker — auto-routes between authority and traffic queues.

Each worker gets its own per-run Chrome profile (replicated from a master
profile downloaded from a GitHub release on every start), runs a browser,
and pulls execution records from BOTH pull-based endpoints in round-robin:

  GET/POST /ahref-authority/   (authority queue)
  GET/POST /ahref-traffic/     (traffic queue)

Whichever endpoint returns a record dictates which JS / target URL /
extractor is used to scrape that one domain — the workflow doesn't have
to pick a mode upfront. Both queues feed the same browser; if only one
queue has work, the worker drains it and idles on the other.

The master profile is downloaded once per run from a GitHub release and
contains the cf-autoclick extension pre-installed. Every worker copies it
into a unique directory ``ahref_w{worker_id}_{uuid8}`` so concurrent
instances never collide on profile state. All per-worker profile dirs (and
the master cache) are cleaned up when the worker exits or the process is
killed.

Usage:
    python ahrefs_checker.py [--modes MODES] [--api-url URL] [--headless] [--workers 5]

    MODES is a comma-separated list (default: 'authority,traffic'). Set to
    a single value (e.g. 'traffic') to lock the worker to one queue.
"""

import argparse
import atexit
import json
import os
import platform
import random
import shutil
import signal
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import requests
from requests.adapters import HTTPAdapter

try:
    from urllib3.util.retry import Retry
except ImportError:  # pragma: no cover
    from requests.packages.urllib3.util.retry import Retry  # type: ignore

AHREFS_JSON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ahrefs.json")
AHREFS_TRAFFIC_JSON_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "ahrefs-traffic-checker.json"
)
PROXIES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "proxies.txt")
DEFAULT_API_URL = "https://b-domain.articleinnovator.com/domain-metrics-management-service/api/v1"

# --------------------------------------------------------------------------- #
# Master profile (downloaded from GitHub release on every run)
# --------------------------------------------------------------------------- #
#
# The release `worker-profile-v1` on sanket-sakariya/test-abc contains the
# Chrome profile used by the workers, with the cf-autoclick extension already
# baked into Extensions/cf_autoclick/. To rotate the profile, update the
# release using tools/upload_profile_release.sh.
#
# Both the URL and the extracted-profile path can be overridden via env vars
# — the GitHub Actions workflow pre-downloads the profile in its own step
# and sets AHREF_MASTER_PROFILE_DIR so this script skips the redundant
# download (faster watchdog restarts, cleaner workflow logs).
PROFILE_RELEASE_URL = os.environ.get(
    "AHREF_PROFILE_RELEASE_URL",
    "https://github.com/sanket-sakariya/test-abc/releases/download/"
    "worker-profile-v1/ahrefs-worker-profile.zip",
)
PROFILE_PREEXTRACTED_DIR = os.environ.get("AHREF_MASTER_PROFILE_DIR", "").strip() or None

# All per-worker profile dirs created during this process — cleaned up on exit.
_CREATED_PROFILES: List[str] = []
_PROFILES_LOCK = threading.Lock()

# Master profile cache (populated by _download_master_profile on first call).
_MASTER_PROFILE_DIR: Optional[str] = None
_MASTER_PROFILE_LOCK = threading.Lock()


# ----------------------------------------------------------------------------
# Mode dispatch — authority vs traffic share most of the browser machinery
# but differ in which endpoint they pull from, which JS spec they inject,
# and how they extract the result.
# ----------------------------------------------------------------------------

class CheckerMode:
    """Per-mode configuration: endpoint, target URL, JS spec, result parser."""

    def __init__(
        self,
        name: str,
        endpoint: str,
        target_url: str,
        spec_path: str,
        extract_row: Callable[[Dict[str, Any], str], Dict[str, Any]],
    ):
        self.name = name
        self.endpoint = endpoint
        self.target_url = target_url
        self.spec_path = spec_path
        self.extract_row = extract_row


def _extract_authority_row(parsed: Dict[str, Any], domain: str) -> Dict[str, Any]:
    """Authority-mode result extractor: DR / backlinks / linking websites."""
    row: Dict[str, Any] = {"domain_name": domain, "status": "error"}
    results = parsed.get("results")
    if results and isinstance(results, list):
        r = results[0]
        row["domain_name"] = r.get("domain_name", domain)
        row["dr"] = r.get("dr")
        row["backlinks"] = r.get("backlinks")
        row["linking_websites"] = r.get("linking_websites")
        row["backlinks_dofollow_percentage"] = r.get("backlinks_dofollow_percentage")
        row["linking_websites_dofollow_percentage"] = r.get("linking_websites_dofollow_percentage")
        row["status"] = "completed"
    return row


def _extract_traffic_row(parsed: Dict[str, Any], domain: str) -> Dict[str, Any]:
    """Traffic-mode result extractor: organic_traffic / traffic_value / etc."""
    row: Dict[str, Any] = {"domain_name": domain, "status": "error"}
    results = parsed.get("results")
    if results and isinstance(results, list):
        r = results[0]
        row["domain_name"] = r.get("domain_name", domain)
        row["organic_traffic"] = r.get("organic_traffic")
        row["traffic_value"] = r.get("traffic_value")
        row["traffic_graph"] = r.get("traffic_graph") or {}
        row["top_countries"] = r.get("top_countries") or []
        row["top_keywords"] = r.get("top_keywords") or []
        row["top_pages"] = r.get("top_pages") or []
        row["turnstile_retries"] = r.get("turnstile_retries")
        # A row counts as completed if we got at least the headline metric.
        if r.get("organic_traffic") is not None or r.get("traffic_value") is not None:
            row["status"] = "completed"
        elif r.get("error"):
            row["error"] = r.get("error")
    return row


MODES: Dict[str, CheckerMode] = {
    "authority": CheckerMode(
        name="authority",
        endpoint="/ahref-authority/",
        target_url="https://ahrefs.com/website-authority-checker/",
        spec_path=AHREFS_JSON_PATH,
        extract_row=_extract_authority_row,
    ),
    "traffic": CheckerMode(
        name="traffic",
        endpoint="/ahref-traffic/",
        target_url="https://ahrefs.com/traffic-checker/?mode=subdomains",
        spec_path=AHREFS_TRAFFIC_JSON_PATH,
        extract_row=_extract_traffic_row,
    ),
}


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
# Master-profile bootstrap — download once per run from GitHub release.
# ----------------------------------------------------------------------------

def _download_master_profile() -> str:
    """Resolve the master Chrome profile, downloading it if necessary.

    Resolution order:
      1. In-process cache (already resolved on a previous call)
      2. AHREF_MASTER_PROFILE_DIR env var pointing at an already-extracted
         profile (the GitHub Actions workflow uses this to skip the download
         after restarting the script)
      3. Download + extract the GitHub release asset

    Thread-safe via _MASTER_PROFILE_LOCK so workers launching concurrently
    don't both try to download.

    Raises RuntimeError if the asset is missing or the extracted dir does
    not contain the cf-autoclick extension — fail fast rather than start
    browsers without captcha-solving.
    """
    global _MASTER_PROFILE_DIR
    with _MASTER_PROFILE_LOCK:
        # (1) already resolved this run
        if _MASTER_PROFILE_DIR and os.path.isdir(_MASTER_PROFILE_DIR):
            return _MASTER_PROFILE_DIR

        # (2) pre-extracted by the workflow — just verify and reuse.
        if PROFILE_PREEXTRACTED_DIR and os.path.isdir(PROFILE_PREEXTRACTED_DIR):
            ext_manifest = os.path.join(
                PROFILE_PREEXTRACTED_DIR, "Extensions", "cf_autoclick", "manifest.json"
            )
            if os.path.isfile(ext_manifest):
                _MASTER_PROFILE_DIR = PROFILE_PREEXTRACTED_DIR
                print(
                    f"✅ Using pre-extracted master profile from "
                    f"AHREF_MASTER_PROFILE_DIR={PROFILE_PREEXTRACTED_DIR}",
                    flush=True,
                )
                return _MASTER_PROFILE_DIR
            print(
                f"[!] AHREF_MASTER_PROFILE_DIR={PROFILE_PREEXTRACTED_DIR} exists "
                f"but is missing Extensions/cf_autoclick/manifest.json — "
                f"falling back to download.",
                flush=True,
            )

        # (3) fall through to download
        run_token = uuid.uuid4().hex[:8]
        download_path = os.path.join(tempfile.gettempdir(), f"ahref_master_{run_token}.zip")
        extract_root = os.path.join(tempfile.gettempdir(), f"ahref_master_{run_token}")

        print(f"[*] Downloading master profile from {PROFILE_RELEASE_URL}", flush=True)
        try:
            with _HTTP.get(PROFILE_RELEASE_URL, timeout=(15, 300), stream=True) as resp:
                resp.raise_for_status()
                with open(download_path, "wb") as fh:
                    for chunk in resp.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            fh.write(chunk)
        except Exception as e:
            raise RuntimeError(
                f"Failed to download master profile from {PROFILE_RELEASE_URL}: {e}. "
                "Make sure the release exists — run "
                "tools/upload_profile_release.sh to create it."
            ) from e

        size_mb = os.path.getsize(download_path) / (1024 * 1024)
        print(f"[*] Downloaded {size_mb:.1f} MB; extracting to {extract_root}", flush=True)

        os.makedirs(extract_root, exist_ok=True)
        try:
            with zipfile.ZipFile(download_path, "r") as zf:
                zf.extractall(extract_root)
        except Exception as e:
            raise RuntimeError(f"Failed to extract master profile zip: {e}") from e
        finally:
            try:
                os.remove(download_path)
            except OSError:
                pass

        # The zip wraps a single top-level directory (e.g. "ahrefs-worker-profile").
        # Find the real profile root — it must contain Extensions/cf_autoclick.
        candidates = [extract_root] + [
            os.path.join(extract_root, d) for d in os.listdir(extract_root)
            if os.path.isdir(os.path.join(extract_root, d))
        ]
        master = None
        for c in candidates:
            if os.path.isfile(os.path.join(c, "Extensions", "cf_autoclick", "manifest.json")):
                master = c
                break

        if master is None:
            raise RuntimeError(
                f"Downloaded master profile is malformed — no Extensions/cf_autoclick/"
                f"manifest.json found under {extract_root}. Rebuild the release with "
                "tools/upload_profile_release.sh from a profile that has the "
                "cf-autoclick extension installed."
            )

        _MASTER_PROFILE_DIR = master
        print(f"✅ Master profile ready at {master}", flush=True)
        return master


def _create_worker_profile(worker_id: int) -> str:
    """Copy the master profile into a fresh per-worker directory.

    Profile id format: ``ahref_w{worker_id}_{uuid8}`` so two concurrent
    workers (or two restarts of the same worker_id within one run) never
    fight over the same /tmp dir. Tracked in _CREATED_PROFILES so the
    global cleanup hook can remove every one on process exit.
    """
    master = _download_master_profile()
    profile_id = f"ahref_w{worker_id}_{uuid.uuid4().hex[:8]}"
    dest = os.path.join(tempfile.gettempdir(), profile_id)
    if os.path.isdir(dest):
        shutil.rmtree(dest, ignore_errors=True)
    shutil.copytree(master, dest)

    ext_manifest = os.path.join(dest, "Extensions", "cf_autoclick", "manifest.json")
    if not os.path.isfile(ext_manifest):
        # Should be impossible given the master-profile check, but defend
        # against a half-copied directory (disk full mid-copytree, etc.).
        shutil.rmtree(dest, ignore_errors=True)
        raise RuntimeError(
            f"[W{worker_id}] Per-worker profile missing cf-autoclick after copy: "
            f"{ext_manifest}"
        )

    with _PROFILES_LOCK:
        _CREATED_PROFILES.append(dest)

    return dest


def _remove_worker_profile(path: Optional[str]) -> None:
    """Remove a single per-worker profile dir and stop tracking it."""
    if not path:
        return
    try:
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
    except Exception:
        pass
    with _PROFILES_LOCK:
        try:
            _CREATED_PROFILES.remove(path)
        except ValueError:
            pass


def _global_profile_cleanup() -> None:
    """atexit + SIGTERM hook — wipe every per-worker dir and the master cache."""
    with _PROFILES_LOCK:
        paths = list(_CREATED_PROFILES)
        _CREATED_PROFILES.clear()
    for p in paths:
        try:
            shutil.rmtree(p, ignore_errors=True)
        except Exception:
            pass
    global _MASTER_PROFILE_DIR
    if _MASTER_PROFILE_DIR:
        # Don't delete a master profile we didn't download (the GitHub
        # Actions workflow pre-extracts to a fixed path and manages its
        # own cleanup; nuking it here would break the watchdog restart).
        is_preextracted = (
            PROFILE_PREEXTRACTED_DIR
            and os.path.abspath(_MASTER_PROFILE_DIR) == os.path.abspath(PROFILE_PREEXTRACTED_DIR)
        )
        if not is_preextracted:
            try:
                # also remove the extract_root one level up so we don't leak the
                # ahref_master_<token> wrapper directory.
                parent = os.path.dirname(_MASTER_PROFILE_DIR)
                if parent.startswith(tempfile.gettempdir()) and "ahref_master_" in parent:
                    shutil.rmtree(parent, ignore_errors=True)
                else:
                    shutil.rmtree(_MASTER_PROFILE_DIR, ignore_errors=True)
            except Exception:
                pass
        _MASTER_PROFILE_DIR = None


atexit.register(_global_profile_cleanup)


def _install_signal_handlers() -> None:
    """SIGTERM/SIGINT → flush profile cleanup then exit cleanly."""
    def _handler(signum, _frame):
        try:
            _global_profile_cleanup()
        finally:
            # Re-raise default behavior so the process actually exits.
            sys.exit(128 + signum)
    for sig in (signal.SIGTERM, signal.SIGHUP):
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):
            # SIGHUP doesn't exist on Windows; non-main threads can't
            # install handlers — both are fine to skip.
            pass


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
    """Cross-process lock around uc.Chrome() construction."""
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


def load_mode_js(spec_path: str) -> str:
    """Load the 'evaluate' script from an ahrefs JSON spec file."""
    with open(spec_path, "r", encoding="utf-8") as fh:
        spec = json.load(fh)
    action = next((a for a in spec.get("actions", []) if a.get("type") == "evaluate"), None)
    if not action or "script" not in action:
        raise RuntimeError(f"{spec_path} missing the 'evaluate' action / script")
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
    """Build a uc.Chrome driver using a fresh per-worker profile copy.

    Returns (driver, profile_path) so the caller can clean up the profile
    when the driver is recycled / shut down.
    """
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

    # Per-worker, per-run profile — replicated from the GitHub master profile.
    # cf-autoclick extension is already inside Extensions/cf_autoclick/ thanks
    # to the master profile bake.
    profile = _create_worker_profile(worker_id)
    print(f"  [worker-{worker_id}] Profile: {profile}", flush=True)

    ext_dest = os.path.join(profile, "Extensions", "cf_autoclick")
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
    return driver, profile


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
        if os.path.exists(_UC_BIN_BACKUP):
            try:
                if os.path.getsize(canonical) == os.path.getsize(_UC_BIN_BACKUP):
                    return
            except Exception:
                pass
        shutil.copy2(canonical, _UC_BIN_BACKUP)
    except Exception:
        pass


def scrape_domain(driver, domain: str, mode: CheckerMode) -> Dict[str, Any]:
    """Process a single domain in the browser using the given mode."""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.action_chains import ActionChains
    from selenium.common.exceptions import WebDriverException

    t0 = time.time()
    row: Dict[str, Any] = {"domain_name": domain, "status": "error"}

    try:
        try:
            driver.get(mode.target_url)
        except Exception:
            pass

        time.sleep(2)

        deadline = time.time() + 10
        while time.time() < deadline:
            has_input = driver.execute_script("return !!document.querySelector(\"input[type='text']\")")
            if has_input:
                break
            time.sleep(0.3)

        js_template = load_mode_js(mode.spec_path)
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

        # Traffic-checker scrapes a modal that takes longer to load than the
        # authority check (multiple sections, table rendering, recharts SVG).
        # Give it more time before giving up.
        poll_deadline = time.time() + (120 if mode.name == "traffic" else 60)
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
                    row = mode.extract_row(parsed, domain)
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


def pull_domain(api_url: str, mode: CheckerMode) -> Optional[Dict[str, Any]]:
    """Pull a single domain from the queue. Uses resilient session for retries."""
    try:
        resp = _HTTP.get(f"{api_url}{mode.endpoint}", timeout=(10, 30))
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
        tprint(f"  [pull] error after retries: {e}")
    return None


def post_result(api_url: str, mode: CheckerMode, execution_record: Dict, result: Dict) -> bool:
    """Post a single result. Failed results are buffered to disk."""
    try:
        resp = _HTTP.post(
            f"{api_url}{mode.endpoint}",
            json={"execution_record": execution_record, "result": result},
            timeout=(10, 60),
        )
        resp.raise_for_status()
        ok = resp.json().get("success", False)
        if not ok:
            _buffer_ahref_failed_post(mode, execution_record, result)
        return ok
    except Exception as e:
        tprint(f"  [post] error after retries: {e}; buffering result for {result.get('domain_name')}")
        _buffer_ahref_failed_post(mode, execution_record, result)
        return False


# ----------------------------------------------------------------------------
# Persistent post buffer — never lose a result.
# We use a per-mode buffer file so authority results and traffic results
# don't get re-sent to the wrong endpoint after a crash.
# ----------------------------------------------------------------------------

def _ahref_post_buffer_path(mode: CheckerMode) -> str:
    return os.environ.get(
        f"AHREF_{mode.name.upper()}_POST_BUFFER",
        f"/tmp/ahref-local_pending_posts_{mode.name}.jsonl",
    )


_ahref_buffer_lock = threading.Lock()


def _buffer_ahref_failed_post(mode: CheckerMode, execution_record: Dict, result: Dict) -> None:
    try:
        with _ahref_buffer_lock:
            with open(_ahref_post_buffer_path(mode), "a", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "execution_record": execution_record,
                    "result": result,
                }) + "\n")
    except Exception as e:  # pragma: no cover
        print(f"  [buffer] FATAL: cannot write ahref post buffer: {e}", flush=True)


def _flush_pending_ahref_posts(api_url: str, mode: CheckerMode, max_per_run: int = 50) -> int:
    buf_path = _ahref_post_buffer_path(mode)
    if not os.path.exists(buf_path):
        return 0
    flushed = 0
    remaining: List[str] = []
    with _ahref_buffer_lock:
        try:
            with open(buf_path, "r", encoding="utf-8") as fh:
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
                    f"{api_url}{mode.endpoint}",
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
            with open(buf_path, "w", encoding="utf-8") as fh:
                for ln in remaining:
                    fh.write(ln + "\n")
        except Exception:  # pragma: no cover
            pass
    return flushed


def _start_ahref_buffer_flusher(api_url: str, mode: CheckerMode) -> threading.Thread:
    def loop():
        while True:
            time.sleep(30)
            try:
                n = _flush_pending_ahref_posts(api_url, mode)
                if n:
                    tprint(f"  [flusher] re-sent {n} buffered {mode.name} posts")
            except Exception as e:
                tprint(f"  [flusher] error: {e}")
    t = threading.Thread(target=loop, daemon=True, name=f"ahref-{mode.name}-post-flusher")
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
                chrome_bin: Optional[str], version_main: Optional[int],
                modes: List[CheckerMode]):
    """Single worker: opens browser with proxy, pulls and processes domains forever.

    The worker is **mode-agnostic**. On each iteration it tries each enabled
    mode's pull endpoint in round-robin order; the first one to return a
    record dictates which JS / target URL / extractor to use for that
    domain. Both queues feed the same browser, so the workflow doesn't have
    to pick a mode upfront — whichever queue has work, the worker picks it
    up.

    The round-robin offset is per-worker (`worker_id % len(modes)`) so
    multiple workers don't all hammer the same endpoint first; on average
    each endpoint sees fair attention even when only one queue has work.
    """
    proxy_short = proxy.split(":")[0] if proxy else "local"
    mode_names = ",".join(m.name for m in modes)
    tprint(f"  [W{worker_id}] Starting [modes={mode_names}] with proxy {proxy_short}...")

    driver = None
    profile_path: Optional[str] = None
    processed = 0
    # Per-worker round-robin offset — incremented each time we have to try
    # multiple endpoints, so the next pull starts where the last one left off.
    rr_offset = worker_id % max(1, len(modes))

    def start_browser():
        """Build the driver with up to 10 retries with exponential backoff."""
        nonlocal driver, profile_path
        # Tear down previous driver + profile before rebuilding so we don't
        # leak per-run uuid dirs across recycles.
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
            time.sleep(2)
        old_profile = profile_path
        if old_profile:
            _remove_worker_profile(old_profile)
        profile_path = None

        last_err = None
        for attempt in range(10):
            try:
                driver, profile_path = build_driver(
                    worker_id, headless=headless, chrome_binary=chrome_bin,
                    version_main=version_main, proxy=proxy,
                )
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
                # The failed attempt may have already created a profile dir;
                # remove it before retry so we don't accumulate orphans.
                if profile_path:
                    _remove_worker_profile(profile_path)
                    profile_path = None
                sleep_s = min(5 * (2 ** attempt), 60)
                time.sleep(sleep_s)
        if driver is None:
            raise RuntimeError(f"driver build failed after 10 attempts: {last_err}")
        try:
            driver.get("about:blank")
        except Exception:
            pass
        tprint(f"  [W{worker_id}] Browser ready (proxy: {proxy_short}, profile: {profile_path})")

    def _pull_any() -> Optional[tuple]:
        """Try each enabled mode in round-robin order. Return (mode, record)
        for the first hit, or None if every endpoint returned 204."""
        nonlocal rr_offset
        n = len(modes)
        for i in range(n):
            mode = modes[(rr_offset + i) % n]
            record = pull_domain(api_url, mode)
            if record is not None:
                # Advance the offset so the NEXT pull starts on the queue we
                # didn't just drain — keeps the two queues balanced even when
                # one is much busier than the other.
                rr_offset = (rr_offset + i + 1) % n
                return mode, record
        return None

    try:
        start_browser()
        last_healthcheck = time.time()

        while True:
            now = time.time()
            if now - last_healthcheck > 60:
                if not _is_driver_alive(driver):
                    tprint(f"  [W{worker_id}] healthcheck failed — rebuilding browser")
                    start_browser()
                last_healthcheck = now

            pull_result = _pull_any()

            if pull_result is None:
                time.sleep(3 + random.random() * 2)
                continue

            mode, record = pull_result
            domain = record.get("domain_name", "unknown")
            execution_id = record.get("execution_id", "?")[:8]
            tprint(f"  [W{worker_id}] Got [{mode.name}]: {domain} (exec: {execution_id})")

            try:
                result = scrape_domain(driver, domain, mode)
                mark = "OK" if result["status"] == "completed" else "FAIL"
                if mode.name == "authority":
                    tprint(f"  [W{worker_id}] [{mark}][{mode.name}] {domain} "
                           f"DR={result.get('dr', '-')} BL={result.get('backlinks', '-')} "
                           f"({result['elapsed_seconds']:.1f}s)")
                else:
                    tprint(f"  [W{worker_id}] [{mark}][{mode.name}] {domain} "
                           f"OT={result.get('organic_traffic', '-')} "
                           f"TV={result.get('traffic_value', '-')} "
                           f"({result['elapsed_seconds']:.1f}s)")
                try:
                    driver.get("about:blank")
                except Exception:
                    tprint(f"  [W{worker_id}] post-scrape navigation failed; rebuilding browser")
                    start_browser()
            except Exception as e:
                tprint(f"  [W{worker_id}] Error: {e.__class__.__name__}: {e}. Restarting browser...")
                try:
                    start_browser()
                except Exception as e2:
                    tprint(f"  [W{worker_id}] FATAL: cannot rebuild browser: {e2}")
                    raise
                result = {"domain_name": domain, "status": "error", "error": str(e)}

            # POST goes back to the SAME endpoint we pulled from — the result
            # has the shape that endpoint expects (the management service's
            # POST handler unpacks fields specific to its mode).
            post_result(api_url, mode, record, result)
            processed += 1

            # Proactive recycle every 50 domains. start_browser() handles
            # tearing down the old profile + creating a new uuid'd one.
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
        # Final per-worker cleanup. The global atexit hook will also catch
        # this if we crash before reaching here, but doing it explicitly
        # frees ~150 MB per worker faster.
        if profile_path:
            _remove_worker_profile(profile_path)
            tprint(f"  [W{worker_id}] Cleaned up profile {profile_path}")
        tprint(f"  [W{worker_id}] Stopped. Processed: {processed}")


def main():
    p = argparse.ArgumentParser(
        description="Ahrefs Checker — Parallel Pull Mode. By default the worker "
                    "polls BOTH /ahref-authority/ and /ahref-traffic/ in round-robin "
                    "and processes whichever queue has a message — the workflow "
                    "doesn't need to pick a mode upfront.",
    )
    p.add_argument("--api-url", default=DEFAULT_API_URL)
    p.add_argument("--modes", default="authority,traffic",
                   help="Comma-separated list of Ahrefs checks to pull (default: "
                        "'authority,traffic' — both queues). Set to 'authority' or "
                        "'traffic' alone to lock the worker to one queue.")
    p.add_argument("--mode", choices=list(MODES.keys()),
                   help="DEPRECATED — use --modes. Single-mode shortcut kept for "
                        "backwards compatibility; equivalent to --modes <value>.")
    p.add_argument("--headless", action="store_true")
    p.add_argument("--workers", type=int, default=5, help="Number of parallel browser instances")
    p.add_argument("--chrome", help="Path to chrome binary")
    p.add_argument("--proxies", default=PROXIES_PATH, help="Path to proxies file")
    p.add_argument("--no-proxy", action="store_true", help="Disable proxies, run all instances on local IP")
    args = p.parse_args()

    # --mode is deprecated but still respected; --modes wins if both given.
    raw_modes = args.modes if args.modes else (args.mode or "authority,traffic")
    requested = [m.strip().lower() for m in raw_modes.split(",") if m.strip()]
    unknown = [m for m in requested if m not in MODES]
    if unknown:
        print(f"[FATAL] Unknown mode(s): {unknown}. Valid: {list(MODES.keys())}",
              file=sys.stderr)
        sys.exit(2)
    if not requested:
        print(f"[FATAL] No modes given. Pass --modes authority,traffic.", file=sys.stderr)
        sys.exit(2)
    enabled_modes = [MODES[m] for m in requested]

    _install_signal_handlers()

    # Pre-download the master profile once before any worker starts, so a
    # download failure aborts the run before we burn cycles spinning up
    # ThreadPoolExecutor and partially-initialized workers.
    try:
        _download_master_profile()
    except Exception as e:
        print(f"[FATAL] Cannot bootstrap master profile: {e}", file=sys.stderr)
        sys.exit(2)

    proxies = [] if args.no_proxy else load_proxies()
    num_workers = args.workers

    if not args.no_proxy and not proxies:
        print("[WARN] No proxies found. Running all instances on local IP.")

    chrome_bin = args.chrome or find_chrome_binary()
    version_main = detect_chrome_major(chrome_bin)

    mode_summary = ", ".join(f"{m.name}->{m.endpoint}" for m in enabled_modes)
    print(f"[*] Ahrefs Checker - Parallel Pull Mode")
    print(f"[*] Modes: {mode_summary}")
    print(f"[*] API: {args.api_url}")
    print(f"[*] Chrome: {chrome_bin}")
    print(f"[*] Workers: {num_workers} | Proxy: {'disabled' if args.no_proxy or not proxies else f'{len(proxies)} loaded'}")
    print(f"[*] Launching {num_workers} browser instances...\n")

    # Flush any results buffered from a previous crashed run for EACH enabled
    # mode (each has its own per-mode buffer file), then start a flusher
    # thread per mode so ongoing failed POSTs drain in the background.
    for mode in enabled_modes:
        initial = _flush_pending_ahref_posts(args.api_url, mode, max_per_run=200)
        if initial:
            print(f"[*] Recovered {initial} buffered {mode.name} posts from previous run")
        _start_ahref_buffer_flusher(args.api_url, mode)

    with ThreadPoolExecutor(max_workers=num_workers) as executor:
        futures = []
        for i in range(num_workers):
            proxy = proxies[i % len(proxies)] if proxies else None
            f = executor.submit(
                worker_loop, i, proxy, args.api_url, args.headless,
                chrome_bin, version_main, enabled_modes,
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
