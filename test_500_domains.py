"""
Run 500 domains through the local ahref-lambda scraper with 6 parallel
browser workers. Each completed result is appended to a JSON file
immediately so you can `tail` it live. At the end, prints the total
wall-clock time + per-domain average.

Usage:
    python test_500_domains.py
    python test_500_domains.py --workers 6 --domains domains_500.txt --out results_500.json
    python test_500_domains.py --timeout 180

The output JSON is a list of objects (streamed/appended atomically) e.g.:
    [
      {"domain": "github.com", "dr": 96, "backlinks": ..., "elapsed": 42.1, "status": "completed", "worker": 2, "finished_at": "..."},
      ...
    ]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Append-safe JSON writer (the file is always a valid JSON array on disk)
# ---------------------------------------------------------------------------
class StreamingJsonArray:
    """Maintain a JSON array on disk. Each append() rewrites the closing
    bracket so the file is *always* valid JSON, even if the script is
    killed mid-run. Thread/async-safe via an asyncio.Lock."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = asyncio.Lock()
        self._count = 0
        # Reset / initialise file
        self.path.write_text("[]")

    async def append(self, obj: Dict[str, Any]) -> None:
        async with self._lock:
            # Read existing, append, write atomically
            data = json.loads(self.path.read_text() or "[]")
            data.append(obj)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(data, indent=2, default=str))
            tmp.replace(self.path)
            self._count = len(data)

    @property
    def count(self) -> int:
        return self._count


# ---------------------------------------------------------------------------
# One scrape (one browser, one domain)
# ---------------------------------------------------------------------------
async def scrape_one(
    domain: str,
    worker_id: int,
    timeout_s: int,
) -> Dict[str, Any]:
    """Scrape a single domain in its own isolated profile dir."""
    from lambda_handler import _scrape_ahrefs_async  # lazy import

    profile_dir = f"/tmp/ts_profile_w{worker_id}"

    t0 = time.time()
    try:
        result = await asyncio.wait_for(
            _scrape_ahrefs_async([domain], headless=True, profile_dir=profile_dir),
            timeout=timeout_s,
        )
    except asyncio.TimeoutError:
        result = {"status": "error", "error": f"hard timeout {timeout_s}s"}
    except Exception as e:
        result = {"status": "error", "error": f"{type(e).__name__}: {e}"}

    elapsed = round(time.time() - t0, 2)

    row: Dict[str, Any] = {
        "worker": worker_id,
        "domain": domain,
        "elapsed_seconds": elapsed,
        "status": result.get("status"),
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    if result.get("status") == "completed":
        r0 = (result.get("results") or [{}])[0]
        row.update({
            "dr": r0.get("dr"),
            "backlinks": r0.get("backlinks"),
            "linking_websites": r0.get("linking_websites"),
            "dofollow_pct": r0.get("dofollow_pct") or r0.get("dofollow_percent"),
            "turnstile_retries": r0.get("turnstile_retries"),
        })
        if r0.get("error"):
            row["status"] = "error"
            row["error"] = r0["error"]
    else:
        row["error"] = result.get("error", "unknown")
    return row


# ---------------------------------------------------------------------------
# Worker loop — pulls domains off a shared queue
# ---------------------------------------------------------------------------
async def worker(
    worker_id: int,
    queue: "asyncio.Queue[Optional[str]]",
    sink: StreamingJsonArray,
    total: int,
    timeout_s: int,
    started_at: float,
) -> None:
    while True:
        domain = await queue.get()
        try:
            if domain is None:
                return  # sentinel
            print(f"[w{worker_id}] ▶ start  {domain}")
            row = await scrape_one(domain, worker_id, timeout_s)
            await sink.append(row)
            done = sink.count
            mark = "✅" if row.get("status") == "completed" else "❌"
            wall = time.time() - started_at
            eta = (wall / done) * (total - done) if done else 0
            print(
                f"[w{worker_id}] {mark} {row['domain']:32} "
                f"{row.get('elapsed_seconds',0):6.1f}s  "
                f"DR={row.get('dr','-')!s:>3}  BL={row.get('backlinks','-')!s:>10}  "
                f"({done}/{total} done, wall {wall/60:.1f}m, ETA {eta/60:.1f}m)"
            )
        except Exception as e:
            print(f"[w{worker_id}] !! crashed on {domain}: {e}", file=sys.stderr)
        finally:
            queue.task_done()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def main_async(args: argparse.Namespace) -> None:
    here = Path(__file__).parent
    domains_path = (here / args.domains).resolve() if not os.path.isabs(args.domains) else Path(args.domains)
    out_path = (here / args.out).resolve() if not os.path.isabs(args.out) else Path(args.out)

    if not domains_path.exists():
        sys.exit(f"[!] domains file not found: {domains_path}")

    domains = [d.strip() for d in domains_path.read_text().splitlines() if d.strip()]
    if args.limit:
        domains = domains[: args.limit]
    total = len(domains)
    print(f"[*] loaded {total} domains from {domains_path}")
    print(f"[*] writing results to     {out_path}")
    print(f"[*] workers={args.workers}  timeout/domain={args.timeout}s")
    print()

    # Make sure no headless-skip envs interfere; we want headful + Xvfb on Linux
    os.environ.setdefault("USE_XVFB", "1")
    os.environ.setdefault("SCRIPT_TIMEOUT", str(args.timeout))
    os.environ.pop("TS_PROFILE_DIR", None)  # workers set their own

    # Fresh per-worker profile dirs so they don't clash on SingletonLock
    for i in range(args.workers):
        shutil.rmtree(f"/tmp/ts_profile_w{i}", ignore_errors=True)

    sink = StreamingJsonArray(out_path)

    queue: asyncio.Queue[Optional[str]] = asyncio.Queue()
    for d in domains:
        queue.put_nowait(d)
    for _ in range(args.workers):
        queue.put_nowait(None)  # poison pills

    started_at = time.time()
    workers = [
        asyncio.create_task(worker(i, queue, sink, total, args.timeout, started_at))
        for i in range(args.workers)
    ]
    await asyncio.gather(*workers)
    wall = time.time() - started_at

    # Final summary
    rows = json.loads(out_path.read_text())
    ok = sum(1 for r in rows if r.get("status") == "completed")
    failed = len(rows) - ok

    print()
    print("#" * 78)
    print(f"#  DONE   total={total}   ok={ok}   failed={failed}")
    print(f"#  wall-clock   = {wall:.1f}s  ({wall/60:.2f} min)")
    print(f"#  per-domain   = {wall/max(1,total):.2f}s (avg across all)")
    print(f"#  throughput   = {total / (wall/60):.1f} domains/min")
    print(f"#  results JSON = {out_path}")
    print("#" * 78)

    # Drop a small summary alongside the JSON
    summary_path = out_path.with_suffix(".summary.json")
    summary_path.write_text(json.dumps({
        "total": total,
        "ok": ok,
        "failed": failed,
        "workers": args.workers,
        "wall_clock_seconds": round(wall, 2),
        "wall_clock_minutes": round(wall / 60, 2),
        "avg_seconds_per_domain": round(wall / max(1, total), 2),
        "throughput_domains_per_min": round(total / (wall / 60), 2),
        "started_at": datetime.fromtimestamp(started_at, tz=timezone.utc).isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "results_file": str(out_path),
    }, indent=2))
    print(f"[*] summary written to {summary_path}")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--domains", default="domains_500.txt", help="path to newline-separated domains")
    p.add_argument("--out", default="results_500.json", help="JSON file results are appended to")
    p.add_argument("--workers", type=int, default=6, help="parallel browsers (default 6)")
    p.add_argument("--timeout", type=int, default=120, help="per-domain hard timeout in seconds")
    p.add_argument("--limit", type=int, default=0, help="optional: only run first N domains (0=all)")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("\n[!] interrupted — partial results preserved in JSON file")


if __name__ == "__main__":
    main()
