from __future__ import annotations

import os
import types
import click
import yt_dlp

from vinetrimmer.objects import AudioTrack, TextTrack, Title, Tracks, VideoTrack, Track
from vinetrimmer.services.BaseService import BaseService
from vinetrimmer.config import directories


# ---------------------------------------------------------------------------
# Module-level download helpers (must live OUTSIDE the class so the service
# loader's line-by-line scanner never chokes on them).
# ---------------------------------------------------------------------------

def _ydl_opts_base(cookie_jar=None, proxy=None, extra=None):
    """Build a base yt-dlp options dict, optionally adding cookies / proxy."""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "overwrites": True,
        "extractor_args": {"youtube": {"player_client": ["ios", "tv", "web"]}},
    }
    if extra:
        opts.update(extra)

    # yt-dlp works best with a cookiefile path, as it uses YoutubeDLCookieJar
    # to handle YouTube domain matching and API headers.
    cookie_path = None
    if cookie_jar is not None and getattr(cookie_jar, "filename", None) and os.path.isfile(cookie_jar.filename):
        cookie_path = cookie_jar.filename
    else:
        cookie_path = _find_cookie_file()

    if cookie_path:
        opts["cookiefile"] = cookie_path
    elif cookie_jar is not None:
        opts["cookiejar"] = cookie_jar

    if proxy:
        opts["proxy"] = proxy

    return opts


def _find_cookie_file():
    """
    Fallback: look for a Netscape cookie file in the vinetrimmer cookies dir.
    Mirrors the folder-naming convention used by dl.py / get_cookie_jar():
        cookies/YouTube/default.txt
    """
    base = str(directories.cookies)
    candidates = [
        os.path.join(base, "YouTube", "default.txt"),
        os.path.join(base, "youtube", "default.txt"),
        os.path.join(base, "YouTube.txt"),
        os.path.join(base, "youtube.txt"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def _download_video(self, out, name=None, headers=None, proxy=None):
    """Download method bound to VideoTrack instances."""
    os.makedirs(out, exist_ok=True)
    stem = (name or "VideoTrack_{id}_{enc}").format(
        id=self.id,
        enc="enc" if self.encrypted else "dec",
    )
    save_path = os.path.join(out, f"{stem}.mp4")

    fmt = self.extra.get("format_id")
    format_spec = f"{fmt}/bestvideo" if fmt else "bestvideo"

    opts = _ydl_opts_base(
        cookie_jar=self.extra.get("cookie_jar"),
        proxy=proxy,
        extra={
            "format": format_spec,
            "outtmpl": save_path,
        },
    )
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([self.extra["video_url"]])
    except Exception as exc:
        if "403" in str(exc) or "Forbidden" in str(exc):
            import logging
            log = logging.getLogger("YouTube")
            # Stage 1: retry with ios player client — gets a fresh signed URL
            # that is less likely to be blocked by YouTube's CDN.
            log.warning(f" - Format {fmt} returned 403. Retrying with ios client...")
            opts_ios = dict(opts)
            opts_ios["extractor_args"] = {"youtube": {"player_client": ["ios"]}}
            ios_ok = False
            try:
                with yt_dlp.YoutubeDL(opts_ios) as ydl:
                    ydl.download([self.extra["video_url"]])
                ios_ok = True
            except Exception:
                pass  # fall through to muxed regardless of error type
            if not ios_ok:
                # Stage 2: fall back to best muxed format (720p max, never 403s)
                log.warning(" - ios client failed. Falling back to muxed format (720p)...")
                opts_muxed = dict(opts)
                opts_muxed["format"] = "22/18/best"
                opts_muxed.pop("extractor_args", None)
                with yt_dlp.YoutubeDL(opts_muxed) as ydl:
                    ydl.download([self.extra["video_url"]])
        else:
            raise

    if not os.path.exists(save_path):
        for ext in ("mp4", "webm", "mkv"):
            alt_path = os.path.join(out, f"{stem}.{ext}")
            if os.path.exists(alt_path):
                save_path = alt_path
                break

    self._location = save_path
    return save_path


def _download_audio(self, out, name=None, headers=None, proxy=None):
    """Download method bound to AudioTrack instances."""
    os.makedirs(out, exist_ok=True)
    stem = (name or "AudioTrack_{id}_{enc}").format(
        id=self.id,
        enc="enc" if self.encrypted else "dec",
    )
    ext = "webm" if (self.extra.get("ext") or "").lower() in ("webm",) else "m4a"
    save_path = os.path.join(out, f"{stem}.{ext}")

    fmt = self.extra.get("format_id")
    format_spec = f"{fmt}/bestaudio" if fmt else "bestaudio"

    opts = _ydl_opts_base(
        cookie_jar=self.extra.get("cookie_jar"),
        proxy=proxy,
        extra={
            "format": format_spec,
            "outtmpl": save_path,
            "no_cache_dir": True,  # always fetch a fresh signed URL
        },
    )
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([self.extra["video_url"]])
    except Exception as exc:
        if "403" in str(exc) or "Forbidden" in str(exc) or "not available" in str(exc):
            import logging
            logging.getLogger("YouTube").warning(
                f" - Audio format {fmt} returned 403 Forbidden. Retrying with fresh bestaudio fetch..."
            )
            # Build a completely fresh opts dict without the specific format_id
            # so yt-dlp re-extracts and obtains a fresh signed URL.
            retry_opts = _ydl_opts_base(
                cookie_jar=self.extra.get("cookie_jar"),
                proxy=proxy,
                extra={
                    "format": "bestaudio/best",
                    "outtmpl": save_path,
                    "no_cache_dir": True,
                },
            )
            with yt_dlp.YoutubeDL(retry_opts) as ydl:
                ydl.download([self.extra["video_url"]])
        else:
            raise

    if not os.path.exists(save_path):
        for candidate_ext in ("webm", "m4a", "opus", "mp3", "aac"):
            alt_path = os.path.join(out, f"{stem}.{candidate_ext}")
            if os.path.exists(alt_path):
                save_path = alt_path
                break

    self._location = save_path
    return save_path


def _clean_youtube_vtt(file_path):
    """Clean YouTube WebVTT file by stripping inline timestamp tags and micro-cue glitches."""
    if not os.path.isfile(file_path):
        return
    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()

        import re
        cue_pattern = re.compile(r"(\d{2}:\d{2}:\d{2}[\.,]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[\.,]\d{3})")

        def to_ms(t):
            parts = t.replace(",", ".").split(":")
            h, m, s = parts[0], parts[1], parts[2]
            sec, ms = s.split(".")
            return int(h) * 3600000 + int(m) * 60000 + int(sec) * 1000 + int(ms)

        cues = []
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            match = cue_pattern.search(line)
            if match:
                start_str = match.group(1).replace(",", ".")
                end_str = match.group(2).replace(",", ".")
                dur = to_ms(end_str) - to_ms(start_str)

                i += 1
                text_lines = []
                while i < len(lines) and lines[i].strip() != "":
                    raw_text = lines[i].strip()
                    clean = re.sub(r"<[^>]+>", "", raw_text).strip()
                    if clean and clean not in text_lines:
                        text_lines.append(clean)
                    i += 1

                # Ignore micro-cues (< 100ms) which YouTube uses for 10ms roll-up transitions
                if text_lines and dur >= 100:
                    cues.append((to_ms(start_str), to_ms(end_str), start_str, end_str, "\n".join(text_lines)))
            else:
                i += 1

        # Deduplicate consecutive cues with identical text
        merged = []
        for start_ms, end_ms, start_str, end_str, text in cues:
            if merged and (text == merged[-1][4] or (start_ms <= merged[-1][1] and text in merged[-1][4])):
                if end_ms > merged[-1][1]:
                    merged[-1] = (merged[-1][0], end_ms, merged[-1][2], end_str, merged[-1][4])
            else:
                merged.append((start_ms, end_ms, start_str, end_str, text))

        out = ["WEBVTT\n\n"]
        for s_ms, e_ms, s_str, e_str, text in merged:
            out.append(f"{s_str} --> {e_str}\n{text}\n\n")

        with open(file_path, "w", encoding="utf-8") as f:
            f.writelines(out)
    except Exception as exc:
        import logging
        logging.getLogger("YouTube").warning(f"Failed to clean VTT subtitle file {file_path}: {exc}")


def _download_sub(self, out, name=None, headers=None, proxy=None):
    """Download method bound to TextTrack instances."""
    lang_code = self.extra.get("lang_code", "")
    if lang_code in ("live_chat", "chat") or "chat" in lang_code.lower():
        return None

    os.makedirs(out, exist_ok=True)
    stem = (name or "TextTrack_{id}").format(id=self.id)
    save_path = os.path.join(out, f"{stem}.vtt")

    is_auto = self.extra.get("is_auto", False)

    opts = _ydl_opts_base(
        cookie_jar=self.extra.get("cookie_jar"),
        proxy=proxy,
        extra={
            "skip_download": True,
            "writesubtitles": not is_auto,
            "writeautomaticsub": is_auto,
            "subtitleslangs": [lang_code],
            "subtitlesformat": "vtt",
            "outtmpl": os.path.join(out, stem),
        },
    )
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([self.extra["video_url"]])
    except Exception as exc:
        import logging
        logging.getLogger("YouTube").warning(
            f" - Subtitle '{lang_code}' download failed, skipping: {exc}"
        )
        return None

    # yt-dlp names the file like  stem.LANG.vtt  – find and rename it
    for fname in os.listdir(out):
        if fname.startswith(stem) and fname.endswith(".vtt"):
            actual = os.path.join(out, fname)
            if actual != save_path:
                if os.path.exists(save_path):
                    os.remove(save_path)
                os.rename(actual, save_path)
            break

    if os.path.exists(save_path):
        _clean_youtube_vtt(save_path)
        self._location = save_path
        return save_path
    return None


# ---------------------------------------------------------------------------
# Service class
# ---------------------------------------------------------------------------

class YouTube(BaseService):
    """
    Service code for YouTube (https://www.youtube.com/)

    \\b
    Authorization: Cookies (optional – required for members-only / paid content)
    Security: Unencrypted streams (no DRM)

    \\b
    Tips: - Export cookies from your browser with a "Get cookies.txt LOCALLY" extension
          - Save the file to:  cookies/YouTube/default.txt
          - Supports: videos, shorts, playlists
    """

    ALIASES = ["YouTube", "youtube", "yt", "YT"]
    TITLE_RE = [
        r"https?://(?:www\.|m\.)?youtube\.com/watch\?v=(?P<id>[\w-]+)",
        r"https?://youtu\.be/(?P<id>[\w-]+)",
        r"https?://(?:www\.|m\.)?youtube\.com/shorts/(?P<id>[\w-]+)",
        r"https?://(?:www\.|m\.)?youtube\.com/playlist\?list=(?P<id>[\w-]+)",
    ]

    @staticmethod
    @click.command(name="YouTube", short_help="https://www.youtube.com/", help=__doc__)
    @click.argument("title", type=str)
    @click.pass_context
    def cli(ctx, **kwargs):
        return YouTube(ctx, **kwargs)

    def __init__(self, ctx, title):
        self.url = title
        super().__init__(ctx)
        self.parse_title(ctx, self.url)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _cookie_jar(self):
        """Return the MozillaCookieJar loaded by BaseService (may be None)."""
        return self.cookies or None

    def _ydl_base(self, extra=None):
        """Return a yt-dlp options dict wired with our cookies / session proxy."""
        proxy = getattr(self.session, "proxies", {}).get("all") or None
        return _ydl_opts_base(
            cookie_jar=self._cookie_jar(),
            proxy=proxy,
            extra=extra,
        )

    def _resolve_url(self, video_id):
        """Canonical watch URL for a given video ID."""
        return f"https://www.youtube.com/watch?v={video_id}"

    def _cookie_hint(self):
        """Human-readable path where the cookie file should be placed."""
        from vinetrimmer.config import directories
        return str(directories.cookies / "YouTube" / "default.txt")

    def _handle_ydl_error(self, exc):
        """Convert a yt-dlp bot-detection or cookie error into a clean logged exit."""
        msg = str(exc)
        if any(k in msg.lower() for k in ("sign in", "bot", "cookies", "members-only", "join this channel")):
            self.log.error(
                " - YouTube authentication / cookie error (Bot protection or Members-Only restriction).\n"
                "   1. Open youtube.com in your browser and ensure you are logged in.\n"
                "   2. Export fresh cookies using 'Get cookies.txt LOCALLY' extension.\n"
                f"   3. Save the cookie file to: {self._cookie_hint()}"
            )
            raise SystemExit(1)
        raise exc

    # ------------------------------------------------------------------
    # Required abstract methods
    # ------------------------------------------------------------------

    def get_titles(self):
        url = self.url
        if not url.startswith("http"):
            url = self._resolve_url(self.url)

        self.log.debug(f"Fetching title metadata: {url}")

        opts = self._ydl_base(extra={
            "extract_flat": True,
        })
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False, process=False)
        except Exception as e:
            self._handle_ydl_error(e)

        if not info:
            raise ValueError(f"yt-dlp returned no metadata for: {url}")

        titles = []

        if info.get("_type") == "playlist" and "entries" in info:
            playlist_name = info.get("title") or "YouTube Playlist"
            for idx, entry in enumerate(info["entries"], start=1):
                if not entry:
                    continue
                v_id = entry.get("id") or ""
                v_title = entry.get("title") or f"Video {v_id}"
                v_url = entry.get("url") or self._resolve_url(v_id)
                year = self._parse_year(entry)
                titles.append(Title(
                    id_=v_id,
                    type_=Title.Types.TV,
                    name=playlist_name,
                    season=1,
                    episode=idx,
                    episode_name=v_title,
                    year=year,
                    source=self.ALIASES[0],
                    service_data={"url": v_url, "info": entry},
                ))
        else:
            v_id = info.get("id") or self.title
            v_title = info.get("title") or f"YouTube Video {v_id}"
            v_url = info.get("webpage_url") or url
            year = self._parse_year(info)
            titles.append(Title(
                id_=v_id,
                type_=Title.Types.MOVIE,
                name=v_title,
                year=year,
                source=self.ALIASES[0],
                service_data={"url": v_url, "info": info},
            ))

        return titles

    def get_tracks(self, title):
        v_url = title.service_data.get("url") or self._resolve_url(title.id)

        self.log.debug(f"Fetching format list: {v_url}")

        opts = self._ydl_base(extra={"format": "all"})
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(v_url, download=False)
        except Exception as exc:
            self._handle_ydl_error(exc)

        if not info:
            raise ValueError(f"yt-dlp returned no format data for: {v_url}")

        # Infer video's spoken language from the -orig auto-caption key
        # (e.g. 'es-orig' → 'es') when yt-dlp doesn't tag audio streams.
        inferred_lang = self._infer_spoken_lang(info)

        videos = self._build_video_tracks(info, v_url)
        audios = self._build_audio_tracks(info, v_url, fallback_lang=inferred_lang)
        subs = self._build_subtitle_tracks(info, v_url)

        if not videos:
            self.log.warning(" - Default player client returned no video formats. Retrying with iOS/TV player clients (m3u8/mpd)...")
            opts_fallback = self._ydl_base(extra={
                "format": "all",
                "extractor_args": {"youtube": {"player_client": ["ios", "tv", "android", "web"]}}
            })
            try:
                with yt_dlp.YoutubeDL(opts_fallback) as ydl:
                    info = ydl.extract_info(v_url, download=False)
                inferred_lang = self._infer_spoken_lang(info)
                videos = self._build_video_tracks(info, v_url)
                audios = self._build_audio_tracks(info, v_url, fallback_lang=inferred_lang)
                subs = self._build_subtitle_tracks(info, v_url)
            except Exception as exc:
                self._handle_ydl_error(exc)

        self.log.debug(
            f"Found {len(videos)} video, {len(audios)} audio, {len(subs)} subtitle tracks"
        )

        return Tracks([*videos, *audios, *subs])

    def get_chapters(self, title):
        return []

    def certificate(self, **_):
        return None  # No DRM on YouTube

    def license(self, challenge, **_):
        return None  # No DRM on YouTube

    # ------------------------------------------------------------------
    # Track builders
    # ------------------------------------------------------------------

    def _build_video_tracks(self, info, v_url):
        tracks = []
        seen = set()

        # Sort formats so pure video-only streams (acodec == "none") are prioritized over muxed formats
        formats = sorted(
            info.get("formats", []),
            key=lambda f: (0 if (f.get("acodec") or "none") == "none" else 1)
        )

        for f in formats:
            vcodec = f.get("vcodec") or "none"
            if vcodec == "none":
                continue

            height = f.get("height") or 0
            width = f.get("width") or 0
            if height <= 0 or width <= 0:
                continue

            codec = self._normalise_vcodec(vcodec)
            fps = f.get("fps")
            tbr = f.get("tbr") or f.get("vbr") or 0
            bitrate = int(tbr * 1000) if tbr else 0

            key = (codec, height, width, fps)
            if key in seen:
                continue
            seen.add(key)

            track = VideoTrack(
                id_=f"video-{f['format_id']}",
                source=self.ALIASES[0],
                url=f.get("url", ""),
                codec=codec,
                width=width,
                height=height,
                bitrate=bitrate,
                fps=fps,
                descriptor=Track.Descriptor.URL,
                encrypted=False,
                extra={
                    "format_id": f["format_id"],
                    "video_url": v_url,
                    "cookie_jar": self._cookie_jar(),
                    "is_muxed_with_audio": (f.get("acodec") or "none") != "none",
                },
            )
            track.download = types.MethodType(_download_video, track)
            tracks.append(track)

        return tracks

    def _build_audio_tracks(self, info, v_url, fallback_lang="und"):
        tracks = []
        seen = set()

        # Sort formats so pure audio-only streams (vcodec == "none") are prioritized
        formats = sorted(
            info.get("formats", []),
            key=lambda f: (0 if (f.get("vcodec") or "none") == "none" else 1)
        )

        for f in formats:
            acodec = f.get("acodec") or "none"
            if acodec == "none":
                continue

            codec = self._normalise_acodec(acodec)
            tbr = f.get("tbr") or f.get("abr") or 0
            bitrate = int(tbr * 1000) if tbr else 0
            channels = f.get("audio_channels") or 2
            # Per-format lang → top-level info lang → inferred from -orig captions
            lang = f.get("language") or info.get("language") or fallback_lang

            key = (codec, bitrate, lang)
            if key in seen:
                continue
            seen.add(key)

            track = AudioTrack(
                id_=f"audio-{f['format_id']}",
                source=self.ALIASES[0],
                url=f.get("url", ""),
                codec=codec,
                bitrate=bitrate,
                channels=channels,
                language=lang,
                descriptor=Track.Descriptor.URL,
                encrypted=False,
                extra={
                    "format_id": f["format_id"],
                    "ext": f.get("ext", "m4a"),
                    "video_url": v_url,
                    "cookie_jar": self._cookie_jar(),
                },
            )
            track.download = types.MethodType(_download_audio, track)
            tracks.append(track)

        return tracks

    def _build_subtitle_tracks(self, info, v_url):
        """
        Return TextTrack objects for:
          1. All manual (human-provided) subtitles
          2. The primary auto-generated caption for the video's spoken language
             (identified by the '-orig' suffix that yt-dlp adds)
        """
        tracks = []
        seen_langs = set()

        manual_subs = info.get("subtitles") or {}
        auto_subs = info.get("automatic_captions") or {}

        # --- Manual subtitles (always include all, except live chat) ---
        for lang_code, sub_list in manual_subs.items():
            if lang_code in ("live_chat", "chat") or "chat" in lang_code.lower():
                continue
            sub = self._pick_vtt(sub_list)
            if not sub:
                continue
            clean_lang = lang_code.split("-")[0]
            if clean_lang in seen_langs:
                continue
            seen_langs.add(clean_lang)

            track = self._make_sub_track(lang_code, sub, v_url, is_auto=False)
            tracks.append(track)

        # --- Auto-generated: only the '-orig' (original spoken language) keys ---
        orig_keys = [k for k in auto_subs if k.endswith("-orig") and "chat" not in k.lower()]

        # Fallback: if no -orig keys but no manual subs either, take first few auto keys
        if not orig_keys and not manual_subs:
            orig_keys = [k for k in list(auto_subs.keys())[:5] if "chat" not in k.lower()]

        for lang_code in orig_keys:
            sub_list = auto_subs[lang_code]
            sub = self._pick_vtt(sub_list)
            if not sub:
                continue
            # The clean lang for -orig  e.g. "hi-orig" → "hi"
            clean_lang = lang_code.replace("-orig", "").split("-")[0]
            if clean_lang in seen_langs:
                continue
            seen_langs.add(clean_lang)

            track = self._make_sub_track(lang_code, sub, v_url, is_auto=True)
            tracks.append(track)

        return tracks

    def _make_sub_track(self, lang_code, sub_entry, v_url, *, is_auto):
        clean_lang = lang_code.replace("-orig", "").split("-")[0]
        track = TextTrack(
            id_=f"sub-{lang_code}",
            source=self.ALIASES[0],
            url=sub_entry.get("url", ""),
            codec="vtt",
            language=clean_lang,
            encrypted=False,
            extra={
                "lang_code": lang_code,
                "is_auto": is_auto,
                "video_url": v_url,
                "cookie_jar": self._cookie_jar(),
            },
        )
        track.download = types.MethodType(_download_sub, track)
        return track

    # ------------------------------------------------------------------
    # Static / codec helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _infer_spoken_lang(info):
        """
        Infer the video's spoken language when yt-dlp doesn't tag audio
        streams (common with the TV player client used with cookies).

        Strategy:
          1. info["language"] / info["original_language"]  — most direct
          2. The '-orig' auto-caption key (e.g. 'es-orig' → 'es') — reliable
          3. 'und' — honest fallback; YouTube TV client omits this metadata
        """
        lang = info.get("language") or info.get("original_language")
        if lang:
            return lang

        # '-orig' suffix marks the primary spoken language in auto-captions
        auto_caps = info.get("automatic_captions") or {}
        for key in auto_caps:
            if key.endswith("-orig"):
                return key.replace("-orig", "").split("-")[0]

        return "und"

    @staticmethod
    def _normalise_vcodec(raw):
        base = raw.split(".")[0].lower()
        if base.startswith("avc"):
            return "avc1"
        if base in ("vp9", "vp09"):
            return "vp9"
        if base == "av01":
            return "av01"
        if base.startswith(("hev", "hvc")):
            return "hvc1"
        return base

    @staticmethod
    def _normalise_acodec(raw):
        base = raw.split(".")[0].lower()
        if base.startswith("mp4a"):
            return "aac"
        if base == "opus":
            return "opus"
        return base

    @staticmethod
    def _pick_vtt(sub_list):
        """Prefer VTT, fall back to first available valid subtitle format."""
        if not sub_list:
            return None
        vtt = next((s for s in sub_list if s.get("ext") == "vtt"), None)
        if vtt:
            return vtt
        for s in sub_list:
            if s.get("ext") in ("vtt", "ttml", "srv3", "srv2", "srv1"):
                return s
        return None

    @staticmethod
    def _parse_year(entry):
        year = entry.get("release_year")
        if not year:
            raw = (entry.get("upload_date") or "")[:4]
            try:
                year = int(raw)
            except (ValueError, TypeError):
                year = None
        return year
