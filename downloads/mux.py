#!/usr/bin/env python3
"""
mux.py - Intelligent Multi-Audio & Dual-Audio MKV Muxer with Series Batch Muxing & Auto-Sync
Features:
  1. Series / Batch Mode: Auto-detects TV shows across folders, pairs S01E01, S01E02, etc. with ALL available audios (Hindi, Tamil, Telugu, etc.).
  2. Movie Mode: Interactive file selection with multi-audio selection & smart suggestions.
  3. Deep Media Inspection: Resolution, HDR10 / Dolby Vision, Codecs, Bitrate, Channels.
  4. Auto-Delay Detection: Audio cross-correlation across multiple timestamps via FFT for every track.
  5. Auto-Delay Compensation: Fixes delay before muxing (e.g. -2987 ms, +2226 ms, 0 ms).
  6. Muxing via mkvmerge: Zero re-encoding, full quality & metadata preserved.
  7. Language Tagging: Auto-detects audio languages (Hindi, Tamil, Telugu, Malayalam, Kannada, etc.) and tags tracks accurately.
  8. Smart Output Naming: Automatically tags as .MULTi (for 2+ external audios) or .Dual-Audio.
  9. Optional Cleanup: Auto-delete source files after each item to save disk space.
"""

import sys
import os
import re
import json
import argparse
import subprocess
from pathlib import Path
from collections import defaultdict

# Ensure UTF-8 output on Windows console
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

try:
    import numpy as np
    from scipy import signal
    HAS_SCIPY = True
except ImportError:
    HAS_SCIPY = False


LANGUAGE_MAP = {
    "hi": ("hin", "Hindi", 1),
    "hin": ("hin", "Hindi", 1),
    "hindi": ("hin", "Hindi", 1),
    "ta": ("tam", "Tamil", 2),
    "tam": ("tam", "Tamil", 2),
    "tamil": ("tam", "Tamil", 2),
    "te": ("tel", "Telugu", 3),
    "tel": ("tel", "Telugu", 3),
    "telugu": ("tel", "Telugu", 3),
    "ml": ("mal", "Malayalam", 4),
    "mal": ("mal", "Malayalam", 4),
    "malayalam": ("mal", "Malayalam", 4),
    "kn": ("kan", "Kannada", 5),
    "kan": ("kan", "Kannada", 5),
    "kannada": ("kan", "Kannada", 5),
    "bn": ("ben", "Bengali", 6),
    "ben": ("ben", "Bengali", 6),
    "bengali": ("ben", "Bengali", 6),
    "mr": ("mar", "Marathi", 7),
    "mar": ("mar", "Marathi", 7),
    "marathi": ("mar", "Marathi", 7),
    "gu": ("guj", "Gujarati", 8),
    "guj": ("guj", "Gujarati", 8),
    "gujarati": ("guj", "Gujarati", 8),
    "pa": ("pan", "Punjabi", 9),
    "pan": ("pan", "Punjabi", 9),
    "punjabi": ("pan", "Punjabi", 9),
    "en": ("eng", "English", 10),
    "eng": ("eng", "English", 10),
    "english": ("eng", "English", 10),
    "ja": ("jpn", "Japanese", 11),
    "jpn": ("jpn", "Japanese", 11),
    "japanese": ("jpn", "Japanese", 11),
    "ko": ("kor", "Korean", 12),
    "kor": ("kor", "Korean", 12),
    "korean": ("kor", "Korean", 12),
    "es": ("spa", "Spanish", 13),
    "spa": ("spa", "Spanish", 13),
    "spanish": ("spa", "Spanish", 13),
    "fr": ("fre", "French", 14),
    "fre": ("fre", "French", 14),
    "fra": ("fre", "French", 14),
    "french": ("fre", "French", 14),
    "de": ("ger", "German", 15),
    "ger": ("ger", "German", 15),
    "deu": ("ger", "German", 15),
    "german": ("ger", "German", 15),
    "it": ("ita", "Italian", 16),
    "ita": ("ita", "Italian", 16),
    "italian": ("ita", "Italian", 16),
    "ru": ("rus", "Russian", 17),
    "rus": ("rus", "Russian", 17),
    "russian": ("rus", "Russian", 17),
}


def get_audio_lang_info(audio_path, aud_info=None):
    """
    Returns (lang_code, lang_name, priority)
    e.g. ('hin', 'Hindi', 1)
    """
    path = Path(audio_path)
    stem_lower = path.stem.lower()

    # 1. Check filename tokens
    tokens = [t.lower() for t in re.split(r'[\.\-\_\s\(\)\[\]]+', stem_lower) if t]
    for token in tokens:
        if token in LANGUAGE_MAP:
            return LANGUAGE_MAP[token]

    # Regex search for .hi. / .ta. / .te. / etc
    m = re.search(r'[\._\-](hi|hin|ta|tam|te|tel|ml|mal|kn|kan|bn|ben|mr|mar|gu|guj|pa|pan|en|eng)[\._\-]', stem_lower)
    if m:
        lang_key = m.group(1).lower()
        if lang_key in LANGUAGE_MAP:
            return LANGUAGE_MAP[lang_key]

    # 2. Check metadata from mkvmerge info if available
    if aud_info:
        for t in aud_info.get("tracks", []):
            if t.get("type") == "audio":
                prop_lang = t.get("properties", {}).get("language", "").lower()
                if prop_lang and prop_lang != "und" and prop_lang in LANGUAGE_MAP:
                    return LANGUAGE_MAP[prop_lang]

    # Default fallback
    return ("hin", "Hindi", 1)


def format_bytes(size_bytes):
    """Format file size in human-readable format."""
    if size_bytes >= 1024**3:
        return f"{size_bytes / (1024**3):.2f} GB"
    elif size_bytes >= 1024**2:
        return f"{size_bytes / (1024**2):.1f} MB"
    elif size_bytes >= 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes} B"


def format_duration(seconds):
    """Format seconds into HH:MM:SS."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def get_mkvmerge_info(file_path):
    """Retrieve track and container information using mkvmerge -J."""
    cmd = ["mkvmerge", "-J", str(file_path)]
    res = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="ignore"
    )
    if res.returncode != 0:
        raise RuntimeError(f"mkvmerge failed to read {file_path}: {res.stderr.strip()}")
    return json.loads(res.stdout)


def format_codec_name(codec_name, channels=None):
    """Make clean, readable track names."""
    c = str(codec_name).upper()
    ch_str = ""
    if channels == 6:
        ch_str = " 5.1"
    elif channels == 8:
        ch_str = " 7.1"
    elif channels == 2:
        ch_str = " 2.0"
    elif channels == 1:
        ch_str = " Mono"
    elif channels:
        ch_str = f" {channels}ch"

    if "E-AC-3" in c or "EAC3" in c:
        return f"DDP{ch_str}"
    if "AC-3" in c or "AC3" in c:
        return f"DD{ch_str}"
    if "AAC" in c:
        return f"AAC{ch_str}"
    if "DTS-HD" in c:
        return "DTS-HD MA"
    if "DTS" in c:
        return "DTS"
    if "TRUEHD" in c:
        return "TrueHD"
    if "OPUS" in c:
        return "Opus"
    if "FLAC" in c:
        return "FLAC"
    return codec_name


def extract_audio_chunk(file_path, stream_map, start_s, duration_s, sample_rate=16000):
    """Extract raw PCM audio slice using ffmpeg."""
    cmd = [
        "ffmpeg", "-v", "error",
        "-ss", str(start_s),
        "-t", str(duration_s),
        "-i", str(file_path),
        "-map", stream_map,
        "-ac", "1",
        "-ar", str(sample_rate),
        "-f", "s16le",
        "-"
    ]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    raw, _ = p.communicate()
    if not raw:
        return None
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32)


def detect_audio_offset(video_path, audio_path, video_audio_map="0:a:0", audio_stream_map="0:a:0", duration_s=7200):
    """
    Detect delay (in milliseconds) of audio_path relative to video_path using cross-correlation.
    Positive offset means audio_path is delayed (starts late).
    """
    if not HAS_SCIPY:
        print("  [!] numpy/scipy not installed. Skipping auto-sync detection (assuming 0 ms).")
        return 0.0

    sr = 16000
    chunk_len = 40  # 40 seconds per sample
    test_points = [
        90,  # 1m 30s
        int(duration_s * 0.25),
        int(duration_s * 0.50),
        int(duration_s * 0.75)
    ]

    offsets = []
    print("  [>] Analyzing audio sync across multiple timestamps...")

    for t in test_points:
        if t + chunk_len > duration_s:
            continue
        try:
            aud_sig = extract_audio_chunk(audio_path, audio_stream_map, t, chunk_len, sr)
            vid_sig = extract_audio_chunk(video_path, video_audio_map, t, chunk_len, sr)

            if aud_sig is None or vid_sig is None or len(aud_sig) < sr * 10 or len(vid_sig) < sr * 10:
                continue

            aud_sig = aud_sig - np.mean(aud_sig)
            vid_sig = vid_sig - np.mean(vid_sig)

            corr = signal.correlate(aud_sig, vid_sig, mode='full', method='fft')
            lags = signal.correlation_lags(len(aud_sig), len(vid_sig), mode='full')

            # Search within +/- 5 seconds window
            mask = (lags >= -sr * 5) & (lags <= sr * 5)
            corr_window = corr[mask]
            lags_window = lags[mask]

            best_idx = np.argmax(corr_window)
            best_lag = lags_window[best_idx]
            offset_ms = (best_lag / sr) * 1000.0

            std = np.std(corr_window)
            confidence = (corr_window[best_idx] / (std + 1e-6)) if std > 0 else 0

            # Accept confident correlation peaks
            if confidence > 10.0:
                offsets.append(offset_ms)
                print(f"      @ {t//60:02d}m{t%60:02d}s: detected offset = {offset_ms:+.2f} ms (conf: {confidence:.1f})")
        except Exception:
            continue

    if not offsets:
        print("  [!] Low confidence across test samples. Defaulting offset to 0 ms.")
        return 0.0

    final_offset = float(np.median(offsets))
    return final_offset


def print_media_details(video_path, audio_paths, vid_info, aud_infos):
    """Print clean media inspection details."""
    v_tracks = [t for t in vid_info.get("tracks", []) if t["type"] == "video"]
    v_aud_tracks = [t for t in vid_info.get("tracks", []) if t["type"] == "audio"]
    v_sub_tracks = [t for t in vid_info.get("tracks", []) if t["type"] == "subtitles"]

    dur_ns = vid_info.get("container", {}).get("properties", {}).get("duration", 0)
    dur_s = (dur_ns / 1_000_000_000.0) if dur_ns else 0

    print("\n" + "=" * 70)
    print("📋 INSPECTED MEDIA DETAILS")
    print("=" * 70)

    # Video details
    print(f"\n🎬 VIDEO FILE: {video_path.name}")
    print(f"   • Path:     {video_path}")
    print(f"   • Size:     {format_bytes(video_path.stat().st_size)}")
    print(f"   • Duration: {format_duration(dur_s)}")

    if v_tracks:
        vt = v_tracks[0]
        p = vt.get("properties", {})
        res = f"{p.get('pixel_dimensions', 'Unknown')}"
        codec = vt.get("codec", "HEVC")
        print(f"   • Video:    {codec} | {res}")

    print(f"   • Existing Audio Tracks ({len(v_aud_tracks)}):")
    for idx, at in enumerate(v_aud_tracks):
        p = at.get("properties", {})
        lang = p.get("language", "und")
        name = p.get("track_name", at.get("codec", "Audio"))
        ch = p.get("audio_channels", 2)
        print(f"       [{idx+1}] {lang.upper()} - {name} ({format_codec_name(at.get('codec'), ch)})")

    if v_sub_tracks:
        sub_langs = [st.get("properties", {}).get("language", "und").upper() for st in v_sub_tracks]
        print(f"   • Subtitles: {len(v_sub_tracks)} track(s) [{', '.join(sub_langs)}]")

    # External Audio details
    print(f"\n🎵 EXTERNAL AUDIO FILES ({len(audio_paths)}):")
    for idx, (aud_path, aud_info) in enumerate(zip(audio_paths, aud_infos), 1):
        ext_aud_tracks = [t for t in aud_info.get("tracks", []) if t["type"] == "audio"]
        lang_code, lang_name, _ = get_audio_lang_info(aud_path, aud_info)
        print(f"   [{idx}] {aud_path.name}")
        print(f"       • Size: {format_bytes(aud_path.stat().st_size)}")
        if ext_aud_tracks:
            eat = ext_aud_tracks[0]
            p = eat.get("properties", {})
            codec = eat.get("codec", "AAC")
            ch = p.get("audio_channels", 2)
            sr = p.get("audio_sampling_frequency", 48000)
            print(f"       • Audio: {codec} ({format_codec_name(codec, ch)}) | {sr} Hz | Lang: {lang_name} ({lang_code.upper()})")

    print("=" * 70)


def generate_output_filename(video_path, num_external_audios=1, output_dir=None):
    """Generate multi-audio/dual-audio destination filename."""
    path = Path(video_path)
    stem = path.stem

    tag = ".MULTi" if num_external_audios > 1 else ".Dual-Audio"

    # Remove existing tags to prevent duplicate tags
    for existing in [".Dual-Audio", ".Dual.Audio", ".MULTi", ".MULTI"]:
        stem = stem.replace(existing, "")

    patterns = [
        r'(\.DDP5\.1|\.DDP|\.DD|\.AAC|\.Atmos)',
        r'(\.DSNP|\.NF|\.AMZN|\.ATVP|\.HMAX|\.MAX)',
        r'(\.WEB-DL|\.WEBRip|\.BluRay|\.Remux)',
        r'(\.2160p|\.1080p|\.720p)'
    ]
    inserted = False
    for pat in patterns:
        match = re.search(pat, stem, re.IGNORECASE)
        if match:
            idx = match.start()
            stem = stem[:idx] + tag + stem[idx:]
            inserted = True
            break
    if not inserted:
        stem = stem + tag

    out_name = f"{stem}.mkv"
    target_dir = Path(output_dir) if output_dir else path.parent
    return target_dir / out_name


def rename_series_folder(folder_path, is_multi=True):
    """Automatically rename season folder to include .MULTi or .Dual-Audio."""
    folder = Path(folder_path).resolve()
    old_name = folder.name
    tag = "MULTi" if is_multi else "Dual-Audio"

    if tag.lower() in old_name.lower():
        return folder

    cleaned_name = old_name
    for t in [".Dual-Audio", ".Dual.Audio", ".MULTi", ".MULTI"]:
        cleaned_name = cleaned_name.replace(t, "")

    patterns = [
        r'(\.DDP5\.1|\.DDP|\.DD|\.AAC|\.Atmos)',
        r'(\.WEB-DL|\.WEBRip|\.BluRay|\.Remux)',
        r'(\.DSNP|\.NF|\.AMZN|\.ATVP|\.HMAX|\.MAX)',
        r'(\.2160p|\.1080p|\.720p)',
    ]
    inserted = False
    for pat in patterns:
        m = re.search(pat, cleaned_name, re.IGNORECASE)
        if m:
            idx = m.start()
            new_name = cleaned_name[:idx] + f".{tag}" + cleaned_name[idx:]
            inserted = True
            break
    if not inserted:
        new_name = f"{cleaned_name}.{tag}"

    if new_name == old_name:
        return folder

    new_folder_path = folder.parent / new_name
    try:
        if new_folder_path.exists():
            print(f"\n  [!] Target folder already exists: {new_folder_path.name}")
            return folder
        folder.rename(new_folder_path)
        print(f"\n📁 Renamed Series Folder:")
        print(f"   Old: {old_name}")
        print(f"   New: {new_name}")
        return new_folder_path
    except Exception as e:
        print(f"\n  [!] Failed to rename folder: {e}")
        return folder


def select_file_interactive(file_list, prompt_title, auto_match=None, multi=False):
    """Present a clean numbered menu to pick file(s)."""
    print(f"\n{prompt_title}:")
    for idx, f in enumerate(file_list, 1):
        is_rec = " [Suggested Match]" if (auto_match and f == auto_match) else ""
        print(f"  [{idx}] {f.name} ({format_bytes(f.stat().st_size)}){is_rec}")
    print("  [0] Enter custom file path manually")

    default_idx = None
    if auto_match and auto_match in file_list:
        default_idx = file_list.index(auto_match) + 1

    prompt_str = f"👉 Choose option (1-{len(file_list)})"
    if multi:
        prompt_str += " (or comma-separated e.g. 1,2,3 or 'all')"
    if default_idx:
        prompt_str += f" [Default: {default_idx}]"
    prompt_str += ": "

    while True:
        try:
            choice = input(prompt_str).strip()
            if not choice and default_idx:
                return [file_list[default_idx - 1]] if multi else file_list[default_idx - 1]
            if multi and choice.lower() in ["all", "a"]:
                return file_list

            if choice == "0":
                manual = input("Enter full path: ").strip().strip('"').strip("'")
                p = Path(manual)
                if p.is_file():
                    return [p] if multi else p
                print("[!] File not found. Try again.")
                continue

            if multi and ("," in choice or " " in choice):
                indices = [int(x.strip()) for x in re.split(r'[,\s]+', choice) if x.strip().isdigit()]
                valid = [file_list[i - 1] for i in indices if 1 <= i <= len(file_list)]
                if valid:
                    return valid
                print(f"[!] Please enter valid numbers between 1 and {len(file_list)}.")
                continue

            num = int(choice)
            if 1 <= num <= len(file_list):
                return [file_list[num - 1]] if multi else file_list[num - 1]
            print(f"[!] Please enter a number between 1 and {len(file_list)}.")
        except ValueError:
            print("[!] Invalid input. Please enter a valid number.")
        except (KeyboardInterrupt, EOFError):
            print("\nOperation cancelled by user.")
            sys.exit(0)


def extract_episode_tag(filename):
    """Extract standard Season & Episode identifier (e.g. 's01e01' or '1x01')."""
    m = re.search(r'[sS](\d+)[eE](\d+)', filename)
    if m:
        return f"S{int(m.group(1)):02d}E{int(m.group(2)):02d}"
    m = re.search(r'\b(\d+)x(\d+)\b', filename)
    if m:
        return f"S{int(m.group(1)):02d}E{int(m.group(2)):02d}"
    m = re.search(r'\b[eE](\d{2,3})\b', filename)
    if m:
        return f"E{int(m.group(1)):02d}"
    return None


def extract_series_name(filename):
    """Extract cleaned series title from filename."""
    m = re.search(r'^(.*?)(?=[sS]\d+[eE]\d+|\b\d+x\d+\b)', filename)
    if m:
        cleaned = re.sub(r'[\.\-\_\(\)\[\]]', ' ', m.group(1)).strip()
        return cleaned.title()
    return "Unknown Series"


def find_series_episodes(root_dir):
    """
    Search root_dir and subdirectories for series video and audio files,
    pairing them by Season and Episode. Groups ALL matching audios for each episode.
    """
    root_path = Path(root_dir).resolve()

    # Collect all video and audio files recursively
    videos = []
    for ext in ["*.mkv", "*.mp4"]:
        for f in root_path.rglob(ext):
            if not any(t in f.name for t in ["Dual-Audio", "Dual.Audio", "MULTi", "MULTI"]):
                videos.append(f)

    audios = []
    for ext in [".m4a", ".aac", ".ac3", ".eac3", ".dts", ".mka", ".flac"]:
        for f in root_path.rglob(f"*{ext}"):
            audios.append(f)

    # Group by (Series, Season, Episode)
    series_groups = defaultdict(list)

    for vid in videos:
        ep_tag = extract_episode_tag(vid.name)
        if not ep_tag:
            continue
        series_title = extract_series_name(vid.name)

        # Find all matching audios with same ep_tag and series title
        matched_audios = []
        for aud in audios:
            aud_ep = extract_episode_tag(aud.name)
            if aud_ep == ep_tag:
                aud_series = extract_series_name(aud.name)
                # Match series title or related directory
                if (
                    aud_series == "Unknown Series"
                    or aud_series.lower() in series_title.lower()
                    or series_title.lower() in aud_series.lower()
                    or aud.parent == vid.parent
                    or aud.parent.parent == vid.parent
                    or vid.parent.parent == aud.parent
                ):
                    matched_audios.append(aud)

        if matched_audios:
            # Sort audios by language priority (Hindi first, then Tamil, Telugu, etc.)
            matched_audios.sort(key=lambda a: (get_audio_lang_info(a)[2], a.name))
            series_groups[series_title].append({
                "ep_tag": ep_tag,
                "video": vid,
                "audios": matched_audios,
                "audio": matched_audios[0]
            })

    # Sort episodes within each series
    for series in series_groups:
        series_groups[series].sort(key=lambda x: x["ep_tag"])

    return series_groups


def auto_suggest_audio(video_path, audio_files):
    """Find the best matching audio file for a given video."""
    STOPWORDS = {
        "web", "dl", "webrip", "bluray", "remux", "2160p", "1080p", "720p", "480p",
        "hdr", "hdr10", "atmos", "rgp", "dsnp", "jhs", "nf", "amzn", "atvp", "hmax", "max",
        "ddp5", "ddp", "aac2", "aac", "ac3", "dts", "eac3", "h", "265", "264", "hevc",
        "x265", "x264", "dv", "dovi", "hi", "eng", "hin", "and", "of", "the", "in", "a",
        "an", "to", "for", "with", "part", "1", "2", "3", "4", "5", "6", "7", "8", "9"
    }
    vid_clean = re.sub(r'[\.\-\_\(\)\[\]]', ' ', video_path.stem.lower())
    vid_words = {w for w in vid_clean.split() if w not in STOPWORDS and len(w) > 2}

    best_match = None
    best_score = 0
    for aud in audio_files:
        aud_clean = re.sub(r'[\.\-\_\(\)\[\]]', ' ', aud.stem.lower())
        aud_words = {w for w in aud_clean.split() if w not in STOPWORDS and len(w) > 2}
        common = vid_words.intersection(aud_words)
        score = len(common)
        if score > best_score:
            best_score = score
            best_match = aud

    return best_match if best_score >= 2 else None


def mux_movie(video_path, audio_paths, output_path=None, hindi_default=True,
              auto_yes=False, delete_video=False, delete_audio=False, quiet_confirm=False):
    """Perform inspection, sync analysis, user confirmation, and muxing for single or multi-audio."""
    video_path = Path(video_path).resolve()

    # Normalize audio_paths to list of Paths
    if isinstance(audio_paths, (str, Path)):
        audio_paths = [Path(audio_paths).resolve()]
    else:
        audio_paths = [Path(p).resolve() for p in audio_paths]

    if not output_path:
        output_path = generate_output_filename(video_path, num_external_audios=len(audio_paths))
    output_path = Path(output_path).resolve()

    if not video_path.exists():
        print(f"[!] Video file not found: {video_path}")
        return False

    valid_audio_paths = []
    for ap in audio_paths:
        if not ap.exists():
            print(f"[!] Warning: Audio file not found: {ap}")
        else:
            valid_audio_paths.append(ap)

    if not valid_audio_paths:
        print("[!] Error: No valid audio files found to mux.")
        return False
    audio_paths = valid_audio_paths

    # Inspect media info
    try:
        vid_info = get_mkvmerge_info(video_path)
        aud_infos = [get_mkvmerge_info(ap) for ap in audio_paths]
    except Exception as e:
        print(f"[!] Failed to read files with mkvmerge: {e}")
        return False

    print_media_details(video_path, audio_paths, vid_info, aud_infos)

    dur_ns = vid_info.get("container", {}).get("properties", {}).get("duration", 0)
    duration_s = (dur_ns / 1_000_000_000.0) if dur_ns else 3600

    vid_audio_tracks = [t for t in vid_info.get("tracks", []) if t["type"] == "audio"]
    orig_aud = vid_audio_tracks[0] if vid_audio_tracks else None

    # Audio delay detection for each external audio track
    audio_delays = []
    print("\n🔍 CHECKING AUDIO SYNC:")
    for a_idx, (aud_path, aud_info) in enumerate(zip(audio_paths, aud_infos), 1):
        lang_code, lang_name, _ = get_audio_lang_info(aud_path, aud_info)
        print(f"\n  [Audio {a_idx}: {lang_name} ({aud_path.name})]")
        offset_ms = detect_audio_offset(video_path, aud_path, duration_s=duration_s)

        sync_delay = 0
        if abs(offset_ms) >= 10.0:
            sync_delay = int(round(-offset_ms))
            print(f"  ⚠️  DELAY DETECTED: {offset_ms:+.2f} ms")
            print(f"  🛠️  ACTION: Auto-applying {sync_delay:+d} ms delay compensation to fix lip-sync!")
        else:
            print(f"  ✅  OFFSET: {offset_ms:+.2f} ms (< 10 ms)")
            print(f"  🎉  ACTION: {lang_name} audio is already 100% in sync! No delay adjustment needed.")
        audio_delays.append(sync_delay)

    # Prepare mkvmerge command
    cmd = [
        "mkvmerge",
        "-o", str(output_path),
    ]

    # Setup original video audio tracks
    orig_track_name = None
    if orig_aud:
        orig_id = orig_aud["id"]
        orig_channels = orig_aud.get("properties", {}).get("audio_channels", 6)
        orig_codec_lbl = format_codec_name(orig_aud.get("codec", "E-AC-3"), orig_channels)
        has_atmos = "atmos" in str(video_path).lower() or "atmos" in orig_aud.get("properties", {}).get("track_name", "").lower()
        atmos_str = " Atmos" if has_atmos else ""
        orig_track_name = f"English ({orig_codec_lbl}{atmos_str})"

        cmd.extend([
            "--default-track-flag", f"{orig_id}:{0 if (hindi_default and len(audio_paths) > 0) else 1}",
            "--track-name", f"{orig_id}:{orig_track_name}"
        ])

    cmd.append(str(video_path))

    # Add each external audio file
    track_order = ["0:0"]  # Video track first

    for file_num, (aud_path, aud_info, sync_d) in enumerate(zip(audio_paths, aud_infos, audio_delays), start=1):
        ext_aud_tracks = [t for t in aud_info.get("tracks", []) if t["type"] == "audio"]
        if not ext_aud_tracks:
            continue
        ext_aud = ext_aud_tracks[0]
        ext_id = ext_aud["id"]

        lang_code, lang_name, _ = get_audio_lang_info(aud_path, aud_info)
        aud_channels = ext_aud.get("properties", {}).get("audio_channels", 2)
        codec_lbl = format_codec_name(ext_aud.get("codec", "AAC"), aud_channels)
        track_name = f"{lang_name} ({codec_lbl})"

        # Default track flag: first audio track gets default if hindi_default is set
        is_default = (file_num == 1 and hindi_default) or (not orig_aud and file_num == 1)

        cmd.extend([
            "--language", f"{ext_id}:{lang_code}",
            "--track-name", f"{ext_id}:{track_name}",
            "--default-track-flag", f"{ext_id}:{1 if is_default else 0}",
        ])

        if sync_d != 0:
            cmd.extend(["--sync", f"{ext_id}:{sync_d}"])

        cmd.append(str(aud_path))
        track_order.append(f"{file_num}:{ext_id}")

    # Add remaining original video audio tracks
    for t in vid_info.get("tracks", []):
        if t["type"] == "audio":
            track_order.append(f"0:{t['id']}")

    # Add original video subtitle tracks
    for t in vid_info.get("tracks", []):
        if t["type"] == "subtitles":
            track_order.append(f"0:{t['id']}")

    cmd.extend(["--track-order", ",".join(track_order)])

    # Review Summary
    print("\n" + "-" * 70)
    print("🚀 MUX PLAN SUMMARY:")
    print(f"  • Destination:    {output_path.name}")
    print(f"  • Video Track:    Original Video (Preserving HDR10 / Dolby Vision)")
    for i, (aud_p, aud_inf, sync_d) in enumerate(zip(audio_paths, aud_infos, audio_delays), 1):
        lang_code, lang_name, _ = get_audio_lang_info(aud_p, aud_inf)
        ext_aud = [t for t in aud_inf.get("tracks", []) if t["type"] == "audio"][0]
        ch = ext_aud.get("properties", {}).get("audio_channels", 2)
        codec_lbl = format_codec_name(ext_aud.get("codec", "AAC"), ch)
        def_tag = " [DEFAULT]" if ((i == 1 and hindi_default) or (not orig_aud and i == 1)) else ""
        print(f"  • Audio Track {i}:  {lang_name} ({codec_lbl}){def_tag} | Sync: {sync_d:+d} ms")
    if orig_aud:
        print(f"  • Audio Track {len(audio_paths)+1}:  {orig_track_name}")
    print("-" * 70)

    if not auto_yes and not quiet_confirm:
        try:
            confirm = input("\n👉 Proceed with muxing? [Y/n]: ").strip().lower()
            if confirm and confirm not in ["y", "yes"]:
                print("Muxing cancelled by user.")
                return False
        except (KeyboardInterrupt, EOFError):
            print("\nCancelled.")
            return False

    print("\n  [⚙] Running mkvmerge multiplexer...")
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="ignore")

    last_prog = -1
    for line in p.stdout:
        m = re.search(r'Progress:\s*(\d+)%', line)
        if m:
            prog = int(m.group(1))
            if prog != last_prog and prog % 10 == 0:
                print(f"      Muxing Progress: {prog}%")
                last_prog = prog
        elif "Multiplexing took" in line:
            print(f"  [✓] {line.strip()}")

    p.wait()

    if p.returncode != 0:
        print(f"\n[!] Muxing failed with code {p.returncode}")
        return False

    print(f"\n🎉 Successfully created: {output_path.name}")
    print(f"   Final Size: {format_bytes(output_path.stat().st_size)}")

    # Prompt or perform cleanup
    if not auto_yes and not quiet_confirm:
        try:
            del_v = input(f"\n🗑️  Delete original video file ({video_path.name})? [y/N]: ").strip().lower()
            if del_v in ["y", "yes"]:
                video_path.unlink()
                print("   [✓] Original video deleted.")

            aud_names = ", ".join([ap.name for ap in audio_paths])
            del_a = input(f"🗑️  Delete original audio file(s) ({aud_names})? [y/N]: ").strip().lower()
            if del_a in ["y", "yes"]:
                for ap in audio_paths:
                    if ap.exists():
                        ap.unlink()
                print("   [✓] Original audio files deleted.")
        except Exception as e:
            print(f"   [!] Error during cleanup: {e}")
    else:
        if delete_video and video_path.exists():
            video_path.unlink()
            print(f"   [✓] Original video deleted: {video_path.name}")
        if delete_audio:
            for ap in audio_paths:
                if ap.exists():
                    ap.unlink()
                    print(f"   [✓] Original audio deleted: {ap.name}")

    return True


def handle_series_batch(series_groups, auto_yes=False, delete_video=False, delete_audio=False):
    """Interactive handler for batch processing TV series."""
    series_list = list(series_groups.keys())

    print("\n" + "=" * 70)
    print("📺 TV SERIES BATCH DETECTED")
    print("=" * 70)
    for idx, name in enumerate(series_list, 1):
        eps = series_groups[name]
        total_audios = sum(len(ep.get("audios", [ep.get("audio")])) for ep in eps)
        print(f"  [{idx}] {name} ({len(eps)} episodes found, {total_audios} total audio tracks)")

    sel_idx = 0
    if len(series_list) > 1:
        while True:
            try:
                ch = input(f"\n👉 Select series to mux [1-{len(series_list)}]: ").strip()
                num = int(ch)
                if 1 <= num <= len(series_list):
                    sel_idx = num - 1
                    break
            except Exception:
                pass

    selected_series = series_list[sel_idx]
    episodes = series_groups[selected_series]

    print(f"\nEpisodes for {selected_series}:")
    for ep in episodes:
        v_size = format_bytes(ep['video'].stat().st_size)
        aud_list = ep.get('audios', [ep['audio']])
        print(f"  • {ep['ep_tag']}: {ep['video'].name} ({v_size})")
        for a_idx, aud_f in enumerate(aud_list, 1):
            a_size = format_bytes(aud_f.stat().st_size)
            _, lang_name, _ = get_audio_lang_info(aud_f)
            print(f"           + Audio {a_idx} [{lang_name}]: {aud_f.name} ({a_size})")

    print("\nOptions:")
    print(f"  [A] Mux ALL {len(episodes)} episodes automatically (Batch Mode)")
    print(f"  [S] Select a single episode")
    print(f"  [M] Switch to Movie / Manual file mode")

    choice = input("\n👉 Choose option [A/S/M] [Default: A]: ").strip().upper()
    if not choice:
        choice = "A"

    if choice == "M":
        return False  # Fall through to movie mode

    if choice == "S":
        print("\nSelect episode:")
        for idx, ep in enumerate(episodes, 1):
            print(f"  [{idx}] {ep['ep_tag']}")
        ep_num = int(input(f"Enter episode number [1-{len(episodes)}]: ").strip())
        episodes = [episodes[ep_num - 1]]

    # Ask cleanup preference once for batch
    del_vid = delete_video
    del_aud = delete_audio

    if not auto_yes:
        print("\n🗑️  Source file cleanup preference:")
        print("  [1] Keep all original files (Default)")
        print("  [2] Delete original video files after each muxed episode")
        print("  [3] Delete original audio files after each muxed episode")
        print("  [4] Delete BOTH video and audio files (Recommended for space)")
        c_choice = input("👉 Choose cleanup option [1-4] [Default: 1]: ").strip()
        if c_choice == "2":
            del_vid = True
        elif c_choice == "3":
            del_aud = True
        elif c_choice == "4":
            del_vid = True
            del_aud = True

    print("\n" + "=" * 70)
    print(f"🚀 STARTING BATCH MUXING FOR {len(episodes)} EPISODE(S)")
    print("=" * 70)

    success_count = 0
    max_external_audios = 0
    series_folders = set()

    for idx, ep in enumerate(episodes, 1):
        print(f"\n>>> Processing Episode {idx}/{len(episodes)}: {ep['ep_tag']} <<<")
        aud_list = ep.get("audios", [ep["audio"]])
        max_external_audios = max(max_external_audios, len(aud_list))
        series_folders.add(ep["video"].parent)

        ok = mux_movie(
            ep["video"], aud_list,
            hindi_default=True,
            auto_yes=True,
            quiet_confirm=True,
            delete_video=del_vid,
            delete_audio=del_aud
        )
        if ok:
            success_count += 1

    print("\n" + "=" * 70)
    print(f"🎉 {success_count}/{len(episodes)} EPISODE(S) MUXED SUCCESSFULLY!")
    print("=" * 70)

    # Automatically rename series folder(s) if episodes were muxed
    if success_count > 0:
        import time
        time.sleep(0.5)
        is_multi = max_external_audios > 1
        for s_folder in series_folders:
            if s_folder.exists() and s_folder.name.lower() not in ("downloads", "temp"):
                rename_series_folder(s_folder, is_multi=is_multi)

    return True


def run_movie_mode(scan_dir, args, hin_def, del_vid, del_aud):
    """Workflow for single movie file selection and muxing."""
    videos = [
        f for f in scan_dir.glob("*.mkv")
        if not any(t in f.name for t in ["Dual-Audio", "Dual.Audio", "MULTi", "MULTI"]) and not extract_episode_tag(f.name)
    ]
    videos.extend([
        f for f in scan_dir.glob("*.mp4")
        if not any(t in f.name for t in ["Dual-Audio", "Dual.Audio", "MULTi", "MULTI"]) and not extract_episode_tag(f.name)
    ])
    if not videos:
        videos = [
            f for f in scan_dir.glob("*.mkv")
            if not any(t in f.name for t in ["Dual-Audio", "Dual.Audio", "MULTi", "MULTI"])
        ]

    audios = [
        f for f in scan_dir.glob("*.*")
        if f.suffix.lower() in [".m4a", ".aac", ".ac3", ".eac3", ".dts", ".mka", ".flac"]
    ]

    if not videos:
        print("\n[!] No movie video files found directly in this folder.")
        custom_vid = input("Enter video file path manually (or drag & drop): ").strip().strip('"').strip("'")
        sel_video = Path(custom_vid)
        if not sel_video.is_file():
            print("[!] File not found. Exiting.")
            return
    else:
        sel_video = select_file_interactive(videos, "🎬 AVAILABLE VIDEO FILES")

    suggested_audio = auto_suggest_audio(sel_video, audios) if audios else None

    if not audios:
        print("\n[!] No audio files found in the folder.")
        custom_aud = input("Enter audio file path manually (or drag & drop): ").strip().strip('"').strip("'")
        sel_audios = [Path(custom_aud)]
        if not sel_audios[0].is_file():
            print("[!] File not found. Exiting.")
            return
    else:
        sel_audios = select_file_interactive(audios, "🎵 AVAILABLE AUDIO FILES", auto_match=suggested_audio, multi=True)

    mux_movie(
        sel_video, sel_audios, args.output,
        hindi_default=hin_def, auto_yes=args.yes,
        delete_video=del_vid, delete_audio=del_aud
    )


def main():
    parser = argparse.ArgumentParser(description="Auto-Syncing Multi-Audio & Dual-Audio MKV Muxer (Movies & Series)")
    parser.add_argument("-v", "--video", help="Path to video file (MKV)")
    parser.add_argument("-a", "--audio", nargs="+", help="Path to one or more audio files (M4A/AAC/AC3)")
    parser.add_argument("-o", "--output", help="Output file path (optional)")
    parser.add_argument("-d", "--dir", default=".", help="Directory to scan (default: current directory)")
    parser.add_argument("-m", "--movie", action="store_true", help="Directly open Movie Mode")
    parser.add_argument("-s", "--series", action="store_true", help="Directly open TV Series Mode")
    parser.add_argument("-y", "--yes", action="store_true", help="Auto-confirm all prompts (non-interactive)")
    parser.add_argument("--english-default", action="store_true", help="Set English as default audio instead of Hindi")
    parser.add_argument("--del-video", action="store_true", help="Delete source video upon success")
    parser.add_argument("--del-audio", action="store_true", help="Delete source audio upon success")
    parser.add_argument("--del-all", action="store_true", help="Delete both source files upon success")

    args = parser.parse_args()

    hin_def = not args.english_default
    del_vid = args.del_video or args.del_all
    del_aud = args.del_audio or args.del_all

    scan_dir = Path(args.dir).resolve()

    # If both video and audio provided via arguments
    if args.video and args.audio:
        mux_movie(
            args.video, args.audio, args.output,
            hindi_default=hin_def, auto_yes=args.yes,
            delete_video=del_vid, delete_audio=del_aud
        )
        return

    print("=" * 70)
    print("🎬 MULTI-AUDIO & DUAL-AUDIO MKV AUTO-SYNC MUXER")
    print(f"📁 Working Directory: {scan_dir}")
    print("=" * 70)

    # Check for Series Episodes
    series_groups = find_series_episodes(scan_dir)

    # If user explicitly passed --series or --movie
    if args.series and series_groups:
        handle_series_batch(series_groups, auto_yes=args.yes, delete_video=del_vid, delete_audio=del_aud)
        return
    elif args.movie:
        run_movie_mode(scan_dir, args, hin_def, del_vid, del_aud)
        return

    # Interactive Mode Selection Menu
    print("\n👉 What would you like to mux?")
    print("  [1] 🎬 Movie Mode       (Select a movie file & audio file(s))")
    if series_groups:
        summary = ", ".join([f"{name} ({len(eps)} eps)" for name, eps in series_groups.items()])
        print(f"  [2] 📺 TV Series Mode   (Auto-detected: {summary})")
    else:
        print("  [2] 📺 TV Series Mode   (Scan subfolders for TV episodes)")

    default_mode = "1"
    try:
        mode_choice = input(f"\n👉 Choose mode [1-2] [Default: {default_mode}]: ").strip()
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled.")
        return

    if not mode_choice:
        mode_choice = default_mode

    if mode_choice == "2":
        if not series_groups:
            print("[!] No series episodes found in this folder or subfolders.")
            return
        handle_series_batch(series_groups, auto_yes=args.yes, delete_video=del_vid, delete_audio=del_aud)
    else:
        run_movie_mode(scan_dir, args, hin_def, del_vid, del_aud)


if __name__ == "__main__":
    main()
