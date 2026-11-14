from __future__ import annotations

import base64
import json
import re
import click
import requests
import m3u8

from vinetrimmer.objects import Title, Tracks, TextTrack
from vinetrimmer.services.BaseService import BaseService


class Vivamax(BaseService):
    """
    Service code for Vivamax / VMX (https://www.vivamax.net/ / https://vmx.ph/)
    """

    ALIASES = ["VMX", "vivamax", "Vivamax"]
    GEOFENCE = []

    TITLE_RE = [
        r"https?://(?:www\.|web\.)?vivamax\.net/(?:watch|movie|movies|shows)/(?P<id>[a-zA-Z0-9_-]+)",
        r"https?://(?:www\.)?vmx\.ph/(?:watch|movie|movies|shows)/(?P<id>[a-zA-Z0-9_-]+)",
        r"^(?P<id>[a-zA-Z0-9_-]+)$",
    ]

    @staticmethod
    @click.command(name="Vivamax", short_help="https://www.vivamax.net/", help=__doc__)
    @click.argument("title", type=str)
    @click.pass_context
    def cli(ctx, **kwargs):
        return Vivamax(ctx, **kwargs)

    def __init__(self, ctx, title):
        self.url = title
        super().__init__(ctx)
        self.parse_title(ctx, self.url)

        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://web.vivamax.net",
            "Referer": "https://web.vivamax.net/",
        })

    def get_titles(self) -> Title | list[Title]:
        # Retrieve authorization token
        auth_token = (
            self.config.get("headers", {}).get("Authorization")
            or self.session.cookies.get("Authorization")
            or self.session.cookies.get("token")
            or self.session.cookies.get("access_token")
        )
        headers = {}
        if auth_token:
            headers["Authorization"] = auth_token
            if not auth_token.lower().startswith("bearer "):
                headers["Authorization"] = f"Bearer {auth_token}"
        else:
            self.log.warning("No Authorization token or cookies (Authorization, token, access_token) found. The request might fail with a 404 or 401.")

        # Fetch movie info metadata
        metadata_url = f"https://api2.vivamax.net/v1/movies/{self.title}"
        self.log.info(f"Requesting movie metadata from: {metadata_url}")

        try:
            r = self.session.get(metadata_url, headers=headers)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            self.log.warning(f"Failed to fetch metadata from {metadata_url}: {e}. Using fallback title info.")
            data = {}

        name = data.get("title") or data.get("name") or f"Vivamax Movie {self.title}"
        year = None
        release_date = data.get("release_date") or data.get("year")
        if release_date:
            try:
                year = int(str(release_date)[:4])
            except Exception:
                pass

        return Title(
            id_=self.title,
            type_=Title.Types.MOVIE,
            name=name,
            year=year,
            source=self.ALIASES[0],
            service_data={},
        )

    def get_tracks(self, title):
        # Retrieve authorization token
        auth_token = (
            self.config.get("headers", {}).get("Authorization")
            or self.session.cookies.get("Authorization")
            or self.session.cookies.get("token")
            or self.session.cookies.get("access_token")
        )
        headers = {}
        if auth_token:
            headers["Authorization"] = auth_token
            if not auth_token.lower().startswith("bearer "):
                headers["Authorization"] = f"Bearer {auth_token}"

        # Request playback manifest and license info
        play_url = self.config["endpoints"]["play"].format(title_id=self.title)
        self.log.info(f"Requesting playback info from: {play_url}")

        try:
            r = self.session.get(play_url, headers=headers)
            r.raise_for_status()
            play_data = r.json()
        except requests.HTTPError as e:
            if e.response.status_code == 401:
                self.log.exit(" - Access denied: Unauthorized. Make sure you set a valid cookie or authorization token in vinetrimmer.yml")
            raise self.log.exit(f" - Failed to get playback info: {e}")

        self.log.debug(f"Play response data: {json.dumps(play_data)}")

        manifest_url = None
        license_url = None

        # Recursively find manifest (.mpd / .m3u8) and license URLs
        def find_url(d):
            nonlocal manifest_url, license_url
            if isinstance(d, dict):
                for k, v in d.items():
                    if isinstance(v, str):
                        if ".mpd" in v or ".m3u8" in v:
                            manifest_url = v
                        elif "onwards-widevine" in v or "license" in v:
                            license_url = v
                    else:
                        find_url(v)
            elif isinstance(d, list):
                for item in d:
                    find_url(item)

        find_url(play_data)

        if not manifest_url:
            manifest_url = play_data.get("stream_url") or play_data.get("url") or play_data.get("manifest")
        
        if not license_url:
            license_url = play_data.get("license_url") or play_data.get("license") or self.config["endpoints"]["license"]

        if not manifest_url:
            raise self.log.exit(f" - Could not find stream manifest URL in play response: {play_data}")

        self.log.info(f"Found manifest URL: {manifest_url}")
        self.log.info(f"Found license URL: {license_url}")

        title.service_data["license_url"] = license_url

        if ".mpd" in manifest_url:
            self.log.info("Loading tracks from DASH manifest")
            tracks = Tracks.from_mpd(
                url=manifest_url,
                session=self.session,
                source=self.ALIASES[0]
            )
        else:
            self.log.info("Loading tracks from HLS manifest")
            tracks = Tracks.from_m3u8(
                m3u8.loads(self.session.get(manifest_url).text, manifest_url),
                source=self.ALIASES[0]
            )

        return tracks

    def get_chapters(self, title):
        return []

    def certificate(self, **_):
        return None

    def license(self, challenge, title, track, session_id, **_):
        lic_url = title.service_data.get("license_url") or self.config["endpoints"]["license"]
        self.log.info(f"Requesting Widevine license from: {lic_url}")

        auth_token = self.config.get("headers", {}).get("Authorization") or self.session.cookies.get("Authorization") or self.session.cookies.get("token")
        headers = {
            "Content-Type": "application/octet-stream",
            "Accept": "*/*",
            "Referer": "https://web.vivamax.net/",
            "Origin": "https://web.vivamax.net",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        }
        if auth_token:
            headers["Authorization"] = auth_token
            if not auth_token.lower().startswith("bearer "):
                headers["Authorization"] = f"Bearer {auth_token}"

        r = self.session.post(lic_url, data=challenge, headers=headers)
        r.raise_for_status()
        return r.content
