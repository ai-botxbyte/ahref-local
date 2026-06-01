# Ahrefs Domain Authority Checker — Local Browser Integration

## Overview

This feature replaces the previous AI Management Service → browser-agent-management-service pipeline for Ahrefs domain authority checks with a **direct HTTP call from the Kubernetes worker to a local machine** running the browser automation script.

The local machine is exposed to the internet via a Cloudflare tunnel, allowing the k8s worker to send domains directly to your laptop for processing.

---

## Architecture

### Previous Flow (Removed)
```
RabbitMQ (ahref.authority-checker-queue)
    → Worker (k8s pod)
    → AI Management Service (create task + poll for result)
    → browser-agent-management-service (SeleniumBase)
    → Browser
```

### New Flow
```
RabbitMQ (ahref.authority-checker-queue)
    → Worker (k8s pod)
    → HTTP POST /check (via Cloudflare tunnel)
    → Local machine (ahref-lambda/ahrefs_checker.py)
    → Single Chrome browser instance
    → JSON response back to worker
    → workflow.domain-final-queue
```

---

## Components Modified

### 1. `ahref-lambda/ahrefs_checker.py`

**What changed:** Added a FastAPI HTTP server mode alongside the existing CLI mode.

**HTTP Server Mode (default):**
- Runs on port 8000
- Exposes `POST /check` endpoint
- Accepts `{"domains": ["example.com", "github.com"]}`
- Opens ONE browser instance, processes all domains sequentially
- Returns structured JSON with DR, backlinks, linking websites, dofollow percentages
- Thread-lock ensures only one browser check runs at a time (returns 503 if busy)
- `GET /health` endpoint for monitoring

**CLI Mode (--cli flag):**
- Same as before, processes domains from file or single domain argument
- Writes results to JSON file

**Usage:**
```bash
# HTTP server mode (default)
cd ahref-lambda
.venv/bin/python ahrefs_checker.py --port 8000

# With headless browser
.venv/bin/python ahrefs_checker.py --port 8000 --headless

# CLI mode
.venv/bin/python ahrefs_checker.py --cli --domain example.com
.venv/bin/python ahrefs_checker.py --cli --domains domains.txt --out results.json
```

**API Request/Response:**
```json
// POST /check
// Request:
{"domains": ["botxbyte.com", "github.com"]}

// Response:
{
  "total_domains": 2,
  "results": [
    {
      "domain_name": "botxbyte.com",
      "status": "completed",
      "dr": "0.2",
      "backlinks": "123",
      "linking_websites": "45",
      "backlinks_dofollow_percentage": "80",
      "linking_websites_dofollow_percentage": "60",
      "elapsed_seconds": 19.5,
      "finished_at": "2026-06-01T09:58:16.170567+00:00"
    },
    ...
  ]
}
```

---

### 2. `domain-metrics-orchestration-service/app/service/ahref_domain_authority_checker_service.py`

**What changed:** Replaced AI Management Client + task polling with a single HTTP POST to the local ahref-lambda service.

**Before:**
- Created domain-matrix tasks via `AiManagementClient.create_domain_matrix_tasks()`
- Polled for task completion with exponential backoff (up to 5 minutes)
- Parsed `ahrefs_batch_results` from task variables

**After:**
- Single `httpx.post()` to `{AHREF_LOCAL_SERVICE_URL}/check` with `{"domains": [...]}`
- Parses response directly
- 300s timeout for the HTTP call (enough for browser processing)
- Same output format (`ahrefs_domain_rating`, `ahrefs_backlinks`, etc.) for downstream workflow compatibility

**Removed dependencies:** `AiManagementClient`, `poll_task_result`

---

### 3. `domain-metrics-orchestration-service/app/config/config.py`

**Added:**
```python
AHREF_LOCAL_SERVICE_URL: str = Field(
    default="http://localhost:8000",
    env="AHREF_LOCAL_SERVICE_URL",
    description="Base URL for local Ahrefs checker HTTP service"
)
```

---

### 4. `domain-metrics-orchestration-service/.env.dev`

**Added:**
```
AHREF_LOCAL_SERVICE_URL=https://norfolk-finals-fed-give.trycloudflare.com
```

---

## Deployment Steps

### 1. Start the local ahref-lambda server
```bash
cd /home/sanket777/Desktop/Botxbyte/ahref-lambda
.venv/bin/python ahrefs_checker.py --port 8000
```

### 2. Expose via Cloudflare tunnel
```bash
cloudflared tunnel --url http://localhost:8000
# Note the generated URL (e.g., https://xxx-xxx-xxx.trycloudflare.com)
```

### 3. Update .env.dev with tunnel URL
```
AHREF_LOCAL_SERVICE_URL=https://<your-tunnel-url>.trycloudflare.com
```

### 4. Git push and deploy
```bash
cd domain-metrics-orchestration-service
git add .env.dev app/config/config.py app/service/ahref_domain_authority_checker_service.py
git commit -m "feat: ahref worker calls local ahref-lambda HTTP API directly"
git push
```

### 5. Trigger GitHub Actions workflow
```bash
gh workflow run deploy.yaml -f environment=dev -f workers_override=ahref_domain_authority_checker_worker
```

Or manually trigger from GitHub Actions UI:
- Workflow: "Build and Deploy Workers to DO Kubernetes"
- Environment: dev
- workers_override: `ahref_domain_authority_checker_worker`

---

## Important Notes

### Cloudflare Tunnel URL Changes
The free `trycloudflare.com` tunnel generates a **random URL each time** you restart it. When you restart the tunnel:
1. Get the new URL from cloudflared output
2. Update `.env.dev` with the new URL
3. Push and redeploy the worker

For a persistent URL, set up a named Cloudflare tunnel with your domain.

### Single Browser Instance
- Only ONE browser check runs at a time (thread lock)
- If the server receives a request while busy, it returns HTTP 503
- The k8s worker has 4 replicas, but all will hit the same local server — they'll queue up naturally via the 503 + DLX retry mechanism

### Browser Visibility
- By default, the browser runs in headed mode (visible on your desktop)
- Use `--headless` flag for headless operation
- The browser opens fresh for each `/check` request and closes after all domains are processed

### Monitoring
- Local server logs: `/tmp/ahref_server.log` (if started with nohup)
- Worker pod logs: `kubectl logs -l app=dm-orch-ahref-domain-authority-checker-worker -n domain-metrics`
- Health check: `curl https://<tunnel-url>/health`

---

## Testing

### Quick local test
```bash
curl -X POST http://localhost:8000/check \
  -H "Content-Type: application/json" \
  -d '{"domains": ["example.com", "github.com"]}'
```

### Full integration test
1. Start server + tunnel
2. Create campaign in domain-metrics-management with a domain and ahref workflow
3. Watch local server logs for incoming POST /check
4. Watch worker pod logs for "step-1 = success"

---

## Files Summary

| File | Change |
|------|--------|
| `ahref-lambda/ahrefs_checker.py` | Added FastAPI HTTP server with POST /check endpoint |
| `domain-metrics-orchestration-service/app/service/ahref_domain_authority_checker_service.py` | Replaced AI Management polling with direct HTTP call |
| `domain-metrics-orchestration-service/app/config/config.py` | Added `AHREF_LOCAL_SERVICE_URL` config field |
| `domain-metrics-orchestration-service/.env.dev` | Added tunnel URL |
