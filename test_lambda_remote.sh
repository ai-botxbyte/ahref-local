#!/usr/bin/env bash
# Test the deployed Lambda ahref-worker-1 with proxy env vars from /tmp/proxies.txt
# Try up to 3 proxies for domain botxbyte.com.

set -euo pipefail

LAMBDA_URL="https://licys2nnyntn2uyn3uszcvqnkq0pglvk.lambda-url.us-east-1.on.aws/"
FUNCTION="ahref-worker-1"
REGION="us-east-1"
DOMAIN="botxbyte.com"
MAX_ATTEMPTS=3

mapfile -t PROXIES < <(head -n "$MAX_ATTEMPTS" /tmp/proxies.txt)

attempt=0
success=false
for line in "${PROXIES[@]}"; do
    attempt=$((attempt + 1))
    IFS=':' read -r PHOST PPORT PUSER PPASS <<<"$line"
    echo
    echo "===================================================================="
    echo "ATTEMPT $attempt/$MAX_ATTEMPTS  proxy=$PHOST:$PPORT"
    echo "===================================================================="

    echo "[*] Updating Lambda env vars..."
    aws lambda update-function-configuration \
        --function-name "$FUNCTION" \
        --region "$REGION" \
        --environment "Variables={CHROME_BINARY=/opt/google/chrome/chrome,USE_XVFB=1,HOME=/tmp,XDG_CACHE_HOME=/tmp/.cache,XDG_CONFIG_HOME=/tmp/.config,CHROME_MAJOR_VERSION=148,PROXY_HOST=$PHOST,PROXY_PORT=$PPORT,PROXY_USER=$PUSER,PROXY_PASS=$PPASS}" \
        --output json >/dev/null

    echo "[*] Waiting for config update to finish..."
    aws lambda wait function-updated --function-name "$FUNCTION" --region "$REGION"

    echo "[*] Invoking Lambda URL..."
    HTTP_RESP="/tmp/lambda_attempt_${attempt}.json"
    HTTP_STATUS=$(curl -sS -w "%{http_code}" -o "$HTTP_RESP" \
        -X POST "$LAMBDA_URL" \
        -H "Content-Type: application/json" \
        --max-time 540 \
        -d "{\"domain\":\"$DOMAIN\",\"headless\":true}" || echo "000")

    echo "[*] HTTP $HTTP_STATUS"
    echo "[*] Response:"
    cat "$HTTP_RESP" | head -c 2000
    echo

    if [ "$HTTP_STATUS" = "200" ]; then
        # check status field
        if grep -q '"status": "completed"' "$HTTP_RESP" || grep -q '"status":"completed"' "$HTTP_RESP"; then
            echo
            echo "✅ SUCCESS on proxy $PHOST:$PPORT"
            success=true
            break
        fi
    fi
    echo "[!] Attempt $attempt failed — trying next proxy if any"
done

echo
echo "===================================================================="
if [ "$success" = "true" ]; then
    echo "FINAL: ✅ SUCCESS after $attempt attempt(s)"
else
    echo "FINAL: ❌ FAILED after $attempt attempt(s)"
fi
echo "===================================================================="
