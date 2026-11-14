#!/usr/bin/env python3
"""
bot.py - Ultra-Fast Telegram Controller Bot for Local Media Services, Rclone / GDrive Upload & VPN
Optimizations:
  1. Persistent Keep-Alive Session: Reusable HTTP connection pool for sub-50ms Telegram API calls.
  2. Non-blocking Asynchronous Dispatch: Polling loop never freezes; commands execute instantly in worker threads.
  3. Decoupled Progress Ticker: Stdout reading never blocks on Telegram network calls; download runs at 100% full wire speed.
  4. Instant Zero-I/O Snapshotting: Detects downloads without slow disk-crawling rglob.
  5. Live Progress Bar: Real-time visual progress bar ([████░░░░] 52%), speed & ETA in Telegram.
  6. Rclone & Robocopy Support: High-speed cloud uploads directly to Google Drive.
  7. Auto-Upload Pipeline: /auto and /dl auto-trigger upload on completion.
  8. ExpressVPN CLI Controls: /vpn status, connect, disconnect, ip.
"""

import os
import sys
import time
import json
import shutil
import re
import threading
import subprocess
import uuid
import html
from pathlib import Path
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ==================== CONFIGURATION ====================
WORKING_DIR = Path(__file__).parent.resolve() if "__file__" in globals() else Path.cwd().resolve()

def load_env_file():
    env_path = WORKING_DIR / ".env"
    if env_path.exists():
        try:
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        k = k.strip()
                        v = v.strip().strip('"\'')
                        if k not in os.environ:
                            os.environ[k] = v
        except Exception:
            pass

load_env_file()

BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN_HERE")
try:
    ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
except ValueError:
    ADMIN_ID = 0

DOWNLOADS_DIR = WORKING_DIR / "downloads"

# Upload Engine: "rclone" (Direct Drive API) or "robocopy" (Mounted Drive letter)
UPLOAD_ENGINE = "rclone"

# Rclone remote target
RCLONE_REMOTE = "gdrive:TV Shows"

def find_rclone_bin():
    found = shutil.which("rclone")
    if found:
        return "rclone"
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    if local_app_data:
        winget_link = Path(local_app_data) / "Microsoft" / "WinGet" / "Links" / "rclone.exe"
        if winget_link.exists():
            return f'"{winget_link}"'
        winget_pkgs = Path(local_app_data) / "Microsoft" / "WinGet" / "Packages"
        if winget_pkgs.exists():
            matches = list(winget_pkgs.glob("**/rclone.exe"))
            if matches:
                return f'"{matches[0]}"'
    candidates = [
        Path(r"C:\rclone\rclone.exe"),
        Path(r"C:\Program Files\rclone\rclone.exe"),
    ]
    for c in candidates:
        if c.exists():
            return f'"{c}"'
    return "rclone"

RCLONE_BIN = find_rclone_bin()

# Robocopy local Google Drive destination
GDRIVE_DEFAULT_DEST = r"G:\My Drive\TV Shows"

# Auto-upload to Google Drive immediately after download completes
AUTO_DRIVE_UPLOAD = True

# ExpressVPN CLI Path
EXPRESSVPN_PATH = r"C:\Program Files\ExpressVPN\expressvpnctl.exe"

# Quality, Codec & Audio Defaults
DEFAULT_QUALITY = "1080p"
DEFAULT_VCODEC = "H264"
DEFAULT_AUDIO_LANGS = ["hin", "tam", "tel"]

SETTINGS_FILE = WORKING_DIR / "bot_settings.json"
pending_downloads = {}
# ========================================================

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# High-Performance Persistent HTTP Session
session = requests.Session()
retries = Retry(total=3, backoff_factor=0.3, status_forcelist=[500, 502, 503, 504])
adapter = HTTPAdapter(pool_connections=20, pool_maxsize=30, max_retries=retries)
session.mount("https://", adapter)
session.mount("http://", adapter)

active_process = None
active_cmd = None
active_start_time = None
process_lock = threading.Lock()

# Regex patterns for real-time progress parsing
RE_ARIA2_MAIN = re.compile(r'\((\d{1,3})%\)\s+CN:\d+\s+DL:([^\s]+)\s+ETA:([^\s\]]+)')
RE_ARIA2_ALT = re.compile(r'\[#\w+\s+([^\s]+)/([^\s]+)\((\d{1,3})%\)')
RE_ROBOCOPY = re.compile(r'^\s*(\d{1,3}(?:\.\d+)?)%\s*$')
RE_MKVMERGE = re.compile(r'Progress:\s*(\d+)%')
RE_SEGMENTS = re.compile(r'(\d+)/(\d+)\s+segments')
RE_PERCENT_GENERIC = re.compile(r'(\d{1,3}(?:\.\d+)?)%')

# Rclone progress patterns
RE_RCLONE_MAIN = re.compile(r'Transferred:\s+(.*?)\s*/\s*(.*?),\s*(\d{1,3})%,\s*([^,]+),\s*ETA\s*([^\s]+)')
RE_RCLONE_FILE = re.compile(r'\*\s+([^:]+):\s+(\d{1,3})%\s+/([^\s,]+),\s+([^\s,]+),\s+([^\s]+)')


# ---------------- Settings Persistence ----------------
def load_settings():
    global DEFAULT_QUALITY, DEFAULT_VCODEC, DEFAULT_AUDIO_LANGS, UPLOAD_ENGINE, RCLONE_REMOTE, GDRIVE_DEFAULT_DEST, AUTO_DRIVE_UPLOAD, ADMIN_ID
    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                DEFAULT_QUALITY = data.get("quality", DEFAULT_QUALITY)
                DEFAULT_VCODEC = data.get("vcodec", DEFAULT_VCODEC)
                DEFAULT_AUDIO_LANGS = data.get("audio_langs", DEFAULT_AUDIO_LANGS)
                UPLOAD_ENGINE = data.get("upload_engine", UPLOAD_ENGINE)
                RCLONE_REMOTE = data.get("rclone_remote", RCLONE_REMOTE)
                if data.get("gdrive_dest"):
                    GDRIVE_DEFAULT_DEST = data.get("gdrive_dest")
                AUTO_DRIVE_UPLOAD = data.get("auto_upload", AUTO_DRIVE_UPLOAD)
                if data.get("admin_id"):
                    ADMIN_ID = data.get("admin_id")
            print("[+] Loaded persistent settings from bot_settings.json")
        except Exception as e:
            print(f"[!] Error loading settings: {e}")


def save_settings():
    try:
        data = {
            "quality": DEFAULT_QUALITY,
            "vcodec": DEFAULT_VCODEC,
            "audio_langs": DEFAULT_AUDIO_LANGS,
            "upload_engine": UPLOAD_ENGINE,
            "rclone_remote": RCLONE_REMOTE,
            "gdrive_dest": str(GDRIVE_DEFAULT_DEST),
            "auto_upload": AUTO_DRIVE_UPLOAD,
            "admin_id": ADMIN_ID
        }
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"[!] Error saving settings: {e}")


# ---------------- Telegram API Helpers ----------------
def send_message(chat_id, text, parse_mode="HTML", reply_markup=None):
    try:
        url = f"{TELEGRAM_API}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        res = session.post(url, json=payload, timeout=12)
        data = res.json()
        if not data.get("ok") and parse_mode:
            payload.pop("parse_mode", None)
            res2 = session.post(url, json=payload, timeout=12)
            return res2.json()
        return data
    except Exception as e:
        print(f"[TG Error] send_message: {e}")
        return None


def edit_message(chat_id, message_id, text, parse_mode="HTML", reply_markup=None):
    try:
        url = f"{TELEGRAM_API}/editMessageText"
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        res = session.post(url, json=payload, timeout=8)
        data = res.json()
        if not data.get("ok") and parse_mode:
            payload.pop("parse_mode", None)
            res2 = session.post(url, json=payload, timeout=8)
            return res2.json()
        return data
    except Exception as e:
        print(f"[TG Error] edit_message: {e}")
        return None


def answer_callback_query(callback_query_id, text=None, show_alert=False):
    try:
        url = f"{TELEGRAM_API}/answerCallbackQuery"
        payload = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
            payload["show_alert"] = show_alert
        session.post(url, json=payload, timeout=8)
    except Exception:
        pass


def format_bytes(size_bytes):
    if size_bytes >= 1024**3:
        return f"{size_bytes / (1024**3):.2f} GB"
    elif size_bytes >= 1024**2:
        return f"{size_bytes / (1024**2):.1f} MB"
    elif size_bytes >= 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes} B"


def render_progress_bar(pct, length=12):
    """Generate visual progress bar: [████████░░░░] 66.7%"""
    pct = max(0.0, min(100.0, float(pct)))
    filled = int(round(length * pct / 100.0))
    bar = "█" * filled + "░" * (length - filled)
    return f"<code>[{bar}] {pct:5.1f}%</code>"


def detect_gdrive_path():
    """Detect mounted Google Drive desktop drive letter or Colab mount."""
    colab_path = Path("/content/drive/MyDrive/TV Shows")
    if colab_path.exists() or colab_path.parent.exists():
        return colab_path
    colab_root = Path("/content/drive/MyDrive")
    if colab_root.exists():
        return colab_root

    curr = Path(GDRIVE_DEFAULT_DEST)
    if curr.exists() or curr.parent.exists():
        return curr

    for letter in ["G", "H", "F", "I", "E", "D"]:
        cand = Path(f"{letter}:/My Drive/TV Shows")
        if cand.exists() or cand.parent.exists():
            return cand
        cand_root = Path(f"{letter}:/My Drive")
        if cand_root.exists():
            return cand_root

    return curr


def snapshot_downloads():
    """Fast non-recursive snapshot of items in DOWNLOADS_DIR."""
    if not DOWNLOADS_DIR.exists():
        return {}
    snap = {}
    try:
        with os.scandir(DOWNLOADS_DIR) as it:
            for entry in it:
                try:
                    stat = entry.stat()
                    snap[entry.name] = (stat.st_mtime, stat.st_size)
                except Exception:
                    pass
    except Exception:
        pass
    return snap


def find_new_or_updated_target(pre_snapshot):
    """Fast detection of newly created or updated target in downloads."""
    if not DOWNLOADS_DIR.exists():
        return None

    candidates = []
    ignored = {"__pycache__", "mux.py", "mux.py.bak"}
    try:
        with os.scandir(DOWNLOADS_DIR) as it:
            for entry in it:
                if entry.name.startswith(".") or entry.name in ignored:
                    continue
                try:
                    stat = entry.stat()
                    prev = pre_snapshot.get(entry.name)
                    if prev is None or stat.st_mtime > prev[0] or stat.st_size > prev[1]:
                        candidates.append((Path(entry.path), stat.st_mtime))
                except Exception:
                    pass
    except Exception:
        pass

    if candidates:
        candidates.sort(key=lambda x: x[1], reverse=True)
        return candidates[0][0]

    # Fallback to newest item
    items = [p for p in DOWNLOADS_DIR.iterdir() if not p.name.startswith(".") and p.name not in ignored]
    if items:
        items.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return items[0]
    return None


def parse_progress_line(line, state):
    """Extract percentage, speed, ETA, and activity from process output."""
    cleaned = line.strip()
    if not cleaned:
        return

    # 1. Rclone overall progress
    m_rc = RE_RCLONE_MAIN.search(cleaned)
    if m_rc:
        state["pct"] = float(m_rc.group(3))
        state["speed"] = m_rc.group(4)
        state["eta"] = m_rc.group(5)
        state["status_text"] = f"Rclone: {m_rc.group(1)} / {m_rc.group(2)}"
        return

    # 2. Rclone file line
    m_rc_file = RE_RCLONE_FILE.search(cleaned)
    if m_rc_file:
        state["file_info"] = m_rc_file.group(1)
        state["pct"] = float(m_rc_file.group(2))
        state["speed"] = m_rc_file.group(4)
        state["eta"] = m_rc_file.group(5)
        return

    # 3. Aria2c download stats
    m_aria = RE_ARIA2_MAIN.search(cleaned)
    if m_aria:
        state["pct"] = float(m_aria.group(1))
        state["speed"] = m_aria.group(2)
        state["eta"] = m_aria.group(3)
        return

    m_aria_alt = RE_ARIA2_ALT.search(cleaned)
    if m_aria_alt:
        state["pct"] = float(m_aria_alt.group(3))

    # 4. Robocopy percentage line
    m_robo = RE_ROBOCOPY.search(cleaned)
    if m_robo:
        state["pct"] = float(m_robo.group(1))
        return

    # 5. Mkvmerge progress line
    m_mkv = RE_MKVMERGE.search(cleaned)
    if m_mkv:
        state["pct"] = float(m_mkv.group(1))
        state["status_text"] = "Muxing streams into MKV..."
        return

    # 6. Segment progress
    m_seg = RE_SEGMENTS.search(cleaned)
    if m_seg:
        cur, total = int(m_seg.group(1)), int(m_seg.group(2))
        if total > 0:
            state["pct"] = (cur / total) * 100.0
            state["status_text"] = f"Segments: {cur}/{total}"

    # 7. Contextual activity text
    if "Downloading:" in cleaned:
        parts = cleaned.split("Downloading:", 1)[1].strip()
        state["status_text"] = f"DL: {parts}"
    elif "Logging into" in cleaned:
        state["status_text"] = "Authenticating with service..."
    elif "Retrieving Titles" in cleaned:
        state["status_text"] = "Fetching metadata & titles..."
    elif "Getting tracks for" in cleaned:
        title_info = cleaned.split("Getting tracks for")[-1].strip()
        state["status_text"] = f"Tracks: {title_info}"
    elif "Decrypting" in cleaned or "decrypted" in cleaned.lower():
        state["status_text"] = "Decrypting content keys..."
    elif "New File" in cleaned or "Older" in cleaned or "Same" in cleaned:
        state["file_info"] = cleaned.split()[-1]


def build_live_status_message(title, elapsed_sec, state):
    """Construct an informative Telegram message with progress bar."""
    mins, secs = divmod(elapsed_sec, 60)
    time_str = f"{mins:02d}m {secs:02d}s"

    pct = state.get("pct")
    speed = state.get("speed")
    eta = state.get("eta")
    status_text = state.get("status_text")
    file_info = state.get("file_info")

    lines = [
        f"⏳ <b>Task Running:</b> (<code>{time_str}</code>)",
        f"<code>{title}</code>",
        ""
    ]

    if pct is not None:
        pct = max(0.0, min(100.0, float(pct)))
        bar = render_progress_bar(pct, length=12)
        lines.append(f"{bar}")
    else:
        spinners = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
        spin = spinners[int(elapsed_sec) % len(spinners)]
        lines.append(f"<code>[{spin}] Processing...</code>")

    info_parts = []
    if speed:
        info_parts.append(f"⚡ <b>Speed:</b> {speed}")
    if eta:
        info_parts.append(f"⏱ <b>ETA:</b> {eta}")
    if info_parts:
        lines.append(" | ".join(info_parts))

    if status_text:
        lines.append(f"📋 <i>{status_text[:100]}</i>")
    elif file_info:
        lines.append(f"📄 <code>{file_info[:70]}</code>")

    return "\n".join(lines)


def terminate_active_process():
    global active_process, active_cmd
    with process_lock:
        if active_process and active_process.poll() is None:
            cmd_name = active_cmd
            try:
                if sys.platform == "win32":
                    subprocess.run(f"taskkill /PID {active_process.pid} /T /F", shell=True)
                else:
                    active_process.terminate()
                    time.sleep(0.5)
                    if active_process.poll() is None:
                        active_process.kill()
            except Exception as e:
                print(f"[!] Error terminating process: {e}")
            active_process = None
            active_cmd = None
            return cmd_name
        return None


# ---------------- Background Execution (Decoupled & Non-blocking) ----------------
def run_command_async(chat_id, full_cmd, task_name=None, on_success_callback=None):
    global active_process, active_cmd, active_start_time

    def worker():
        global active_process, active_cmd, active_start_time
        title = task_name or full_cmd
        start_ts = time.time()
        active_start_time = start_ts
        active_cmd = title

        cancel_markup = {"inline_keyboard": [[{"text": "🛑 Cancel Task", "callback_data": "task:cancel"}]]}
        init_res = send_message(chat_id, f"🚀 <b>Task Started:</b>\n<code>{title}</code>", reply_markup=cancel_markup)
        status_msg_id = init_res.get("result", {}).get("message_id") if init_res and init_res.get("ok") else None

        output_lines = []
        state = {
            "pct": None,
            "speed": None,
            "eta": None,
            "status_text": None,
            "file_info": None
        }

        stop_ticker = threading.Event()

        # Decoupled ticker thread: updates Telegram independently so stdout is never blocked
        def ticker():
            last_text = ""
            while not stop_ticker.is_set():
                stop_ticker.wait(2.8)
                if stop_ticker.is_set():
                    break
                if status_msg_id:
                    elapsed = int(time.time() - start_ts)
                    msg_text = build_live_status_message(title, elapsed, state)
                    if msg_text != last_text:
                        edit_message(chat_id, status_msg_id, msg_text, reply_markup=cancel_markup)
                        last_text = msg_text

        ticker_thread = threading.Thread(target=ticker, daemon=True)

        try:
            with process_lock:
                active_process = subprocess.Popen(
                    full_cmd,
                    shell=True,
                    cwd=str(WORKING_DIR),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1
                )

            ticker_thread.start()

            # High-speed line consumption: only memory writes, ZERO network wait
            for line in iter(active_process.stdout.readline, ''):
                output_lines.append(line)
                if len(output_lines) > 250:
                    output_lines.pop(0)
                parse_progress_line(line, state)

            active_process.wait()
            ret_code = active_process.returncode
            elapsed = int(time.time() - start_ts)
            mins, secs = divmod(elapsed, 60)

            # Stop ticker before sending final result
            stop_ticker.set()
            ticker_thread.join(timeout=1.0)

            last_output = "".join(output_lines[-15:]).strip()
            if len(last_output) > 3000:
                last_output = last_output[-3000:]

            # In Robocopy, exit codes < 8 indicate success
            is_robocopy = "robocopy" in full_cmd.lower()
            is_success = (ret_code < 8) if is_robocopy else (ret_code == 0)

            clean_output = html.escape(last_output) if last_output else "No output produced"
            print(f"[{'SUCCESS' if is_success else 'FAIL'}] {title} (Code {ret_code})")
            if last_output:
                print(last_output)

            clear_kb = {"inline_keyboard": []}
            if is_success:
                finish_text = (
                    f"✅ <b>Task Completed!</b> ({mins}m {secs}s)\n"
                    f"<code>{html.escape(title)}</code>\n\n"
                    f"<pre>{clean_output}</pre>"
                )
                res = edit_message(chat_id, status_msg_id, finish_text, reply_markup=clear_kb) if status_msg_id else None
                if not res or not res.get("ok"):
                    send_message(chat_id, finish_text)

                if on_success_callback:
                    on_success_callback()
            else:
                fail_text = (
                    f"❌ <b>Task Failed! (Code {ret_code})</b> ({mins}m {secs}s)\n"
                    f"<code>{html.escape(title)}</code>\n\n"
                    f"<pre>{clean_output}</pre>"
                )
                res = edit_message(chat_id, status_msg_id, fail_text, reply_markup=clear_kb) if status_msg_id else None
                if not res or not res.get("ok"):
                    send_message(chat_id, fail_text)

        except Exception as e:
            stop_ticker.set()
            send_message(chat_id, f"⚠️ <b>Execution Error:</b> {e}")
        finally:
            stop_ticker.set()
            with process_lock:
                active_process = None
                active_cmd = None
                active_start_time = None

    t = threading.Thread(target=worker, daemon=True)
    t.start()


# ---------------- Google Drive Upload (Rclone & Robocopy) ----------------
def upload_to_gdrive(chat_id, target_name=None, on_finish_callback=None):
    global active_process, active_cmd, UPLOAD_ENGINE, RCLONE_REMOTE, GDRIVE_DEFAULT_DEST

    if active_process and active_process.poll() is None:
        send_message(chat_id, "⚠️ <b>A task is already running!</b> Wait for it to finish or use <code>/cancel</code>.")
        return

    # Identify file or folder to upload
    target_path = None
    if target_name:
        cand = DOWNLOADS_DIR / target_name.strip()
        if cand.exists():
            target_path = cand
        else:
            send_message(chat_id, f"❌ File or folder not found in downloads:\n<code>{target_name}</code>")
            return
    else:
        ignored = {"__pycache__", "mux.py", "mux.py.bak"}
        items = [p for p in DOWNLOADS_DIR.iterdir() if not p.name.startswith(".") and p.name not in ignored]
        if not items:
            send_message(chat_id, "❌ No files or folders found in <code>D:\\temp\\downloads</code> to upload.")
            return
        items.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        target_path = items[0]

    # Calculate size & files
    if target_path.is_dir():
        total_size = sum(f.stat().st_size for f in target_path.rglob('*') if f.is_file())
        file_count = sum(1 for f in target_path.rglob('*') if f.is_file())
        size_str = f"{file_count} files ({format_bytes(total_size)})"
    else:
        total_size = target_path.stat().st_size
        size_str = format_bytes(total_size)

    # 1. Rclone Mode (Direct Google Drive Cloud API)
    if UPLOAD_ENGINE.lower() == "rclone":
        if target_path.is_dir():
            dest = f"{RCLONE_REMOTE}/{target_path.name}"
        else:
            dest = RCLONE_REMOTE

        send_message(
            chat_id,
            f"☁️ <b>Starting Rclone Upload to Google Drive:</b>\n"
            f"📁 <b>Target:</b> <code>{target_path.name}</code>\n"
            f"📦 <b>Size:</b> {size_str}\n"
            f"🎯 <b>Remote:</b> <code>{dest}</code>\n"
            f"⚡ <i>Chunk Size: 64M | Parallel Transfers: 4</i>"
        )

        rclone_cmd = (
            f'{RCLONE_BIN} copy "{target_path}" "{dest}" '
            f'-P --stats 2s --drive-chunk-size 64M --transfers 4 --checkers 8'
        )
        run_command_async(chat_id, rclone_cmd, task_name=f"Rclone: {target_path.name}", on_success_callback=on_finish_callback)
        return

    # 2. Robocopy Mode (Mounted Desktop Drive Letter)
    gdrive_dest_root = detect_gdrive_path()
    if not gdrive_dest_root.exists() and not gdrive_dest_root.parent.exists():
        send_message(
            chat_id,
            f"⚠️ <b>Google Drive folder not detected!</b>\n"
            f"Path: <code>{gdrive_dest_root}</code> does not exist.\n\n"
            f"💡 <i>Tip: Switch to Rclone for direct cloud upload via <code>/engine rclone</code></i>\n"
            f"Or set path via <code>/setgdrive &lt;path&gt;</code>"
        )
        return

    try:
        gdrive_dest_root.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    if target_path.is_dir():
        dest_folder = gdrive_dest_root / target_path.name
        send_message(
            chat_id,
            f"☁️ <b>Starting Robocopy Upload (Folder):</b>\n"
            f"📁 <b>Folder:</b> <code>{target_path.name}</code>\n"
            f"📦 <b>Size:</b> {size_str}\n"
            f"🎯 <b>Dest:</b> <code>{dest_folder}</code>"
        )
        robocopy_cmd = f'robocopy "{target_path}" "{dest_folder}" /E /MT:8 /R:3 /W:2'
        run_command_async(chat_id, robocopy_cmd, task_name=f"Robocopy: {target_path.name}", on_success_callback=on_finish_callback)
    else:
        send_message(
            chat_id,
            f"☁️ <b>Starting Robocopy Upload (File):</b>\n"
            f"📄 <b>File:</b> <code>{target_path.name}</code>\n"
            f"📦 <b>Size:</b> {size_str}\n"
            f"🎯 <b>Dest:</b> <code>{gdrive_dest_root}\\{target_path.name}</code>"
        )
        robocopy_cmd = f'robocopy "{target_path.parent}" "{gdrive_dest_root}" "{target_path.name}" /MT:8 /R:3 /W:2'
        run_command_async(chat_id, robocopy_cmd, task_name=f"Robocopy: {target_path.name}", on_success_callback=on_finish_callback)


# ---------------- Interactive Button & Selection Helpers ----------------
SUPPORTED_SERVICES = {
    "JHS": {"name": "JioHotstar", "icon": "🌟", "domains": ["hotstar.com", "jiocinema.com"]},
    "AMZN": {"name": "Amazon Prime", "icon": "📦", "domains": ["primevideo.com", "amazon."]},
    "NF": {"name": "Netflix", "icon": "🍿", "domains": ["netflix.com"]},
    "DSNP": {"name": "Disney+", "icon": "🏰", "domains": ["disneyplus.com"]},
    "SLIV": {"name": "SonyLIV", "icon": "📺", "domains": ["sonyliv.com"]},
    "STAGE": {"name": "STAGE", "icon": "🎭", "domains": ["stage.in"]},
    "ZEE5": {"name": "Zee5", "icon": "✨", "domains": ["zee5.com"]},
    "ATV": {"name": "Apple TV+", "icon": "🍎", "domains": ["tv.apple.com"]},
    "CR": {"name": "Crunchyroll", "icon": "🍣", "domains": ["crunchyroll.com"]},
    "CHAU": {"name": "Chaupal", "icon": "🌾", "domains": ["chaupal.tv"]},
    "SUN": {"name": "SunNXT", "icon": "☀️", "domains": ["sunnxt.net"]},
    "AHA": {"name": "Aha", "icon": "🎬", "domains": ["aha.video"]},
    "HCH": {"name": "Hoichoi", "icon": "🎥", "domains": ["hoichoi.tv"]}
}

def build_service_picker_keyboard(sid):
    return {
        "inline_keyboard": [
            [
                {"text": "🌟 JioHotstar (JHS)", "callback_data": f"dl:{sid}:srv:JHS"},
                {"text": "📦 Prime Video (AMZN)", "callback_data": f"dl:{sid}:srv:AMZN"}
            ],
            [
                {"text": "🍿 Netflix (NF)", "callback_data": f"dl:{sid}:srv:NF"},
                {"text": "🏰 Disney+ (DSNP)", "callback_data": f"dl:{sid}:srv:DSNP"}
            ],
            [
                {"text": "📺 SonyLIV (SLIV)", "callback_data": f"dl:{sid}:srv:SLIV"},
                {"text": "🎭 STAGE (STAGE)", "callback_data": f"dl:{sid}:srv:STAGE"}
            ],
            [
                {"text": "✨ Zee5 (ZEE5)", "callback_data": f"dl:{sid}:srv:ZEE5"},
                {"text": "🍎 Apple TV+ (ATV)", "callback_data": f"dl:{sid}:srv:ATV"}
            ],
            [
                {"text": "🍣 Crunchyroll (CR)", "callback_data": f"dl:{sid}:srv:CR"},
                {"text": "🌾 Chaupal (CHAU)", "callback_data": f"dl:{sid}:srv:CHAU"}
            ],
            [
                {"text": "☀️ SunNXT (SUN)", "callback_data": f"dl:{sid}:srv:SUN"},
                {"text": "🎬 Aha (AHA)", "callback_data": f"dl:{sid}:srv:AHA"}
            ],
            [
                {"text": "🔙 Back", "callback_data": f"dl:{sid}:back"}
            ]
        ]
    }


# Language metadata: code -> display label & row grouping
LANG_META = {
    "hin": "Hindi", "tam": "Tamil", "tel": "Telugu", "eng": "English",
    "mal": "Malayalam", "kan": "Kannada", "ben": "Bengali", "mar": "Marathi",
    "pun": "Punjabi", "ori": "Odia", "guj": "Gujarati", "san": "Sanskrit",
    "jpn": "Japanese", "kor": "Korean", "zho": "Chinese", "fra": "French",
    "spa": "Spanish", "por": "Portuguese", "deu": "German", "ara": "Arabic",
}
LANG_ROW_ORDER = [
    ["hin", "tam", "tel", "eng"],
    ["mal", "kan", "ben", "mar"],
    ["pun", "ori", "guj"],
    ["jpn", "kor", "zho", "fra", "spa", "por", "deu", "ara"],
]

def _chunk_list(lst, n):
    """Split list into sub-lists of max length n."""
    return [lst[i:i+n] for i in range(0, len(lst), n)]


def fetch_track_metadata(service, url, extra=""):
    """
    Run `vt dl --list <service> <url>` and parse available tracks.
    Returns dict: { "qualities": ["2160p","1080p",...], "audio_codes": ["hin","tam",...],
                    "seasons": [1,2,...], "ep_counts": {1: 12, 2: 8} }
    Returns None on timeout/error.
    """
    cmd = f'poetry run vt dl --list {extra} {service} "{url}"'
    try:
        proc = subprocess.run(
            cmd, shell=True, cwd=str(WORKING_DIR),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", timeout=45
        )
        output = proc.stdout or ""
    except subprocess.TimeoutExpired:
        return None
    except Exception:
        return None

    qualities = []
    audio_codes = []
    seasons = set()
    ep_counts = {}   # {season_num: count}

    for line in output.splitlines():
        # --- Video qualities ---
        for q in ["2160p", "1080p", "720p", "576p", "480p", "360p", "240p"]:
            if q in line and q not in qualities:
                qualities.append(q)

        # --- Audio language codes (3-letter ISO 639-2) ---
        # Vinetrimmer typically prints lines like:  [A] hin: Hindi  or  Language: hin
        m_lang = re.search(r'\b([a-z]{3})\b', line)
        if m_lang:
            code = m_lang.group(1)
            if code in LANG_META and code not in audio_codes:
                audio_codes.append(code)

        # --- Season/episode lines like S01E01 or S01 E12 ---
        m_ep = re.findall(r'[Ss](\d{1,2})[Ee](\d{1,3})', line)
        for s_str, e_str in m_ep:
            s = int(s_str)
            e = int(e_str)
            seasons.add(s)
            ep_counts[s] = max(ep_counts.get(s, 0), e)

    # Order qualities from highest to lowest
    q_order = ["2160p", "1080p", "720p", "576p", "480p", "360p", "240p"]
    qualities = [q for q in q_order if q in qualities] or qualities

    return {
        "qualities": qualities,
        "audio_codes": audio_codes,
        "seasons": sorted(seasons),
        "ep_counts": ep_counts,
        "raw": output[:2000],
    }


def build_download_keyboard(sid, session):
    curr_q = session.get("quality", "1080p")
    curr_langs = session.get("audio_langs", ["hin", "tam", "tel"])
    curr_codec = session.get("vcodec", "H264")
    curr_wanted = session.get("wanted", "all")
    is_series = session.get("is_series", False)
    curr_srv = session.get("service", "JHS")
    info = SUPPORTED_SERVICES.get(curr_srv, {"name": curr_srv, "icon": "🎬"})
    srv_label = f"{info.get('icon', '🎬')} {curr_srv}"

    # --- Actual track metadata (if already fetched) ---
    meta = session.get("track_meta")           # None = not fetched yet / failed
    meta_fetching = session.get("meta_fetching", False)

    # Row 1: Quality buttons — use real qualities if available
    if meta and meta.get("qualities"):
        avail_q = meta["qualities"]
        if "best" not in avail_q:
            avail_q = avail_q + ["best"]
    else:
        avail_q = ["2160p", "1080p", "720p", "480p", "best"]

    q_row = []
    for q_code in avail_q:
        q_label = q_code.upper() if q_code != "best" else "Best"
        check = "✅ " if curr_q == q_code else ""
        q_row.append({"text": f"{check}{q_label}", "callback_data": f"dl:{sid}:q:{q_code}"})

    keyboard_rows = [q_row]

    # --- Audio rows — use real audio tracks if available ---
    if meta and meta.get("audio_codes"):
        avail_langs = meta["audio_codes"]
        # Build rows of up to 4 buttons each
        rows_data = _chunk_list(avail_langs, 4)
        for row_codes in rows_data:
            row = []
            for code in row_codes:
                label = LANG_META.get(code, code.upper())
                check = "✅ " if code in curr_langs else ""
                row.append({"text": f"{check}{label}", "callback_data": f"dl:{sid}:a:{code}"})
            keyboard_rows.append(row)
    else:
        # Fallback: show all standard language buttons
        for row_codes in LANG_ROW_ORDER[:2]:
            row = []
            for code in row_codes:
                label = LANG_META.get(code, code)
                check = "✅ " if code in curr_langs else ""
                row.append({"text": f"{check}{label}", "callback_data": f"dl:{sid}:a:{code}"})
            keyboard_rows.append(row)

    # All / Original + Codec row
    a_misc = []
    for code, label in [("all", "All Audios"), ("orig", "Original")]:
        check = "✅ " if code in curr_langs else ""
        a_misc.append({"text": f"{check}{label}", "callback_data": f"dl:{sid}:a:{code}"})
    c_check_264 = "✅ " if curr_codec.upper() == "H264" else ""
    c_check_265 = "✅ " if curr_codec.upper() == "H265" else ""
    a_misc.append({"text": f"{c_check_264}H264", "callback_data": f"dl:{sid}:c:H264"})
    a_misc.append({"text": f"{c_check_265}H265", "callback_data": f"dl:{sid}:c:H265"})
    keyboard_rows.append(a_misc)

    # --- Episode Selection rows (series mode) ---
    if is_series:
        if meta and meta.get("seasons"):
            # Build dynamic episode buttons from real metadata
            seasons = meta["seasons"]
            ep_counts = meta.get("ep_counts", {})
            for s in seasons:
                ep_total = ep_counts.get(s, 0)
                s_str = f"S{s:02d}"
                ep_btns = [{"text": f"{'✅ ' if curr_wanted == s_str else ''}{s_str} All",
                             "callback_data": f"dl:{sid}:w:{s_str}"}]
                if ep_total > 0:
                    # Individual episode buttons, up to 10 shown
                    for e in range(1, min(ep_total + 1, 11)):
                        code = f"{s_str}E{e:02d}"
                        label = f"E{e:02d}"
                        ep_btns.append({"text": f"{'✅ ' if curr_wanted == code else ''}{label}",
                                        "callback_data": f"dl:{sid}:w:{code}"})
                # Split into rows of 5
                for chunk in _chunk_list(ep_btns, 5):
                    keyboard_rows.append(chunk)
        else:
            # Fallback static episode buttons
            ep_row1 = []
            for ep_code, ep_label in [("all", "All Eps"), ("S01E01", "Ep 1"), ("S01E02", "Ep 2"), ("S01E03", "Ep 3"), ("S01E04", "Ep 4")]:
                check = "✅ " if curr_wanted == ep_code else ""
                ep_row1.append({"text": f"{check}{ep_label}", "callback_data": f"dl:{sid}:w:{ep_code}"})

            ep_row2 = []
            for ep_code, ep_label in [("S01E05", "Ep 5"), ("S01E06", "Ep 6"), ("S01E01-S01E05", "E1-5"), ("S01", "S01 All"), ("S02", "S02 All")]:
                check = "✅ " if curr_wanted == ep_code else ""
                ep_row2.append({"text": f"{check}{ep_label}", "callback_data": f"dl:{sid}:w:{ep_code}"})

            keyboard_rows.extend([ep_row1, ep_row2])

    # Mode Toggle, Service Switch & Fetch Tracks
    series_btn_text = "📺 Series: ON" if is_series else "🎬 Movie (Tap→Series)"
    fetch_btn_text = "🔄 Fetching…" if meta_fetching else ("🔁 Re-fetch Tracks" if meta else "🔍 Fetch Tracks")
    ctrl_row = [
        {"text": series_btn_text, "callback_data": f"dl:{sid}:tseries"},
        {"text": f"🔄 {srv_label}", "callback_data": f"dl:{sid}:ch_srv"},
        {"text": fetch_btn_text, "callback_data": f"dl:{sid}:fetch"},
    ]
    keyboard_rows.append(ctrl_row)

    # Primary Action Buttons
    act_row = [
        {"text": "🚀 Start Download", "callback_data": f"dl:{sid}:start"},
        {"text": "⚡ Auto (Mux+Up)", "callback_data": f"dl:{sid}:auto"},
        {"text": "❌ Cancel", "callback_data": f"dl:{sid}:cancel"}
    ]
    keyboard_rows.append(act_row)

    return {"inline_keyboard": keyboard_rows}


def build_download_text(session):
    srv = session.get("service", "JHS")
    info = SUPPORTED_SERVICES.get(srv, {"name": srv, "icon": "🎬"})
    service_name = info.get("name", srv)
    service_icon = info.get("icon", "🎬")
    content_type = "📺 Series" if session.get("is_series") else "🎬 Movie"
    langs = session.get("audio_langs", [])
    langs_display = ", ".join(langs) if langs else "None"
    extra_str = f"\n⚙️ <b>Extra:</b> <code>{session['extra']}</code>" if session.get("extra") else ""

    eps_str = ""
    if session.get("is_series"):
        w = session.get("wanted", "all")
        w_disp = "All Episodes" if w == "all" else w
        eps_str = f"🎯 <b>Episodes:</b> <code>{w_disp}</code>\n"

    meta = session.get("track_meta")
    fetching = session.get("meta_fetching", False)
    if fetching:
        track_line = "🔍 <i>Fetching track info…</i>"
    elif meta is None:
        track_line = "💡 <i>Tap 🔍 Fetch Tracks to load real audio/quality options</i>"
    else:
        q_avail = ", ".join(meta.get("qualities", [])) or "?"
        a_avail = ", ".join(meta.get("audio_codes", [])) or "?"
        track_line = f"📊 <b>Available:</b> <code>{q_avail}</code> | 🔊 <code>{a_avail}</code>"

    return (
        f"{service_icon} <b>{service_name} ({srv}) Download Setup</b> ({content_type})\n"
        f"🔗 <b>URL:</b> <code>{session['url']}</code>{extra_str}\n\n"
        f"📺 <b>Quality:</b> <code>{session.get('quality', '1080p')}</code>\n"
        f"🎞 <b>Codec:</b> <code>{session.get('vcodec', 'H264')}</code>\n"
        f"🔊 <b>Audio:</b> <code>{langs_display}</code>\n"
        f"{eps_str}"
        f"{track_line}\n"
        f"☁️ <b>Auto Drive Upload:</b> <code>{'ON' if AUTO_DRIVE_UPLOAD else 'OFF'}</code> (<code>{UPLOAD_ENGINE}</code>)\n\n"
        f"<i>Tap buttons below to change Quality, Audio &amp; Episodes:</i>"
    )


def parse_url_and_extra(raw_input):
    tokens = raw_input.strip().split()
    url = ""
    extra_parts = []
    for token in tokens:
        clean = token.strip('"\'')
        if clean.startswith("http://") or clean.startswith("https://"):
            url = clean
        else:
            extra_parts.append(token)
    if not url and tokens:
        url = tokens[0].strip('"\'')
        extra_parts = tokens[1:]
    return url, " ".join(extra_parts)


def _do_fetch_and_refresh(sid, service, url, extra, chat_id):
    """Background thread: fetch track metadata then refresh the Telegram message."""
    sess = pending_downloads.get(sid)
    if not sess:
        return
    meta = fetch_track_metadata(service, url, extra)
    sess = pending_downloads.get(sid)   # re-check after the blocking call
    if not sess:
        return
    sess["track_meta"] = meta           # None means fetch failed / no info
    sess["meta_fetching"] = False

    # Smart defaults: if movie (not series) auto-select only available audio langs
    if meta and meta.get("audio_codes") and not sess.get("is_series"):
        # filter the user's DEFAULT_AUDIO_LANGS to only those actually present
        filtered = [l for l in sess["audio_langs"] if l in meta["audio_codes"]]
        if filtered:
            sess["audio_langs"] = filtered
        elif meta["audio_codes"]:
            # None of the defaults are available; pick the first available
            sess["audio_langs"] = [meta["audio_codes"][0]]

    text = build_download_text(sess)
    kb = build_download_keyboard(sid, sess)
    msg_id = sess.get("message_id")
    if msg_id:
        edit_message(chat_id, msg_id, text, reply_markup=kb)


def initiate_download_setup(chat_id, service, raw_input):
    url, extra = parse_url_and_extra(raw_input)
    if not url:
        send_message(chat_id, "❌ No valid URL provided.")
        return

    is_series = bool(re.search(r'/(shows|tv|series)/', url, re.IGNORECASE))
    wanted = "all"
    if "-w" in extra:
        m = re.search(r'-w\s+([^\s]+)', extra)
        if m:
            wanted = m.group(1)
            extra = extra.replace(m.group(0), "").strip()
            is_series = True

    sid = str(uuid.uuid4())[:8]
    pending_downloads[sid] = {
        "chat_id": chat_id,
        "service": service,
        "url": url,
        "extra": extra,
        "is_series": is_series,
        "wanted": wanted,
        "quality": DEFAULT_QUALITY,
        "vcodec": DEFAULT_VCODEC,
        "audio_langs": list(DEFAULT_AUDIO_LANGS),
        "track_meta": None,
        "meta_fetching": True,          # will auto-fetch in background
        "created_at": time.time()
    }

    # Evict sessions older than 2 hours
    now = time.time()
    stale = [k for k, v in pending_downloads.items() if now - v.get("created_at", 0) > 7200]
    for k in stale:
        pending_downloads.pop(k, None)

    text = build_download_text(pending_downloads[sid])
    keyboard = build_download_keyboard(sid, pending_downloads[sid])
    res = send_message(chat_id, text, reply_markup=keyboard)
    if res and res.get("ok"):
        pending_downloads[sid]["message_id"] = res["result"]["message_id"]

    # Kick off background metadata fetch (non-blocking)
    threading.Thread(
        target=_do_fetch_and_refresh,
        args=(sid, service, url, extra, chat_id),
        daemon=True
    ).start()


def start_session_download(chat_id, session_id, auto_pipeline=False):
    session = pending_downloads.pop(session_id, None)
    if not session:
        send_message(chat_id, "⚠️ <i>Session expired. Please send the link or command again.</i>")
        return

    if active_process and active_process.poll() is None:
        send_message(chat_id, "⚠️ <b>A task is already running!</b> Wait or use <code>/cancel</code>.")
        return

    service = session["service"]
    url = session["url"]
    quality = session["quality"]
    vcodec = session["vcodec"]
    audio_langs = session["audio_langs"]
    extra = session.get("extra", "")
    wanted = session.get("wanted", "all")
    is_series = session.get("is_series", False)

    msg_id = session.get("message_id")
    if msg_id:
        eps_info = f" | Eps: <code>{wanted}</code>" if is_series and wanted != "all" else ""
        edit_message(chat_id, msg_id, f"🚀 <b>Starting Download...</b>\nQuality: <code>{quality}</code> | Audio: <code>{','.join(audio_langs)}</code>{eps_info}\nURL: <code>{url}</code>")

    cmd_parts = ["poetry run vt dl"]
    if quality and quality.lower() != "best":
        cmd_parts.append(f"-q {quality}")
    if vcodec:
        cmd_parts.append(f"-v {vcodec}")
    if audio_langs:
        cmd_parts.append(f"-al {','.join(audio_langs)}")
    if is_series and wanted and wanted.lower() != "all":
        cmd_parts.append(f"-w {wanted}")
    if extra:
        cmd_parts.append(extra)
    cmd_parts.append(f"{service} \"{url}\"")
    full_cmd = " ".join(cmd_parts)

    eps_title = f" [{wanted}]" if is_series and wanted != "all" else ""
    task_name = f"Download ({service}): {quality} [{','.join(audio_langs)}]{eps_title} - {url}"

    if auto_pipeline:
        def step_upload():
            send_message(chat_id, f"🔄 <b>Auto-Pipeline: Step 3/3</b> ➔ Uploading to Google Drive ({UPLOAD_ENGINE})...")
            upload_to_gdrive(chat_id)

        def step_mux():
            send_message(chat_id, "🔄 <b>Auto-Pipeline: Step 2/3</b> ➔ Running Multi-Audio Muxer...")
            run_command_async(chat_id, "python downloads/mux.py -y -s", task_name="Auto-Pipeline: Muxing", on_success_callback=step_upload)

        send_message(chat_id, "🔄 <b>Auto-Pipeline: Step 1/3</b> ➔ Downloading content...")
        run_command_async(chat_id, full_cmd, task_name=f"Auto-Pipeline: {task_name}", on_success_callback=step_mux)
    else:
        pre_snap = snapshot_downloads()
        def on_dl_finish():
            if AUTO_DRIVE_UPLOAD:
                target = find_new_or_updated_target(pre_snap)
                if target:
                    send_message(chat_id, f"☁️ <b>Auto-Upload Triggered:</b>\nTarget: <code>{target.name}</code> ➔ Google Drive ({UPLOAD_ENGINE})")
                    upload_to_gdrive(chat_id, target_name=target.name)

        run_command_async(chat_id, full_cmd, task_name=task_name, on_success_callback=on_dl_finish)


def send_quality_menu(chat_id, message_id=None):
    text = (
        f"📺 <b>Default Quality Setting</b>\n"
        f"Current Default: <b>{DEFAULT_QUALITY}</b>\n\n"
        f"<i>Select your preferred default resolution:</i>"
    )
    row1 = []
    for q_code, q_label in [("2160p", "2160p (4K)"), ("1080p", "1080p (FHD)"), ("720p", "720p (HD)")]:
        check = "✅ " if DEFAULT_QUALITY == q_code else ""
        row1.append({"text": f"{check}{q_label}", "callback_data": f"cfg:q:{q_code}"})

    row2 = []
    for q_code, q_label in [("576p", "576p"), ("480p", "480p (SD)"), ("best", "Best")]:
        check = "✅ " if DEFAULT_QUALITY == q_code else ""
        row2.append({"text": f"{check}{q_label}", "callback_data": f"cfg:q:{q_code}"})

    keyboard = [row1, row2, [{"text": "❌ Close", "callback_data": "cfg:close"}]]
    reply_markup = {"inline_keyboard": keyboard}

    if message_id:
        edit_message(chat_id, message_id, text, reply_markup=reply_markup)
    else:
        send_message(chat_id, text, reply_markup=reply_markup)


def send_audio_menu(chat_id, message_id=None):
    langs_str = ", ".join(DEFAULT_AUDIO_LANGS) if DEFAULT_AUDIO_LANGS else "None"
    text = (
        f"🔊 <b>Default Audio Languages Setting</b>\n"
        f"Current Defaults: <b>{langs_str}</b>\n\n"
        f"<i>Tap buttons to toggle languages ON/OFF:</i>"
    )
    row1 = []
    for code, label in [("hin", "Hindi"), ("tam", "Tamil"), ("tel", "Telugu"), ("eng", "English")]:
        check = "✅ " if code in DEFAULT_AUDIO_LANGS else ""
        row1.append({"text": f"{check}{label}", "callback_data": f"cfg:a:{code}"})

    row2 = []
    for code, label in [("mal", "Malayalam"), ("kan", "Kannada"), ("ben", "Bengali"), ("mar", "Marathi")]:
        check = "✅ " if code in DEFAULT_AUDIO_LANGS else ""
        row2.append({"text": f"{check}{label}", "callback_data": f"cfg:a:{code}"})

    row3 = []
    for code, label in [("all", "All"), ("orig", "Original")]:
        check = "✅ " if code in DEFAULT_AUDIO_LANGS else ""
        row3.append({"text": f"{check}{label}", "callback_data": f"cfg:a:{code}"})
    row3.append({"text": "🔄 Reset", "callback_data": "cfg:a:reset"})
    row3.append({"text": "❌ Close", "callback_data": "cfg:close"})

    reply_markup = {"inline_keyboard": [row1, row2, row3]}
    if message_id:
        edit_message(chat_id, message_id, text, reply_markup=reply_markup)
    else:
        send_message(chat_id, text, reply_markup=reply_markup)


def send_settings_menu(chat_id, message_id=None):
    langs_str = ", ".join(DEFAULT_AUDIO_LANGS) if DEFAULT_AUDIO_LANGS else "None"
    text = (
        f"⚙️ <b>Bot Settings & Preferences</b>\n\n"
        f"📺 <b>Default Quality:</b> <code>{DEFAULT_QUALITY}</code>\n"
        f"🎞 <b>Default Codec:</b> <code>{DEFAULT_VCODEC}</code>\n"
        f"🔊 <b>Default Audio:</b> <code>{langs_str}</code>\n"
        f"☁️ <b>Upload Engine:</b> <code>{UPLOAD_ENGINE}</code>\n"
        f"🎯 <b>Target:</b> <code>{RCLONE_REMOTE if UPLOAD_ENGINE == 'rclone' else detect_gdrive_path()}</code>\n"
        f"⚡ <b>Auto Drive Upload:</b> <code>{'ENABLED' if AUTO_DRIVE_UPLOAD else 'DISABLED'}</code>\n\n"
        f"<i>Tap below to customize your preferences:</i>"
    )
    keyboard = [
        [
            {"text": "📺 Quality Selection", "callback_data": "cfg:menu:quality"},
            {"text": "🔊 Audio Selection", "callback_data": "cfg:menu:audio"}
        ],
        [
            {"text": f"🎞 Codec: {DEFAULT_VCODEC}", "callback_data": "cfg:toggle:codec"},
            {"text": f"⚡ Auto-Up: {'ON' if AUTO_DRIVE_UPLOAD else 'OFF'}", "callback_data": "cfg:toggle:autoup"}
        ],
        [
            {"text": f"☁️ Engine: {UPLOAD_ENGINE}", "callback_data": "cfg:toggle:engine"},
            {"text": "❌ Close", "callback_data": "cfg:close"}
        ]
    ]
    reply_markup = {"inline_keyboard": keyboard}
    if message_id:
        edit_message(chat_id, message_id, text, reply_markup=reply_markup)
    else:
        send_message(chat_id, text, reply_markup=reply_markup)


def handle_callback_query(cb):
    global DEFAULT_QUALITY, DEFAULT_VCODEC, DEFAULT_AUDIO_LANGS, AUTO_DRIVE_UPLOAD, UPLOAD_ENGINE, ADMIN_ID

    cb_id = cb.get("id")
    from_user = cb.get("from", {})
    user_id = from_user.get("id")
    chat_id = cb.get("message", {}).get("chat", {}).get("id")
    message_id = cb.get("message", {}).get("message_id")
    data = cb.get("data", "")

    if ADMIN_ID and user_id != ADMIN_ID:
        answer_callback_query(cb_id, text="⛔ Access Denied.", show_alert=True)
        return

    answer_callback_query(cb_id)

    # 1. Task cancellation
    if data == "task:cancel":
        terminated = terminate_active_process()
        if terminated:
            send_message(chat_id, f"🛑 <b>Task Terminated via button:</b>\n<code>{terminated}</code>")
        else:
            send_message(chat_id, "ℹ️ No task running to cancel.")
        return

    # 2. Download session interactions (dl:<sid>:...)
    if data.startswith("dl:"):
        parts = data.split(":")
        if len(parts) >= 3:
            sid = parts[1]
            action = parts[2]
            session = pending_downloads.get(sid)
            if not session:
                edit_message(chat_id, message_id, "⚠️ <i>This download session has expired. Please send the link again.</i>")
                return

            if action == "q" and len(parts) >= 4:
                session["quality"] = parts[3]
                text = build_download_text(session)
                kb = build_download_keyboard(sid, session)
                edit_message(chat_id, message_id, text, reply_markup=kb)

            elif action == "a" and len(parts) >= 4:
                lang = parts[3]
                curr = session["audio_langs"]
                if lang == "all":
                    session["audio_langs"] = ["all"] if "all" not in curr else list(DEFAULT_AUDIO_LANGS)
                elif lang == "orig":
                    session["audio_langs"] = ["orig"] if "orig" not in curr else list(DEFAULT_AUDIO_LANGS)
                else:
                    if "all" in curr:
                        curr.remove("all")
                    if "orig" in curr:
                        curr.remove("orig")
                    if lang in curr:
                        curr.remove(lang)
                        if not curr:
                            curr.append(lang)
                    else:
                        curr.append(lang)
                text = build_download_text(session)
                kb = build_download_keyboard(sid, session)
                edit_message(chat_id, message_id, text, reply_markup=kb)

            elif action == "c" and len(parts) >= 4:
                session["vcodec"] = parts[3]
                text = build_download_text(session)
                kb = build_download_keyboard(sid, session)
                edit_message(chat_id, message_id, text, reply_markup=kb)

            elif action == "w" and len(parts) >= 4:
                session["wanted"] = parts[3]
                text = build_download_text(session)
                kb = build_download_keyboard(sid, session)
                edit_message(chat_id, message_id, text, reply_markup=kb)

            elif action == "tseries":
                session["is_series"] = not session.get("is_series", False)
                text = build_download_text(session)
                kb = build_download_keyboard(sid, session)
                edit_message(chat_id, message_id, text, reply_markup=kb)

            elif action == "ch_srv":
                kb = build_service_picker_keyboard(sid)
                edit_message(chat_id, message_id, "🏷 <b>Choose Streaming Service:</b>", reply_markup=kb)

            elif action == "back":
                text = build_download_text(session)
                kb = build_download_keyboard(sid, session)
                edit_message(chat_id, message_id, text, reply_markup=kb)

            elif action == "srv" and len(parts) >= 4:
                session["service"] = parts[3]
                text = build_download_text(session)
                kb = build_download_keyboard(sid, session)
                edit_message(chat_id, message_id, text, reply_markup=kb)

            elif action == "start":
                start_session_download(chat_id, sid, auto_pipeline=False)

            elif action == "auto":
                start_session_download(chat_id, sid, auto_pipeline=True)

            elif action == "fetch":
                # Manual (re-)fetch of track metadata
                if session.get("meta_fetching"):
                    answer_callback_query(cb_id, text="⏳ Already fetching…", show_alert=False)
                    return
                session["meta_fetching"] = True
                session["track_meta"] = None
                # Refresh keyboard to show "Fetching…" state
                edit_message(chat_id, message_id, build_download_text(session),
                             reply_markup=build_download_keyboard(sid, session))
                threading.Thread(
                    target=_do_fetch_and_refresh,
                    args=(sid, session["service"], session["url"], session.get("extra", ""), chat_id),
                    daemon=True
                ).start()

            elif action == "list":
                service = session["service"]
                url = session["url"]
                extra = session.get("extra", "")
                cmd = f"poetry run vt dl --list {extra} {service} \"{url}\""
                run_command_async(chat_id, cmd, task_name=f"Inspect Tracks ({service}): {url}")

            elif action == "cancel":
                pending_downloads.pop(sid, None)
                edit_message(chat_id, message_id, "❌ <i>Download setup cancelled.</i>")
        return

    # 3. Settings & Config interactions (cfg:...)
    if data.startswith("cfg:"):
        parts = data.split(":")
        sub = parts[1] if len(parts) > 1 else ""

        if sub == "q" and len(parts) >= 3:
            DEFAULT_QUALITY = parts[2]
            save_settings()
            send_quality_menu(chat_id, message_id)

        elif sub == "a" and len(parts) >= 3:
            lang = parts[2]
            if lang == "reset":
                DEFAULT_AUDIO_LANGS = ["hin", "tam", "tel"]
            elif lang == "all":
                DEFAULT_AUDIO_LANGS = ["all"] if "all" not in DEFAULT_AUDIO_LANGS else ["hin", "tam", "tel"]
            elif lang == "orig":
                DEFAULT_AUDIO_LANGS = ["orig"] if "orig" not in DEFAULT_AUDIO_LANGS else ["hin", "tam", "tel"]
            else:
                if "all" in DEFAULT_AUDIO_LANGS:
                    DEFAULT_AUDIO_LANGS.remove("all")
                if "orig" in DEFAULT_AUDIO_LANGS:
                    DEFAULT_AUDIO_LANGS.remove("orig")
                if lang in DEFAULT_AUDIO_LANGS:
                    DEFAULT_AUDIO_LANGS.remove(lang)
                    if not DEFAULT_AUDIO_LANGS:
                        DEFAULT_AUDIO_LANGS.append(lang)
                else:
                    DEFAULT_AUDIO_LANGS.append(lang)
            save_settings()
            send_audio_menu(chat_id, message_id)

        elif sub == "menu":
            menu_type = parts[2] if len(parts) > 2 else ""
            if menu_type == "quality":
                send_quality_menu(chat_id, message_id)
            elif menu_type == "audio":
                send_audio_menu(chat_id, message_id)

        elif sub == "toggle":
            target = parts[2] if len(parts) > 2 else ""
            if target == "codec":
                DEFAULT_VCODEC = "H265" if DEFAULT_VCODEC.upper() == "H264" else "H264"
                save_settings()
                send_settings_menu(chat_id, message_id)
            elif target == "autoup":
                AUTO_DRIVE_UPLOAD = not AUTO_DRIVE_UPLOAD
                save_settings()
                send_settings_menu(chat_id, message_id)
            elif target == "engine":
                UPLOAD_ENGINE = "robocopy" if UPLOAD_ENGINE == "rclone" else "rclone"
                save_settings()
                send_settings_menu(chat_id, message_id)

        elif sub == "close":
            edit_message(chat_id, message_id, "⚙️ <i>Settings closed.</i>")
        return


def handle_url_input(chat_id, text):
    url = text.strip()
    detected_srv = None
    lower_url = url.lower()
    for srv_code, meta in SUPPORTED_SERVICES.items():
        for d in meta["domains"]:
            if d in lower_url:
                detected_srv = srv_code
                break
        if detected_srv:
            break

    if detected_srv:
        initiate_download_setup(chat_id, detected_srv, url)
    else:
        sid = str(uuid.uuid4())[:8]
        is_series = any(k in lower_url for k in ["/show", "/shows", "/series", "/season", "/tv"])
        pending_downloads[sid] = {
            "chat_id": chat_id,
            "service": "JHS",
            "url": url,
            "extra": "",
            "quality": DEFAULT_QUALITY,
            "vcodec": DEFAULT_VCODEC,
            "audio_langs": list(DEFAULT_AUDIO_LANGS),
            "wanted": "all",
            "is_series": is_series,
            "created_at": time.time()
        }
        text_msg = (
            f"🔗 <b>URL Detected:</b>\n<code>{url}</code>\n\n"
            f"<i>Choose service to download with:</i>"
        )
        res = send_message(chat_id, text_msg, reply_markup=build_service_picker_keyboard(sid))
        if res and res.get("ok"):
            pending_downloads[sid]["message_id"] = res["result"]["message_id"]


# ---------------- Command Handlers ----------------
def handle_command(chat_id, text):
    global active_process, active_cmd, active_start_time, AUTO_DRIVE_UPLOAD, UPLOAD_ENGINE, RCLONE_REMOTE, GDRIVE_DEFAULT_DEST

    parts = text.strip().split(maxsplit=1)
    cmd = parts[0].lower()
    args = parts[1].strip() if len(parts) > 1 else ""

    if cmd in ("/start", "/help"):
        langs_str = ", ".join(DEFAULT_AUDIO_LANGS) if DEFAULT_AUDIO_LANGS else "None"
        help_text = (
            "🤖 <b>Remote Media, Cloud & VPN Controller</b>\n\n"
            "<b>Supported Services:</b>\n"
            "• JioHotstar (<code>/jhs</code>) | Prime Video (<code>/prime</code>) | Netflix (<code>/nf</code>)\n"
            "• Disney+ (<code>/dsnp</code>) | SonyLIV (<code>/sony</code>) | STAGE (<code>/stage</code>)\n"
            "• Zee5 (<code>/zee5</code>) | Apple TV+ (<code>/atv</code>) | Crunchyroll (<code>/cr</code>)\n"
            "• Chaupal (<code>/chau</code>) | SunNXT (<code>/sun</code>) | Aha (<code>/aha</code>)\n"
            "💡 <i>Tip: Paste any URL directly into chat to open interactive buttons!</i>\n\n"
            "<b>🎛 Quality & Audio Controls:</b>\n"
            f"• <code>/quality</code> - Set default video quality (Current: <b>{DEFAULT_QUALITY}</b>)\n"
            f"• <code>/audio</code> - Toggle default audio languages (Current: <b>{langs_str}</b>)\n"
            f"• <code>/settings</code> - Interactive Control Panel & Defaults\n"
            f"  <i>Auto-Upload:</i> {'✅ <b>ON</b>' if AUTO_DRIVE_UPLOAD else '❌ <b>OFF</b>'} (Engine: <code>{UPLOAD_ENGINE}</code>)\n\n"
            "<b>📦 Processing & Storage:</b>\n"
            "• <code>/dl &lt;args&gt;</code> - Custom download via poetry\n"
            "• <code>/mux</code> - Run multi-audio batch muxer (.MULTi.mkv)\n"
            "• <code>/upload [name]</code> - Upload folder/file to Google Drive\n"
            "• <code>/auto &lt;args&gt;</code> - Pipeline: Download ➔ Mux ➔ Drive Upload\n"
            "• <code>/clean</code> - Clean temp cache directory\n\n"
            "<b>🛡 VPN Controls:</b>\n"
            "• <code>/vpn status</code> - Check VPN & Public IP\n"
            "• <code>/vpn ip</code> - Public IP, City, Country & ISP info\n"
            "• <code>/vpn connect [loc]</code> - Connect ExpressVPN (e.g. <code>smart</code>, <code>india</code>)\n"
            "• <code>/vpn disconnect</code> - Disconnect ExpressVPN\n\n"
            "<b>☁️ Storage Settings:</b>\n"
            "• <code>/engine [rclone/robocopy]</code> - Switch upload backend\n"
            "• <code>/setremote &lt;remote:path&gt;</code> - Set Rclone remote\n"
            "• <code>/autoupload [on/off]</code> - Toggle auto-upload after download\n\n"
            "<b>Control:</b>\n"
            "• <code>/status</code> - Check active task with progress bar\n"
            "• <code>/cancel</code> - Terminate currently running task\n"
            "• <code>/list</code> - List files & folders in downloads\n"
            "• <code>/disk</code> - Check local disk space"
        )
        send_message(chat_id, help_text)

    # ---------------- Interactive Quality & Audio Buttons ----------------
    elif cmd in ("/quality", "/res"):
        send_quality_menu(chat_id)

    elif cmd in ("/audio", "/alang", "/lang"):
        send_audio_menu(chat_id)

    elif cmd in ("/settings", "/config", "/pref"):
        send_settings_menu(chat_id)

    # ---------------- Quick Service Downloads ----------------
    elif cmd in (
        "/jhs", "/hotstar",
        "/prime", "/amzn",
        "/nf", "/netflix",
        "/dsnp", "/disney",
        "/sony", "/sliv",
        "/stage",
        "/zee5", "/z5",
        "/atv", "/appletv",
        "/cr", "/crunchyroll",
        "/chau", "/chaupal",
        "/sun", "/sunnxt",
        "/aha",
        "/hch", "/hoichoi"
    ):
        cmd_to_srv = {
            "/jhs": "JHS", "/hotstar": "JHS",
            "/prime": "AMZN", "/amzn": "AMZN",
            "/nf": "NF", "/netflix": "NF",
            "/dsnp": "DSNP", "/disney": "DSNP",
            "/sony": "SLIV", "/sliv": "SLIV",
            "/stage": "STAGE",
            "/zee5": "ZEE5", "/z5": "ZEE5",
            "/atv": "ATV", "/appletv": "ATV",
            "/cr": "CR", "/crunchyroll": "CR",
            "/chau": "CHAU", "/chaupal": "CHAU",
            "/sun": "SUN", "/sunnxt": "SUN",
            "/aha": "AHA",
            "/hch": "HCH", "/hoichoi": "HCH"
        }
        target_srv = cmd_to_srv[cmd]
        info = SUPPORTED_SERVICES.get(target_srv, {"name": target_srv, "icon": "🎬"})
        srv_name = info.get("name", target_srv)

        if not args:
            send_message(
                chat_id,
                f"❌ <i>Please provide {srv_name} URL.</i>\n"
                f"Example: <code>{cmd} &lt;url&gt;</code>\n"
                f"💡 <i>Or simply paste the URL directly into chat!</i>"
            )
            return

        if active_process and active_process.poll() is None:
            send_message(chat_id, "⚠️ <b>A task is already running!</b> Wait or use <code>/cancel</code>.")
            return

        # Immediate bypass flag (-now or -y)
        if "-now" in args or "-y" in args:
            clean_args = args.replace("-now", "").replace("-y", "").strip()
            if "http" in clean_args and not clean_args.startswith("http"):
                full_cmd = f"poetry run vt dl {clean_args} {target_srv}"
            else:
                q_flag = f"-q {DEFAULT_QUALITY}" if DEFAULT_QUALITY and DEFAULT_QUALITY.lower() != "best" else ""
                full_cmd = f"poetry run vt dl {q_flag} -v {DEFAULT_VCODEC} -al {','.join(DEFAULT_AUDIO_LANGS)} {target_srv} {clean_args}".replace("  ", " ")
            pre_snap = snapshot_downloads()
            def on_srv_success():
                if AUTO_DRIVE_UPLOAD:
                    target = find_new_or_updated_target(pre_snap)
                    if target:
                        send_message(chat_id, f"☁️ <b>Auto-Upload Triggered:</b>\nTarget: <code>{target.name}</code> ➔ Google Drive ({UPLOAD_ENGINE})")
                        upload_to_gdrive(chat_id, target_name=target.name)
            run_command_async(chat_id, full_cmd, task_name=f"Download ({target_srv}): {clean_args}", on_success_callback=on_srv_success)
        else:
            initiate_download_setup(chat_id, target_srv, args)

    elif cmd == "/dl":
        if not args:
            send_message(chat_id, "❌ <i>Please provide download command.</i>\nExample: <code>/dl vt dl -v h264 JHS &lt;url&gt;</code>")
            return
        if active_process and active_process.poll() is None:
            send_message(chat_id, "⚠️ <b>A task is already running!</b> Wait or use <code>/cancel</code>.")
            return

        pre_snap = snapshot_downloads()
        full_cmd = f"poetry run {args}"

        def on_dl_success():
            if AUTO_DRIVE_UPLOAD:
                target = find_new_or_updated_target(pre_snap)
                if target:
                    send_message(chat_id, f"☁️ <b>Auto-Upload Triggered:</b>\nTarget: <code>{target.name}</code> ➔ Google Drive ({UPLOAD_ENGINE})")
                    upload_to_gdrive(chat_id, target_name=target.name)
                else:
                    send_message(chat_id, "ℹ️ Download finished, but no new target detected for auto-upload.")

        run_command_async(chat_id, full_cmd, task_name=f"Download: {args}", on_success_callback=on_dl_success)

    elif cmd == "/clean":
        temp_dir = WORKING_DIR / "temp"
        if not temp_dir.exists():
            send_message(chat_id, "ℹ️ Temp folder is already clean.")
            return
        freed = 0
        count = 0
        for root, dirs, files in os.walk(temp_dir, topdown=False):
            for f in files:
                try:
                    fp = Path(root) / f
                    sz = fp.stat().st_size
                    fp.unlink()
                    freed += sz
                    count += 1
                except Exception:
                    pass
            for d in dirs:
                try:
                    Path(root, d).rmdir()
                except Exception:
                    pass
        send_message(chat_id, f"🧹 <b>Temporary Cache Cleaned!</b>\nDeleted: {count} files\nFreed Space: <b>{format_bytes(freed)}</b>")

    # ---------------- VPN Commands ----------------
    elif cmd == "/vpn":
        vpn_args = args.split(maxsplit=1)
        sub_cmd = vpn_args[0].lower() if vpn_args else "status"
        param = vpn_args[1].strip() if len(vpn_args) > 1 else ""

        if sub_cmd == "status":
            status_text = "🛡 <b>VPN & Network Status:</b>\n\n"
            try:
                ip_res = session.get("https://ipinfo.io/json", timeout=4).json()
                status_text += (
                    f"🌐 <b>Public IP:</b> <code>{ip_res.get('ip', 'Unknown')}</code>\n"
                    f"📍 <b>Location:</b> {ip_res.get('city', '')}, {ip_res.get('region', '')} ({ip_res.get('country', '')})\n"
                    f"🏢 <b>ISP / Org:</b> <code>{ip_res.get('org', '')}</code>\n\n"
                )
            except Exception:
                status_text += "🌐 <b>Public IP:</b> <i>(Unable to query ipinfo)</i>\n\n"

            if Path(EXPRESSVPN_PATH).exists():
                try:
                    p = subprocess.run(
                        f'"{EXPRESSVPN_PATH}" status',
                        shell=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        timeout=5
                    )
                    status_text += f"🔐 <b>ExpressVPN Client:</b>\n<pre>{p.stdout.strip()[:1000]}</pre>"
                except Exception:
                    status_text += "⚠️ <i>ExpressVPN status timed out (is GUI app or daemon running?)</i>"
            else:
                status_text += "ℹ️ <i>ExpressVPN CLI not found.</i>"

            send_message(chat_id, status_text)

        elif sub_cmd == "ip":
            try:
                ip_res = session.get("https://ipinfo.io/json", timeout=4).json()
                msg = (
                    f"🌐 <b>Current Public IP Info:</b>\n"
                    f"• <b>IP:</b> <code>{ip_res.get('ip')}</code>\n"
                    f"• <b>Country:</b> {ip_res.get('country')}\n"
                    f"• <b>City:</b> {ip_res.get('city')}, {ip_res.get('region')}\n"
                    f"• <b>ISP / Org:</b> <code>{ip_res.get('org')}</code>"
                )
                send_message(chat_id, msg)
            except Exception as e:
                send_message(chat_id, f"⚠️ Failed to fetch IP: {e}")

        elif sub_cmd == "connect":
            region = param if param else "smart"
            if not Path(EXPRESSVPN_PATH).exists():
                send_message(chat_id, f"❌ ExpressVPN CLI not found at <code>{EXPRESSVPN_PATH}</code>")
                return
            send_message(chat_id, f"🔄 <b>Connecting ExpressVPN to:</b> <code>{region}</code>...")
            connect_cmd = f'"{EXPRESSVPN_PATH}" connect "{region}"'
            run_command_async(chat_id, connect_cmd, task_name=f"VPN Connect: {region}")

        elif sub_cmd == "disconnect":
            if not Path(EXPRESSVPN_PATH).exists():
                send_message(chat_id, f"❌ ExpressVPN CLI not found at <code>{EXPRESSVPN_PATH}</code>")
                return
            send_message(chat_id, "🔄 <b>Disconnecting ExpressVPN...</b>")
            disconnect_cmd = f'"{EXPRESSVPN_PATH}" disconnect'
            run_command_async(chat_id, disconnect_cmd, task_name="VPN Disconnect")

        else:
            send_message(chat_id, "❓ Usage: <code>/vpn status</code>, <code>/vpn ip</code>, <code>/vpn connect [location]</code>, <code>/vpn disconnect</code>")

    # ---------------- Upload Engine Settings ----------------
    elif cmd == "/engine":
        if args.lower() in ("rclone", "rcl"):
            UPLOAD_ENGINE = "rclone"
            send_message(chat_id, f"✅ <b>Upload engine switched to:</b> <code>rclone</code>\nRemote: <code>{RCLONE_REMOTE}</code>")
        elif args.lower() in ("robocopy", "robo"):
            UPLOAD_ENGINE = "robocopy"
            send_message(chat_id, f"✅ <b>Upload engine switched to:</b> <code>robocopy</code>\nDest: <code>{detect_gdrive_path()}</code>")
        else:
            send_message(
                chat_id,
                f"ℹ️ <b>Current Upload Engine:</b> <code>{UPLOAD_ENGINE}</code>\n"
                f"• Rclone Remote: <code>{RCLONE_REMOTE}</code>\n"
                f"• Robocopy Path: <code>{detect_gdrive_path()}</code>\n\n"
                f"To change: <code>/engine rclone</code> or <code>/engine robocopy</code>"
            )

    elif cmd == "/setremote":
        if not args:
            send_message(chat_id, f"☁️ <b>Current Rclone Remote:</b> <code>{RCLONE_REMOTE}</code>\n\nUsage: <code>/setremote gdrive:TV Shows</code>")
            return
        RCLONE_REMOTE = args.strip()
        send_message(chat_id, f"✅ <b>Rclone Remote updated to:</b> <code>{RCLONE_REMOTE}</code>")

    elif cmd == "/autoupload":
        if args.lower() in ("on", "true", "1", "enable"):
            AUTO_DRIVE_UPLOAD = True
            send_message(chat_id, f"✅ <b>Direct Google Drive upload is ENABLED!</b>\nEngine: <code>{UPLOAD_ENGINE}</code>")
        elif args.lower() in ("off", "false", "0", "disable"):
            AUTO_DRIVE_UPLOAD = False
            send_message(chat_id, "⏸ <b>Direct Google Drive upload is DISABLED.</b>")
        else:
            status_str = f"ENABLED (via {UPLOAD_ENGINE})" if AUTO_DRIVE_UPLOAD else "DISABLED"
            send_message(chat_id, f"ℹ️ <b>Auto Drive Upload:</b> <code>{status_str}</code>\nToggle with: <code>/autoupload on</code> or <code>/autoupload off</code>")

    elif cmd == "/setgdrive":
        if not args:
            send_message(chat_id, f"☁️ <b>Current Robocopy GDrive Path:</b>\n<code>{detect_gdrive_path()}</code>\n\nUsage: <code>/setgdrive G:\\My Drive\\TV Shows</code>")
            return
        GDRIVE_DEFAULT_DEST = args.strip()
        send_message(chat_id, f"✅ <b>Robocopy destination updated to:</b>\n<code>{GDRIVE_DEFAULT_DEST}</code>")

    elif cmd == "/status":
        with process_lock:
            if active_process and active_process.poll() is None:
                elapsed = int(time.time() - (active_start_time or time.time()))
                m, s = divmod(elapsed, 60)
                send_message(
                    chat_id,
                    f"⚙️ <b>Active Task Running:</b> ({m}m {s}s)\n<code>{active_cmd}</code>"
                )
            else:
                send_message(chat_id, "💤 <b>Idle:</b> No active tasks running.")

    elif cmd in ("/cancel", "/kill", "/stop"):
        terminated = terminate_active_process()
        if terminated:
            send_message(chat_id, f"🛑 <b>Task Terminated:</b>\n<code>{terminated}</code>")
        else:
            send_message(chat_id, "ℹ️ No task running to cancel.")

    elif cmd == "/mux":
        if active_process and active_process.poll() is None:
            send_message(chat_id, "⚠️ <b>A task is already running!</b> Wait or use <code>/cancel</code>.")
            return
        mux_cmd = "python downloads/mux.py -y -s"
        run_command_async(chat_id, mux_cmd, task_name="Batch Multi-Audio Muxer")

    elif cmd == "/upload":
        upload_to_gdrive(chat_id, target_name=args if args else None)

    elif cmd == "/auto":
        if not args:
            send_message(chat_id, "❌ <i>Provide download args for /auto.</i>\nExample: <code>/auto vt dl -v h264 JHS &lt;url&gt;</code>")
            return
        if active_process and active_process.poll() is None:
            send_message(chat_id, "⚠️ <b>A task is already running!</b> Wait or use <code>/cancel</code>.")
            return

        def step_upload():
            send_message(chat_id, f"🔄 <b>Auto-Pipeline: Step 3/3</b> ➔ Uploading to Google Drive ({UPLOAD_ENGINE})...")
            upload_to_gdrive(chat_id)

        def step_mux():
            send_message(chat_id, "🔄 <b>Auto-Pipeline: Step 2/3</b> ➔ Running Multi-Audio Muxer...")
            run_command_async(chat_id, "python downloads/mux.py -y -s", task_name="Auto-Pipeline: Muxing", on_success_callback=step_upload)

        send_message(chat_id, "🔄 <b>Auto-Pipeline: Step 1/3</b> ➔ Downloading content...")
        run_command_async(chat_id, f"poetry run {args}", task_name=f"Auto-Pipeline: Download {args}", on_success_callback=step_mux)

    elif cmd == "/cmd":
        if not args:
            send_message(chat_id, "❌ Provide command to run. Example: <code>/cmd dir</code>")
            return
        if active_process and active_process.poll() is None:
            send_message(chat_id, "⚠️ <b>A task is already running!</b> Wait or use <code>/cancel</code>.")
            return
        run_command_async(chat_id, args, task_name=f"CLI: {args}")

    elif cmd == "/log":
        logs_dir = WORKING_DIR / "vinetrimmer" / "logs"
        if not logs_dir.exists():
            send_message(chat_id, "❌ Logs folder not found.")
            return
        log_files = sorted(logs_dir.glob("*.log"), key=os.path.getmtime, reverse=True)
        if not log_files:
            send_message(chat_id, "❌ No log files found.")
            return

        latest = log_files[0]
        try:
            with open(latest, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
            chunk = "".join(lines[-25:]).strip()
            if len(chunk) > 3500:
                chunk = chunk[-3500:]
            send_message(chat_id, f"📄 <b>Latest Log: {latest.name}</b>\n<pre>{chunk}</pre>")
        except Exception as e:
            send_message(chat_id, f"⚠️ Failed to read log: {e}")

    elif cmd == "/disk":
        msg = "💾 <b>Disk Space Overview:</b>\n"
        for letter in ["C", "D", "G", "H"]:
            root = f"{letter}:\\"
            if os.path.exists(root):
                try:
                    usage = shutil.disk_usage(root)
                    msg += f"• <b>{letter}: Drive:</b> Free: {format_bytes(usage.free)} / Total: {format_bytes(usage.total)}\n"
                except Exception:
                    pass
        msg += f"\n☁️ <b>Active Upload Engine:</b> <code>{UPLOAD_ENGINE}</code>"
        if UPLOAD_ENGINE == "rclone":
            msg += f"\n🎯 <b>Rclone Remote:</b> <code>{RCLONE_REMOTE}</code>"
        else:
            msg += f"\n🎯 <b>Robocopy Path:</b> <code>{detect_gdrive_path()}</code>"
        msg += f"\n⚡ <b>Auto Drive Upload:</b> {'ON' if AUTO_DRIVE_UPLOAD else 'OFF'}"
        send_message(chat_id, msg)

    elif cmd == "/list":
        if not DOWNLOADS_DIR.exists():
            send_message(chat_id, "❌ Downloads folder does not exist.")
            return
        ignored = {"__pycache__", "mux.py", "mux.py.bak"}
        items = [p for p in DOWNLOADS_DIR.iterdir() if not p.name.startswith(".") and p.name not in ignored]
        if not items:
            send_message(chat_id, "📂 Downloads folder is empty.")
            return
        msg = "📂 <b>Downloads Folder:</b>\n"
        for p in sorted(items, key=lambda x: x.stat().st_mtime, reverse=True)[:15]:
            if p.is_dir():
                size = sum(f.stat().st_size for f in p.rglob('*') if f.is_file())
                msg += f"📁 <code>{p.name}</code> ({format_bytes(size)})\n"
            else:
                msg += f"📄 <code>{p.name}</code> ({format_bytes(p.stat().st_size)})\n"
        send_message(chat_id, msg)

    else:
        send_message(chat_id, f"❓ Unknown command: <code>{cmd}</code>. Type /help to see all commands.")


def setup_bot_commands():
    """Register commands with Telegram menu."""
    commands = [
        {"command": "jhs", "description": "Download JioHotstar title"},
        {"command": "prime", "description": "Download Amazon Prime title"},
        {"command": "quality", "description": "Select default video quality (Buttons)"},
        {"command": "audio", "description": "Select default audio languages (Buttons)"},
        {"command": "settings", "description": "Configure bot settings & buttons"},
        {"command": "dl", "description": "Custom Download: /dl vt dl ..."},
        {"command": "mux", "description": "Batch Mux Multi-Audio (.MULTi.mkv)"},
        {"command": "upload", "description": "Upload folder/file to Google Drive"},
        {"command": "auto", "description": "Pipeline: Download -> Mux -> Drive"},
        {"command": "vpn", "description": "VPN: status | ip | connect | disconnect"},
        {"command": "status", "description": "Check active task & progress"},
        {"command": "cancel", "description": "Stop / Kill active task"},
        {"command": "list", "description": "List files & folders in downloads"},
        {"command": "clean", "description": "Clean temporary cache files"},
        {"command": "disk", "description": "Check local & cloud disk space"},
        {"command": "engine", "description": "Switch upload engine (rclone / robocopy)"},
        {"command": "autoupload", "description": "Toggle auto Google Drive upload"},
        {"command": "log", "description": "View latest log file"},
        {"command": "help", "description": "Show help documentation"}
    ]
    try:
        url = f"{TELEGRAM_API}/setMyCommands"
        res = session.post(url, json={"commands": commands}, timeout=10)
        if res.status_code == 200 and res.json().get("ok"):
            print("[+] Telegram Bot Menu Commands registered successfully!")
            return True
    except Exception as e:
        print(f"[-] Failed to register commands: {e}")
    return False


# ---------------- Long Polling Loop ----------------
def run_bot():
    global ADMIN_ID
    load_settings()
    print("=" * 60)
    print("⚡ Ultra-Fast Telegram Media & Cloud Controller Started")
    print(f"📁 Working Directory: {WORKING_DIR}")
    print(f"🔒 Admin User ID:     {ADMIN_ID if ADMIN_ID else 'NOT SET (Waiting for /start)'}")
    print(f"📺 Default Quality:   {DEFAULT_QUALITY}")
    print(f"🔊 Default Audio:     {','.join(DEFAULT_AUDIO_LANGS)}")
    print(f"☁️ Upload Engine:     {UPLOAD_ENGINE} ({RCLONE_REMOTE if UPLOAD_ENGINE == 'rclone' else GDRIVE_DEFAULT_DEST})")
    print(f"⚡ Auto Drive Upload: {'ENABLED' if AUTO_DRIVE_UPLOAD else 'DISABLED'}")
    print("=" * 60)

    if BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN_HERE" or not BOT_TOKEN:
        print("\n[!] ERROR: Please open bot.py and paste your BOT_TOKEN from @BotFather!")
        return

    setup_bot_commands()

    offset = 0

    while True:
        try:
            url = f"{TELEGRAM_API}/getUpdates"
            params = {"offset": offset, "timeout": 20}
            res = session.get(url, params=params, timeout=25)

            if res.status_code != 200:
                time.sleep(2)
                continue

            data = res.json()
            if not data.get("ok"):
                time.sleep(2)
                continue

            for update in data.get("result", []):
                offset = update["update_id"] + 1

                # 1. Handle Inline Button Clicks (callback_query)
                cb = update.get("callback_query")
                if cb:
                    from_user = cb.get("from", {})
                    cb_user_id = from_user.get("id")
                    cb_user_name = from_user.get("username") or from_user.get("first_name", "User")
                    if ADMIN_ID and cb_user_id != ADMIN_ID:
                        print(f"[-] Unauthorized callback attempt by {cb_user_name} (ID: {cb_user_id})")
                        answer_callback_query(cb.get("id"), text="⛔ Access Denied.", show_alert=True)
                        continue
                    threading.Thread(target=handle_callback_query, args=(cb,), daemon=True).start()
                    continue

                # 2. Handle Text Messages
                msg = update.get("message")
                if not msg or "text" not in msg:
                    continue

                user_id = msg["from"]["id"]
                user_name = msg["from"].get("username") or msg["from"].get("first_name", "User")
                chat_id = msg["chat"]["id"]
                text = msg["text"].strip()

                # Auto-assign ADMIN_ID on first /start if not set
                if ADMIN_ID == 0 and text.startswith("/start"):
                    ADMIN_ID = user_id
                    save_settings()
                    print(f"\n[+] Admin automatically bound to Telegram User ID: {ADMIN_ID} (@{user_name})")
                    send_message(chat_id, f"👑 <b>Admin Registered!</b>\nYour User ID: <code>{ADMIN_ID}</code> is now the authorized master.")

                # Security check
                if user_id != ADMIN_ID:
                    print(f"[-] Unauthorized attempt by {user_name} (ID: {user_id}): {text}")
                    send_message(chat_id, "⛔ <b>Access Denied.</b> You are not authorized to control this server.")
                    continue

                # Direct URL recognition -> open interactive buttons
                if text.startswith("http://") or text.startswith("https://"):
                    print(f"[+] URL received from @{user_name}: {text}")
                    threading.Thread(target=handle_url_input, args=(chat_id, text), daemon=True).start()
                    continue

                print(f"[+] Command received from @{user_name}: {text}")
                # Asynchronous instantaneous dispatch: never blocks polling loop
                threading.Thread(target=handle_command, args=(chat_id, text), daemon=True).start()

        except requests.exceptions.RequestException:
            time.sleep(1)
        except Exception as e:
            print(f"[!] Polling loop error: {e}")
            time.sleep(1)

        except requests.exceptions.RequestException:
            time.sleep(1)
        except Exception as e:
            print(f"[!] Polling loop error: {e}")
            time.sleep(1)


if __name__ == "__main__":
    run_bot()
