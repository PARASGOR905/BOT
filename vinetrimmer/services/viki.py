from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
import click
from langcodes import Language

from vinetrimmer.objects import Title, Tracks
from vinetrimmer.services.BaseService import BaseService


class Viki(BaseService):
    """
    Rakuten Viki (https://www.viki.com)

    \b
    Authorization: Cookies (default.txt)
    Security: Widevine DRM (unencrypted streams also available for free content)

    \b
    Notes:
    - Viki's SSR returns a 4-second "Non Original Bumper" placeholder for non-Viki-Original
      content when fetching page props server-side. The real per-episode manifests are loaded
      client-side with JavaScript authentication.
    - This service attempts to resolve the real manifest from the vsId + CDN pattern.
    - If that fails, a clear warning is shown with the WeTV alternative.
    - Max resolution is 720p on Viki (platform limitation).
    """

    ALIASES = ["VIKI", "viki", "Viki"]
    TITLE_RE = r"^(?:https?://(?:www\.)?viki\.com/)?(?:tv|movies|videos)/(?P<id>[0-9a-z-]+)"

    # Viki Non-Original Bumper content ID (4-second placeholder)
    _BUMPER_ID = "1193129v"

    @staticmethod
    @click.command(name="Viki", short_help="https://www.viki.com")
    @click.argument("title", type=str, required=False)
    @click.pass_context
    def cli(ctx, **kwargs):
        return Viki(ctx, **kwargs)

    def __init__(self, ctx, title=None):
        super().__init__(ctx)
        self.title_input = title
        self.license_url: str | None = None

    def configure(self):
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.viki.com/",
            "Origin": "https://www.viki.com",
        })

    # ------------------------------------------------------------------ #
    #  Page scraping                                                       #
    # ------------------------------------------------------------------ #

    def _extract_page_props(self, url: str, retries: int = 3) -> tuple[dict, str]:
        """Scrape Next.js page props from a Viki page."""
        for attempt in range(1, retries + 1):
            try:
                r = self.session.get(url)
                r.raise_for_status()

                json_blobs = re.findall(
                    r'<script[^>]*type=["\']application/json["\'][^>]*>(.*?)</script>',
                    r.text, re.DOTALL,
                )
                for b in json_blobs:
                    try:
                        data = json.loads(b)
                        if "props" in data and "pageProps" in data["props"]:
                            props = data["props"]["pageProps"]
                            if props:
                                return props, r.text
                    except Exception:
                        continue
                return {}, r.text
            except Exception as e:
                if attempt == retries:
                    self.log.warning(f" - Failed to fetch page props for {url}: {e}")
                    return {}, ""
                time.sleep(1.0 * attempt)
        return {}, ""

    def _fetch_all_container_episodes(self, container_id: str) -> list[dict]:
        """Paginate through all container episodes via Viki API."""
        episodes: list[dict] = []
        page = 1
        while True:
            api_url = (
                f"https://api.viki.io/v4/containers/{container_id}/episodes.json"
                f"?app=100000a&per_page=50&direction=asc&page={page}"
            )
            try:
                r = self.session.get(api_url)
                if r.status_code != 200:
                    break
                data = r.json()
                items = data.get("response", [])
                if not items:
                    break
                episodes.extend(items)
                if not data.get("more"):
                    break
                page += 1
            except Exception as e:
                self.log.warning(f" - Error fetching container episodes API page {page}: {e}")
                break
        return episodes

    # ------------------------------------------------------------------ #
    #  get_titles                                                          #
    # ------------------------------------------------------------------ #

    def get_titles(self) -> Title | list[Title]:
        if not self.title_input:
            raise self.log.exit(" - No URL/ID provided.")

        input_str = self.title_input.strip()

        # Direct video URL or video ID (e.g. 1268838v)
        m_vid = re.search(r"(\d{6,8}v)", input_str)
        if m_vid or "/videos/" in input_str:
            video_id = m_vid.group(1) if m_vid else input_str.split("/")[-1]
            url = f"https://www.viki.com/videos/{video_id}"
            self.log.info(f" + Fetching Viki video page: {url}")
            props, _ = self._extract_page_props(url)

            video_meta = props.get("videoMetadataJson")
            if isinstance(video_meta, str):
                try:
                    video_meta = json.loads(video_meta)
                except Exception:
                    video_meta = None

            container_meta = props.get("containerJson")
            if isinstance(container_meta, str):
                try:
                    container_meta = json.loads(container_meta)
                except Exception:
                    container_meta = None

            title_name = "Viki Content"
            if video_meta and isinstance(video_meta, dict):
                title_name = (
                    video_meta.get("titles", {}).get("en")
                    or video_meta.get("titles", {}).get("container")
                    or title_name
                )
                ep_num = video_meta.get("number")
            else:
                ep_num = None

            if container_meta and isinstance(container_meta, dict) and title_name == "Viki Content":
                title_name = container_meta.get("titles", {}).get("en") or title_name

            return Title(
                id_=video_id,
                type_=Title.Types.TV if ep_num is not None else Title.Types.MOVIE,
                name=title_name,
                season=1 if ep_num is not None else None,
                episode=ep_num,
                original_lang=Language.get("ko"),
                source=self.ALIASES[0],
                service_data={
                    "video_id": video_id,
                    "url": url,
                },
            )

        # TV Series container URL (e.g. /tv/41419c-my-page-in-the-90s)
        url = input_str if input_str.startswith("http") else f"https://www.viki.com/tv/{input_str}"
        self.log.info(f" + Fetching Viki series page: {url}")
        props, html_text = self._extract_page_props(url)

        container_meta = props.get("containerJson")
        if isinstance(container_meta, str):
            try:
                container_meta = json.loads(container_meta)
            except Exception:
                container_meta = None

        series_name = "Viki Series"
        container_id = None
        if container_meta and isinstance(container_meta, dict):
            series_name = container_meta.get("titles", {}).get("en") or series_name
            container_id = container_meta.get("id")

        if not container_id:
            m_cid = re.search(r"(\d+[a-z]+)", input_str)
            if m_cid:
                container_id = m_cid.group(1)

        api_episodes = self._fetch_all_container_episodes(container_id) if container_id else []

        titles: list[Title] = []
        if api_episodes:
            self.log.info(f" + Found {len(api_episodes)} episodes via Viki container API")
            for ep in api_episodes:
                vid = ep.get("id")
                if not vid:
                    continue
                ep_num = ep.get("number") or 1
                ep_title = (
                    ep.get("titles", {}).get("en")
                    or ep.get("titles", {}).get("container")
                    or f"Episode {ep_num}"
                )
                titles.append(
                    Title(
                        id_=vid,
                        type_=Title.Types.TV,
                        name=series_name,
                        season=1,
                        episode=ep_num,
                        episode_name=ep_title,
                        original_lang=Language.get("ko"),
                        source=self.ALIASES[0],
                        service_data={
                            "video_id": vid,
                            "url": f"https://www.viki.com/videos/{vid}",
                        },
                    )
                )
        else:
            # Fallback to HTML regex extraction
            vids = list(dict.fromkeys(re.findall(r"(\d{6,8}v)", html_text)))
            if not vids:
                raise self.log.exit(f" - No episode video IDs found on series page: {url}")

            self.log.info(f" + Found {len(vids)} video IDs on series page HTML")
            for idx, vid in enumerate(vids, start=1):
                titles.append(
                    Title(
                        id_=vid,
                        type_=Title.Types.TV,
                        name=series_name,
                        season=1,
                        episode=idx,
                        episode_name=f"Episode {idx}",
                        original_lang=Language.get("ko"),
                        source=self.ALIASES[0],
                        service_data={
                            "video_id": vid,
                            "url": f"https://www.viki.com/videos/{vid}",
                        },
                    )
                )

        return titles

    # ------------------------------------------------------------------ #
    #  get_tracks                                                          #
    # ------------------------------------------------------------------ #

    def get_tracks(self, title: Title) -> Tracks:
        video_id = title.service_data.get("video_id")
        if not video_id:
            raise self.log.exit(" - Title missing video_id.")

        url = title.service_data.get("url") or f"https://www.viki.com/videos/{video_id}"
        self.log.info(f" + Fetching episode page props: {url}")
        props, _ = self._extract_page_props(url)

        # ── Extract stream JSON ───────────────────────────────────────────
        stream_json = props.get("videoPlaybackStreamJson")
        if isinstance(stream_json, str):
            try:
                stream_json = json.loads(stream_json)
            except Exception:
                stream_json = None

        if not stream_json or not isinstance(stream_json, dict):
            self.log.warning(f" - No videoPlaybackStreamJson for {video_id}")
            return Tracks()

        # ── Check for stream fetch error from page ─────────────────────
        stream_error = props.get("videoStreamFetchError")
        if stream_error:
            self.log.warning(f" - Viki stream fetch error: {stream_error}")

        # ── Extract DRM license URL ────────────────────────────────────
        drm_b64 = stream_json.get("drm")
        if drm_b64:
            try:
                drm_data = json.loads(base64.b64decode(drm_b64).decode("utf-8"))
                self.license_url = (
                    drm_data.get("dt1")
                    or drm_data.get("dt2")
                    or drm_data.get("dt3")
                )
                if self.license_url:
                    self.log.info(f" + License URL extracted: {self.license_url[:80]}...")
            except Exception as e:
                self.log.warning(f" - Failed to parse DRM license endpoints: {e}")

        # ── Decode the raw stream URL from page props ─────────────────
        streams = stream_json.get("streams", {})
        raw_manifest_url = None
        if isinstance(streams, dict):
            dash_info = streams.get("dash")
            hls_info = streams.get("hls")
            candidate = None
            if isinstance(dash_info, dict):
                candidate = dash_info.get("url")
            elif isinstance(hls_info, dict):
                candidate = hls_info.get("url")

            if candidate:
                # Viki wraps manifest URLs: 0.viki.io/b/e-stream-url?stream={base64}
                if "stream=" in candidate:
                    b64_val = candidate.split("stream=")[-1]
                    b64_val += "=" * (-len(b64_val) % 4)
                    try:
                        candidate = base64.b64decode(b64_val).decode("utf-8")
                    except Exception as e:
                        self.log.warning(f" - Failed to base64 decode manifest URL: {e}")
                        return Tracks()
                raw_manifest_url = candidate

        manifest_url = raw_manifest_url

        # ── Detect bumper placeholder and attempt to resolve real URL ──
        # Viki SSR always returns 'Viki Non Original Bumper' (1193129v, PT4S)
        # for non-Viki-Original content — regardless of login.
        # We try to reconstruct the real episode manifest from vsId + CDN pattern.
        if manifest_url and self._BUMPER_ID in manifest_url:
            vs_id = stream_json.get("vsId")
            if vs_id:
                real_url = self._try_resolve_real_manifest(manifest_url, video_id, vs_id)
                if real_url:
                    self.log.info(f" + Resolved real manifest from CDN/vsId pattern")
                    manifest_url = real_url
                else:
                    self.log.warning(
                        f" - [!] Viki returned bumper placeholder for {video_id} (PT4S). "
                        f"Download will only be ~4 seconds. "
                        f"Try WeTV instead: poetry run vt dl WETV "
                        f"https://wetv.vip/en/play/b7qj8hm7xpaifcj"
                    )
            else:
                self.log.warning(
                    f" - [!] No vsId found and manifest is bumper placeholder for {video_id}. "
                    f"Content may not be available via server-side scraping."
                )

        if not manifest_url:
            self.log.warning(f" - No manifest URL found for video {video_id}.")
            return Tracks()

        self.log.info(f" + Loading manifest: {manifest_url}")
        r = self.session.get(manifest_url)
        r.raise_for_status()

        if ".mpd" in manifest_url or "<MPD" in r.text:
            return Tracks.from_mpd(r.text, url=manifest_url, source=self.ALIASES[0])
        else:
            return Tracks.from_m3u8(r.text, source=self.ALIASES[0])

    def _try_resolve_real_manifest(
        self, bumper_url: str, video_id: str, vs_id: str
    ) -> str | None:
        """
        Attempt to construct the real per-episode manifest URL from:
        - The bumper manifest URL (to extract CDN base + manifest hash/date)
        - The vsId from page props (contains episode content hash)
        - The video_id

        Viki CDN pattern: {cdn}/{content_id}/dash/mpdhd_{fmt_hash}_{build_date}.mpd
        """
        cdn_match = re.match(
            r"(https://[^/]+)/[^/]+/dash/mpdhd_([a-z0-9]+)_(\d+)\.mpd",
            bumper_url,
        )
        if not cdn_match:
            return None

        cdn_base   = cdn_match.group(1)  # https://cloudfront-3.vikiplatform.com
        fmt_hash   = cdn_match.group(2)  # bdc65a  (format/encoder hash)
        build_date = cdn_match.group(3)  # 2608140526 (build date code)

        vs_hash = vs_id.split("/")[0]    # dfcfc7c61f66eb0dc82fcc479e0710c8

        # Build candidate manifest URLs to probe
        # The real episode content folder might use video_id directly or vs_hash prefix
        vid_sha256 = hashlib.sha256(video_id.encode()).hexdigest()[:6]
        candidates = [
            # Try video_id as content folder with different hash variants
            f"{cdn_base}/{video_id}/dash/mpdhd_{fmt_hash}_{build_date}.mpd",
            f"{cdn_base}/{video_id}/dash/mpdhd_{vid_sha256}_{build_date}.mpd",
            # Try vs_hash[:8] as content folder prefix
            f"{cdn_base}/{vs_hash[:8]}/dash/mpdhd_{fmt_hash}_{build_date}.mpd",
            # Try alternate CDN with video_id
            f"https://vikiplatform-3.akamaized.net/{video_id}/dash/mpdhd_{fmt_hash}_{build_date}.mpd",
        ]

        for candidate in candidates:
            try:
                cr = self.session.get(candidate, timeout=8)
                if cr.status_code == 200 and "<MPD" in cr.text:
                    # Confirm it's a real episode (duration > 10s, not bumper)
                    dur_match = re.search(
                        r'mediaPresentationDuration="PT(\d+)S"', cr.text
                    )
                    if dur_match and int(dur_match.group(1)) > 10:
                        return candidate
            except Exception:
                continue

        return None

    def get_chapters(self, title: Title) -> list:
        return []

    def certificate(self, **_):
        return None

    def license(self, challenge: bytes, **_) -> bytes | None:
        if not self.license_url:
            self.log.warning(" - No license URL found for Widevine challenge.")
            return None

        r = self.session.post(
            self.license_url,
            data=challenge,
            headers={
                "Content-Type": "application/octet-stream",
                "Referer": "https://www.viki.com/",
                "Origin": "https://www.viki.com",
            },
        )
        r.raise_for_status()
        return r.content
