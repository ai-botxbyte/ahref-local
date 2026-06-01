"""
Parallel local test harness for the nodriver-based Ahrefs scraper.

Usage:
  python parallel_test.py --mode direct  --workers 5
  python parallel_test.py --mode proxy   --workers 5
  python parallel_test.py --mode both    --workers 5

For each mode, fan out N concurrent browser instances. Each instance
scrapes ONE distinct domain (drawn from DOMAINS below) so we can answer:

  "How many distinct domains can a single IP scrape before Ahrefs starts
   to silently block (no Turnstile, no modal) ?"

Each browser gets its own isolated profile dir under /tmp/ts_profile_pN
so they don't fight over the same SingletonLock file.

Reports per-domain: time_taken, status, DR, backlinks, linking_websites.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import shutil
import time
from typing import Any, Dict, List, Optional, Tuple

# Domains to scrape (40+ unique) — small/medium/large mix so DR varies
DOMAINS = [
    "botxbyte.com", "example.com", "wikipedia.org", "github.com", "stackoverflow.com",
    "python.org", "nodejs.org", "rust-lang.org", "go.dev", "kotlinlang.org",
    "djangoproject.com", "flask.palletsprojects.com", "fastapi.tiangolo.com", "vuejs.org", "react.dev",
    "svelte.dev", "angular.io", "nextjs.org", "nuxt.com", "remix.run",
    "tailwindcss.com", "getbootstrap.com", "mui.com", "chakra-ui.com", "primevue.org",
    "redis.io", "postgresql.org", "mysql.com", "mongodb.com", "elastic.co",
    "kafka.apache.org", "rabbitmq.com", "nginx.org", "apache.org", "cloudflare.com",
    "vercel.com", "netlify.com", "digitalocean.com", "linode.com", "fly.io",
    "supabase.com", "firebase.google.com", "auth0.com", "stripe.com", "twilio.com",
]


def _proxies_from_file(path: str) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or ":" not in line:
                continue
            parts = line.split(":")
            if len(parts) != 4:
                continue
            out.append({"host": parts[0], "port": parts[1], "user": parts[2], "pass": parts[3]})
    return out


async def _scrape_one(
    domain: str,
    worker_id: int,
    mode: str,
    proxy: Optional[Dict[str, str]],
    timeout_s: int = 180,
) -> Dict[str, Any]:
    """Run one isolated scrape and return a result row."""
    # Per-worker env overrides
    if proxy:
        os.environ["PROXY_HOST"] = proxy["host"]
        os.environ["PROXY_PORT"] = proxy["port"]
        os.environ["PROXY_USER"] = proxy["user"]
        os.environ["PROXY_PASS"] = proxy["pass"]
    else:
        for k in ("PROXY_HOST", "PROXY_PORT", "PROXY_USER", "PROXY_PASS"):
            os.environ.pop(k, None)

    # Import lazily so PROXY_* env is picked up at build_browser-time
    from lambda_handler import _scrape_ahrefs_async

    profile_dir = f"/tmp/ts_profile_w{worker_id}"
    shutil.rmtree(profile_dir, ignore_errors=True)  # fresh profile per run

    t0 = time.time()
    try:
        result = await asyncio.wait_for(
            _scrape_ahrefs_async([domain], headless=True, profile_dir=profile_dir),
            timeout=timeout_s,
        )
    except asyncio.TimeoutError:
        result = {"status": "error", "error": f"hard timeout {timeout_s}s"}
    elapsed = round(time.time() - t0, 1)

    row: Dict[str, Any] = {
        "worker": worker_id,
        "mode": mode,
        "domain": domain,
        "elapsed": elapsed,
        "status": result.get("status"),
    }
    if result.get("status") == "completed":
        r0 = (result.get("results") or [{}])[0]
        row["dr"] = r0.get("dr")
        row["backlinks"] = r0.get("backlinks")
        row["linking_websites"] = r0.get("linking_websites")
        row["turnstile_retries"] = r0.get("turnstile_retries")
        if r0.get("error"):
            row["status"] = "error"
            row["error"] = r0["error"]
    else:
        row["error"] = result.get("error", "unknown")
    return row


async def run_batch(
    mode: str,
    workers: int,
    domains: List[str],
    proxies: Optional[List[Dict[str, str]]] = None,
) -> List[Dict[str, Any]]:
    """Run `workers` concurrent scrapes (one domain per worker). All workers
    use the same IP — either direct local IP (proxies=None) or one shared
    proxy IP (proxies=[only_one])."""
    pick = domains[:workers]
    chosen_proxy = proxies[0] if proxies else None
    print(f"[batch] mode={mode} workers={workers} proxy="
          f"{chosen_proxy['host']+':'+chosen_proxy['port'] if chosen_proxy else 'NONE'}")
    print(f"[batch] domains: {pick}")

    tasks = [
        _scrape_one(d, i, mode, chosen_proxy)
        for i, d in enumerate(pick)
    ]
    rows = await asyncio.gather(*tasks)
    return rows


def _print_report(label: str, rows: List[Dict[str, Any]], wall: float) -> None:
    print(f"\n{'='*78}\n  {label} — wall_clock={wall:.1f}s\n{'='*78}")
    print(f"  {'#':>2} {'domain':28} {'elapsed':>8} {'status':>10} {'DR':>4} {'BL':>8} {'LW':>6} {'retries':>7}")
    print(f"  {'-'*2} {'-'*28} {'-'*8} {'-'*10} {'-'*4} {'-'*8} {'-'*6} {'-'*7}")
    ok = 0
    for r in rows:
        if r.get("status") == "completed":
            ok += 1
            print(f"  {r['worker']:>2} {r['domain']:28} {r['elapsed']:>7.1f}s {'OK':>10} "
                  f"{str(r.get('dr','')):>4} {str(r.get('backlinks','')):>8} "
                  f"{str(r.get('linking_websites','')):>6} {str(r.get('turnstile_retries','')):>7}")
        else:
            err = (r.get('error') or '')[:30]
            print(f"  {r['worker']:>2} {r['domain']:28} {r['elapsed']:>7.1f}s {'FAIL':>10}   --      --     --   --   {err}")
    print(f"\n  ✅ success: {ok}/{len(rows)}    ⏱  total: {wall:.1f}s    ⚡ avg/domain: {wall/max(1,len(rows)):.1f}s")


async def main_async():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["direct", "proxy", "both"], default="both")
    parser.add_argument("--workers", type=int, default=5, help="parallel browsers per batch")
    parser.add_argument("--proxies-file", default="/tmp/proxies.txt")
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()

    # Local-only sanity: must NOT run under USE_XVFB on a desktop with DISPLAY
    os.environ.setdefault("USE_XVFB", "0")
    os.environ.setdefault("SCRIPT_TIMEOUT", str(args.timeout))

    # Make sure each worker uses its own profile dir
    os.environ.pop("TS_PROFILE_DIR", None)

    # Shuffle deterministically so direct/proxy hit different domains
    random.seed(42)
    pool = DOMAINS.copy()
    random.shuffle(pool)
    pool_direct = pool[:args.workers]
    pool_proxy  = pool[args.workers : args.workers * 2]

    proxies = _proxies_from_file(args.proxies_file) if os.path.exists(args.proxies_file) else []

    results: Dict[str, Tuple[List[Dict[str, Any]], float]] = {}

    if args.mode in ("direct", "both"):
        t0 = time.time()
        rows = await run_batch("direct", args.workers, pool_direct, proxies=None)
        results["DIRECT (local IP)"] = (rows, time.time() - t0)

    if args.mode in ("proxy", "both"):
        if not proxies:
            print("[!] No proxies in", args.proxies_file)
        else:
            t0 = time.time()
            rows = await run_batch("proxy", args.workers, pool_proxy, proxies=proxies[:1])
            results[f"PROXY ({proxies[0]['host']}:{proxies[0]['port']})"] = (rows, time.time() - t0)

    print("\n" + "#" * 78 + "\n#  FINAL REPORT\n" + "#" * 78)
    for label, (rows, wall) in results.items():
        _print_report(label, rows, wall)

    # Machine-readable
    out_path = "/tmp/parallel_test_results.json"
    with open(out_path, "w") as f:
        json.dump({k: {"rows": r, "wall": w} for k, (r, w) in results.items()}, f, indent=2)
    print(f"\n[*] JSON results written to {out_path}")


def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
