# ahref-lambda

AWS Lambda function that runs Ahrefs **Website Authority Checker** via
**undetected-chromedriver** and returns DR / Backlinks / Linking Websites
/ dofollow % for one or more domains.

## How Turnstile is solved
Cloudflare Turnstile **always detects `--headless` flags**. The proven
work-around (same approach as the [EzSolver](https://github.com/Tangramconditionalsale300/EzSolver)
project) is:

1. Run Chrome with **`headless=False`** (real headful browser)
2. Wrap it in an **Xvfb virtual display** on Linux servers without a monitor

This module auto-starts Xvfb when:
- `platform.system() == "Linux"` and
- `$DISPLAY` is not already set and
- `USE_XVFB` env var is not `0`

When the caller passes `headless=true`, the code transparently flips to
`headless=False` + Xvfb. The browser never appears on a real screen, but
Cloudflare sees a real browser session.

## Install (Linux)
```bash
sudo apt-get install -y xvfb google-chrome-stable
cd /home/sanket777/Desktop/Botxbyte/ahref-lambda
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## Test
```bash
# Visible browser (Turnstile solves easily)
python3 lambda_handler.py example.com

# "Headless" — actually headful + Xvfb under the hood
python3 -c "import json; from lambda_handler import lambda_handler; \
print(json.dumps(json.loads(lambda_handler({'domains':['example.com'],'headless':True}, None)['body']), indent=2))"
```

## Request payload
```json
{ "domains": ["example.com", "google.com"], "headless": true }
```
or single:
```json
{ "domain": "example.com" }
```

## Files
| File | Purpose |
|------|---------|
| `lambda_handler.py` | UC driver + Xvfb wrapper + Turnstile solver + lambda entrypoint |
| `ahrefs.json` | Browser-automation spec (selectors + JS that drives the page). The JS in `actions[2].script` is executed verbatim. |
| `requirements.txt` | Python deps |
| `serverless.yml` | Deploy config (Chromium provided by a Lambda layer) |
