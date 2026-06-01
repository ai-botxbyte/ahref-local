"""
Saturation test: keep scraping new domains through a 5-worker pool until
the IP gets silently blocked by Ahrefs. Report the count.

A "block" is declared when CONSECUTIVE_FAIL_THRESHOLD scrapes in a row
fail (timeout / no result / no Turnstile rendered).

Usage:
  python saturation_test.py --mode direct
  python saturation_test.py --mode proxy
  python saturation_test.py --mode both
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import shutil
import time
from typing import Any, Dict, List, Optional

MAX_CONCURRENCY = 5
CONSECUTIVE_FAIL_THRESHOLD = 5      # 5 fails in a row → "blocked"
PER_DOMAIN_TIMEOUT = 120            # individual scrape timeout
HARD_CAP = 500                      # absolute upper bound, defensive

# Big pool of distinct domains (200+). Drawn from common-crawl-style mix
# so DR varies and we never reuse a domain in the same session.
DOMAIN_POOL = [
    "wikipedia.org","github.com","stackoverflow.com","python.org","nodejs.org","rust-lang.org",
    "go.dev","kotlinlang.org","djangoproject.com","flask.palletsprojects.com","fastapi.tiangolo.com",
    "vuejs.org","react.dev","svelte.dev","angular.io","nextjs.org","nuxt.com","remix.run",
    "tailwindcss.com","getbootstrap.com","mui.com","chakra-ui.com","primevue.org","redis.io",
    "postgresql.org","mysql.com","mongodb.com","elastic.co","kafka.apache.org","rabbitmq.com",
    "nginx.org","apache.org","cloudflare.com","vercel.com","netlify.com","digitalocean.com",
    "linode.com","fly.io","supabase.com","firebase.google.com","auth0.com","stripe.com",
    "twilio.com","sendgrid.com","mailchimp.com","hubspot.com","salesforce.com","zoom.us",
    "slack.com","discord.com","telegram.org","whatsapp.com","signal.org","protonmail.com",
    "fastmail.com","zoho.com","dropbox.com","box.com","figma.com","canva.com","notion.so",
    "airtable.com","asana.com","trello.com","monday.com","jira.atlassian.com","linear.app",
    "clickup.com","basecamp.com","todoist.com","evernote.com","onenote.com","obsidian.md",
    "logseq.com","roamresearch.com","craft.do","bear.app","ulysses.app","scrivener.com",
    "amazon.com","ebay.com","etsy.com","shopify.com","woocommerce.com","magento.com",
    "bigcommerce.com","squarespace.com","wix.com","wordpress.com","ghost.org","medium.com",
    "substack.com","beehiiv.com","convertkit.com","mailerlite.com","activecampaign.com",
    "hubspot.com","intercom.com","drift.com","zendesk.com","freshdesk.com","helpscout.com",
    "linkedin.com","twitter.com","facebook.com","instagram.com","tiktok.com","youtube.com",
    "vimeo.com","twitch.tv","reddit.com","pinterest.com","tumblr.com","quora.com",
    "yelp.com","tripadvisor.com","booking.com","airbnb.com","expedia.com","kayak.com",
    "uber.com","lyft.com","doordash.com","grubhub.com","instacart.com","ubereats.com",
    "netflix.com","hulu.com","disney.com","spotify.com","apple.com","microsoft.com",
    "google.com","yahoo.com","bing.com","duckduckgo.com","baidu.com","yandex.com",
    "cnn.com","bbc.com","nytimes.com","theguardian.com","reuters.com","bloomberg.com",
    "wsj.com","forbes.com","economist.com","wired.com","techcrunch.com","theverge.com",
    "arstechnica.com","engadget.com","gizmodo.com","mashable.com","venturebeat.com",
    "hackernews.com","producthunt.com","indiehackers.com","makerlog.com","wip.co",
    "imdb.com","rottentomatoes.com","metacritic.com","goodreads.com","letterboxd.com",
    "discogs.com","last.fm","bandcamp.com","soundcloud.com","mixcloud.com",
    "khanacademy.org","coursera.org","udemy.com","udacity.com","edx.org","pluralsight.com",
    "linkedin-learning.com","skillshare.com","masterclass.com","brilliant.org","duolingo.com",
    "memrise.com","babbel.com","rosettastone.com","busuu.com","italki.com",
    "leetcode.com","hackerrank.com","codewars.com","exercism.io","kaggle.com",
    "topcoder.com","codeforces.com","atcoder.jp","projecteuler.net","rosalind.info",
    "freecodecamp.org","theodinproject.com","fullstackopen.com","cs50.harvard.edu",
    "mit.edu","stanford.edu","harvard.edu","berkeley.edu","cmu.edu","caltech.edu",
    "ox.ac.uk","cam.ac.uk","ethz.ch","imperial.ac.uk","ucl.ac.uk","nyu.edu",
    "ycombinator.com","techstars.com","500.co","sequoiacap.com","a16z.com","accel.com",
    "kpcb.com","greylock.com","benchmark.com","firstround.com","unionsquareventures.com",
    "indexventures.com","atomico.com","balderton.com","creandum.com","earlybird.com",
    "rocketinternet.com","alibaba.com","tencent.com","baidu.com","sina.com.cn",
    "naver.com","kakao.com","line.me","rakuten.com","yahoo.co.jp","mercari.com",
    "softbank.jp","sony.com","panasonic.com","samsung.com","lg.com","huawei.com",
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
    worker_slot: int,
    proxy: Optional[Dict[str, str]],
) -> Dict[str, Any]:
    """Run one isolated scrape and return a result row."""
    # PROXY env must be in place before lambda_handler builds the browser
    if proxy:
        os.environ["PROXY_HOST"] = proxy["host"]
        os.environ["PROXY_PORT"] = proxy["port"]
        os.environ["PROXY_USER"] = proxy["user"]
        os.environ["PROXY_PASS"] = proxy["pass"]
    else:
        for k in ("PROXY_HOST", "PROXY_PORT", "PROXY_USER", "PROXY_PASS"):
            os.environ.pop(k, None)

    from lambda_handler import _scrape_ahrefs_async

    profile_dir = f"/tmp/ts_profile_slot{worker_slot}_{int(time.time()*1000)%100000}"
    shutil.rmtree(profile_dir, ignore_errors=True)

    t0 = time.time()
    try:
        result = await asyncio.wait_for(
            _scrape_ahrefs_async([domain], headless=True, profile_dir=profile_dir),
            timeout=PER_DOMAIN_TIMEOUT,
        )
    except asyncio.TimeoutError:
        result = {"status": "error", "error": f"hard timeout {PER_DOMAIN_TIMEOUT}s"}
    except Exception as e:
        result = {"status": "error", "error": f"exception: {type(e).__name__}: {e}"}
    elapsed = round(time.time() - t0, 1)

    row: Dict[str, Any] = {
        "slot": worker_slot,
        "domain": domain,
        "elapsed": elapsed,
        "status": result.get("status"),
    }
    if result.get("status") == "completed":
        r0 = (result.get("results") or [{}])[0]
        if r0.get("dr") is not None:
            row["dr"] = r0.get("dr")
            row["backlinks"] = r0.get("backlinks")
            row["linking_websites"] = r0.get("linking_websites")
            row["turnstile_retries"] = r0.get("turnstile_retries")
        else:
            row["status"] = "error"
            row["error"] = r0.get("error", "no_data")
    else:
        row["error"] = (result.get("error") or "unknown")[:60]

    # cleanup profile to keep /tmp manageable
    shutil.rmtree(profile_dir, ignore_errors=True)
    return row


async def run_until_blocked(
    label: str,
    proxy: Optional[Dict[str, str]],
    domains: List[str],
) -> Dict[str, Any]:
    """Run the 5-slot pool against `domains` until CONSECUTIVE_FAIL_THRESHOLD
    consecutive failures (== "IP blocked")."""
    print(f"\n{'#'*78}\n#  {label}\n{'#'*78}")
    print(f"#  proxy={proxy['host']+':'+proxy['port'] if proxy else 'NONE (local IP)'}")
    print(f"#  concurrency={MAX_CONCURRENCY}  consec-fail-threshold={CONSECUTIVE_FAIL_THRESHOLD}")
    print(f"#  hard-cap={HARD_CAP} domains  per-domain-timeout={PER_DOMAIN_TIMEOUT}s\n")

    sem = asyncio.Semaphore(MAX_CONCURRENCY)
    results: List[Dict[str, Any]] = []
    consec_fail = 0
    blocked = False
    domain_iter = iter(domains)
    inflight: Dict[asyncio.Task, str] = {}
    next_slot = 0
    submitted = 0
    t0 = time.time()

    async def runner(d: str, slot: int) -> Dict[str, Any]:
        async with sem:
            return await _scrape_one(d, slot, proxy)

    # Submit initial batch
    for _ in range(MAX_CONCURRENCY):
        try:
            d = next(domain_iter)
        except StopIteration:
            break
        t = asyncio.create_task(runner(d, next_slot % MAX_CONCURRENCY))
        inflight[t] = d
        next_slot += 1
        submitted += 1

    while inflight and not blocked and submitted < HARD_CAP:
        done, _ = await asyncio.wait(inflight.keys(), return_when=asyncio.FIRST_COMPLETED)
        for t in done:
            row = t.result()
            del inflight[t]
            results.append(row)
            mark = "✅" if row["status"] == "completed" else "❌"
            extra = (f"DR={row.get('dr')} BL={row.get('backlinks')}"
                     if row["status"] == "completed"
                     else f"err={row.get('error','')[:30]}")
            wall = round(time.time() - t0, 1)
            print(f"  [t+{wall:>5.0f}s] {mark} #{len(results):>3} slot={row['slot']} "
                  f"{row['domain']:<28} {row['elapsed']:>5.1f}s  {extra}")

            if row["status"] == "completed":
                consec_fail = 0
            else:
                consec_fail += 1
                if consec_fail >= CONSECUTIVE_FAIL_THRESHOLD:
                    blocked = True
                    print(f"\n  🛑 {CONSECUTIVE_FAIL_THRESHOLD} consecutive failures → IP considered BLOCKED")
                    break

            # Submit next domain
            if not blocked and submitted < HARD_CAP:
                try:
                    nd = next(domain_iter)
                except StopIteration:
                    continue
                nt = asyncio.create_task(runner(nd, next_slot % MAX_CONCURRENCY))
                inflight[nt] = nd
                next_slot += 1
                submitted += 1

    # Drain remaining inflight after block (cancel)
    for t in inflight:
        t.cancel()
    if inflight:
        await asyncio.gather(*inflight.keys(), return_exceptions=True)

    wall = round(time.time() - t0, 1)
    ok = sum(1 for r in results if r["status"] == "completed")
    fail = len(results) - ok
    last_ok_idx = max((i for i, r in enumerate(results) if r["status"] == "completed"), default=-1)

    summary = {
        "label": label,
        "total_attempted": len(results),
        "successful": ok,
        "failed": fail,
        "wall_clock_sec": wall,
        "avg_per_success": round(wall / max(1, ok), 1),
        "blocked": blocked,
        "successful_before_first_block": last_ok_idx + 1 if blocked else ok,
        "rows": results,
    }

    print(f"\n  {'─'*70}")
    print(f"  TOTAL: attempted={len(results)} success={ok} fail={fail} wall={wall}s")
    print(f"  Successful scrapes before block: {summary['successful_before_first_block']}")
    print(f"  Throughput: {round(ok / (wall/60), 1)} successful domains / minute")
    print(f"  {'─'*70}\n")
    return summary


async def main_async():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["direct", "proxy", "both"], default="both")
    parser.add_argument("--proxies-file", default="/tmp/proxies.txt")
    args = parser.parse_args()

    os.environ.setdefault("USE_XVFB", "0")
    os.environ.setdefault("SCRIPT_TIMEOUT", "120")
    os.environ.pop("TS_PROFILE_DIR", None)

    # Same-seed shuffle so direct & proxy don't share domains
    random.seed(7)
    pool = DOMAIN_POOL.copy()
    random.shuffle(pool)
    half = len(pool) // 2
    direct_pool = pool[:half]
    proxy_pool  = pool[half:]

    proxies = _proxies_from_file(args.proxies_file) if os.path.exists(args.proxies_file) else []

    summaries: List[Dict[str, Any]] = []

    if args.mode in ("direct", "both"):
        s = await run_until_blocked("DIRECT (local IP)", None, direct_pool)
        summaries.append(s)

    if args.mode in ("proxy", "both"):
        if not proxies:
            print("[!] No proxies in", args.proxies_file)
        else:
            p = proxies[0]
            s = await run_until_blocked(f"PROXY ({p['host']}:{p['port']})", p, proxy_pool)
            summaries.append(s)

    print("\n" + "=" * 78 + "\n  FINAL SCOREBOARD\n" + "=" * 78)
    print(f"  {'mode':<32} {'attempted':>10} {'success':>8} {'fail':>5} "
          f"{'before_block':>13} {'wall_s':>7} {'/min':>6}")
    print(f"  {'-'*32} {'-'*10} {'-'*8} {'-'*5} {'-'*13} {'-'*7} {'-'*6}")
    for s in summaries:
        rpm = round(s["successful"] / (s["wall_clock_sec"]/60), 1) if s["wall_clock_sec"] else 0
        print(f"  {s['label']:<32} {s['total_attempted']:>10} {s['successful']:>8} "
              f"{s['failed']:>5} {s['successful_before_first_block']:>13} "
              f"{s['wall_clock_sec']:>7} {rpm:>6}")

    with open("/tmp/saturation_results.json", "w") as f:
        json.dump(summaries, f, indent=2, default=str)
    print(f"\n[*] JSON written to /tmp/saturation_results.json")


if __name__ == "__main__":
    asyncio.run(main_async())
