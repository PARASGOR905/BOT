from __future__ import annotations

import base64
import hashlib
import re
import click
import m3u8
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

from vinetrimmer.objects import AudioTrack, Title, Tracks, TextTrack, Track
from vinetrimmer.services.BaseService import BaseService


class ShemarooMe(BaseService):
    """
    Service code for ShemarooMe (https://www.shemaroome.com/)
    """

    ALIASES = ["SM", "shemaroome", "ShemarooMe"]

    TITLE_RE = [
        r"https?://(?:www\.)?shemaroome\.com/(?:movies|shows)/(?P<id>[^?#]+)",
    ]

    @staticmethod
    @click.command(name="ShemarooMe", short_help="https://www.shemaroome.com/", help=__doc__)
    @click.argument("title", type=str)
    @click.pass_context
    def cli(ctx, **kwargs):
        return ShemarooMe(ctx, **kwargs)

    def __init__(self, ctx, title):
        self.url = title
        super().__init__(ctx)
        self.parse_title(ctx, self.url)

        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "Accept": "*/*",
            "Origin": "https://www.shemaroome.com",
            "Referer": "https://www.shemaroome.com/",
        })

    def _get_input_value(self, html, id_name):
        patterns = [
            rf'id=["\']{id_name}["\']\s+value=["\']([^"\']+)["\']',
            rf'value=["\']([^"\']+)["\']\s+id=["\']{id_name}["\']',
        ]
        for pattern in patterns:
            match = re.search(pattern, html)
            if match:
                return match.group(1)
        return None

    def get_titles(self):
        r = self.session.get(self.url)
        r.raise_for_status()
        html = r.text

        theme_type = self._get_input_value(html, "theme_type") or "movie"
        ma_title = self._get_input_value(html, "ma_title") or "ShemarooMe Show"

        if theme_type == "show":
            # Scrape unique episode slugs from the page
            # Episode URLs look like: /shows/show-name/episode-slug
            # We match relative or absolute links
            episode_slugs = []
            pattern = rf'href=["\']/shows/{self.title}/([^/\'\"\?#]+)["\']'
            for slug in re.findall(pattern, html):
                if slug != "episodes" and slug not in episode_slugs:
                    episode_slugs.append(slug)

            self.log.info(f"Found {len(episode_slugs)} episode slugs for show {ma_title}")
            
            titles = []
            for index, slug in enumerate(episode_slugs, start=1):
                episode_url = f"https://www.shemaroome.com/shows/{self.title}/{slug}"
                self.log.info(f"Fetching episode {index}/{len(episode_slugs)}: {episode_url}")
                try:
                    r_ep = self.session.get(episode_url)
                    r_ep.raise_for_status()
                    ep_html = r_ep.text
                except Exception as e:
                    self.log.warning(f"Failed to fetch episode page {episode_url}: {e}")
                    continue

                content_id = self._get_input_value(ep_html, "content_id")
                catalog_id = self._get_input_value(ep_html, "catalog_id")
                item_category = self._get_input_value(ep_html, "item_category")
                content_def = self._get_input_value(ep_html, "content_definition")
                branch_title = self._get_input_value(ep_html, "branch_title")
                release_date = self._get_input_value(ep_html, "release_date")

                if not content_id:
                    continue

                year = None
                if release_date:
                    try:
                        year = int(release_date[:4])
                    except Exception:
                        pass

                service_data = {
                    "catalog_id": catalog_id,
                    "content_id": content_id,
                    "item_category": item_category,
                    "content_def": content_def,
                    "ma_title": ma_title,
                    "branch_title": branch_title,
                    "theme_type": "episode",
                    "year": year,
                }

                # Try to extract episode number from branch_title
                episode_num = index
                if branch_title:
                    ep_match = re.search(r'episode[-_\s]*(?P<num>\d+)', branch_title, re.IGNORECASE)
                    if ep_match:
                        episode_num = int(ep_match.group("num"))

                original_lang = "gu" if item_category == "gujarati" else "hi"

                titles.append(Title(
                    id_=content_id,
                    type_=Title.Types.TV,
                    name=ma_title,
                    season=1,
                    episode=episode_num,
                    episode_name=branch_title,
                    original_lang=original_lang,
                    source=self.ALIASES[0],
                    service_data=service_data,
                ))

            if not titles:
                raise ValueError(f"No episodes could be parsed for show: {ma_title}")
            return titles

        else:
            # Handle single movie or single episode URL directly
            content_id = self._get_input_value(html, "content_id")
            catalog_id = self._get_input_value(html, "catalog_id")
            item_category = self._get_input_value(html, "item_category")
            content_def = self._get_input_value(html, "content_definition")
            branch_title = self._get_input_value(html, "branch_title")
            release_date = self._get_input_value(html, "release_date")

            if not content_id:
                raise ValueError(f"Could not extract content_id from {self.url}")

            year = None
            if release_date:
                try:
                    year = int(release_date[:4])
                except Exception:
                    pass

            service_data = {
                "catalog_id": catalog_id,
                "content_id": content_id,
                "item_category": item_category,
                "content_def": content_def,
                "ma_title": ma_title,
                "branch_title": branch_title,
                "theme_type": theme_type,
                "year": year,
            }

            original_lang = "gu" if item_category == "gujarati" else "hi"

            if theme_type == "episode":
                episode_num = 1
                if branch_title:
                    ep_match = re.search(r'episode[-_\s]*(?P<num>\d+)', branch_title, re.IGNORECASE)
                    if ep_match:
                        episode_num = int(ep_match.group("num"))

                return Title(
                    id_=content_id,
                    type_=Title.Types.TV,
                    name=ma_title,
                    season=1,
                    episode=episode_num,
                    episode_name=branch_title,
                    original_lang=original_lang,
                    source=self.ALIASES[0],
                    service_data=service_data,
                )
            else:
                return Title(
                    id_=content_id,
                    type_=Title.Types.MOVIE,
                    name=ma_title or branch_title or "ShemarooMe Video",
                    year=year,
                    original_lang=original_lang,
                    source=self.ALIASES[0],
                    service_data=service_data,
                )

    def _try_upgrade_playlist_url(self, url):
        """
        ShemarooMe CDN hosts multiple playlist variants at the same path using a device-prefix
        naming convention, e.g.:
          laptop_paid_us_playlist.m3u8
          smarttv_paid_us_playlist.m3u8
          4k_paid_us_playlist.m3u8

        The play API always returns the 'laptop' variant regardless of subscription.
        Try each higher-quality variant and return the one with the best max resolution.
        """
        import re as _re
        # Match the device prefix at the start of the filename
        match = _re.search(r'/([^/]+)_paid_([^/]+\.m3u8)$', url)
        if not match:
            return url

        base_url = url[:url.rfind('/') + 1]
        suffix = f"_paid_{match.group(2)}"

        # Ordered from highest to lowest quality preference
        candidates = ["4k", "smarttv", "tv", "laptop"]
        current_prefix = match.group(1)

        self.log.debug(f"Current playlist variant: {current_prefix}")

        best_url = url
        best_max_res = 0

        # Get baseline resolution of current/default playlist
        try:
            r = self.session.get(url, timeout=3)
            if r.ok:
                parsed = m3u8.loads(r.text)
                best_max_res = max(
                    (p.stream_info.resolution[0] * p.stream_info.resolution[1]
                     for p in parsed.playlists if p.stream_info and p.stream_info.resolution),
                    default=0
                )
        except Exception:
            pass

        for prefix in candidates:
            if prefix == current_prefix:
                continue

            candidate_url = base_url + prefix + suffix
            try:
                r = self.session.get(candidate_url, timeout=3)
                if not r.ok or not r.text.strip():
                    continue
                parsed = m3u8.loads(r.text)
                if not parsed.playlists:
                    continue
                max_res = max(
                    (p.stream_info.resolution[0] * p.stream_info.resolution[1]
                     for p in parsed.playlists if p.stream_info and p.stream_info.resolution),
                    default=0
                )
                self.log.debug(f"Variant '{prefix}': max resolution pixels = {max_res} (URL: {candidate_url})")
                if max_res > best_max_res:
                    best_max_res = max_res
                    best_url = candidate_url
            except Exception as e:
                self.log.debug(f"Variant '{prefix}' failed: {e}")

        if best_url != url:
            self.log.debug(f"Upgraded playlist to: {best_url}")
        else:
            self.log.debug(f"No higher-quality variant found, using: {url}")

        return best_url

    def get_tracks(self, title):
        sd = title.service_data

        payload = {
            "catalog_id": sd["catalog_id"],
            "content_id": sd["content_id"],
            "category": sd["item_category"],
            "content_def": sd["content_def"] or "AVOD",
            "user_agent": self.session.headers.get("User-Agent", ""),
        }

        self.log.debug(f"Retrieving play URL for content ID: {sd['content_id']}")

        # First visit the content page so that server-side session is refreshed
        import urllib.parse
        content_slug = sd.get("content_slug") or title.name or ""
        content_slug = content_slug.lower().replace(" ", "-")

        r = self.session.post(self.config["endpoints"]["play_url"], data=payload)
        r.raise_for_status()
        data_json = r.json()

        if not data_json.get("status"):
            # Fallback: try to read play data from cookies (set by a previous browser visit)
            play_url_cookie = urllib.parse.unquote(self.session.cookies.get("new_play_url", "") or "")
            key_cookie = urllib.parse.unquote(self.session.cookies.get("key", "") or "")
            content_id_cookie = urllib.parse.unquote(self.session.cookies.get("content_id", "") or "")

            if play_url_cookie and key_cookie and content_id_cookie == sd["content_id"]:
                self.log.warning("API returned false status. Using play data from cookies (fallback).")
                data_json = {
                    "status": True,
                    "new_play_url": play_url_cookie,
                    "key": key_cookie,
                    "stream_key": urllib.parse.unquote(self.session.cookies.get("stream_key", "") or ""),
                    "subtitle": urllib.parse.unquote(self.session.cookies.get("subtitle_url", "") or ""),
                }
            else:
                raise ValueError(
                    "ShemarooMe returned false status. Your cookies have likely expired.\n"
                    "  Please re-export cookies from your browser:\n"
                    "  1. Log in to https://www.shemaroome.com\n"
                    "  2. Export cookies using a browser extension\n"
                    "  3. Save to: vinetrimmer/cookies/shemaroome/default.txt"
                )

        # Decrypt play URL
        url_data = base64.b64decode(data_json["new_play_url"])
        key = base64.b64decode(data_json["key"])
        iv = bytes(16)
        cipher = AES.new(key, AES.MODE_CBC, iv)
        m3u8_url = unpad(cipher.decrypt(url_data), AES.block_size).decode("ascii")

        self.log.debug(f"Successfully decrypted master playlist URL: {m3u8_url}")

        # Set stream_key BEFORE any CDN requests — CloudFront requires it for playlist access
        stream_key = data_json.get("stream_key")
        if stream_key:
            self.session.headers["stream_key"] = stream_key

        # Try to upgrade to a higher-quality CDN playlist variant (smarttv / 4k prefix)
        m3u8_url = self._try_upgrade_playlist_url(m3u8_url)

        # Load master playlist
        playlist_res = self.session.get(m3u8_url)
        playlist_res.raise_for_status()

        master = m3u8.loads(playlist_res.text, uri=m3u8_url)
        tracks = Tracks.from_m3u8(master, source=self.ALIASES[0])

        # Muxed HLS fallback
        if not tracks.audios:
            lang_code = title.original_lang or "hi"
            self.log.debug(
                f"No separate audio rendition found — stream is muxed (lang={lang_code})"
            )
            import uuid
            from langcodes import Language
            muxed_audio = AudioTrack(
                id_=uuid.uuid4().hex[:6],
                source=self.ALIASES[0],
                url=m3u8_url,
                codec="aac",
                language=Language.get(lang_code),
                bitrate=128000,
                channels="2.0",
                descriptor=Track.Descriptor.URL,
                encrypted=False,
            )
            muxed_audio.is_muxed_with_video = True
            tracks.audios.append(muxed_audio)

        # Add external subtitles if available
        sub_url = data_json.get("subtitle")
        if sub_url:
            if sub_url.startswith("//"):
                sub_url = "https:" + sub_url
            self.log.debug(f"Adding external subtitle track: {sub_url}")
            tracks.add(TextTrack(
                id_=hashlib.md5(sub_url.encode()).hexdigest()[0:6],
                source=self.ALIASES[0],
                url=sub_url,
                codec="vtt",
                language="en",
                forced=False,
                sdh=False,
            ))

        # Check for direct 4K UHD video track if 2160/2160p stream exists on CDN
        if tracks.videos:
            first_video = tracks.videos[0]
            candidate_urls = []
            
            # Pattern 1: /1080/media-1/video.m3u8 -> /2160/media-1/video.m3u8
            m1 = re.search(r'/(?P<res>\d+p?)(?=/media-\d+/|/video\.m3u8)', first_video.url)
            if m1:
                for target_res in ["2160", "2160p"]:
                    candidate_urls.append(first_video.url[:m1.start("res")] + target_res + first_video.url[m1.end("res"):])

            # Pattern 2: filename_1080p.m3u8 -> filename_2160p.m3u8
            m2 = re.search(r'/(?P<base_name>[^/]+)_(?P<res>\d+p)\.m3u8$', first_video.url)
            if m2:
                base_name = m2.group("base_name")
                for suffix in ["2160", "2160p"]:
                    candidate_urls.append(first_video.url.replace(m2.group(0), f"/{base_name}_{suffix}.m3u8"))

            for uhd_url in candidate_urls:
                try:
                    r_uhd = self.session.get(uhd_url, timeout=3)
                    if r_uhd.ok and "#EXTM3U" in r_uhd.text:
                        self.log.info(f" + Found direct 4K UHD video track (3840x2160)!")
                        import uuid
                        from vinetrimmer.objects import VideoTrack
                        
                        # Dynamically measure actual bitrate from playlist segments
                        uhd_bitrate = 8000000
                        try:
                            p_uhd = m3u8.loads(r_uhd.text)
                            if p_uhd.segments:
                                sample_indices = [
                                    len(p_uhd.segments) // 4,
                                    len(p_uhd.segments) // 2,
                                    len(p_uhd.segments) * 3 // 4
                                ]
                                sample_bytes = 0
                                sample_dur = 0
                                base_url = uhd_url.rsplit("/", 1)[0]
                                for idx in sample_indices:
                                    seg = p_uhd.segments[idx]
                                    seg_url = f"{base_url}/{seg.uri}" if not seg.uri.startswith("http") else seg.uri
                                    try:
                                        head_res = self.session.head(seg_url, timeout=2)
                                        cl = head_res.headers.get("Content-Length")
                                        if cl and int(cl) > 0:
                                            sample_bytes += int(cl)
                                            sample_dur += (seg.duration or 9.6)
                                    except Exception:
                                        pass
                                if sample_dur > 0:
                                    uhd_bitrate = int((sample_bytes * 8) / sample_dur)
                        except Exception as e:
                            self.log.debug(f"Failed calculating dynamic 4K bitrate: {e}")

                        uhd_track = VideoTrack(
                            id_=uuid.uuid4().hex[:6],
                            source=self.ALIASES[0],
                            url=uhd_url,
                            codec="avc1",
                            width=3840,
                            height=2160,
                            bitrate=uhd_bitrate,
                            fps=24.0,
                            encrypted=False,
                            descriptor=Track.Descriptor.M3U,
                        )
                        tracks.videos.insert(0, uhd_track)
                        break
                except Exception as e:
                    self.log.debug(f"Failed checking 4K URL {uhd_url}: {e}")

        # Mark tracks as unencrypted (clear content)
        for track in tracks.videos + tracks.audios + tracks.subtitles:
            track.encrypted = False

        return tracks

    def get_chapters(self, title):
        return []

    def certificate(self, **_):
        return None

    def license(self, challenge, **_):
        return None
