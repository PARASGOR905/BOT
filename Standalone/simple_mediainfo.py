import os
import sys
import json
from pathlib import Path
from pymediainfo import MediaInfo

def get_simple_mediainfo(file_path: str):
    path = Path(file_path).resolve()
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")

    mi = MediaInfo.parse(str(path))
    
    general = next((t for t in mi.tracks if t.track_type == "General"), None)
    video = next((t for t in mi.tracks if t.track_type == "Video"), None)
    audios = [t for t in mi.tracks if t.track_type == "Audio"]
    subtitles = [t for t in mi.tracks if t.track_type == "Text"]

    # Analyze Video & HDR Hybrid
    is_hybrid = False
    hdr_summary = []
    
    if video:
        hdr_format = getattr(video, "hdr_format", "") or ""
        hdr_compat = getattr(video, "hdr_format_compatibility", "") or ""
        hdr_profile = getattr(video, "hdr_format_profile", "") or ""
        hdr_settings = getattr(video, "hdr_format_settings", "") or ""
        
        has_dv = "Dolby Vision" in hdr_format
        has_hdr10 = "SMPTE ST 2084" in getattr(video, "transfer_characteristics", "") or "HDR10" in hdr_compat or "BT.2020" in getattr(video, "color_primaries", "")
        
        if has_dv and has_hdr10:
            is_hybrid = True
            dv_desc = "Dolby Vision"
            if "dvhe.08" in hdr_profile or "Profile 8" in hdr_profile:
                dv_desc += " Profile 8.1 (BL+RPU)"
            elif hdr_profile:
                dv_desc += f" {hdr_profile.strip('/')} ({hdr_settings.strip('/')})"
            hdr_summary.append(f"{dv_desc} + HDR10 (Hybrid)")
        elif has_dv:
            hdr_summary.append(f"Dolby Vision ({hdr_profile.strip('/')})")
        elif has_hdr10:
            hdr_summary.append("HDR10")

    # Build structured dict
    info_dict = {
        "general": {
            "file_name": path.name,
            "size": general.other_file_size[0] if general and general.other_file_size else f"{path.stat().st_size} bytes",
            "duration": general.other_duration[3] if general and general.other_duration else "N/A",
            "overall_bit_rate": general.other_overall_bit_rate[0] if general and general.other_overall_bit_rate else "N/A",
            "format": general.format if general else "Matroska"
        },
        "video": {
            "format": f"{video.format} ({video.format_profile})" if video and video.format_profile else (video.format if video else "N/A"),
            "resolution": f"{video.width}x{video.height}" if video else "N/A",
            "aspect_ratio": getattr(video, "display_aspect_ratio", "16:9") if video else "N/A",
            "frame_rate": f"{video.frame_rate} FPS" if video and video.frame_rate else "N/A",
            "bit_rate": video.other_bit_rate[0] if video and getattr(video, "other_bit_rate", None) else (f"{video.bit_rate} b/s" if video and getattr(video, "bit_rate", None) else "N/A"),
            "hdr_format": " / ".join(hdr_summary) if hdr_summary else "SDR",
            "is_hybrid": is_hybrid,
            "color_primaries": getattr(video, "color_primaries", "N/A") if video else "N/A"
        },
        "audio": [
            {
                "title": getattr(a, "title", "") or getattr(a, "language", "Unknown"),
                "language": getattr(a, "other_language", [getattr(a, "language", "Unknown")])[0] if getattr(a, "other_language", None) else getattr(a, "language", "Unknown"),
                "format": getattr(a, "commercial_name", None) or f"{a.format} ({getattr(a, 'format_profile', '')})".strip(),
                "channels": f"{a.channel_s} channels ({getattr(a, 'channel_layout', '')})".strip(),
                "bit_rate": a.other_bit_rate[0] if getattr(a, "other_bit_rate", None) else (f"{a.bit_rate} b/s" if getattr(a, "bit_rate", None) else "N/A")
            }
            for a in audios
        ],
        "subtitles": [
            {
                "language": getattr(s, "other_language", [getattr(s, "language", "Unknown")])[0] if getattr(s, "other_language", None) else getattr(s, "language", "Unknown"),
                "title": getattr(s, "title", ""),
                "format": s.format,
                "elements": getattr(s, "element_count", None)
            }
            for s in subtitles
        ]
    }

    # Text presentation
    lines = []
    lines.append("=" * 65)
    lines.append(f"  {info_dict['general']['file_name']}")
    lines.append("=" * 65)
    lines.append("")
    lines.append("[ General ]")
    lines.append(f"  Container ......... : {info_dict['general']['format']}")
    lines.append(f"  File Size ......... : {info_dict['general']['size']}")
    lines.append(f"  Duration .......... : {info_dict['general']['duration']}")
    lines.append(f"  Overall Bitrate ... : {info_dict['general']['overall_bit_rate']}")
    lines.append("")
    lines.append("[ Video ]")
    lines.append(f"  Format ............ : {info_dict['video']['format']}")
    lines.append(f"  Resolution ........ : {info_dict['video']['resolution']} (16:9)")
    lines.append(f"  Framerate ......... : {info_dict['video']['frame_rate']}")
    lines.append(f"  Bitrate ........... : {info_dict['video']['bit_rate']}")
    lines.append(f"  HDR Format ........ : {info_dict['video']['hdr_format']}")
    lines.append(f"  Color Primaries ... : {info_dict['video']['color_primaries']}")
    lines.append("")
    lines.append("[ Audio ]")
    for i, a in enumerate(info_dict["audio"], 1):
        lines.append(f"  Track #{i} .......... : {a['language']} | {a['format']} | {a['channels']} | {a['bit_rate']}")
    lines.append("")
    lines.append("[ Subtitles ]")
    if info_dict["subtitles"]:
        sub_list = []
        seen_langs = {}
        for s in info_dict["subtitles"]:
            lang = s['language']
            seen_langs[lang] = seen_langs.get(lang, 0) + 1
            if seen_langs[lang] > 1 or (s.get('elements') and int(s['elements']) > 1200):
                sub_list.append(f"{lang} (SDH)")
            else:
                sub_list.append(lang)
        lines.append("  " + " | ".join(sub_list))
    else:
        lines.append("  None")
    lines.append("")
    lines.append("=" * 65)

    text_output = "\n".join(lines)
    return info_dict, text_output

if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else r"D:\temp\downloads\Kattalan.2026.2160p.SLIV.WEB-DL.DDP5.1.Atmos.DV.HDR.H.265-RGP.mkv"
    info, text = get_simple_mediainfo(target)
    print(text)
    
    # Save simple JSON
    p = Path(target)
    out_json = p.with_name(f"{p.stem}.simple.json")
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(info, f, indent=2, ensure_ascii=False)
    print(f"\n[+] Simple JSON saved to: {out_json}")

    # Save simple TXT
    out_txt = p.with_name(f"{p.stem}.mediainfo.txt")
    with open(out_txt, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"[+] Simple Text saved to: {out_txt}")
