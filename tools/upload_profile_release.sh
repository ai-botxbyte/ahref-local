#!/usr/bin/env bash
# Upload the local worker chrome profile (with cf-autoclick extension) as a
# GitHub release asset on sanket-sakariya/test-abc so ahrefs_checker.py can
# bootstrap from it on every run.
#
# Usage:
#   bash tools/upload_profile_release.sh [SOURCE_PROFILE_DIR]
#
# Defaults to ahref-local/master-profile/ — the directory that
# tools/open_chrome_profile.sh creates and that you've optionally tweaked
# by opening Chrome on it. If you don't pass an arg and that directory is
# missing, the script bails with instructions.
#
# The script will:
#   1. Verify the source has Extensions/cf_autoclick/manifest.json
#   2. Stage a clean copy of the profile (strips Chrome runtime garbage —
#      dangling Singleton* symlinks, caches, crash reports, lock files —
#      that aren't portable across machines)
#   3. Zip the staged copy to /tmp/ahrefs-worker-profile.zip
#   4. Delete the existing 'worker-profile-v1' release (if any) on
#      sanket-sakariya/test-abc and re-create it with the new zip attached
#
# Requires `gh` CLI authenticated against sanket-sakariya (or any user with
# write access). Run `gh auth login` first if you haven't.
#
# Resulting asset URL (consumed by ahrefs_checker.py):
#   https://github.com/sanket-sakariya/test-abc/releases/download/worker-profile-v1/ahrefs-worker-profile.zip

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AHREF_DIR="$(dirname "${SCRIPT_DIR}")"
DEFAULT_SOURCE="${AHREF_DIR}/master-profile"

SOURCE_DIR="${1:-${DEFAULT_SOURCE}}"
REPO="sanket-sakariya/test-abc"
TAG="worker-profile-v1"
ASSET="ahrefs-worker-profile.zip"
ZIP_PATH="/tmp/${ASSET}"
STAGE_DIR="/tmp/ahrefs-worker-profile-stage"

echo "[*] Source profile dir: ${SOURCE_DIR}"
if [[ ! -d "${SOURCE_DIR}" ]]; then
  echo "❌ Source profile directory not found: ${SOURCE_DIR}" >&2
  echo "" >&2
  echo "   Run this first to build it:" >&2
  echo "     bash tools/open_chrome_profile.sh" >&2
  echo "" >&2
  echo "   That opens Chrome with the cf-autoclick extension preloaded so" >&2
  echo "   you can verify it works. Close Chrome cleanly, then re-run this." >&2
  echo "" >&2
  echo "   Or pass a different profile path:" >&2
  echo "     bash tools/upload_profile_release.sh /path/to/profile" >&2
  exit 1
fi

EXT_MANIFEST="${SOURCE_DIR}/Extensions/cf_autoclick/manifest.json"
if [[ ! -f "${EXT_MANIFEST}" ]]; then
  echo "❌ cf-autoclick extension missing in source profile: ${EXT_MANIFEST}" >&2
  echo "   The profile must contain Extensions/cf_autoclick/{manifest.json,background.js,...}" >&2
  echo "   Run: bash tools/open_chrome_profile.sh   (rebuilds master-profile/ from scratch)" >&2
  exit 1
fi
echo "✅ cf-autoclick extension present in source"

if ! command -v gh >/dev/null 2>&1; then
  echo "❌ 'gh' CLI not installed. Install: https://cli.github.com/" >&2
  exit 1
fi

if ! gh auth status >/dev/null 2>&1; then
  echo "❌ 'gh' not authenticated. Run: gh auth login" >&2
  exit 1
fi

# ----------------------------------------------------------------------------
# Stage a clean copy of the profile.
#
# Chrome leaves runtime-only state in the profile dir that we don't want
# (and can't even zip in some cases — Singleton* are dangling symlinks
# pointing at sockets that no longer exist after Chrome exits). The clean
# copy lives in STAGE_DIR; the original SOURCE_DIR is never touched.
#
# Excluded patterns (Chrome regenerates all of these on first launch):
#   Singleton*           — "another instance is running" markers (dangling symlinks)
#   *Lock / lockfile     — process locks
#   BrowserMetrics*.pma  — binary metrics files (4 MB+ each, runtime-only)
#   Crashpad/            — crash report database
#   *Cache* / *cache*    — GPUCache, Code Cache, ShaderCache, etc.
#   GraphiteDawnCache    — GPU shader cache
#   blob_storage/        — runtime blob storage
#   Service Worker/*     — service-worker runtime state
# ----------------------------------------------------------------------------

echo "[*] Staging clean copy of profile -> ${STAGE_DIR}"
rm -rf "${STAGE_DIR}"
mkdir -p "${STAGE_DIR}"

# rsync handles broken symlinks gracefully and lets us prune in one pass.
# --no-links drops symlinks entirely from the staged copy. That's safe
# here because every symlink in a Chrome profile is either a Singleton*
# pointer (we don't want them) or — extremely rarely — a per-installation
# helper that the next Chrome launch will recreate. The cf-autoclick
# extension files are plain files, not symlinks.
rsync -a --no-links \
  --exclude='Singleton*' \
  --exclude='*Lock' \
  --exclude='lockfile' \
  --exclude='BrowserMetrics*.pma' \
  --exclude='Crashpad/' \
  --exclude='*Cache*/' \
  --exclude='*cache*/' \
  --exclude='GraphiteDawnCache/' \
  --exclude='GrShaderCache/' \
  --exclude='ShaderCache/' \
  --exclude='component_crx_cache/' \
  --exclude='blob_storage/' \
  --exclude='Service Worker/CacheStorage/' \
  --exclude='*.log' \
  --exclude='*.old' \
  --exclude='*.tmp' \
  "${SOURCE_DIR}/" "${STAGE_DIR}/"

# Sanity check: the extension must have survived the cleanup.
STAGED_EXT="${STAGE_DIR}/Extensions/cf_autoclick/manifest.json"
if [[ ! -f "${STAGED_EXT}" ]]; then
  echo "❌ cf-autoclick extension missing in STAGED copy: ${STAGED_EXT}" >&2
  echo "   An exclude pattern in this script removed it — review and adjust." >&2
  exit 1
fi
echo "✅ cf-autoclick extension preserved in staged copy"

STAGED_SIZE=$(du -sh "${STAGE_DIR}" | cut -f1)
echo "[*] Staged size: ${STAGED_SIZE}"

# ----------------------------------------------------------------------------
# Zip the staged profile.
#
# We zip from /tmp so the archive root contains 'ahrefs-worker-profile/'.
# ahrefs_checker.py is tolerant of either layout (it scans for the dir
# containing Extensions/cf_autoclick) but a stable wrapper dir name keeps
# the extracted structure predictable.
# ----------------------------------------------------------------------------

echo "[*] Zipping profile -> ${ZIP_PATH}"
rm -f "${ZIP_PATH}"

WRAPPER_DIR="ahrefs-worker-profile"
PARENT_OF_STAGE="$(dirname "${STAGE_DIR}")"
rm -rf "${PARENT_OF_STAGE}/${WRAPPER_DIR}"
mv "${STAGE_DIR}" "${PARENT_OF_STAGE}/${WRAPPER_DIR}"

( cd "${PARENT_OF_STAGE}" && zip -qr -X "${ZIP_PATH}" "${WRAPPER_DIR}" )

# Clean up the staged wrapper dir now that the zip is built.
rm -rf "${PARENT_OF_STAGE}/${WRAPPER_DIR}"

ZIP_SIZE=$(du -h "${ZIP_PATH}" | cut -f1)
echo "✅ Zip created (${ZIP_SIZE})"

# ----------------------------------------------------------------------------
# Upload to GitHub release.
# ----------------------------------------------------------------------------

echo "[*] Deleting existing release '${TAG}' on ${REPO} (if any)..."
gh release delete "${TAG}" --repo "${REPO}" --yes --cleanup-tag 2>/dev/null || true

echo "[*] Creating release '${TAG}' on ${REPO}..."
gh release create "${TAG}" "${ZIP_PATH}" \
  --repo "${REPO}" \
  --title "Ahrefs Worker Profile (with cf-autoclick)" \
  --notes "Master Chrome profile used by ahref-local/ahrefs_checker.py. Contains the cf-autoclick extension preinstalled. Replaced on each upload. Asset: ${ASSET}"

ASSET_URL="https://github.com/${REPO}/releases/download/${TAG}/${ASSET}"
echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║                  ✅ PROFILE UPLOAD COMPLETE                  ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
echo "Release URL: https://github.com/${REPO}/releases/tag/${TAG}"
echo "Asset URL:   ${ASSET_URL}"
echo ""
echo "ahrefs_checker.py will now download from this URL on every start."
