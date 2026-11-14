from __future__ import annotations

import re
import click
import m3u8

from vinetrimmer.objects import Title, Tracks, VideoTrack, AudioTrack, TextTrack
from vinetrimmer.services.BaseService import BaseService


class AmiPlus(BaseService):
    """
    Service code for AMI+ streaming platform (https://amiplus.ca).

    \b
    Authorization: None (Free service, content is DRM-free)
    Security: UHD@-- HD@L3 -- Content is UNENCRYPTED plain HLS/MP4.

    \b
    URL Formats Accepted:
      - Show name slug (resolves full series):
          https://www.amiplus.ca/shows/igotthis
          igotthis
          blind-since-birth
      - Direct JW Player media ID (8 alphanumeric chars):
          iOWCCtMA
      - Direct video URL:
          https://www.amiplus.ca/video/iOWCCtMA

    \b
    Notes:
      - No DRM. All content served as plain HLS from cdn.jwplayer.com.
      - Geoblocking: "No" for all AMI content (accessible worldwide).
      - Series playlist lookup via JW Player Management API v2.
    """

    ALIASES = ["AMIP", "amiplus", "ami+"]
    GEOFENCE = []  # No geo-restriction

    TITLE_RE = [
        # Full URL with /shows/ or /video/ prefix
        r"^https?://(?:www\.)?amiplus\.ca/(?:shows|video)/(?P<id>[A-Za-z0-9][A-Za-z0-9-_]{1,60})(?:\?.*)?$",
        # Bare URL like https://www.amiplus.ca/Underdog
        r"^https?://(?:www\.)?amiplus\.ca/(?P<id>[A-Za-z0-9][A-Za-z0-9-_]{1,60})(?:\?.*)?$",
        # Slug or direct JW media ID (no URL)
        r"^(?P<id>[A-Za-z0-9][A-Za-z0-9-_]{1,60})$",
    ]

    # JW Player site IDs extracted from AMI+ app JS bundle
    JWP_SITE_ENGLISH = "pdEStcHj"
    JWP_SITE_FRENCH  = "7v5fDPr4"

    # Public read-only InPlayer/JWP merchant token from AMI+ app bundle
    JWP_AUTH_TOKEN = (
        "A_wPK12kkn-8cRsP7NPtzWInYmxkV1kwY3dUbmRwTXpaMmVqWlZNekI2U0ZaUlpVZHEn"
    )

    JWP_CDN_BASE = "https://cdn.jwplayer.com"
    JWP_API_BASE = "https://api.jwplayer.com/v2/sites"

    VIDEO_CODEC_MAP = {
        "H264": ["avc1"],
        "H265": ["hvc1"],
    }
    AUDIO_CODEC_MAP = {
        "AAC": "mp4a",
    }

    @staticmethod
    @click.command(name="AmiPlus", short_help="https://amiplus.ca")
    @click.argument("title", type=str, required=False)
    @click.option("-m", "--movie", is_flag=True, default=False, help="Title is a movie.")
    @click.option("-f", "--french", is_flag=True, default=False,
                  help="Search French AMI-tele library instead of English.")
    @click.pass_context
    def cli(ctx, **kwargs):
        return AmiPlus(ctx, **kwargs)

    def __init__(self, ctx, title, movie, french):
        super().__init__(ctx)
        self.parse_title(ctx, title)
        self.movie = movie
        self.french = french
        self.vcodec = ctx.parent.params["vcodec"]
        self.acodec = ctx.parent.params["acodec"]
        self.cdm = ctx.obj.cdm

        self.playlist_id = None
        self.is_media_id = False

        self.configure()

    # ------------------------------------------------------------------
    # vinetrimmer abstract methods
    # ------------------------------------------------------------------

    def get_titles(self):
        if self.is_media_id:
            return self._titles_from_media_id(self.title)
        return self._titles_from_playlist(self.playlist_id)

    def get_tracks(self, title):
        item = title.service_data
        media_id = item["mediaid"]

        media = self.session.get(
            f"{self.JWP_CDN_BASE}/v2/media/{media_id}"
        ).json()
        item_data = media["playlist"][0]

        sources    = item_data.get("sources", [])
        tracks_raw = item_data.get("tracks", [])

        # ── Video + Audio via HLS manifest ────────────────────────────────
        hls_url = next(
            (s["file"] for s in sources if "mpegurl" in s.get("type", "")),
            None
        )

        if hls_url:
            tracks = Tracks.from_m3u8(
                m3u8.loads(self.session.get(hls_url).text, hls_url),
                source=self.ALIASES[0]
            )
        else:
            # Fallback: discrete MP4 sources
            self.log.warning(" ! No HLS manifest found, building from MP4 sources")
            video_tracks = []
            audio_tracks = []
            for src in sources:
                ftype = src.get("type", "")
                if "video/mp4" in ftype and src.get("height"):
                    video_tracks.append(VideoTrack(
                        id_=f"{media_id}_{src['height']}p",
                        source=self.ALIASES[0],
                        url=src["file"],
                        codec="avc1",
                        language="en",
                        bitrate=src.get("bitrate", 0),
                        height=src.get("height", 0),
                        width=src.get("width", 0),
                        fps=src.get("framerate", 30.0),
                        hdr10=False, hlg=False, dvhdr=False, dv=False,
                        encrypted=False,
                        descriptor=VideoTrack.Descriptor.URL,
                        needs_proxy=False, needs_repack=False,
                    ))
                elif "audio/mp4" in ftype:
                    audio_tracks.append(AudioTrack(
                        id_=f"{media_id}_aac",
                        source=self.ALIASES[0],
                        url=src["file"],
                        codec="mp4a",
                        language="en",
                        bitrate=src.get("bitrate", 0),
                        channels=2, atmos=False,
                        encrypted=False,
                        descriptor=AudioTrack.Descriptor.URL,
                        needs_proxy=False, needs_repack=False,
                    ))
            tracks = Tracks(videos=video_tracks, audios=audio_tracks)

        # ── Subtitles ─────────────────────────────────────────────────────
        seen = set()
        for t in tracks_raw:
            if t.get("kind") != "captions":
                continue
            lang  = t.get("language", "en")
            label = t.get("label", "English")
            # Skip "Transcript" entries — these are plain text files, not valid subtitle streams
            if "Transcript" in label:
                continue
            key   = f"{lang}_{label}"
            if key in seen:
                continue
            seen.add(key)
            sub_url = t["file"]
            codec   = "vtt" if sub_url.endswith(".vtt") else "srt"
            sub_track = TextTrack(
                id_=f"{media_id}_{lang}_{label}",
                source=self.ALIASES[0],
                url=sub_url,
                codec=codec,
                language=lang,
                forced=False,
                sdh="SDH" in label or "Transcript" in label,
                encrypted=False,
                descriptor=TextTrack.Descriptor.URL,
                needs_proxy=False, needs_repack=False,
            )
            sub_track.is_original_lang = lang.startswith("en")
            tracks.subtitles.append(sub_track)

        # Mark ALL tracks unencrypted
        for t in tracks.videos + tracks.audios + tracks.subtitles:
            t.encrypted = False

        return tracks


    def get_chapters(self, title):
        return []

    def certificate(self, **kwargs):
        return None  # DRM-free

    def license(self, challenge, **kwargs):
        raise NotImplementedError("AMI+ content is DRM-free. No license endpoint.")

    # ------------------------------------------------------------------
    # Service-specific helpers
    # ------------------------------------------------------------------

    def configure(self):
        """Detect input type: direct 8-char media ID vs show slug."""
        title = self.title

        # Safety net: if a full URL somehow slipped through TITLE_RE, strip it down to just the slug
        if title.startswith("http"):
            # Take the last non-empty path segment
            from urllib.parse import urlparse
            path = urlparse(title).path.rstrip("/")
            title = path.split("/")[-1]
            self.title = title
            self.log.info(f" + Stripped URL to slug: {title}")

        # Pure 8-char alphanumeric = JW media ID — but only if it looks like one.
        # Real JW IDs (e.g. iOWCCtMA, FFnWjPIB) have unpredictable mixed case with
        # uppercase letters after position 0, or contain digits.
        # Simple title-case words like "Underdog" are NOT media IDs.
        if re.fullmatch(r"[A-Za-z0-9]{8}", title) and self._looks_like_jw_id(title):
            self.log.info(f" + Treating input as a direct JW media ID: {title}")
            self.is_media_id = True
            return

        # Otherwise: resolve show slug to a playlist
        self.log.info(f" + Resolving show slug \"{title}\" to JW Player playlist...")
        self.playlist_id = self._resolve_playlist_id(title)
        if not self.playlist_id:
            raise ValueError(
                f" - No JW Player playlist found for show: \"{title}\"\n"
                f"   Tip: Try the exact show name or a direct 8-char JW media ID."
            )
        self.log.info(f" + Resolved playlist ID: {self.playlist_id}")

    @staticmethod
    def _looks_like_jw_id(s: str) -> bool:
        """
        Returns True if the string looks like a real JW Player media ID.
        JW IDs (e.g. iOWCCtMA, FFnWjPIB, ZK5SPCNF) always have:
          - digits, OR
          - uppercase letters appearing after position 0 AND lowercase letters mixed in
        Simple title-case words like "Underdog" or "Sherlock" fail this check.
        """
        has_digit = any(c.isdigit() for c in s)
        has_upper_after_pos0 = any(c.isupper() for c in s[1:])
        has_lower = any(c.islower() for c in s)
        return has_digit or (has_upper_after_pos0 and has_lower)

    def _resolve_playlist_id(self, show_slug: str) -> str | None:
        """Search JW Management API for a series playlist matching the show name."""
        site      = self.JWP_SITE_FRENCH if self.french else self.JWP_SITE_ENGLISH
        show_name = show_slug.replace("-", " ").replace("_", " ")
        api_headers = {
            "Authorization": f"Bearer {self.JWP_AUTH_TOKEN}",
            "Accept": "application/json",
        }

        for query in [f'title:"{show_name}"', f"title:{show_name}"]:
            resp = self.session.get(
                url=f"{self.JWP_API_BASE}/{site}/playlists/",
                params={"page": 1, "page_length": 10, "q": query},
                headers=api_headers,
            ).json()
            playlists = resp.get("playlists", [])
            if playlists:
                return playlists[0]["id"]

        return None

    def _fetch_all_playlist_items(self, playlist_id: str) -> list[dict]:
        """Fetch ALL items from a JW Player CDN playlist, handling pagination."""
        all_items, page, page_limit = [], 1, 100
        while True:
            resp = self.session.get(
                f"{self.JWP_CDN_BASE}/v2/playlists/{playlist_id}",
                params={"page_limit": page_limit, "page": page},
            ).json()
            items = resp.get("playlist", [])
            all_items.extend(items)
            if len(items) < page_limit:
                break
            page += 1
        return all_items

    def _build_title(self, item: dict, show_name: str | None = None) -> Title:
        """Convert a JW playlist item dict into a vinetrimmer Title object."""
        program_type = item.get("Program_Type", "Series")
        is_movie = self.movie or program_type.lower() in ("movie", "film", "special")

        try:
            season = int(item.get("season") or item.get("seasonNumber") or 1)
        except (ValueError, TypeError):
            season = 1
        try:
            episode = int(item.get("episode") or item.get("episodeNumber") or 0)
        except (ValueError, TypeError):
            episode = 0

        year_raw = (item.get("Production_Year") or item.get("Premiere_Date", ""))[:4]
        try:
            year = int(year_raw) if year_raw and year_raw.isdigit() else None
        except ValueError:
            year = None

        return Title(
            id_=item["mediaid"],
            type_=Title.Types.MOVIE if is_movie else Title.Types.TV,
            name=item.get("Show") or show_name or item.get("title", "Unknown"),
            year=year,
            season=None if is_movie else season,
            episode=None if is_movie else episode,
            episode_name=item.get("title"),
            original_lang="en",
            source=self.ALIASES[0],
            service_data=item,
        )

    def _titles_from_playlist(self, playlist_id: str) -> list[Title]:
        items = self._fetch_all_playlist_items(playlist_id)
        if not items:
            raise ValueError(f" - Playlist {playlist_id} returned no episodes!")
        show_name = items[0].get("Show") or items[0].get("title", "Unknown Show")
        return [self._build_title(item, show_name) for item in items]

    def _titles_from_media_id(self, media_id: str) -> list[Title]:
        resp  = self.session.get(f"{self.JWP_CDN_BASE}/v2/media/{media_id}").json()
        items = resp.get("playlist", [])
        if not items:
            raise ValueError(f" - No media found for ID: {media_id}")
        item = items[0]
        item["mediaid"] = media_id
        return [self._build_title(item)]
