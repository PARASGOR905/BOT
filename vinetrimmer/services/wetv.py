from __future__ import annotations

import json
import random
import re
import string
import time
import click
from langcodes import Language
from yt_dlp.aes import aes_cbc_encrypt_bytes

from vinetrimmer.objects import Title, Tracks, VideoTrack, AudioTrack, TextTrack, Track
from vinetrimmer.services.BaseService import BaseService


class WeTV(BaseService):
    """
    WeTV (https://wetv.vip) - Tencent International Streaming

    \b
    Authorization: Cookies (cookies/WeTV/default.txt)
    Security: Unencrypted HLS VOD streams (free tier up to 720p; VIP unlocks 1080p)

    \b
    URL formats supported:
      - Series:  https://wetv.vip/en/play/{cid}
      - Episode: https://wetv.vip/en/play/{cid}/{vid}
      - Bare ID: g4101tg1v6t

    \b
    Notes:
    - Provides full uncut episodes (typically ~35-45 minutes).
    - Unencrypted HLS streams: downloads directly without DRM decryption issues.
    - Multilingual subtitles available: English, Chinese, Hindi, Thai, Vietnamese, etc.
    - Use VIP cookies in cookies/WeTV/default.txt to unlock 1080p (FHD) streams.
    """

    ALIASES = ["WETV", "wetv", "WeTV"]
    TITLE_RE = [
        r"^https?://(?:www\.)?wetv\.vip/(?:[a-z-]+/)?play/(?P<cid>[a-z0-9]+)(?:/(?P<vid>[a-z0-9]+))?",
        r"^(?P<vid>[a-z0-9]{10,12})$",
    ]

    _API_URL = "https://play.wetv.vip/getvinfo"
    _APP_VERSION = "3.5.57"
    _PLATFORM = "4830201"
    _HOST = "wetv.vip"
    _REFERER = "wetv.vip"

    @staticmethod
    @click.command(name="WeTV", short_help="https://wetv.vip")
    @click.argument("title", type=str, required=False)
    @click.option("-m", "--movie", is_flag=True, default=False, help="Title is a movie.")
    @click.option(
        "-q", "--quality", default="fhd",
        type=click.Choice(["fhd", "shd", "hd", "sd"], case_sensitive=False),
        help="Preferred stream quality: fhd=1080p, shd=720p, hd=480p, sd=360p (default: fhd).",
    )
    @click.pass_context
    def cli(ctx, **kwargs):
        return WeTV(ctx, **kwargs)

    def __init__(self, ctx, title=None, movie=False, quality="fhd"):
        super().__init__(ctx)
        self.title_input = title
        self.movie = movie
        self.quality = quality.lower()

    def configure(self):
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://wetv.vip/",
            "Origin": "https://wetv.vip",
        })

    # ------------------------------------------------------------------ #
    #  WeTV cKey generation (AES-CBC encrypted token)                     #
    # ------------------------------------------------------------------ #

    def _get_ckey(self, video_id: str, video_url: str, guid: str) -> str:
        ua = self.session.headers.get("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
        payload = (
            f"{video_id}|{int(time.time())}|mg3c3b04ba|{self._APP_VERSION}|{guid}|"
            f"{self._PLATFORM}|{video_url[:48]}|{ua.lower()[:48]}||Mozilla|Netscape|Windows x86_64|00|"
        )
        return aes_cbc_encrypt_bytes(
            bytes(f"|{sum(map(ord, payload))}|{payload}", "utf-8"),
            b"Ok\xda\xa3\x9e/\x8c\xb0\x7f^r-\x9e\xde\xf3\x14",
            b"\x01PJ\xf3V\xe6\x19\xcf.B\xbb\xa6\x8c?p\xf9",
            padding_mode="whitespace",
        ).hex().upper()

    # ------------------------------------------------------------------ #
    #  Page scraping                                                       #
    # ------------------------------------------------------------------ #

    def _get_page_data(self, url: str) -> dict:
        r = self.session.get(url)
        r.raise_for_status()

        m = re.search(
            r'<script[^>]*id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
            r.text, re.DOTALL,
        )
        if not m:
            return {}

        try:
            next_data = json.loads(m.group(1))
            page_props = next_data.get("props", {}).get("pageProps", {})
            data_str = page_props.get("data")
            if isinstance(data_str, str):
                return json.loads(data_str)
            return data_str or {}
        except Exception as e:
            self.log.warning(f" - Failed to parse __NEXT_DATA__: {e}")
            return {}

    def _parse_url(self, url_or_id: str) -> tuple[str | None, str | None]:
        s = url_or_id.strip()

        # Episode URL: /en/play/{cid}/{vid}-slug
        m = re.search(r"/play/([a-z0-9]+)/([a-z0-9]+)", s)
        if m:
            return m.group(1), m.group(2)

        # Series URL: /en/play/{cid}-slug or /en/play/{cid}
        m = re.search(r"/play/([a-z0-9]+)(?:[-#]|$)", s)
        if m:
            return m.group(1), None

        # Bare video ID
        m = re.match(r"^([a-z0-9]{10,16})$", s)
        if m:
            return None, m.group(1)

        return None, None

    # ------------------------------------------------------------------ #
    #  get_titles                                                          #
    # ------------------------------------------------------------------ #

    def get_titles(self) -> Title | list[Title]:
        if not self.title_input:
            raise self.log.exit(" - No URL/ID provided.")

        cid, vid = self._parse_url(self.title_input)

        if vid and not cid:
            url = f"https://wetv.vip/en/play/{vid}"
            self.log.info(f" + Fetching WeTV video page: {url}")
            data = self._get_page_data(url)
            return self._build_single_title(vid, data)

        if vid and cid:
            url = f"https://wetv.vip/en/play/{cid}/{vid}"
            self.log.info(f" + Fetching WeTV episode page: {url}")
            data = self._get_page_data(url)
            return self._build_single_title(vid, data, cid)

        if cid:
            url = f"https://wetv.vip/en/play/{cid}"
            self.log.info(f" + Fetching WeTV series page: {url}")
            data = self._get_page_data(url)
            return self._build_series_titles(cid, data)

        raise self.log.exit(f" - Could not parse URL/ID: {self.title_input!r}")

    def _build_single_title(self, vid: str, data: dict, cid: str | None = None) -> Title:
        video_info = data.get("videoInfo") or {}
        cover_info = data.get("coverInfo") or {}

        series_name = cover_info.get("title") or "WeTV Content"
        ep_num_str = video_info.get("episode") or video_info.get("manEpisode") or "1"
        try:
            ep_num = int(ep_num_str)
        except (ValueError, TypeError):
            ep_num = 1

        is_movie = self.movie or video_info.get("type") == "1"

        return Title(
            id_=vid,
            type_=Title.Types.MOVIE if is_movie else Title.Types.TV,
            name=series_name,
            season=1 if not is_movie else None,
            episode=ep_num if not is_movie else None,
            episode_name=video_info.get("title"),
            original_lang=Language.get("zh"),
            source=self.ALIASES[0],
            service_data={
                "vid": vid,
                "cid": cid or cover_info.get("cid"),
            },
        )

    def _build_series_titles(self, cid: str, data: dict) -> list[Title]:
        cover_info = data.get("coverInfo") or {}
        series_name = cover_info.get("title") or "WeTV Series"

        video_id_list = data.get("videoIdList") or []
        video_list_map = {
            ep["vid"]: ep
            for ep in (data.get("videoList") or [])
            if isinstance(ep, dict) and ep.get("vid")
        }

        if not video_id_list:
            raise self.log.exit(f" - No episode IDs found for series {cid}")

        self.log.info(f" + Found {len(video_id_list)} episodes for '{series_name}'")

        titles: list[Title] = []
        for idx, vid in enumerate(video_id_list, start=1):
            ep_info = video_list_map.get(vid, {})
            ep_num_str = ep_info.get("episode") or ep_info.get("manEpisode") or str(idx)
            try:
                ep_num = int(ep_num_str)
            except (ValueError, TypeError):
                ep_num = idx

            ep_title = ep_info.get("title") or f"Episode {ep_num}"

            titles.append(
                Title(
                    id_=vid,
                    type_=Title.Types.TV,
                    name=series_name,
                    season=1,
                    episode=ep_num,
                    episode_name=ep_title,
                    original_lang=Language.get("zh"),
                    source=self.ALIASES[0],
                    service_data={
                        "vid": vid,
                        "cid": cid,
                    },
                )
            )

        return titles

    # ------------------------------------------------------------------ #
    #  get_tracks                                                          #
    # ------------------------------------------------------------------ #

    def get_tracks(self, title: Title) -> Tracks:
        vid = title.service_data.get("vid")
        cid = title.service_data.get("cid") or ""
        if not vid:
            raise self.log.exit(" - Title missing vid.")

        video_url = f"https://wetv.vip/en/play/{cid}/{vid}" if cid else f"https://wetv.vip/en/play/{vid}"
        guid = "".join(random.choices(string.digits + string.ascii_lowercase, k=16))
        flowid = "".join(random.choices(string.digits + string.ascii_lowercase, k=32))

        ckey = self._get_ckey(vid, video_url, guid)

        query = {
            "vid": vid,
            "cid": cid,
            "cKey": ckey,
            "encryptVer": "8.1",
            "spcaptiontype": "1",  # VTT
            "sphls": "2",
            "dtype": "3",          # HLS
            "defn": self.quality,
            "spsrt": "2",          # Enable subtitles
            "sphttps": "1",
            "otype": "json",
            "spwm": "1",
            "hevclv": "28",
            "drm": "40",
            "spvideo": "4",
            "spsfrhdr": "100",
            "host": self._HOST,
            "referer": self._REFERER,
            "ehost": video_url,
            "appVer": self._APP_VERSION,
            "platform": self._PLATFORM,
            "guid": guid,
            "flowid": flowid,
        }

        self.log.info(f" + Fetching stream info for {title.name} [{vid}] ({self.quality.upper()})")

        r = self.session.get(self._API_URL, params=query)
        r.raise_for_status()

        text = r.text.strip()
        if "QZOutputJson=" in text:
            text = text.split("QZOutputJson=")[1].rstrip(";")
            if text.startswith("("):
                text = text[1:]
            if text.endswith(")"):
                text = text[:-1]

        try:
            api_resp = json.loads(text)
        except Exception as e:
            self.log.warning(f" - Failed to parse WeTV API response: {e}")
            return Tracks()

        vl = api_resp.get("vl") or {}
        vi_list = vl.get("vi") or []
        if not vi_list:
            msg = api_resp.get("msg") or str(api_resp)
            self.log.warning(f" - No video streams returned: {msg}")
            return Tracks()

        video_resp = vi_list[0]
        ui_list = video_resp.get("ul", {}).get("ui", [])

        # Find best available manifest URL
        manifest_url = None
        for ui in ui_list:
            base_url = ui.get("url") or ""
            hls_pt = ui.get("hls", {}).get("pt") or ""
            if base_url:
                manifest_url = base_url + hls_pt
                break

        if not manifest_url:
            self.log.warning(f" - No manifest URL found for vid={vid}")
            return Tracks()

        # Parse resolution, duration, bitrate
        width = video_resp.get("vw") or 1280
        height = video_resp.get("vh") or 720
        duration = float(video_resp.get("td") or 0.0)

        # Determine actual resolution from dimensions
        # Note: Ultrawide 2.39:1 aspect ratio means:
        #   720p = 1280x534
        #   1080p = 1920x804
        if width >= 1900 or height >= 1000:
            actual_res = "1080P"
        elif width >= 1200 or height >= 500:
            actual_res = "720P"
        elif width >= 800 or height >= 400:
            actual_res = "480P"
        else:
            actual_res = "360P"

        if self.quality == "fhd" and actual_res != "1080P":
            self.log.info(
                f" + Note: Free tier stream received ({width}x{height}, 720P). "
                f"1080P requires WeTV VIP cookies in cookies/WeTV/default.txt"
            )

        res_str = actual_res

        # Match format info from fl.fi list
        fl_fi = api_resp.get("fl", {}).get("fi", [])
        matched_fi = next(
            (f for f in fl_fi if f.get("name") == self.quality or f.get("resolution") == actual_res),
            None,
        ) or (fl_fi[-1] if fl_fi else {})
        size_bytes = matched_fi.get("fs")

        # Estimate bitrate from size and duration
        if size_bytes and duration > 0:
            bitrate = int((int(size_bytes) * 8) / duration)
        else:
            bitrate = 1200000

        self.log.info(f" + Stream resolved: {width}x{height} ({res_str}), duration: {int(duration)}s")

        tracks = Tracks()

        # Add VideoTrack
        vid_track = VideoTrack(
            id_=f"video-{vid}-{res_str}",
            source=self.ALIASES[0],
            url=manifest_url,
            codec="avc1",
            language=Language.get("zh"),
            bitrate=bitrate,
            width=int(width),
            height=int(height),
            fps=24.0,
            size=int(size_bytes) if size_bytes else None,
            descriptor=Track.Descriptor.M3U,
            encrypted=False,
            duration=duration,
        )
        tracks.add(vid_track)

        # Add AudioTrack (WeTV HLS TS segments contain multiplexed AAC audio)
        aud_track = AudioTrack(
            id_=f"audio-{vid}-zh",
            source=self.ALIASES[0],
            url=manifest_url,
            codec="aac",
            language=Language.get("zh"),
            bitrate=128000,
            channels="2.0",
            descriptor=Track.Descriptor.M3U,
            encrypted=False,
            duration=duration,
        )
        tracks.add(aud_track)

        # Add Subtitles from sfl.fi
        sfl_fi = api_resp.get("sfl", {}).get("fi", [])
        for sub in sfl_fi:
            sub_url = sub.get("url")
            lang_str = sub.get("lang") or "en"
            if not sub_url:
                continue

            try:
                lang_obj = Language.get(lang_str.lower())
            except Exception:
                lang_obj = Language.get("en")

            sub_track = TextTrack(
                id_=f"sub-{vid}-{lang_str.lower()}",
                source=self.ALIASES[0],
                url=sub_url,
                codec="vtt",
                language=lang_obj,
                descriptor=Track.Descriptor.M3U if ".m3u8" in sub_url else Track.Descriptor.URL,
                encrypted=False,
            )
            tracks.add(sub_track)

        return tracks

    def get_chapters(self, title: Title) -> list:
        return []

    def certificate(self, **_):
        return None

    def license(self, challenge: bytes, **_) -> bytes | None:
        return None
