#!/usr/bin/env bash
# Bake the cf-autoclick extension into a fresh Chrome profile and open
# Chrome so you can verify (or further customise) it before uploading.
#
# Why this exists:
#   Chrome's "Load Unpacked" stores the EXTENSION SOURCE PATH inside the
#   profile's Preferences file, not the extension files themselves. If we
#   just opened a profile and clicked "Load Unpacked" on
#   /home/sanket777/.../cf-autoclick-master, the absolute path would get
#   baked into the zip and break the second the GitHub runner extracts it
#   to /home/runner/. So we instead COPY the extension into the profile's
#   own Extensions/ directory and load it with --load-extension. That
#   path is relative to the profile root and travels with the zip.
#
# Workflow:
#   1.  bash tools/open_chrome_profile.sh
#       → cleans master-profile/, copies cf-autoclick in, opens Chrome
#   2.  In the Chrome window:
#       - chrome://extensions → confirm "Cfpass CDP Extension" is on
#       - log in to anything else you want baked into the profile
#       - close Chrome cleanly (window X, not Ctrl+C)
#   3.  bash tools/upload_profile_release.sh
#       → zips master-profile/ (skipping runtime garbage) and uploads
#       → release worker-profile-v1 on sanket-sakariya/test-abc
#
# Usage:
#   bash tools/open_chrome_profile.sh             # defaults below
#   PROFILE_DIR=/tmp/foo bash tools/open_chrome_profile.sh
#   EXTENSION_DIR=/path/to/ext bash tools/open_chrome_profile.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AHREF_DIR="$(dirname "${SCRIPT_DIR}")"

# Defaults — override via env vars if needed.
PROFILE_DIR="${PROFILE_DIR:-${AHREF_DIR}/master-profile}"
EXTENSION_DIR="${EXTENSION_DIR:-${AHREF_DIR}/../cf-autoclick-master}"

# Resolve to absolute paths so Chrome doesn't get confused.
PROFILE_DIR="$(realpath -m "${PROFILE_DIR}")"
EXTENSION_DIR="$(realpath -m "${EXTENSION_DIR}")"

echo "[*] Profile dir:   ${PROFILE_DIR}"
echo "[*] Extension dir: ${EXTENSION_DIR}"

# --- 1. Validate the extension source ---------------------------------------

if [[ ! -d "${EXTENSION_DIR}" ]]; then
  echo "❌ Extension dir not found: ${EXTENSION_DIR}" >&2
  echo "   Pass EXTENSION_DIR=/path/to/cf-autoclick-master if it lives elsewhere." >&2
  exit 1
fi

if [[ ! -f "${EXTENSION_DIR}/manifest.json" ]]; then
  echo "❌ Extension has no manifest.json: ${EXTENSION_DIR}" >&2
  exit 1
fi
echo "✅ Extension source verified"

# --- 2. Find Chrome ---------------------------------------------------------

CHROME_BIN=""
for cand in \
  /usr/bin/google-chrome-stable \
  /usr/bin/google-chrome \
  /usr/bin/chromium-browser \
  /usr/bin/chromium ; do
  if [[ -x "${cand}" ]]; then CHROME_BIN="${cand}"; break; fi
done

if [[ -z "${CHROME_BIN}" ]]; then
  echo "❌ No Chrome binary found. Install google-chrome-stable." >&2
  exit 1
fi
echo "✅ Chrome: ${CHROME_BIN}"

# --- 3. Build a fresh profile dir with the extension baked inside ---------

if [[ -d "${PROFILE_DIR}" ]]; then
  echo "[*] Profile dir already exists — removing for a clean rebuild"
  rm -rf "${PROFILE_DIR}"
fi
mkdir -p "${PROFILE_DIR}/Extensions"

EXT_DEST="${PROFILE_DIR}/Extensions/cf_autoclick"
echo "[*] Copying extension into profile -> ${EXT_DEST}"
cp -r "${EXTENSION_DIR}" "${EXT_DEST}"

if [[ ! -f "${EXT_DEST}/manifest.json" ]]; then
  echo "❌ Extension copy failed — no manifest at destination" >&2
  exit 1
fi
echo "✅ Extension copied (manifest present)"

# --- 4. Launch Chrome with that profile + extension -------------------------

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║              CHROME WILL OPEN — verify & customise           ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
echo "Inside Chrome:"
echo "  1. Visit chrome://extensions → 'Cfpass CDP Extension' should be ON"
echo "  2. (Optional) sign into accounts / set settings you want baked in"
echo "  3. Close Chrome via the window X (not Ctrl+C in this terminal)"
echo ""
echo "When Chrome exits, run:"
echo "  bash tools/upload_profile_release.sh"
echo ""

# Run in the foreground so the script blocks until Chrome exits.
# --no-first-run skips the welcome screen.
# --no-default-browser-check skips the default-browser prompt.
"${CHROME_BIN}" \
  --user-data-dir="${PROFILE_DIR}" \
  --load-extension="${EXT_DEST}" \
  --no-first-run \
  --no-default-browser-check \
  --disable-blink-features=AutomationControlled \
  about:blank chrome://extensions

echo ""
echo "✅ Chrome closed."
echo "[*] Profile dir size: $(du -sh "${PROFILE_DIR}" | cut -f1)"
echo ""
echo "Next step:"
echo "  bash tools/upload_profile_release.sh ${PROFILE_DIR}"
