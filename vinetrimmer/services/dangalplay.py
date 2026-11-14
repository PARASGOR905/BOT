from __future__ import annotations

import base64
import json
import re
import urllib.parse
import click
import m3u8

from vinetrimmer.objects import AudioTrack, TextTrack, Title, Track, Tracks, VideoTrack
from vinetrimmer.services.BaseService import BaseService


class DangalPlay(BaseService):
    """
    Service code for DangalPlay & VideosTech (VT) OTT Platform (https://www.dangalplay.com).

    Supports movies and web series powered by VideosTech / Eshkol Microplay cloud backend.

    Authorization: Cookies / Login token or Guest authentication.
    Security     : HLS (.m3u8) / DASH (.mpd) with Widevine DRM (EZDRM) or Clear streams.

    Input formats accepted:
      Full URL:   https://www.dangalplay.com/movies/dashing-khiladi-4-hindi
                  https://www.dangalplay.com/tv-shows/show-name-slug
      Short form: dangal:dashing-khiladi-4-hindi
                  vt:dashing-khiladi-4-hindi
    """

    ALIASES = ["DANGALPLAY", "dangalplay", "dangal", "Dangal", "VT", "vt"]
    GEOFENCE = []

    TITLE_RE = [
        r"^https?://(?:www\.)?dangalplay\.com/(?:movies|tv-shows|shows|video|movie)/(?P<id>[a-zA-Z0-9_-]+)",
        r"^(?:dangal|vt):(?P<id>[a-zA-Z0-9_-]+)$",
    ]

    STATIC_AUTH = "abd07061a3dd9851e3c9dd551e68e2688c5500a5139a561299a4ffbe40584f233f41d2b974b99122998fad15277a03408953d6e36ee6bbe9186b5cfb2a291f230a926bfef04810c774e7de5e94c5bf5f"
    AUTH_GEN_URL = "https://dangal-api.dangalplay.com/microplay/v1/generate/authToken/device/desktop/platform/web"
    STREAM_URL = "https://dangal-api.dangalplay.com/microplay/v1/content/get/stream/device/desktop/platform/web"
    WIDEVINE_LIC = "https://widevine-dash.ezdrm.com"

    @staticmethod
    @click.command(name="DangalPlay", short_help="https://www.dangalplay.com / VideosTech OTT", help=__doc__)
    @click.argument("title", type=str)
    @click.pass_context
    def cli(ctx, **kwargs):
        return DangalPlay(ctx, **kwargs)

    def __init__(self, ctx, title: str):
        self.url = title
        super().__init__(ctx)
        self.parse_title(ctx, self.url)

        self.slug = self.title
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            "Origin": "https://www.dangalplay.com",
            "Referer": "https://www.dangalplay.com/",
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "authorization": self.STATIC_AUTH,
            "country": "IN",
            "state": "Haryana",
            "language": "EN",
            "profileId": "1",
            "App-Id": "5005",
            "Platform": "web",
            "Device-Type": "web"
        })

        self.auth_token = self._get_auth_token()

    def _get_auth_token(self) -> str | None:
        try:
            r = self.session.post(self.AUTH_GEN_URL, json={}, timeout=10, hooks={"response": []})
            if r.status_code == 200:
                raw_b64 = r.json().get("authToken") or r.json().get("result", {}).get("authToken")
                if raw_b64:
                    jwt_token = base64.b64decode(raw_b64).decode('utf-8')
                    self.session.headers.update({
                        "accessToken": jwt_token,
                        "AccessToken": jwt_token,
                        "authToken": jwt_token,
                        "AuthToken": jwt_token
                    })
                    return jwt_token
        except Exception as e:
            self.log.warning(f" - Failed to acquire auth token: {e}")
        return None

    def get_titles(self) -> list[Title] | Title:
        slug = self.slug
        title_name = slug.replace("-", " ").title()

        title_obj = Title(
            id_=slug,
            type_=Title.Types.MOVIE,
            name=title_name,
            source=self.ALIASES[0],
            service_data={
                "slug": slug,
                "content_id": slug,
                "title_name": title_name
            }
        )
        return title_obj

    def get_tracks(self, title: Title) -> Tracks:
        slug = title.service_data.get("slug") or self.slug

        payload = {
            "contentId": slug,
            "deviceId": "web-uuid-12345",
            "osVersion": "125.0.0.0",
            "makeModel": "Chrome"
        }

        stream_data = None
        try:
            r = self.session.post(self.STREAM_URL, json=payload, timeout=10, hooks={"response": []})
            res_json = r.json() if r.text and r.text.startswith("{") else {}
            if r.status_code == 200 and res_json.get("code") == 1:
                stream_data = res_json.get("result") or res_json
            else:
                err_msg = res_json.get("error") or res_json.get("msg") or r.text
                self.log.error(f"Stream API returned HTTP {r.status_code}: {err_msg}")
        except Exception as e:
            self.log.error(f"HTTP request error fetching stream: {e}")

        if not stream_data or not stream_data.get("url"):
            self.log.error(
                f"Failed to fetch playback stream for {slug}. "
                "This title requires an active subscriber session or logged-in account cookies."
            )
            return Tracks()

        master_m3u8_url = stream_data.get("url")
        tracks = Tracks()

        try:
            r = self.session.get(master_m3u8_url)
            m3u8_obj = m3u8.loads(r.text)
            for playlist in m3u8_obj.playlists:
                bandwidth = playlist.stream_info.bandwidth
                resolution = f"{playlist.stream_info.resolution[0]}x{playlist.stream_info.resolution[1]}" if playlist.stream_info.resolution else "720p"
                tracks.add(
                    VideoTrack(
                        id=playlist.uri,
                        url=urllib.parse.urljoin(master_m3u8_url, playlist.uri),
                        bandwidth=bandwidth,
                        resolution=resolution,
                        codec=playlist.stream_info.codecs,
                    )
                )
        except Exception as e:
            self.log.warning(f"Failed parsing HLS manifest: {e}")

        return tracks

    def get_chapters(self, title: Title) -> list:
        return []

    def certificate(self, **_) -> str | None:
        return None

    def license(self, challenge: bytes, **_) -> bytes:
        r = self.session.post(self.WIDEVINE_LIC, data=challenge)
        return r.content
