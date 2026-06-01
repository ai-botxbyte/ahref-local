"""
Test each proxy from /tmp/proxies.txt against botxbyte.com via lambda_handler.

For each proxy line (host:port:user:pass):
  - Set PROXY_* env vars
  - Call scrape_ahrefs(["botxbyte.com"])
  - Record OK / FAIL with reason
"""
import os
import sys
import json
import time
import traceback

PROXY_FILE = "/tmp/proxies.txt"
DOMAIN = "botxbyte.com"
RESULTS_FILE = "/tmp/proxy_results.json"
PER_PROXY_TIMEOUT_SECS = 180  # cap script timeout for faster failure


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


def run_one(proxy):
    # Set env BEFORE importing module so build_driver reads it
    os.environ["PROXY_HOST"] = proxy["host"]
    os.environ["PROXY_PORT"] = proxy["port"]
    os.environ["PROXY_USER"] = proxy["user"]
    os.environ["PROXY_PASS"] = proxy["pass"]
    os.environ["SCRIPT_TIMEOUT"] = str(PER_PROXY_TIMEOUT_SECS)
    os.environ["TURNSTILE_WAIT_SEC"] = "60"
    os.environ["PAGE_LOAD_TIMEOUT"] = "45"
    os.environ["INPUT_WAIT_SEC"] = "30"

    # Re-import fresh each run so module-level caches don't bleed
    for mod in list(sys.modules):
        if mod.startswith("lambda_handler"):
            del sys.modules[mod]
    import lambda_handler  # noqa

    t0 = time.time()
    try:
        result = lambda_handler.scrape_ahrefs([DOMAIN], headless=True)
    except Exception as e:
        result = {"status": "error", "error": f"exception: {e}", "trace": traceback.format_exc()}
    elapsed = round(time.time() - t0, 1)
    result["__elapsed_s"] = elapsed
    return result


def main():
    proxies = load_proxies()
    print(f"[*] Loaded {len(proxies)} proxies — testing against {DOMAIN}")
    results = []
    for i, p in enumerate(proxies, 1):
        label = f"{p['host']}:{p['port']}"
        print(f"\n{'='*70}\n[{i}/{len(proxies)}] Testing proxy {label}\n{'='*70}")
        res = run_one(p)
        status = res.get("status")
        verdict = "OK" if status == "completed" and not res.get("error") else "FAIL"
        summary = {
            "proxy": label,
            "verdict": verdict,
            "status": status,
            "elapsed_s": res.get("__elapsed_s"),
            "error": res.get("error"),
            "result_preview": {k: v for k, v in res.items() if k not in ("__elapsed_s",)},
        }
        print(f"[{verdict}] {label}  status={status}  elapsed={res.get('__elapsed_s')}s")
        if res.get("error"):
            print(f"   error: {str(res.get('error'))[:200]}")
        results.append(summary)
        # save incrementally
        with open(RESULTS_FILE, "w") as f:
            json.dump(results, f, indent=2, default=str)

    print("\n\n" + "=" * 70)
    print("FINAL SUMMARY")
    print("=" * 70)
    ok = [r for r in results if r["verdict"] == "OK"]
    fail = [r for r in results if r["verdict"] == "FAIL"]
    print(f"OK:   {len(ok)}")
    print(f"FAIL: {len(fail)}")
    for r in results:
        print(f"  [{r['verdict']:4s}] {r['proxy']:30s}  {r['elapsed_s']}s  {(r['error'] or '')[:80]}")
    print(f"\nDetails saved to {RESULTS_FILE}")


if __name__ == "__main__":
    main()
