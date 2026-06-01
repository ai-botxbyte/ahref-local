"""
Ahrefs Website Authority Checker — nodriver / EzSolver-style architecture.

Replaces the previous undetected-chromedriver implementation with `nodriver`
(the engine behind https://github.com/ismoiloffS/EzSolver). nodriver speaks
raw CDP, so synthesized events are indistinguishable from real user input
and Cloudflare Turnstile cannot detect them via the usual
"webdriver / Selenium" heuristics.

Request payload
───────────────
{
    "domains":  ["example.com", "google.com"],
    "domain":   "example.com",       // optional shortcut
    "headless": true                 // honored on local boxes with DISPLAY,
                                     // ignored on Lambda where Xvfb is forced
}
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import random
import subprocess
import sys
import time
import traceback
from typing import Any, Dict, List, Optional

# Force unbuffered output so Lambda CloudWatch sees logs in real time.
try:
    sys.stdout.reconfigure(line_buffering=True, write_through=True)
    sys.stderr.reconfigure(line_buffering=True, write_through=True)
except Exception:
    pass


def _log(msg: str) -> None:
    print(msg, flush=True)


# ─────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────
AHREFS_URL = "https://ahrefs.com/website-authority-checker/"
AHREFS_JSON_PATH = os.path.join(os.path.dirname(__file__), "ahrefs.json")

USE_XVFB = os.environ.get("USE_XVFB", "1") not in ("0", "false", "False", "")

PAGE_LOAD_TIMEOUT  = int(os.environ.get("PAGE_LOAD_TIMEOUT",  "60"))
SCRIPT_TIMEOUT     = int(os.environ.get("SCRIPT_TIMEOUT",     "180"))
INPUT_WAIT_SEC     = int(os.environ.get("INPUT_WAIT_SEC",     "30"))
TURNSTILE_WAIT_SEC = int(os.environ.get("TURNSTILE_WAIT_SEC", "90"))


# ─────────────────────────────────────────────
# ahrefs.json loader (page-side scrape script)
# ─────────────────────────────────────────────
_cached_js: Optional[str] = None


def load_ahrefs_js() -> str:
    global _cached_js
    if _cached_js is not None:
        return _cached_js
    with open(AHREFS_JSON_PATH, "r", encoding="utf-8") as fh:
        spec = json.load(fh)
    action = next((a for a in spec.get("actions", []) if a.get("type") == "evaluate"), None)
    if not action or "script" not in action:
        raise RuntimeError("ahrefs.json missing the 'evaluate' action / script")
    _cached_js = action["script"]
    return _cached_js


# ─────────────────────────────────────────────
# Xvfb — needed on Lambda / any Linux host without DISPLAY
# ─────────────────────────────────────────────
_xvfb_proc: Optional[subprocess.Popen] = None


def start_xvfb_if_needed() -> Optional[subprocess.Popen]:
    global _xvfb_proc
    if _xvfb_proc is not None:
        return _xvfb_proc
    if platform.system() != "Linux":
        return None
    if not USE_XVFB:
        return None
    if os.environ.get("DISPLAY"):
        _log(f"[*] DISPLAY already set to {os.environ['DISPLAY']} — skipping Xvfb")
        return None

    from shutil import which
    if not which("Xvfb"):
        _log("[!] Xvfb not installed")
        return None

    _log("[*] Starting Xvfb on :99 (1920x1080x24)")
    try:
        os.makedirs("/tmp/.X11-unix", exist_ok=True)
        os.chmod("/tmp/.X11-unix", 0o1777)
    except Exception as e:
        _log(f"[!] Could not prepare /tmp/.X11-unix: {e}")

    xvfb_log = open("/tmp/xvfb.log", "wb")
    proc = subprocess.Popen(
        ["Xvfb", ":99", "-screen", "0", "1920x1080x24", "-ac",
         "+extension", "RANDR", "-nolisten", "tcp"],
        stdout=xvfb_log, stderr=xvfb_log,
    )
    os.environ["DISPLAY"] = ":99"

    sock = "/tmp/.X11-unix/X99"
    for i in range(50):
        if os.path.exists(sock):
            _log(f"[+] Xvfb socket ready after {i*0.1:.1f}s")
            break
        time.sleep(0.1)
        if proc.poll() is not None:
            try:
                with open("/tmp/xvfb.log", "r") as f:
                    _log("[!] Xvfb died early:\n" + f.read())
            except Exception:
                pass
            return None
    else:
        _log(f"[!] Xvfb socket {sock} never appeared")

    time.sleep(0.5)
    _xvfb_proc = proc
    return proc


def stop_xvfb() -> None:
    global _xvfb_proc
    if _xvfb_proc is not None:
        try:
            _xvfb_proc.terminate()
        except Exception:
            pass
        _xvfb_proc = None


# ─────────────────────────────────────────────
# Chrome locator + proxy-auth extension
# ─────────────────────────────────────────────
def _find_chrome() -> str:
    env_path = os.environ.get("CHROME_BINARY") or os.environ.get("CHROME_PATH")
    if env_path and os.path.isfile(env_path):
        return env_path
    candidates = [
        "/opt/google/chrome/chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium-browser",
        "/usr/bin/chromium",
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    raise FileNotFoundError("Chrome binary not found. Set CHROME_BINARY.")


def _build_proxy_auth_extension(host: str, port: str, user: str, password: str) -> str:
    """
    Chrome's --proxy-server flag does NOT accept user:pass@host:port for HTTP
    auth, so we build a tiny MV2 extension that supplies credentials via the
    chrome.webRequest.onAuthRequired API.
    """
    ext_dir = f"/tmp/proxy-auth-ext-{abs(hash((host, port, user))) % 10**8}"
    os.makedirs(ext_dir, exist_ok=True)
    manifest = {
        "name": "Proxy Auth",
        "version": "1.0.0",
        "manifest_version": 2,
        "permissions": [
            "proxy", "tabs", "unlimitedStorage", "storage", "<all_urls>",
            "webRequest", "webRequestBlocking"
        ],
        "background": {"scripts": ["background.js"]},
        "minimum_chrome_version": "76.0.0"
    }
    background_js = f"""
var config = {{
    mode: "fixed_servers",
    rules: {{
        singleProxy: {{ scheme: "http", host: "{host}", port: parseInt({port}) }},
        bypassList: ["localhost"]
    }}
}};
chrome.proxy.settings.set({{value: config, scope: "regular"}}, function() {{}});
chrome.webRequest.onAuthRequired.addListener(
    function(details) {{
        return {{ authCredentials: {{ username: "{user}", password: "{password}" }} }};
    }},
    {{ urls: ["<all_urls>"] }},
    ["blocking"]
);
"""
    with open(os.path.join(ext_dir, "manifest.json"), "w") as f:
        json.dump(manifest, f)
    with open(os.path.join(ext_dir, "background.js"), "w") as f:
        f.write(background_js)
    return ext_dir


# ─────────────────────────────────────────────
# xdotool helpers (only used when running under Xvfb)
# ─────────────────────────────────────────────
def xdotool_focus_chrome() -> None:
    from shutil import which
    if not which("xdotool") or not os.environ.get("DISPLAY"):
        return
    try:
        result = subprocess.run(
            ["xdotool", "search", "--onlyvisible", "--name", "Chrome"],
            capture_output=True, timeout=3, text=True,
        )
        wid = (result.stdout.strip().split("\n") or [""])[0]
        if wid:
            subprocess.run(["xdotool", "windowactivate", "--sync", wid], timeout=3,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["xdotool", "mousemove", "960", "540"], timeout=2,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            _log(f"[*] xdotool activated Chrome window {wid}")
    except Exception as e:
        _log(f"[!] xdotool focus failed: {e}")


# ─────────────────────────────────────────────
# nodriver browser builder
# ─────────────────────────────────────────────
async def build_browser(headless: bool, profile_dir: Optional[str] = None):
    import nodriver as uc

    chrome_path = _find_chrome()
    _log(f"[*] Using browser binary: {chrome_path}")

    # Persistent profile so Cloudflare clearance cookies survive warm Lambda
    # invocations (this is EzSolver's main trick to avoid solving Turnstile
    # on every single request).
    user_data_dir = profile_dir or os.environ.get("TS_PROFILE_DIR", "/tmp/ts_profile")
    # Wipe any stale profile from previous handler (UC) layout
    stale_marker = os.path.join(user_data_dir, ".uc_profile")
    if os.path.exists(stale_marker):
        import shutil
        shutil.rmtree(user_data_dir, ignore_errors=True)
    os.makedirs(user_data_dir, exist_ok=True)

    browser_args: List[str] = [
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        "--disable-blink-features=AutomationControlled",
        "--window-size=1920,1080",
        "--lang=en-US,en",
        # NOTE: deliberately NOT passing --disable-gpu / --mute-audio /
        # --no-zygote / --disable-software-rasterizer. Those flags make
        # the WebGL/AudioContext fingerprint look unmistakably headless,
        # and Cloudflare on ahrefs.com silently 0-bytes the response
        # when it sees them.
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
        "--disable-sync",
        "--disable-default-apps",
        "--disable-features=AutomationControlled,Translate",
        "--hide-scrollbars",
        "--metrics-recording-only",
        # Make WebGL report a plausible vendor (Mesa + llvmpipe is fine —
        # it's a real Linux GPU stack, not "SwiftShader" which is a
        # known headless tell).
        "--use-gl=angle",
        "--use-angle=swiftshader-webgl",
        "--enable-webgl",
    ]

    # Authenticated proxy via in-memory extension.
    proxy_host = os.environ.get("PROXY_HOST")
    proxy_port = os.environ.get("PROXY_PORT")
    proxy_user = os.environ.get("PROXY_USER")
    proxy_pass = os.environ.get("PROXY_PASS")
    if proxy_host and proxy_port:
        if proxy_user and proxy_pass:
            ext_dir = _build_proxy_auth_extension(proxy_host, proxy_port, proxy_user, proxy_pass)
            browser_args.append(f"--load-extension={ext_dir}")
            browser_args.append(f"--disable-extensions-except={ext_dir}")
            _log(f"[*] Using authenticated proxy {proxy_host}:{proxy_port} via extension")
        else:
            browser_args.append(f"--proxy-server=http://{proxy_host}:{proxy_port}")
            _log(f"[*] Using anonymous proxy {proxy_host}:{proxy_port}")

    browser = await uc.start(
        browser_executable_path=chrome_path,
        headless=headless,
        user_data_dir=user_data_dir,
        browser_args=browser_args,
        sandbox=False,
    )
    return browser


# ─────────────────────────────────────────────
# Turnstile solver — port of EzSolver/solver.py click loop, but operating
# directly on the live Ahrefs page (not an injected widget).
# ─────────────────────────────────────────────
async def _turnstile_present(page) -> bool:
    raw = await page.evaluate("""
        (() => {
            const fr = document.querySelectorAll('iframe');
            for (const f of fr) {
                const src = (f.src || '').toLowerCase();
                if (src.indexOf('challenges.cloudflare.com') !== -1) return true;
            }
            return false;
        })()
    """)
    return bool(raw)


async def _get_turnstile_iframe_rect(page) -> Optional[dict]:
    raw = await page.evaluate("""
        JSON.stringify((() => {
            for (const f of document.querySelectorAll('iframe')) {
                const src = f.src || '';
                if (src.indexOf('challenges.cloudflare.com') === -1) continue;
                const r = f.getBoundingClientRect();
                if (r.width > 50 && r.height > 20) return {x:r.x, y:r.y, w:r.width, h:r.height};
            }
            return null;
        })())
    """)
    if raw and raw != "null":
        try:
            return json.loads(raw)
        except Exception:
            return None
    return None


async def _modal_ready(page) -> bool:
    raw = await page.evaluate("""
        (() => {
            const m = document.querySelector('.ReactModalPortal [role="dialog"]')
                   || document.querySelector('[class*="ReactModal__Content"]');
            if (!m) return false;
            return m.querySelectorAll('span[class*="css-vyilnr"]').length > 0;
        })()
    """)
    return bool(raw)


async def solve_turnstile(page, timeout: int = TURNSTILE_WAIT_SEC) -> bool:
    if not await _turnstile_present(page):
        _log("[*] No Turnstile iframe — nothing to solve.")
        return True

    _log("[*] Cloudflare Turnstile detected — attempting to solve via nodriver…")
    deadline = asyncio.get_event_loop().time() + timeout
    click_count = 0
    last_click = 0.0
    rect: Optional[dict] = None

    # Wait up to 10s for iframe to grow to real size
    for _ in range(20):
        rect = await _get_turnstile_iframe_rect(page)
        if rect:
            break
        await asyncio.sleep(0.5)

    while asyncio.get_event_loop().time() < deadline:
        if await _modal_ready(page):
            _log("[+] Result modal appeared — Turnstile passed.")
            return True
        if not await _turnstile_present(page):
            _log("[+] Turnstile iframe gone — challenge passed.")
            return True

        now = asyncio.get_event_loop().time()
        if click_count == 0 or (now - last_click) > 6:
            rect = await _get_turnstile_iframe_rect(page) or rect
            if rect:
                cx = rect["x"] + 28 + random.uniform(-3, 3)
                cy = rect["y"] + rect["h"] / 2 + random.uniform(-3, 3)
            else:
                cx = 48.0 + random.uniform(-3, 3)
                cy = 52.0 + random.uniform(-3, 3)

            xdotool_focus_chrome()
            try:
                await page.mouse_move(cx - 80, cy - 20)
                await asyncio.sleep(random.uniform(0.15, 0.25))
                await page.mouse_move(cx, cy)
                await asyncio.sleep(random.uniform(0.08, 0.15))
                await page.mouse_click(cx, cy)
                click_count += 1
                last_click = asyncio.get_event_loop().time()
                _log(f"[*] CDP click attempt #{click_count} at ({cx:.0f},{cy:.0f})")
            except Exception as e:
                _log(f"[!] CDP click failed: {e}")
            await asyncio.sleep(1.5)
            continue

        await asyncio.sleep(0.4)

    _log("[-] Turnstile solve timed out.")
    return False


# ─────────────────────────────────────────────
# Core scrape (async)
# ─────────────────────────────────────────────
async def _scrape_ahrefs_async(domains: List[str], headless: bool, profile_dir: Optional[str] = None) -> Dict[str, Any]:
    js_template = load_ahrefs_js()
    domains_csv = ",".join(d.strip() for d in domains if d and d.strip())
    js_payload = js_template.replace("${domains}", domains_csv)

    # On Linux without DISPLAY → force Xvfb + headful (Turnstile flags --headless)
    if headless and platform.system() == "Linux":
        xvfb = start_xvfb_if_needed()
        if xvfb is not None or os.environ.get("DISPLAY"):
            _log("[*] Forcing headless=False under Xvfb for Turnstile reliability")
            headless = False
        else:
            _log("[!] Xvfb unavailable — running true headless (Turnstile may fail)")

    # Stub the TURNSTILE_FOCUS_REQUEST handler so the ahrefs.json script doesn't
    # idle waiting for an extension reply we don't have.
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

    browser = await build_browser(headless=headless, profile_dir=profile_dir)
    try:
        _log(f"[*] Loading {AHREFS_URL}")
        page = await browser.get(AHREFS_URL)
        # nodriver returns when the navigation starts — give the SPA time.
        await asyncio.sleep(3.0)
        xdotool_focus_chrome()

        # Wait for input OR turnstile to render.
        end_wait = time.time() + INPUT_WAIT_SEC
        while time.time() < end_wait:
            has_input = await page.evaluate(
                "(() => !!document.querySelector(\"input[type='text']\"))()"
            )
            if has_input or await _turnstile_present(page):
                break
            await asyncio.sleep(0.5)

        if await _turnstile_present(page):
            if not await solve_turnstile(page):
                return {
                    "status": "error",
                    "error": "Cloudflare Turnstile could not be solved within timeout.",
                }

        # Mirror `delay: 2000` from the JSON spec
        await asyncio.sleep(2.0)

        _log(f"[*] Executing ahrefs.json script for domains: {domains_csv}")

        # Kick off the page-side script in non-blocking mode and poll for
        # window.__ahrefsResult so we can keep clicking Turnstile in parallel.
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
        await page.evaluate(kickoff_js)

        poll_deadline = time.time() + SCRIPT_TIMEOUT
        last_status_at = 0.0
        last_click_at = 0.0
        click_count = 0
        raw = None
        loop_iter = 0

        while time.time() < poll_deadline:
            loop_iter += 1

            # Heartbeat
            if time.time() - last_status_at > 15:
                last_status_at = time.time()
                try:
                    diag_raw = await page.evaluate("""
                        JSON.stringify((() => {
                            const inp = document.querySelector("input[type='text']");
                            const modal = document.querySelector('.ReactModalPortal [role="dialog"]')
                                       || document.querySelector('[class*="ReactModal__Content"]');
                            return {
                                url: location.href,
                                title: document.title,
                                hasInput: !!inp,
                                inputVal: inp ? inp.value : null,
                                hasModal: !!modal,
                                modalSpans: modal ? modal.querySelectorAll('span[class*="css-vyilnr"]').length : 0,
                                bodyLen: document.body ? document.body.innerText.length : 0,
                                ahrefsResultType: typeof window.__ahrefsResult,
                                ahrefsErrorType: typeof window.__ahrefsError
                            };
                        })())
                    """)
                    _log(f"[poll #{loop_iter}] clicks={click_count} diag={diag_raw}")
                except Exception as de:
                    _log(f"[poll #{loop_iter}] diag exception: {de}")

            # Result?
            try:
                done_raw = await page.evaluate(
                    "JSON.stringify({r: window.__ahrefsResult, e: window.__ahrefsError})"
                )
                done_payload = json.loads(done_raw) if done_raw else {}
            except Exception as poll_exc:
                _log(f"[!] poll exception: {poll_exc}")
                await asyncio.sleep(2)
                continue

            if done_payload.get("r") is not None:
                raw = done_payload["r"]
                break
            if done_payload.get("e") is not None:
                raw = done_payload["e"]
                break

            # Re-click Turnstile if it pops up
            try:
                if await _turnstile_present(page) and (time.time() - last_click_at) > 4:
                    rect = await _get_turnstile_iframe_rect(page)
                    if rect:
                        click_x = rect["x"] + 28
                        click_y = rect["y"] + rect["h"] / 2
                        xdotool_focus_chrome()
                        await page.mouse_move(click_x - 60, click_y - 20)
                        await asyncio.sleep(0.15)
                        await page.mouse_move(click_x, click_y)
                        await asyncio.sleep(0.10)
                        await page.mouse_click(click_x, click_y)
                        click_count += 1
                        last_click_at = time.time()
                        _log(f"[*] CDP click attempt #{click_count} at ({click_x:.0f},{click_y:.0f})")
            except Exception as ts_exc:
                _log(f"[!] turnstile check exception: {ts_exc}")

            await asyncio.sleep(1.5)

        if raw is None:
            return {"status": "error", "error": "Page-side script timed out without producing a result."}

        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = {"raw": raw}
        else:
            parsed = raw if raw is not None else {"error": "empty result"}

        return {
            "status": "completed",
            **(parsed if isinstance(parsed, dict) else {"result": parsed}),
        }

    except Exception as exc:
        traceback.print_exc()
        return {"status": "error", "error": str(exc)}
    finally:
        try:
            browser.stop()
        except Exception:
            pass


def scrape_ahrefs(domains: List[str], headless: bool = False) -> Dict[str, Any]:
    """
    Sync wrapper around the async scrape — Lambda's runtime expects a sync
    handler, and nodriver requires its own event loop.
    """
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        # nodriver insists on `asyncio.run` (it manages its own loop policy).
        return asyncio.run(_scrape_ahrefs_async(domains, headless))


# ─────────────────────────────────────────────
# Lambda entry-point
# ─────────────────────────────────────────────
def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    try:
        if isinstance(event.get("body"), str):
            payload = json.loads(event["body"])
        elif isinstance(event.get("body"), dict):
            payload = event["body"]
        else:
            payload = event

        domains = payload.get("domains")
        if not domains:
            single = payload.get("domain")
            if single:
                domains = [single]

        if not domains or not isinstance(domains, list):
            return {
                "statusCode": 400,
                "body": json.dumps({
                    "error": "Request body must include 'domains' (list) or 'domain' (str)."
                }),
            }

        headless = bool(payload.get("headless", True))
        result = scrape_ahrefs(domains, headless=headless)
        status_code = 200 if result.get("status") == "completed" else 500
        return {
            "statusCode": status_code,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(result),
        }
    except json.JSONDecodeError as je:
        return {"statusCode": 400, "body": json.dumps({"error": f"Invalid JSON: {je}"})}
    except Exception as exc:
        traceback.print_exc()
        return {"statusCode": 500, "body": json.dumps({"error": str(exc)})}


# ─────────────────────────────────────────────
# Local CLI
#   python lambda_handler.py example.com google.com
# ─────────────────────────────────────────────
if __name__ == "__main__":
    test_domains = sys.argv[1:] or ["example.com"]
    out = lambda_handler({"domains": test_domains, "headless": True}, None)
    print(json.dumps(json.loads(out["body"]), indent=2))
