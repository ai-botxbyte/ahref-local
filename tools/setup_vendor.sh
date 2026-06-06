#!/usr/bin/env bash
# One-time setup: download + extract ungoogled-chromium and clone the
# cf-autoclick extension into ahref-local/vendor/. Idempotent — re-running
# is safe and fast (skips work that's already done).
#
# Why this exists:
#   ahrefs_checker.py needs (a) a Chromium binary and (b) an unpacked
#   cf-autoclick extension folder. Rather than depend on whatever Chrome
#   the host happens to have, we vendor a known-good ungoogled-chromium
#   build alongside the extension. ahrefs_checker.py auto-discovers both
#   from vendor/ at runtime.
#
# Usage:
#   bash tools/setup_vendor.sh
#
# After this, you can run:
#   .venv/bin/python ahrefs_checker.py --workers 1 --no-proxy --headless
#
# To upgrade the vendored chromium later:
#   1. Update CHROMIUM_VERSION below
#   2. Delete vendor/ungoogled-chromium/ and vendor/ungoogled-chromium.tar.xz
#   3. Re-run this script

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AHREF_DIR="$(dirname "${SCRIPT_DIR}")"
VENDOR_DIR="${AHREF_DIR}/vendor"

# ─── Pinned versions ────────────────────────────────────────────────────────
# Bump this and re-run to upgrade. The portable Linux build is preferred
# over the AppImage because it doesn't need FUSE and unpacks to plain files.
CHROMIUM_VERSION="149.0.7827.53-1"
CHROMIUM_TARBALL_URL="https://github.com/ungoogled-software/ungoogled-chromium-portablelinux/releases/download/${CHROMIUM_VERSION}/ungoogled-chromium-${CHROMIUM_VERSION}-x86_64_linux.tar.xz"
CHROMIUM_TARBALL="${VENDOR_DIR}/ungoogled-chromium.tar.xz"
CHROMIUM_DIR="${VENDOR_DIR}/ungoogled-chromium"
CHROMIUM_INNER="${VENDOR_DIR}/ungoogled-chromium-${CHROMIUM_VERSION}-x86_64_linux"

EXTENSION_URL="https://github.com/tenacious6/cf-autoclick.git"
EXTENSION_DIR="${VENDOR_DIR}/cf-autoclick"

mkdir -p "${VENDOR_DIR}"

# ─── 1. ungoogled-chromium binary ───────────────────────────────────────────
if [[ -x "${CHROMIUM_DIR}/chrome" ]]; then
  echo "[*] ungoogled-chromium already present at ${CHROMIUM_DIR}/chrome — skipping"
else
  if [[ ! -f "${CHROMIUM_TARBALL}" ]]; then
    echo "[*] Downloading ungoogled-chromium ${CHROMIUM_VERSION}..."
    curl -fL --progress-bar -o "${CHROMIUM_TARBALL}" "${CHROMIUM_TARBALL_URL}"
  else
    echo "[*] Tarball already cached at ${CHROMIUM_TARBALL}"
  fi

  echo "[*] Extracting tarball to ${VENDOR_DIR}..."
  tar -xJf "${CHROMIUM_TARBALL}" -C "${VENDOR_DIR}"

  if [[ ! -d "${CHROMIUM_INNER}" ]]; then
    echo "❌ Expected extracted dir ${CHROMIUM_INNER} not found." >&2
    echo "   The tarball layout may have changed. Check the contents:" >&2
    echo "     tar -tJf ${CHROMIUM_TARBALL} | head" >&2
    exit 1
  fi

  mv "${CHROMIUM_INNER}" "${CHROMIUM_DIR}"
  chmod +x "${CHROMIUM_DIR}/chrome" "${CHROMIUM_DIR}/chromedriver" 2>/dev/null || true

  echo "[*] Verifying binary..."
  if ! "${CHROMIUM_DIR}/chrome" --version; then
    echo "❌ vendor/ungoogled-chromium/chrome failed to run --version." >&2
    echo "   You may be missing system libraries. On Ubuntu try:" >&2
    echo "     sudo apt install -y libnss3 libatk-bridge2.0-0 libxkbcommon0 \\" >&2
    echo "                         libxcomposite1 libxdamage1 libxrandr2 libgbm1 \\" >&2
    echo "                         libpango-1.0-0 libcairo2 libasound2t64" >&2
    exit 1
  fi
  echo "✅ Chromium ready at ${CHROMIUM_DIR}/chrome"
fi

# ─── 2. cf-autoclick extension ──────────────────────────────────────────────
if [[ -f "${EXTENSION_DIR}/manifest.json" ]]; then
  echo "[*] cf-autoclick already present at ${EXTENSION_DIR} — skipping"
else
  echo "[*] Cloning cf-autoclick from ${EXTENSION_URL}..."
  git clone --depth 1 "${EXTENSION_URL}" "${EXTENSION_DIR}"

  if [[ ! -f "${EXTENSION_DIR}/manifest.json" ]]; then
    echo "❌ Cloned extension missing manifest.json: ${EXTENSION_DIR}" >&2
    exit 1
  fi
  echo "✅ Extension ready at ${EXTENSION_DIR}"
fi

# ─── Done ───────────────────────────────────────────────────────────────────
cat <<EOF

╔══════════════════════════════════════════════════════════════╗
║                  ✅  VENDOR SETUP COMPLETE                   ║
╚══════════════════════════════════════════════════════════════╝

  Chromium:  ${CHROMIUM_DIR}/chrome
  Extension: ${EXTENSION_DIR}

ahrefs_checker.py will auto-discover both at runtime. Try:

  cd ${AHREF_DIR}
  python -m venv .venv && .venv/bin/pip install -r requirements.txt
  .venv/bin/python ahrefs_checker.py --workers 1 --no-proxy --headless

EOF
