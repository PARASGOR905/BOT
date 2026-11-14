from __future__ import annotations

import hashlib
import json
import re
import urllib.parse
from typing import Any, Dict, List, Optional

import click
import m3u8
from langcodes import Language

from vinetrimmer.objects import AudioTrack, Title, Track, Tracks
from vinetrimmer.services.BaseService import BaseService


class TarangPlus(BaseService):
    """
    Service code for TarangPlus OTT platform (https://tarangplus.in/).

    Supports free and subscription movies, serial episodes, originals, and live TV channels
    powered by Saranyu OTT backend.

    Authorization: None required for free movies, serial episodes, and live TV channels.
    Security     : HLS (.m3u8) with Saranyu MD5 signed playback tokens / Clear streams or Widevine DRM.

    Input formats accepted:
      Full URL:   https://tarangplus.in/tarangaplus-originals/kie-se-nayak
                  https://tarangplus.in/movies/to-binu-anya-gati-nahi-2
                  https://tarangplus.in/all-movies/bara-badhu
                  https://tarangplus.in/all-channels/tarang-tv
                  https://tarangplus.in/serials/e-mana-manena
      Short form: tarang:kie-se-nayak
                  tarangplus:to-binu-anya-gati-nahi-2
    """

    ALIASES = ["TARANGPLUS", "tarangplus", "tarang", "Tarang", "TARANG"]
    GEOFENCE = ["in"]

    TITLE_RE = [
        r"^https?://(?:www\.)?tarangplus\.in/(?:[^/]+/)?(?:(?P<show>[a-zA-Z0-9_-]+)/)?(?P<id>[a-zA-Z0-9_-]+)(?:/.*)?$",
        r"^(?:tarangplus|tarang):(?P<id>[a-zA-Z0-9_-]+)$",
        r"^(?P<id>[a-zA-Z0-9_-]+)$",
    ]

    API_BASE = "https://api.tarangplus.in"
    AUTH_TOKEN = "3zZmzoHg8z6SM3wpDoyw"
    SARANYU_SECRET = "ywVXaTzycwZ8agEs3ujx"
    DEFAULT_REGION = "IN"

    # Known catalog IDs for direct lookup fallback
    CATALOG_IDS = [
        "tarangaplus-originals",
        "5e381045babd816ab3000013",  # Originals
        "5d301585babd8131f6000017",  # Movies
        "5d301a74babd8131f6000019",  # Serials
        "5d47b95ebabd81099e00002e",  # Channels
        "601cf30e3326af0ebb000f05",  # Featured
    ]

    @staticmethod
    @click.command(name="TarangPlus", short_help="https://tarangplus.in/ (Odia OTT)", help=__doc__)
    @click.argument("title", type=str)
    @click.pass_context
    def cli(ctx, **kwargs):
        return TarangPlus(ctx, **kwargs)

    def parse_title(self, ctx, title: str):
        title = title or ctx.parent.params.get("title")
        if not title:
            self.log.exit(" - No title ID specified")

        clean_url = title.split("?")[0].split("#")[0].rstrip("/")

        self.catalog_hint = None
        self.show_hint = None

        if clean_url.startswith(("http://", "https://")):
            parts = [p for p in clean_url.split("/") if p]
            # e.g. ['https:', 'tarangplus.in', 'tarangaplus-originals', 'kie-se-nayak']
            if len(parts) >= 4:
                self.catalog_hint = parts[2]
                self.title = parts[-1]
                if len(parts) >= 5:
                    self.show_hint = parts[3]
            elif len(parts) == 3:
                self.title = parts[-1]
            else:
                self.title = parts[-1]
            return

        if ":" in clean_url:
            self.title = clean_url.split(":")[-1]
            return

        self.title = clean_url

    def __init__(self, ctx, title: str):
        self.url = title
        super().__init__(ctx)
        self.parse_title(ctx, self.url)

        self.slug = self.title
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Origin": "https://tarangplus.in",
            "Referer": "https://tarangplus.in/",
            "Accept": "application/json, text/plain, */*",
        })

    def _get_item_details(self, slug: str) -> Optional[Dict[str, Any]]:
        """Fetch item metadata by searching known catalogs or search endpoint."""
        candidates = [self.catalog_hint] if self.catalog_hint else []
        candidates.extend([c for c in self.CATALOG_IDS if c not in candidates])

        # Try direct catalog probes first
        for cat_id in candidates:
            url = f"{self.API_BASE}/catalogs/{cat_id}/items/{slug}.gzip?auth_token={self.AUTH_TOKEN}&region={self.DEFAULT_REGION}"
            try:
                r = self.session.get(url, timeout=10, hooks={"response": []})
                if r.status_code == 200:
                    data = r.json().get("data")
                    if data and data.get("title"):
                        return data
            except Exception:
                pass

        # Try search endpoint if catalog probe didn't hit
        search_query = slug.replace("-", " ")
        search_url = (
            f"{self.API_BASE}/search.gzip?filters=category,all&q={urllib.parse.quote(search_query)}"
            f"&region={self.DEFAULT_REGION}&item_language=&auth_token={self.AUTH_TOKEN}"
        )
        try:
            r = self.session.get(search_url, timeout=10, hooks={"response": []})
            if r.status_code == 200:
                results = r.json().get("data", {}).get("items", [])
                for it in results:
                    if it.get("friendly_id") == slug:
                        cat_id = it.get("catalog_id")
                        if cat_id:
                            item_url = f"{self.API_BASE}/catalogs/{cat_id}/items/{slug}.gzip?auth_token={self.AUTH_TOKEN}&region={self.DEFAULT_REGION}"
                            it_res = self.session.get(item_url, timeout=10, hooks={"response": []})
                            if it_res.status_code == 200:
                                return it_res.json().get("data")
                        return it
        except Exception:
            pass

        return None

    def _sign_smart_url(self, smart_url: str) -> str:
        """
        Signs a Saranyu smart_url with MD5 and returns the signed URL.
        Formula:
            n = smart_url + "?service_id=1&protocol=hls&play_url=yes"
            i = n + "&us="
            sig = md5(SECRET + i)
            signed_url = i + sig
        """
        sep = "&" if "?" in smart_url else "?"
        query_base = f"{smart_url}{sep}service_id=1&protocol=hls&play_url=yes"
        sign_target = query_base + "&us="
        sig = hashlib.md5((self.SARANYU_SECRET + sign_target).encode("utf-8")).hexdigest()
        return sign_target + sig

    def get_titles(self) -> List[Title] | Title:
        slug = self.slug
        cat = self.catalog_hint or ""

        # Check if this slug is a series/show that has episodes
        candidates = [cat] if cat else []
        candidates.extend([c for c in self.CATALOG_IDS if c not in candidates])

        for c in candidates:
            if not c:
                continue
            episodes_url = (
                f"{self.API_BASE}/catalogs/{c}/items/{slug}/episodes.gzip?"
                f"order_by=asc&region={self.DEFAULT_REGION}&item_language=&auth_token={self.AUTH_TOKEN}&status=published"
            )
            try:
                r = self.session.get(episodes_url, timeout=10, hooks={"response": []})
                if r.status_code == 200:
                    ep_items = r.json().get("data", {}).get("items", [])
                    if ep_items:
                        self.log.info(f" + Found {len(ep_items)} episode(s) for series '{slug}'")
                        titles = []
                        for idx, ep in enumerate(ep_items, 1):
                            ep_num = ep.get("episode_number")
                            if not ep_num:
                                m = re.search(r"EP\s*(\d+)", ep.get("title", ""), re.IGNORECASE)
                                ep_num = int(m.group(1)) if m else idx

                            play_obj = ep.get("play_url") or {}
                            smart_url = play_obj.get("saranyu", {}).get("url") if isinstance(play_obj, dict) else ep.get("smart_url")

                            show_name = ep.get("show_name") or slug.replace("-", " ").title()
                            ep_title = ep.get("title") or f"Episode {ep_num}"

                            titles.append(
                                Title(
                                    id_=ep.get("friendly_id") or f"{slug}-episode-{ep_num}",
                                    type_=Title.Types.TV,
                                    name=show_name,
                                    season=1,
                                    episode=int(ep_num),
                                    episode_name=ep_title,
                                    source=self.ALIASES[0],
                                    service_data={
                                        "slug": ep.get("friendly_id"),
                                        "smart_url": smart_url,
                                        "item_data": ep,
                                    },
                                )
                            )
                        return titles
            except Exception:
                pass

        # If not a multi-episode show, check single movie/channel
        item_data = self._get_item_details(slug)

        if not item_data:
            self.log.warning(f"Could not fetch full metadata for slug '{slug}', creating fallback title.")
            title_name = slug.replace("-", " ").title()
            return Title(
                id_=slug,
                type_=Title.Types.MOVIE,
                name=title_name,
                source=self.ALIASES[0],
                service_data={"slug": slug, "item_data": {}},
            )

        title_name = item_data.get("title") or slug.replace("-", " ").title()
        theme = (item_data.get("theme") or "").lower()
        is_movie = theme in ("movie", "movies") or "movie" in (item_data.get("layout_type") or "")
        is_tv = theme in ("channel", "livetv", "live", "channels")

        play_obj = item_data.get("play_url") or {}
        smart_url = play_obj.get("saranyu", {}).get("url") if isinstance(play_obj, dict) else item_data.get("smart_url")

        return Title(
            id_=slug,
            type_=Title.Types.MOVIE if is_movie or is_tv else Title.Types.TV,
            name=title_name,
            year=item_data.get("release_year") or item_data.get("year"),
            source=self.ALIASES[0],
            service_data={
                "slug": slug,
                "item_data": item_data,
                "smart_url": smart_url,
            },
        )

    LANG_MAP = {
        "english": "en",
        "odia": "or",
        "oriya": "or",
        "hindi": "hi",
        "bengali": "bn",
        "tamil": "ta",
        "telugu": "te",
        "kannada": "kn",
        "malayalam": "ml",
        "marathi": "mr",
        "gujarati": "gu",
        "punjabi": "pa",
    }

    def _normalize_manifest(self, manifest_text: str) -> str:
        """Normalizes non-standard full language names (e.g. English -> en) for langcodes."""
        def repl(m):
            raw_lang = m.group(1).lower()
            code = self.LANG_MAP.get(raw_lang)
            if not code:
                try:
                    code = Language.find(raw_lang).language
                except Exception:
                    code = raw_lang[:2]
            return f'LANGUAGE="{code}"'
        return re.sub(r'LANGUAGE=["\']?([a-zA-Z_-]+)["\']?', repl, manifest_text)

    def get_tracks(self, title: Title) -> Tracks:
        slug = title.service_data.get("slug") or self.slug
        smart_url = title.service_data.get("smart_url")

        if not smart_url:
            item_data = title.service_data.get("item_data") or self._get_item_details(slug)
            if item_data:
                play_obj = item_data.get("play_url") or {}
                smart_url = play_obj.get("saranyu", {}).get("url") if isinstance(play_obj, dict) else item_data.get("smart_url")

        if not smart_url:
            self.log.exit(f" - No playback smart_url found for '{slug}'. Check if title is available or paid.")

        signed_url = self._sign_smart_url(smart_url)
        self.log.info(f" + Resolving Saranyu playback stream...")

        r = self.session.get(signed_url, timeout=15, hooks={"response": []})
        if r.status_code != 200:
            self.log.exit(f" - Saranyu resolver returned HTTP {r.status_code}: {r.text}")

        res_json = r.json()
        adaptive_urls = res_json.get("adaptive_urls", [])
        if not adaptive_urls:
            self.log.exit(f" - No adaptive playback URLs returned by Saranyu: {res_json}")

        master_m3u8_url = adaptive_urls[0].get("playback_url") or adaptive_urls[0].get("play_url")
        if not master_m3u8_url:
            self.log.exit(f" - No M3U8 stream URL in adaptive_urls: {adaptive_urls}")

        self.log.info(f" + Master HLS playlist: {master_m3u8_url}")

        manifest_res = self.session.get(master_m3u8_url, timeout=15)
        cleaned_manifest = self._normalize_manifest(manifest_res.text)
        manifest_obj = m3u8.loads(cleaned_manifest, uri=master_m3u8_url)

        tracks = Tracks.from_m3u8(
            master=manifest_obj,
            source=self.ALIASES[0],
            url=master_m3u8_url,
        )

        if not tracks.audios:
            # TarangPlus muxes AAC audio directly into the video stream segments
            raw_lang = (
                title.service_data.get("item_data", {}).get("language")
                or (title.service_data.get("item_data", {}).get("languages") or ["or"])[0]
                or "or"
            )
            lang_code = self.LANG_MAP.get(str(raw_lang).lower(), "or")
            try:
                parsed_lang = Language.get(lang_code)
            except Exception:
                parsed_lang = Language.get("or")

            muxed_audio = AudioTrack(
                id_=f"audio_{title.id}",
                source=self.ALIASES[0],
                url=None,
                codec="aac",
                language=parsed_lang,
                bitrate=128000,
                channels="2.0",
                descriptor=Track.Descriptor.URL,
                encrypted=False,
            )
            muxed_audio.is_muxed_with_video = True
            tracks.audios.append(muxed_audio)

        return tracks

    def get_chapters(self, title: Title) -> list:
        return []

    def certificate(self, **_) -> str | None:
        return None

    def license(self, challenge: bytes, **_) -> bytes:
        return b""
