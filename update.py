# SPDX-FileCopyrightText: 2025 Thomas Ziemann
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""OTA updater – checks GitHub Releases for a newer version and overwrites
local project files, then performs a supervisor reload to start the new code.

Call ``check_and_update(requests_session)`` once at boot, after WiFi and the
adafruit_requests session are ready.
"""

import os
import gc
import supervisor
from version import VERSION

# ---------------------------------------------------------------------------
# Files that will be downloaded and overwritten when a new release is found.
# ---------------------------------------------------------------------------
_UPDATE_FILES = (
    "code.py",
    "update.py",
    "version.py",
    "webpage.html",
)

_RAW_BASE = "https://raw.githubusercontent.com/{owner}/{repo}/{tag}/{file}"
_API_URL   = "https://api.github.com/repos/{owner}/{repo}/releases/latest"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _version_tuple(v):
    """Convert ``'v1.2.3'`` or ``'1.2.3'`` to ``(1, 2, 3)`` for comparison."""
    v = v.lstrip("v")
    result = []
    for part in v.split("."):
        num = ""
        for ch in part:
            if ch.isdigit():
                num += ch
            else:
                break
        result.append(int(num) if num else 0)
    return tuple(result)


def _is_writable():
    """Return True when the root filesystem can be written to."""
    try:
        with open("/_ota_test", "w") as fh:
            fh.write("x")
        os.remove("/_ota_test")
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def check_and_update(requests_session):
    """Check GitHub for a newer release and, if found, download updated files.

    Parameters
    ----------
    requests_session:
        A ready ``adafruit_requests.Session`` (WiFi must already be up).

    Returns
    -------
    bool
        ``False`` if no update was needed or the update could not run.
        Does **not** return ``True`` on success – the device reloads instead.
    """
    owner = os.getenv("GITHUB_OWNER", "")
    repo  = os.getenv("GITHUB_REPO",  "")

    if not owner or not repo:
        print("[OTA] GITHUB_OWNER / GITHUB_REPO not set in settings.toml – skipping.")
        return False

    print(f"[OTA] Current version : {VERSION}")
    api_url = _API_URL.format(owner=owner, repo=repo)
    print(f"[OTA] Querying        : {api_url}")

    # ------------------------------------------------------------------
    # 1. Fetch latest release metadata from the GitHub API
    # ------------------------------------------------------------------
    try:
        resp = requests_session.get(api_url, timeout=15)
        http_status = resp.status_code
        if http_status != 200:
            print(f"[OTA] GitHub API returned HTTP {http_status} – skipping.")
            resp.close()
            return False
        data = resp.json()
        resp.close()
        gc.collect()
    except Exception as exc:
        print(f"[OTA] Network error fetching release info: {exc}")
        return False

    tag = data.get("tag_name", "")
    if not tag:
        print("[OTA] Release has no tag_name – skipping.")
        return False

    print(f"[OTA] Latest release  : {tag}")

    # ------------------------------------------------------------------
    # 2. Compare versions – skip if already current
    # ------------------------------------------------------------------
    try:
        if _version_tuple(tag) <= _version_tuple(VERSION):
            print("[OTA] Already up to date.")
            return False
    except Exception as exc:
        print(f"[OTA] Version comparison failed: {exc}")
        return False

    print(f"[OTA] New version available: {tag}  (installed: {VERSION})")

    # ------------------------------------------------------------------
    # 3. Verify write access (filesystem must not be USB-mounted)
    # ------------------------------------------------------------------
    if not _is_writable():
        print("[OTA] Filesystem is read-only (USB connected?). Skipping update.")
        return False

    # ------------------------------------------------------------------
    # 4. Download each file from raw.githubusercontent.com
    # ------------------------------------------------------------------
    any_updated = False
    for filename in _UPDATE_FILES:
        url = _RAW_BASE.format(owner=owner, repo=repo, tag=tag, file=filename)
        print(f"[OTA] Downloading {filename} ...")
        try:
            r = requests_session.get(url, timeout=30)
            if r.status_code != 200:
                print(f"[OTA]   HTTP {r.status_code} for {filename} – skipping.")
                r.close()
                continue

            content = r.content
            r.close()
            gc.collect()

            with open(filename, "wb") as fh:
                fh.write(content)

            print(f"[OTA]   {filename} saved ({len(content)} bytes).")
            any_updated = True
            del content
            gc.collect()

        except Exception as exc:
            print(f"[OTA]   Failed to update {filename}: {exc}")

    # ------------------------------------------------------------------
    # 5. Reload to run the new code
    # ------------------------------------------------------------------
    if any_updated:
        print(f"[OTA] Update to {tag} complete – reloading device.")
        supervisor.reload()

    return any_updated
