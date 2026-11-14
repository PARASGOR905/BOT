import base64
import json
import re
import uuid
from typing import List, Optional
from urllib.parse import unquote, urlparse, parse_qs

import click
import m3u8
import requests

from vinetrimmer.objects import AudioTrack, Title, Tracks
from vinetrimmer.services.BaseService import BaseService
from vinetrimmer.utils.widevine.device import LocalDevice
from vinetrimmer.vendor.pymp4.parser import Box



class AppleMusic(BaseService):
    """
    Service code for Apple Music streaming service (https://music.apple.com).

    \b
    Authorization: Cookies (media-user-token)
    Security:
    - FairPlay (Apple devices)
    - Widevine L1/L3

    Supported URL formats:
    - Song:    https://music.apple.com/in/song/title/1234567890
    - Album:   https://music.apple.com/in/album/title/1234567890
    - Playlist: https://music.apple.com/in/playlist/title/pl.xxxx
    - Direct ID: 1234567890  (song Adam ID)
    - Direct playlist: pl.xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

    Usage:
        vt dl AppleMusic "https://music.apple.com/in/album/title/1234567890"
        vt dl AppleMusic "https://music.apple.com/in/song/title/1234567890"
        vt dl AppleMusic "https://music.apple.com/in/playlist/name/pl.xxxxx"
    """

    ALIASES = ["AM", "applemusic", "apple-music"]

    # Supported URL patterns
    TITLE_RE = [
        # Album URL with ?i=SONG_ID deep-link → prefer song ID from query param
        r"^https?://music\.apple\.com/[a-z]{2}/album/[^/]+/(?P<id>\d{7,12})\?i=(?P<song_id>\d{7,12})",
        # Album URL (no song deep-link) → album ID
        r"^https?://music\.apple\.com/[a-z]{2}/album/[^/]+/(?P<id>\d{7,12})(?:\?(?!i=).*)?$",
        # Song URL → song ID
        r"^https?://music\.apple\.com/[a-z]{2}/song/[^/]+/(?P<id>\d{7,12})(?:\?.*)?$",
        # Playlist URL → pl.xxx ID
        r"^https?://music\.apple\.com/[a-z]{2}/playlist/[^/]+/(?P<id>pl\.[a-zA-Z0-9]+)(?:\?.*)?$",
        # Raw numeric ID (song)
        r"^(?P<id>\d{7,12})$",
        # Raw playlist ID
        r"^(?P<id>pl\.[a-zA-Z0-9]+)$",
    ]

    BASE_URL = "https://music.apple.com"
    API_BASE = "https://api.music.apple.com/v1"
    AMPAPI_BASE = "https://amp-api.music.apple.com/v1"

    # Map ISO country codes to Apple storefront codes
    STOREFRONT_MAP = {
        "US": "us", "GB": "gb", "DE": "de", "FR": "fr",
        "CA": "ca", "AU": "au", "JP": "jp", "BR": "br",
        "MX": "mx", "ES": "es", "IT": "it", "NL": "nl",
        "IN": "in", "SE": "se", "CH": "ch", "AT": "at",
        "BE": "be", "DK": "dk", "FI": "fi", "IE": "ie",
        "NO": "no", "PT": "pt", "PL": "pl", "TR": "tr",
        "CN": "cn", "HK": "hk", "TW": "tw", "KR": "kr",
        "SG": "sg", "AE": "ae", "SA": "sa", "ZA": "za",
        "NZ": "nz", "CL": "cl", "CO": "co", "AR": "ar",
        "PH": "ph", "MY": "my", "TH": "th", "ID": "id",
    }

    @staticmethod
    @click.command(name="AppleMusic", short_help="https://music.apple.com")
    @click.argument("title", type=str, required=False)
    @click.option(
        "-sf", "--storefront", type=str, default=None,
        help="Override storefront/region (e.g. 'us', 'in', 'gb'). Auto-detected from URL."
    )
    @click.option(
        "-q", "--quality", type=str, default="lossless",
        help="Audio quality: 'lossless' (ALAC/FLAC), 'hires-lossless' (Hi-Res), 'high' (256k AAC), 'standard' (64k AAC). Default: lossless."
    )
    @click.pass_context
    def cli(ctx, **kwargs):
        return AppleMusic(ctx, **kwargs)

    def __init__(self, ctx, title, storefront, quality):
        super().__init__(ctx)
        self.raw_title = title or ctx.parent.params.get("title")
        self.storefront_override = storefront
        self.quality = quality or "lossless"
        self.cdm = ctx.obj.cdm
        self.acodec = ctx.parent.params.get("acodec")

        self.developer_token = None
        self.media_user_token = None
        self.storefront = None
        self.content_type = None   # "song", "album", or "playlist"
        self.song_id = None        # set when ?i=SONG_ID deep-link is in URL

        self.configure()
        self._parse_title_with_song_id(ctx, self.raw_title)
        self._detect_content_type()


    # ─────────────────────────── Configuration ──────────────────────────────

    def configure(self):
        """Set up session: get developer token, media-user-token, headers."""
        self.log.info("Configuring Apple Music session...")
        self._get_developer_token()
        self._get_media_user_token()
        self._detect_storefront()
        self._update_headers()

    def _get_developer_token(self):
        """
        Extract the JWT developer token used by Apple Music APIs.
        The token is embedded within the JavaScript web app assets at music.apple.com.
        """
        self.log.info("Acquiring Apple Music developer token...")

        # Source 1: Parse JS bundle assets from music.apple.com
        try:
            res = self.session.get(f"{self.BASE_URL}/in/browse", timeout=15)
            js_urls = re.findall(r'src="([^"]+\.js)"', res.text)
            for js_path in js_urls:
                if not ("index" in js_path or "musickit" in js_path or "app" in js_path):
                    continue
                js_url = js_path if js_path.startswith("http") else f"{self.BASE_URL}{js_path}"
                try:
                    js_res = self.session.get(js_url, timeout=15)
                    tokens = re.findall(r'eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', js_res.text)
                    if tokens:
                        self.developer_token = tokens[0]
                        self.log.info(f"✓ Got developer token from Apple Music JS bundle ({js_path[:40]}...)")
                        return
                except Exception as e:
                    self.log.debug(f"Failed scanning JS bundle {js_path}: {e}")
        except Exception as e:
            self.log.debug(f"Failed fetching music.apple.com page: {e}")

        # Source 2: tv.apple.com inline JSON (fallback)
        try:
            res = self.session.get("https://tv.apple.com", timeout=15)
            m = re.search(r'"developerToken"\s*:\s*"([^"]+)"', res.text)
            if m:
                self.developer_token = m.group(1)
                self.log.info("✓ Got developer token from tv.apple.com inline JSON")
                return
        except Exception as e:
            self.log.debug(f"tv.apple.com inline JSON failed: {e}")

        if not self.developer_token:
            raise ValueError("Could not obtain Apple Music developer token from any source.")


    def _get_media_user_token(self):
        """Extract media-user-token from cookies (required for authenticated streams)."""
        for cookie in self.session.cookies:
            if cookie.name in ("media-user-token", "media_user_token") and cookie.value:
                self.media_user_token = cookie.value
                self.log.info("✓ Found media-user-token in cookies")
                return
        self.log.warning(
            "No 'media-user-token' cookie found — only preview/sample streams will be available. "
            "You need an Apple Music subscription and the cookie from music.apple.com."
        )

    def _detect_storefront(self):
        """Detect storefront from URL, cookie, or default to 'us'."""
        # 1. Manual override
        if self.storefront_override:
            self.storefront = self.storefront_override.lower()
            self.log.info(f"Using storefront override: {self.storefront}")
            return

        # 2. Extract from URL
        if self.raw_title:
            match = re.search(
                r'music\.apple\.com/([a-z]{2})/', self.raw_title, re.IGNORECASE
            )
            if match:
                self.storefront = match.group(1).lower()
                self.log.info(f"Auto-detected storefront from URL: {self.storefront}")
                return

        # 3. Try 'itua' cookie (country code)
        for cookie in self.session.cookies:
            if cookie.name == "itua" and cookie.value:
                cc = cookie.value.strip().upper()[:2]
                self.storefront = self.STOREFRONT_MAP.get(cc, cc.lower())
                self.log.info(f"Storefront from 'itua' cookie: {self.storefront} ({cc})")
                return

        # 4. Default to US
        self.storefront = "us"
        self.log.info("Defaulting to US storefront")

    def _update_headers(self):
        """Update session headers with auth tokens."""
        headers = {
            "Authorization": f"Bearer {self.developer_token}",
            "Origin": self.BASE_URL,
            "Referer": f"{self.BASE_URL}/",
            "Accept": "application/json",
        }
        if self.media_user_token:
            headers["media-user-token"] = self.media_user_token
            headers["x-apple-music-user-token"] = self.media_user_token
        self.session.headers.update(headers)

    def _parse_title_with_song_id(self, ctx, title):
        """
        Parse title URL. Handles the special Apple Music pattern
        music.apple.com/.../album/.../ALBUM_ID?i=SONG_ID — when ?i= is present
        we treat the request as a single-song download using the song ID.
        """
        if not title:
            self.log.exit(" - No title ID specified")
            return

        # First try the ?i= deep-link pattern (album URL with embedded song ID)
        m = re.search(
            r'music\.apple\.com/[a-z]{2}/album/[^/]+/(\d{7,12})\?i=(\d{7,12})',
            title
        )
        if m:
            self.title = m.group(1)       # album ID (kept for context)
            self.song_id = m.group(2)     # song ID takes priority
            self.log.info(f"Deep-link detected: album={self.title}, song={self.song_id}")
            return

        # Fall back to standard parse_title for all other URL shapes
        self.parse_title(ctx, title)

    def _detect_content_type(self):
        """Detect whether the URL refers to a song, album, or playlist."""
        if not self.raw_title:
            return

        # ?i=SONG_ID deep-link → always a single song
        if self.song_id:
            self.content_type = "song"
        elif "/song/" in self.raw_title or re.match(r"^\d{7,12}$", self.title or ""):
            self.content_type = "song"
        elif "/album/" in self.raw_title:
            self.content_type = "album"
        elif "/playlist/" in self.raw_title or (self.title or "").startswith("pl."):
            self.content_type = "playlist"
        else:
            self.content_type = "song"

        self.log.info(f"Content type detected: {self.content_type}")


    # ──────────────────────────── API Helpers ────────────────────────────────

    def _api_get(self, path: str, params: dict = None) -> dict:
        """Make an authenticated request to the Apple Music API."""
        url = f"{self.API_BASE}{path}"
        resp = self.session.get(url, params=params or {}, timeout=15)
        if resp.status_code == 401:
            raise ValueError(
                "Apple Music API returned 401 Unauthorized. "
                "Check your developer token and media-user-token."
            )
        resp.raise_for_status()
        return resp.json()

    def _get_song_metadata(self, song_id: str) -> dict:
        """Fetch metadata for a single song."""
        data = self._api_get(
            f"/catalog/{self.storefront}/songs/{song_id}",
            params={"include": "albums,artists"}
        )
        return data["data"][0] if data.get("data") else {}

    def _get_album_songs(self, album_id: str) -> list:
        """Fetch all songs in an album."""
        tracks = []
        offset = 0
        limit = 100
        while True:
            data = self._api_get(
                f"/catalog/{self.storefront}/albums/{album_id}/tracks",
                params={"limit": limit, "offset": offset}
            )
            items = data.get("data", [])
            tracks.extend(items)
            next_url = data.get("next")
            if not next_url or len(items) < limit:
                break
            offset += limit
        return tracks

    def _get_playlist_songs(self, playlist_id: str) -> list:
        """Fetch all songs in a playlist."""
        tracks = []
        offset = 0
        limit = 100
        while True:
            data = self._api_get(
                f"/catalog/{self.storefront}/playlists/{playlist_id}/tracks",
                params={"limit": limit, "offset": offset}
            )
            items = data.get("data", [])
            tracks.extend(items)
            next_url = data.get("next")
            if not next_url or len(items) < limit:
                break
            offset += limit
        return tracks

    def _get_stream_url(self, song_id: str) -> Optional[str]:
        """
        Get the HLS stream URL for a song.
        Uses the amp-api endpoint which returns playback/stream info.
        """
        try:
            # Try authenticated stream first (requires subscription)
            resp = self.session.get(
                f"{self.AMPAPI_BASE}/catalog/{self.storefront}/songs/{song_id}",
                params={
                    "include[songs]": "lyrics,albums",
                    "extend": "extendedAssetUrls",
                    "fields[songs]": "extendedAssetUrls,previews,url",
                },
                timeout=15
            )
            if resp.ok:
                data = resp.json()
                items = data.get("data", [])
                if items:
                    attrs = items[0].get("attributes", {})

                    # Priority: lossless > hires > high > preview
                    ext_urls = attrs.get("extendedAssetUrls", {})
                    quality_map = {
                        "hires-lossless": ext_urls.get("enhancedHls"),
                        "lossless": ext_urls.get("enhancedHls"),
                        "high": ext_urls.get("plus"),
                        "standard": None,
                    }
                    stream_url = quality_map.get(self.quality) or ext_urls.get("enhancedHls")

                    if stream_url:
                        self.log.info(f"✓ Got authenticated stream URL ({self.quality})")
                        return stream_url

                    # Fallback to preview
                    previews = attrs.get("previews", [])
                    if previews:
                        self.log.warning("⚠ Using preview stream (no subscription or entitlement)")
                        return previews[0].get("url")
        except Exception as e:
            self.log.debug(f"Stream URL fetch failed: {e}")

        return None

    # ──────────────────────────── VT Interface ───────────────────────────────

    def get_titles(self) -> List[Title]:
        """Return list of Title objects for the requested content."""
        titles = []

        if self.content_type == "song":
            # Use song_id from ?i= deep-link if present, otherwise self.title
            song_id = self.song_id or self.title
            self.log.info(f"Fetching song metadata for ID: {song_id}")
            meta = self._get_song_metadata(song_id)
            if not meta:
                raise self.log.exit(f" - Song ID {song_id!r} not found.")

            attrs = meta.get("attributes", {})
            titles.append(self._make_title_from_song(meta, attrs))

        elif self.content_type == "album":
            album_id = self.title
            self.log.info(f"Fetching album tracks for ID: {album_id}")

            # Get album name
            album_data = self._api_get(f"/catalog/{self.storefront}/albums/{album_id}")
            album_attrs = (album_data.get("data") or [{}])[0].get("attributes", {})
            album_name = album_attrs.get("name", "Unknown Album")

            songs = self._get_album_songs(album_id)
            if not songs:
                raise self.log.exit(f" - No tracks found for album {album_id!r}.")

            self.log.info(f"Found {len(songs)} tracks in '{album_name}'")
            for song in songs:
                attrs = song.get("attributes", {})
                titles.append(self._make_title_from_song(song, attrs, album_name=album_name))

        elif self.content_type == "playlist":
            playlist_id = self.title
            self.log.info(f"Fetching playlist tracks for ID: {playlist_id}")

            # Get playlist name
            pl_data = self._api_get(f"/catalog/{self.storefront}/playlists/{playlist_id}")
            pl_attrs = (pl_data.get("data") or [{}])[0].get("attributes", {})
            pl_name = pl_attrs.get("name", "Unknown Playlist")

            songs = self._get_playlist_songs(playlist_id)
            if not songs:
                raise self.log.exit(f" - No tracks found in playlist {playlist_id!r}.")

            self.log.info(f"Found {len(songs)} tracks in playlist '{pl_name}'")
            for i, song in enumerate(songs):
                attrs = song.get("attributes", {})
                titles.append(self._make_title_from_song(
                    song, attrs, playlist_name=pl_name, track_num=i + 1
                ))
        else:
            raise self.log.exit(" - Unknown content type. Provide a song/album/playlist URL.")

        return titles

    def _make_title_from_song(
        self,
        song: dict,
        attrs: dict,
        album_name: str = None,
        playlist_name: str = None,
        track_num: int = None
    ) -> Title:
        """Create a VT Title object from Apple Music song metadata."""
        song_id = song.get("id", self.title)
        name = attrs.get("name", "Unknown")
        artist = attrs.get("artistName", "Unknown Artist")
        disc_num = attrs.get("discNumber", 1)
        track_number = track_num or attrs.get("trackNumber", 1)
        release_date = attrs.get("releaseDate", "")
        year = int(release_date[:4]) if release_date and len(release_date) >= 4 else None
        album = album_name or attrs.get("albumName", "")
        duration_ms = attrs.get("durationInMillis", 0)

        # Use playlist name as the "show" context if in playlist mode
        container = playlist_name or album or artist

        # Determine language tag from attributes, genre, or storefront fallback
        lang_tag = attrs.get("languageTag")
        if not lang_tag:
            genres = [g.lower() for g in attrs.get("genreNames", [])]
            if any("bollywood" in g or "hindi" in g for g in genres):
                lang_tag = "hi"
            elif any("punjabi" in g for g in genres):
                lang_tag = "pa"
            elif any("tamil" in g for g in genres):
                lang_tag = "ta"
            elif any("telugu" in g for g in genres):
                lang_tag = "te"
            elif any("malayalam" in g for g in genres):
                lang_tag = "ml"
            elif any("kannada" in g for g in genres):
                lang_tag = "kn"
            elif any("bengali" in g or "marathi" in g or "indian" in g for g in genres):
                lang_tag = "hi" if self.storefront == "in" else "en"
            elif self.storefront == "in":
                lang_tag = "hi"
            else:
                lang_tag = "en"

        return Title(
            id_=song_id,
            type_=Title.Types.MOVIE,   # Music tracks map to MOVIE (single-file) type
            name=f"{artist} - {name}",
            year=year,
            original_lang=lang_tag,
            source=self.ALIASES[0],
            service_data={
                "id": song_id,
                "name": name,
                "artist": artist,
                "album": album,
                "playlist": playlist_name,
                "disc": disc_num,
                "track": track_number,
                "duration_ms": duration_ms,
                "year": year,
                "container": container,
                "attributes": attrs,
            }
        )

    def get_tracks(self, title: Title) -> Tracks:
        """Get audio tracks for a title (song)."""
        song_id = title.service_data["id"]
        self.log.info(f" - Getting stream for: {title.service_data['name']} by {title.service_data['artist']}")

        stream_url = self._get_stream_url(song_id)
        if not stream_url:
            raise self.log.exit(
                f" - Could not get stream URL for song '{title.service_data['name']}'. "
                "Ensure you have a valid media-user-token cookie (Apple Music subscription required)."
            )

        self.log.info(f" - Fetching HLS manifest: {stream_url[:80]}...")

        # Fetch and parse the HLS manifest
        res = self.session.get(stream_url, timeout=15)
        res.raise_for_status()
        master = m3u8.loads(res.text, stream_url)

        raw_tracks = Tracks.from_m3u8(master, source=self.ALIASES[0])

        audios = list(raw_tracks.audios)

        # Parse session key info if present in master manifest
        session_key_info = None
        if hasattr(master, "session_data"):
            for sd in master.session_data:
                if getattr(sd, "data_id", "") == "com.apple.hls.AudioSessionKeyInfo":
                    session_key_info = getattr(sd, "value", None)
                    break

        # HLS master playlist variants (#EXT-X-STREAM-INF) are placed into raw_tracks.videos by default
        for i, v in enumerate(raw_tracks.videos):
            codec_raw = (v.codec or "").lower()
            url_str = str(getattr(v, "url", "") or "").lower()

            if "alac" in codec_raw or "alac" in url_str:
                codec = "alac"
            elif "ec-3" in codec_raw or "atmos" in url_str:
                codec = "ec-3"
            elif "mp4a.40.2" in codec_raw or "256" in url_str or "128" in url_str:
                codec = "aac"
            elif "mp4a.40.5" in codec_raw or "64" in url_str:
                codec = "he-aac"
            else:
                codec = v.codec or "aac"

            is_atmos = "atmos" in url_str or "ec-3" in codec_raw
            channels = "5.1" if is_atmos else "2.0"

            audio_track = AudioTrack(
                id_=f"audio-{i}",
                source=self.ALIASES[0],
                url=v.url,
                codec=codec,
                language=getattr(title, "original_lang", None) or "en",
                bitrate=v.bitrate or 256000,
                descriptor=v.descriptor,
                encrypted=True,
                atmos=is_atmos,
                channels=channels,
                extra=v.extra if hasattr(v, "extra") else None
            )

            # Apple Music uses data URIs with session key info for licensing
            # Set a valid default dummy PSSH & KID so VT's licensing stage proceeds
            audio_track.pssh = Box.parse(Box.build({
                "type": b"pssh",
                "version": 0,
                "flags": 0,
                "system_ID": uuid.UUID("edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"),
                "init_data": b"\x08\x01\x12\x10" + b"\x00" * 16
            }))
            audio_track.kid = "00000000000000000000000000000000"

            if session_key_info:
                audio_track.key_info_b64 = session_key_info

            audios.append(audio_track)

        if not audios:
            raise self.log.exit(" - No audio tracks found in HLS manifest.")

        self.log.info(f" - Found {len(audios)} audio track(s)")
        return Tracks(audios)




    def get_chapters(self, title: Title) -> list:
        """Apple Music tracks do not have chapters."""
        return []

    def certificate(self, **_) -> Optional[bytes]:
        """Return None to use the common Widevine privacy certificate."""
        return None

    def license(self, challenge: bytes, track, title=None, **_) -> bytes:
        """Request a Widevine or FairPlay license from Apple's license server."""
        song_id = None
        if hasattr(track, "service_data") and track.service_data:
            song_id = track.service_data.get("id")
        elif hasattr(title, "service_data") and title.service_data:
            song_id = title.service_data.get("id")
        elif hasattr(self, "song_id") and self.song_id:
            song_id = self.song_id
        elif hasattr(self, "title") and self.title:
            song_id = self.title

        key_info_b64 = getattr(track, "key_info_b64", None)
        if key_info_b64:
            uri = f"data:text/plain;keyInfo={key_info_b64};charset=UTF-16;base64,{base64.b64encode(b'').decode()}"
        else:
            pssh_box = Box.build(track.pssh) if hasattr(track, "pssh") and track.pssh else b""
            pssh_b64 = base64.b64encode(pssh_box).decode("utf-8")
            uri = f"data:text/plain;base64,{pssh_b64}"

        streaming_key = {
            "id": 1,
            "uri": uri,
            "challenge": base64.b64encode(challenge).decode("utf-8"),
            "key-system": "com.widevine.alpha",
            "lease-action": "start",
            "isExternal": True
        }
        if song_id:
            streaming_key["adamId"] = str(song_id)

        data = {
            "streaming-request": {
                "version": 1,
                "streaming-keys": [streaming_key]
            }
        }

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Origin": self.BASE_URL,
            "Referer": f"{self.BASE_URL}/",
            "x-apple-renewal": "true"
        }
        if song_id:
            headers["X-Apple-Adam-Id"] = str(song_id)

        license_url = self.config["endpoints"]["license"]
        resp = self.session.post(
            url=license_url,
            json=data,
            headers=headers,
        )
        resp.raise_for_status()
        res = resp.json()

        key_response = res.get("streaming-response", {}).get("streaming-keys", [{}])[0]
        status = key_response.get("status", -1)

        if status != 0:
            err_code = key_response.get("errorCode") or status
            raise self.log.exit(
                f" - License request failed with status {status} (errorCode: {err_code}). "
                "Check your subscription and credentials."
            )

        license_b64 = key_response.get("license")
        if not license_b64:
            raise self.log.exit(" - No license data in response.")

        return base64.b64decode(license_b64)


