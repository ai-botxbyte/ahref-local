# Ahrefs Domain Authority Checker — Pull-Based Local Browser Processing

## Overview

Local script that pulls domains from the server queue, processes them in parallel browser instances, and posts results back. No tunnel or port-forwarding needed — the script makes outbound HTTP calls only.

---

## Architecture (Pull Approach)

```
┌─────────────────────────────────────────────────────────┐
│  Server (Kubernetes)                                     │
│                                                          │
│  Campaign created → ahref.authority-checker-queue        │
│       ↑                              ↓                   │
│  workflow.domain-final-queue    GET /ahref-authority/     │
│       ↑                              ↓                   │
│  POST /ahref-authority/  ←──── returns execution record  │
└────────────────────────────────────────────────────────── ┘
         ↑                              ↓
         │         YOUR LAPTOP          │
         │                              │
         │    ┌──────────────────┐      │
         └────│  Worker 0 (proxy)│←─────┘
              │  Worker 1 (proxy)│
              │  Worker 2 (proxy)│
              │  Worker 3 (proxy)│
              │  Worker 4 (proxy)│
              └──────────────────┘
              5 parallel Chrome instances
```

---

## Commands

### Run with 5 instances + proxies (default)
```bash
cd /home/sanket777/Desktop/Botxbyte/ahref-lambda
.venv/bin/python ahrefs_checker.py
```

### Run without proxies (local IP)
```bash
.venv/bin/python ahrefs_checker.py --no-proxy
```

### Run with custom number of instances
```bash
# 3 instances with proxies
.venv/bin/python ahrefs_checker.py --workers 3

# 1 instance, no proxy
.venv/bin/python ahrefs_checker.py --workers 1 --no-proxy

# 2 instances, no proxy, headless
.venv/bin/python ahrefs_checker.py --workers 2 --no-proxy --headless
```

### Run headless (no visible browser)
```bash
.venv/bin/python ahrefs_checker.py --headless
```

### All options
```bash
.venv/bin/python ahrefs_checker.py \
  --workers 5 \
  --no-proxy \
  --headless \
  --api-url http://164.90.252.85/domain-metrics-management-service/api/v1 \
  --chrome /usr/bin/google-chrome-stable \
  --proxies proxies.txt
```

---

## Options Reference

| Flag | Default | Description |
|------|---------|-------------|
| `--workers N` | 5 | Number of parallel browser instances |
| `--no-proxy` | off | Disable proxies, all instances use local IP |
| `--headless` | off | Run browsers in headless mode |
| `--api-url URL` | (production) | Management service API URL |
| `--chrome PATH` | auto-detect | Path to Chrome binary |
| `--proxies FILE` | proxies.txt | Path to proxy list file |

---

## Proxy Format

File: `proxies.txt` (one per line)
```
ip:port:username:password
```

Example:
```
38.154.203.95:5863:kpmtwkyv:t6ggqskw3rka
198.105.121.200:6462:kpmtwkyv:t6ggqskw3rka
64.137.96.74:6641:kpmtwkyv:t6ggqskw3rka
```

If `--workers` exceeds the number of proxies, proxies are reused round-robin.

---

## How It Works

1. Script launches N browser instances (each with its own proxy if enabled)
2. Each worker independently polls `GET /ahref-authority/` for a domain
3. When a domain is available, it opens a new tab, navigates to Ahrefs, extracts metrics
4. Closes the work tab (browser stays alive on blank tab)
5. Posts results to `POST /ahref-authority/` which routes to the next workflow step
6. Loops back to step 2

---

## Services Involved

| Service | Role |
|---------|------|
| **domain-metrics-management-service** | `GET /ahref-authority/` pops from queue, `POST /ahref-authority/` routes results to next step |
| **domain-metrics-orchestration-service** | Worker scaled to 0 (not needed) |
| **ahref-lambda (this script)** | Pulls domains, processes in browser, posts results |

---

## Stopping

Press `Ctrl+C` to gracefully shut down all browser instances.

---

## Troubleshooting

| Issue | Fix |
|-------|-----|
| Browser crashes with proxy | Check proxy credentials, ensure proxy supports HTTP |
| Queue always empty | Create a campaign with ahref workflow in the frontend |
| POST fails | Check if management service is deployed and accessible |
| All workers idle | Normal when queue is empty — they poll every 3-5s |
