"""
FastAPI wrapper around the Ahrefs scraper for Kubernetes deployment.
Reuses scrape_ahrefs() from lambda_handler.py.
"""
import os
import sys

try:
    sys.stdout.reconfigure(line_buffering=True, write_through=True)
    sys.stderr.reconfigure(line_buffering=True, write_through=True)
except Exception:
    pass

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import List, Optional, Union

from lambda_handler import scrape_ahrefs

app = FastAPI(title="Ahrefs Website Authority Checker", version="1.0.0")


class ScrapeRequest(BaseModel):
    domains: Optional[List[str]] = None
    domain: Optional[str] = None
    headless: Optional[bool] = True


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/")
def root():
    return {"service": "ahrefs-checker", "endpoints": ["/scrape", "/healthz"]}


@app.post("/scrape")
def scrape(req: ScrapeRequest):
    domains = req.domains
    if not domains and req.domain:
        domains = [req.domain]
    if not domains:
        raise HTTPException(400, "Provide 'domains' (list) or 'domain' (str)")
    result = scrape_ahrefs(domains, headless=bool(req.headless))
    return result


@app.get("/debug")
def debug():
    """Real-user-style: type domain, real-click the submit button via CDP,
    see if Cloudflare serves a Turnstile widget."""
    import asyncio, json
    from lambda_handler import build_browser, start_xvfb_if_needed, AHREFS_URL, xdotool_focus_chrome
    start_xvfb_if_needed()

    async def _go():
        b = await build_browser(headless=False)
        try:
            page = await b.get(AHREFS_URL)
            await asyncio.sleep(5)
            xdotool_focus_chrome()
            # Dismiss cookie banner
            await page.evaluate("""(() => {
                const dismiss = document.querySelector('.cky-banner-btn-close, [data-cky-tag="close-button"]');
                if (dismiss) dismiss.click();
                document.querySelectorAll('.cky-overlay, .cky-modal').forEach(e=>e.remove());
            })()""")
            await asyncio.sleep(1)
            # Get input element coords for real click
            inp_rect = await page.evaluate("""
                JSON.stringify((() => {
                    const inp = document.querySelector("input[type='text']");
                    if (!inp) return null;
                    const r = inp.getBoundingClientRect();
                    return {x: r.x + r.width/2, y: r.y + r.height/2};
                })())
            """)
            ir = json.loads(inp_rect) if inp_rect and inp_rect != 'null' else None
            if ir:
                await page.mouse_click(ir['x'], ir['y'])
                await asyncio.sleep(0.4)
            # Type using real keyboard events
            for ch in 'botxbyte.com':
                await page.evaluate(f"""(() => {{
                    const inp = document.querySelector("input[type='text']");
                    if (!inp) return;
                    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;
                    setter.call(inp, (inp.value||'') + {json.dumps(ch)});
                    inp.dispatchEvent(new Event('input',{{bubbles:true}}));
                }})()""")
                await asyncio.sleep(0.08)
            # Now real-click the submit button
            btn_rect = await page.evaluate("""
                JSON.stringify((() => {
                    let b = document.querySelector("form button[type='submit']");
                    if (!b) {
                        b = Array.from(document.querySelectorAll('button')).find(x => x.textContent.includes('Check Authority'));
                    }
                    if (!b) return null;
                    const r = b.getBoundingClientRect();
                    return {x: r.x + r.width/2, y: r.y + r.height/2, text: b.textContent.trim()};
                })())
            """)
            br = json.loads(btn_rect) if btn_rect and btn_rect != 'null' else None
            if br:
                await page.mouse_move(br['x'] - 40, br['y'] - 10)
                await asyncio.sleep(0.2)
                await page.mouse_click(br['x'], br['y'])
            # Wait 20s for Turnstile / modal
            await asyncio.sleep(20)
            dump_raw = await page.evaluate("""
                JSON.stringify({
                    url: location.href,
                    iframes: Array.from(document.querySelectorAll('iframe')).map(f=>(f.src||'').slice(0,150)),
                    hasTurnstile: !!document.querySelector('iframe[src*="challenges.cloudflare.com"]'),
                    hasModal: !!document.querySelector('.ReactModalPortal [role="dialog"], [class*="ReactModal__Content"]'),
                    bodyLen: document.body.innerText.length,
                    cookies: document.cookie,
                    hasFocus: document.hasFocus(),
                    visibilityState: document.visibilityState,
                    screenW: screen.width, screenH: screen.height,
                    innerW: window.innerWidth, innerH: window.innerHeight,
                    devicePixelRatio: window.devicePixelRatio,
                    webglVendor: (() => {
                        try {
                            const c = document.createElement('canvas').getContext('webgl');
                            const ext = c.getExtension('WEBGL_debug_renderer_info');
                            return c.getParameter(ext.UNMASKED_VENDOR_WEBGL) + ' | ' + c.getParameter(ext.UNMASKED_RENDERER_WEBGL);
                        } catch(e) { return 'err:'+e.message; }
                    })()
                })
            """)
            return {"button_clicked": br, "input_clicked": ir, "after": json.loads(dump_raw)}
        finally:
            try: b.stop()
            except Exception: pass

    return asyncio.run(_go())


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
