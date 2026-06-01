"""
Test lambda_handler.lambda_handler() (the actual Lambda entry-point)
with proxy support for botxbyte.com.

Strategy:
  - Pick proxy #1 from /tmp/proxies.txt
  - Invoke lambda_handler({"domain": "botxbyte.com"}, None) with PROXY_* env set
  - If success → stop
  - If fail → try next proxy, up to 3 total
"""
import os
import sys
import json
import time

PROXY_FILE = "/tmp/proxies.txt"
DOMAIN = "botxbyte.com"
MAX_ATTEMPTS = 3


def load_proxies():
    proxies = []
    with open(PROXY_FILE) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split(":")
            if len(parts) != 4:
                continue
            proxies.append({"host": parts[0], "port": parts[1], "user": parts[2], "pass": parts[3]})
    return proxies


def invoke_lambda_with_proxy(proxy):
    os.environ["PROXY_HOST"] = proxy["host"]
    os.environ["PROXY_PORT"] = proxy["port"]
    os.environ["PROXY_USER"] = proxy["user"]
    os.environ["PROXY_PASS"] = proxy["pass"]
    os.environ["SCRIPT_TIMEOUT"] = "180"
    os.environ["TURNSTILE_WAIT_SEC"] = "60"
    os.environ["PAGE_LOAD_TIMEOUT"] = "45"

    # Fresh module each attempt
    for m in list(sys.modules):
        if m.startswith("lambda_handler"):
            del sys.modules[m]
    import lambda_handler

    event = {"domain": DOMAIN, "headless": True}
    t0 = time.time()
    response = lambda_handler.lambda_handler(event, None)
    elapsed = round(time.time() - t0, 1)
    return response, elapsed


def main():
    proxies = load_proxies()
    print(f"[*] Will try up to {MAX_ATTEMPTS} proxies for domain={DOMAIN}")

    final = None
    attempts_log = []

    for i in range(min(MAX_ATTEMPTS, len(proxies))):
        p = proxies[i]
        label = f"{p['host']}:{p['port']}"
        print(f"\n{'=' * 70}")
        print(f"ATTEMPT {i + 1}/{MAX_ATTEMPTS}  proxy={label}")
        print(f"{'=' * 70}")

        try:
            response, elapsed = invoke_lambda_with_proxy(p)
        except Exception as e:
            print(f"[FAIL] exception: {e}")
            attempts_log.append({"proxy": label, "verdict": "FAIL", "error": str(e)})
            continue

        status_code = response.get("statusCode")
        try:
            body = json.loads(response.get("body", "{}"))
        except Exception:
            body = {"raw_body": response.get("body")}

        verdict = "OK" if status_code == 200 and body.get("status") == "completed" else "FAIL"
        print(f"[{verdict}] statusCode={status_code} elapsed={elapsed}s")
        print(f"body: {json.dumps(body, indent=2)[:800]}")

        attempts_log.append({
            "proxy": label,
            "verdict": verdict,
            "statusCode": status_code,
            "elapsed_s": elapsed,
            "body": body,
        })

        if verdict == "OK":
            final = attempts_log[-1]
            break

    print("\n" + "=" * 70)
    print("FINAL VERDICT")
    print("=" * 70)
    if final:
        print(f"✅ SUCCESS on proxy {final['proxy']} after {len(attempts_log)} attempt(s)")
        results = final["body"].get("results", [])
        if results:
            print(f"Data for {DOMAIN}:")
            print(json.dumps(results[0], indent=2))
    else:
        print(f"❌ FAILED after {len(attempts_log)} attempt(s)")
        for a in attempts_log:
            print(f"  - {a['proxy']}: {a.get('error') or a.get('body', {}).get('error', 'unknown')}")

    with open("/tmp/lambda_proxy_test.json", "w") as f:
        json.dump(attempts_log, f, indent=2, default=str)
    print("\nFull log saved to /tmp/lambda_proxy_test.json")


if __name__ == "__main__":
    main()
