#!/usr/bin/env python3

import argparse
import os
import subprocess
import sys
import tempfile
from pymediainfo import MediaInfo


def main():
    # Reconfigure stdout/stderr to use UTF-8 to prevent encoding errors on Windows console
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(
        description="Verify if a Dolby Vision video file is a proper hybrid or has a broken/mislabeled configuration."
    )
    parser.add_argument(
        "file",
        help="Path to the media file to analyze."
    )

    args = parser.parse_args()

    if not os.path.exists(args.file):
        print(f"Error: File not found: {args.file}", file=sys.stderr)
        sys.exit(1)

    # Locate binaries relative to script directory
    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    ffmpeg_path = os.path.join(base_dir, "binaries", "ffmpeg.exe")
    dovi_tool_path = os.path.join(base_dir, "binaries", "dovi_tool.exe")

    # Fallback to system path if not found in binaries folder
    if not os.path.exists(ffmpeg_path):
        ffmpeg_path = "ffmpeg"
    if not os.path.exists(dovi_tool_path):
        dovi_tool_path = "dovi_tool"

    print("Analyzing video file metadata...")

    # 1. Parse with MediaInfo
    media_info = MediaInfo.parse(args.file)
    video_track = None
    for track in media_info.tracks:
        if track.track_type == "Video":
            video_track = track
            break

    if not video_track:
        print("Error: No video track found in the file.", file=sys.stderr)
        sys.exit(1)

    # Extract container tags
    hdr_format = getattr(video_track, "hdr_format", "")
    hdr_profile = getattr(video_track, "hdr_format_profile", "")
    hdr_compatibility = getattr(video_track, "hdr_format_compatibility", "")
    color_space = getattr(video_track, "color_space", "")
    color_primaries = getattr(video_track, "color_primaries", "")
    transfer_char = getattr(video_track, "transfer_characteristics", "")

    has_dv = "dolby vision" in hdr_format.lower()

    if not has_dv:
        print("\n=== Result: Standard HDR / SDR File ===")
        print("This file does not contain Dolby Vision metadata in the container.")
        print(f"Base HDR: {hdr_format or 'None/SDR'}")
        return

    print("\nDolby Vision detected in container.")
    print(f"  Container Profile: {hdr_profile}")
    print(f"  Container Compatibility: {hdr_compatibility or 'None'}")
    print(f"  Color Space: {color_space}")
    print(f"  Color Primaries: {color_primaries}")
    print(f"  Transfer Characteristics: {transfer_char}")

    # 2. Extract and parse RPU profile
    print("\nExtracting Dolby Vision RPU metadata (first 100 frames)...")
    temp_rpu = tempfile.mktemp(suffix=".bin")

    # Command to extract RPU
    # ffmpeg -i "file" -c:v copy -f hevc -v quiet - | dovi_tool extract-rpu - -o "temp_rpu" -l 100
    ffmpeg_cmd = [
        ffmpeg_path,
        "-i", args.file,
        "-c:v", "copy",
        "-f", "hevc",
        "-v", "quiet",
        "-"
    ]
    dovi_cmd = [
        dovi_tool_path,
        "extract-rpu",
        "-",
        "-o", temp_rpu,
        "-l", "100"
    ]

    try:
        p1 = subprocess.Popen(ffmpeg_cmd, stdout=subprocess.PIPE)
        p2 = subprocess.Popen(dovi_cmd, stdin=p1.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        p1.stdout.close()
        p2.communicate()

        if not os.path.exists(temp_rpu) or os.path.getsize(temp_rpu) == 0:
            print("Error: Failed to extract Dolby Vision RPU metadata. The file might not contain valid DV NAL units.", file=sys.stderr)
            if os.path.exists(temp_rpu):
                os.remove(temp_rpu)
            sys.exit(1)

        # Parse RPU info
        info_cmd = [dovi_tool_path, "info", "-i", temp_rpu, "-s"]
        info_proc = subprocess.run(info_cmd, capture_output=True, text=True, encoding="utf-8")
        info_output = info_proc.stdout

        # Clean up RPU file
        if os.path.exists(temp_rpu):
            os.remove(temp_rpu)

    except Exception as e:
        print(f"Error executing extraction tools: {e}", file=sys.stderr)
        if os.path.exists(temp_rpu):
            os.remove(temp_rpu)
        sys.exit(1)

    # Parse RPU Profile from dovi_tool output
    rpu_profile = None
    for line in info_output.splitlines():
        if "Profile:" in line:
            rpu_profile = line.split(":", 1)[1].strip()
            break

    if not rpu_profile:
        print("Error: Could not determine Dolby Vision RPU profile.", file=sys.stderr)
        sys.exit(1)

    print(f"  Actual RPU Profile: {rpu_profile}")

    # 3. Hybrid analysis
    is_yuv_bt2020 = color_space == "YUV" and color_primaries == "BT.2020" and transfer_char == "PQ"

    print("\n" + "=" * 50)
    print("ANALYSIS REPORT")
    print("=" * 50)

    if rpu_profile == "5":
        if is_yuv_bt2020:
            print("RESULT: ❌ BROKEN / FAKE HYBRID")
            print("\nExplanation:")
            print("  This file is a broken/mislabeled hybrid. It contains a standard HDR10 base layer (YUV BT.2020 PQ),")
            print("  but the Dolby Vision RPU metadata profile was left as Profile 5.")
            print("  Dolby Vision Profile 5 RPUs use the proprietary IPTPQc2 color space.")
            print("  When played on a Dolby Vision display, the player will trigger Dolby Vision and try to map the colors")
            print("  expecting Profile 5 IPTPQc2 input, but since the base layer is YUV BT.2020, the colors will display incorrectly")
            print("  (often showing a heavy purple, green, or pink tint).")
            print("\nHow to fix it:")
            print("  You need to convert the Dolby Vision RPU profile from 5 to 8.1. You can do this by:")
            print("  1. Extract the RPU:")
            print("     ffmpeg -i \"input.mkv\" -c:v copy -f hevc - | dovi_tool extract-rpu - -o rpu.bin")
            print("  2. Convert the RPU to Profile 8.1 (Mode 2 or Mode 5):")
            print("     dovi_tool -m 2 convert -i rpu.bin -o rpu_p8.bin")
            print("  3. Inject the converted RPU back into the video stream:")
            print("     ffmpeg -i \"input.mkv\" -c:v copy -f hevc - | dovi_tool inject-rpu - -i rpu_p8.bin -o injected.hevc")
            print("  4. Mux the new video stream back into an MKV:")
            print("     mkvmerge -o \"fixed_hybrid.mkv\" injected.hevc --no-video \"input.mkv\"")
        else:
            print("RESULT: ✅ NATIVE STREAMING PROFILE 5")
            print("\nExplanation:")
            print("  This is a standard, native Dolby Vision Profile 5 stream (typically from streaming services like Netflix/Apple TV).")
            print("  It has no HDR10 fallback layer. It will play beautifully on Dolby Vision-capable displays,")
            print("  but will look green/purple on HDR10-only displays.")
    elif rpu_profile in ["8", "8.1"]:
        if is_yuv_bt2020:
            print("RESULT: ✅ PROPER HYBRID (or Native Profile 8.1)")
            print("\nExplanation:")
            print("  This is a properly configured hybrid file or native Profile 8.1 video.")
            print("  It has a standard HDR10 base layer (YUV BT.2020 PQ) and a compatible Profile 8.1 RPU.")
            print("  It will correctly play in Dolby Vision on Dolby Vision-capable displays,")
            print("  and will gracefully fall back to standard HDR10 on standard HDR10 displays.")
        else:
            print("RESULT: ⚠️ PROFILE 8.1 WITH NON-HDR10 BASE LAYER")
            print(f"  The RPU is Profile 8.1, but the base layer is not standard YUV BT.2020 PQ ({color_space} {color_primaries}).")
    elif rpu_profile == "7":
        print("RESULT: ✅ NATIVE UHD BLU-RAY PROFILE 7")
        print("\nExplanation:")
        print("  This is a standard UHD Blu-ray Dolby Vision profile (Profile 7).")
        print("  It contains a base layer with HDR10 fallback and can contain an enhancement layer (MEL/FEL).")
        print("  It is fully backward compatible with HDR10 displays.")
    else:
        print(f"RESULT: UNKNOWN PROFILE ({rpu_profile})")

    print("=" * 50)


if __name__ == "__main__":
    main()
