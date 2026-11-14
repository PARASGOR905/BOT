#!/usr/bin/env python3
import json
import os
import shutil
import subprocess
import sys
import urllib.request


def download_progress_hook(block_num, block_size, total_size):
    downloaded = block_num * block_size
    if total_size > 0:
        percent = min(100.0, downloaded * 100.0 / total_size)
        mb_down = downloaded / (1024 * 1024)
        mb_total = total_size / (1024 * 1024)
        sys.stdout.write(f"\r[INFO] Downloading: {mb_down:.2f} MB / {mb_total:.2f} MB ({percent:.1f}%)")
        sys.stdout.flush()


def update_shaka_packager():
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    binaries_dir = os.path.join(repo_root, "binaries")
    os.makedirs(binaries_dir, exist_ok=True)

    target_exe = os.path.join(binaries_dir, "packager.exe")
    backup_exe = os.path.join(binaries_dir, "packager_backup.exe")
    temp_exe = os.path.join(binaries_dir, "packager_temp.exe")

    api_url = "https://api.github.com/repos/shaka-project/shaka-packager/releases/latest"
    print("=======================================================")
    print("            Shaka Packager Release Updater             ")
    print("=======================================================")
    print(f"[INFO] Fetching latest release info from GitHub API...")

    req = urllib.request.Request(api_url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"[ERROR] Failed to fetch release info: {e}")
        return 1

    tag = data.get("tag_name", "unknown")
    print(f"[INFO] Latest release tag: {tag}")

    assets = data.get("assets", [])
    target_asset = next((a for a in assets if a.get("name") == "packager-win-x64.exe"), None)

    if not target_asset:
        print("[ERROR] Could not find 'packager-win-x64.exe' in release assets.")
        return 1

    download_url = target_asset["browser_download_url"]
    file_size_mb = target_asset.get("size", 0) / (1024 * 1024)
    print(f"[INFO] Asset: {target_asset['name']} ({file_size_mb:.2f} MB)")
    print(f"[INFO] Starting download from {download_url}...")

    try:
        urllib.request.urlretrieve(download_url, temp_exe, reporthook=download_progress_hook)
        print()
    except Exception as e:
        print(f"\n[ERROR] Download failed: {e}")
        if os.path.exists(temp_exe):
            os.remove(temp_exe)
        return 1

    # Backup existing binary
    if os.path.exists(target_exe):
        try:
            if os.path.exists(backup_exe):
                os.remove(backup_exe)
            shutil.copy2(target_exe, backup_exe)
            print(f"[INFO] Existing binary backed up to {backup_exe}")
            os.remove(target_exe)
        except Exception as e:
            print(f"[WARNING] Could not create backup: {e}")

    try:
        shutil.move(temp_exe, target_exe)
        print(f"[SUCCESS] Updated {target_exe} to {tag}")
    except Exception as e:
        print(f"[ERROR] Failed to move executable to target location: {e}")
        return 1

    # Verify version
    print("\n[INFO] Verifying updated binary:")
    try:
        result = subprocess.run([target_exe, "--version"], capture_output=True, text=True, check=True)
        print(f"  Output: {result.stdout.strip() or result.stderr.strip()}")
    except Exception as e:
        print(f"[WARNING] Could not execute {target_exe}: {e}")

    print("\n=======================================================")
    print(" Update Completed Successfully!")
    print("=======================================================")
    return 0


if __name__ == "__main__":
    sys.exit(update_shaka_packager())
