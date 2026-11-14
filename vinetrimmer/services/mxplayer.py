from __future__ import annotations

import hashlib
import os
import re
import urllib.parse
import uuid
import click
import m3u8

from vinetrimmer.objects import Title, Tracks
from vinetrimmer.services.BaseService import BaseService


class MXPlayer(BaseService):
    """
    Service code for MXPlayer (https://www.mxplayer.in/)
    """

    ALIASES = ["MXP", "mxplayer", "MXPlayer"]

    TITLE_RE = [
        # Episode pattern
        r"/show/[-\w]+/[-\w]+/(?P<display_id>[-\w]+)-(?P<id>[a-f0-9]{32})",
        # Movie pattern
        r"/movie/(?P<display_id>[-\w]+)-(?P<id>[a-f0-9]{32})",
        # Show container pattern
        r"/show/(?P<display_id>[-\w]+)-(?P<id>[a-f0-9]{32})",
    ]

    @staticmethod
    @click.command(name="MXPlayer", short_help="https://www.mxplayer.in/", help=__doc__)
    @click.argument("title", type=str)
    @click.pass_context
    def cli(ctx, **kwargs):
        return MXPlayer(ctx, **kwargs)

    def __init__(self, ctx, title):
        self.url = title
        super().__init__(ctx)
        self.userid = str(uuid.uuid4())

        # Determine kind from input URL
        path = urllib.parse.urlparse(self.url).path
        parts = [p for p in path.split("/") if p]
        if "movie" in parts:
            self.kind = "movie"
        elif "show" in parts:
            if "seasons" in parts and len(parts) == 4:
                self.kind = "season"
            elif len(parts) >= 4:
                self.kind = "episode"
            else:
                self.kind = "show"
        else:
            self.kind = "episode"

        # Parse title to get self.title (the ID)
        self.parse_title(ctx, self.url)

        # Update headers
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://www.mxplayer.in/",
            "Origin": "https://www.mxplayer.in",
        })

    def _get_json(self, url, params=None):
        query_params = {
            "device-density": "2",
            "platform": "com.mxplay.desktop",
            "content-languages": "hi,en",
            "kids-mode-enabled": "false",
            "userid": self.userid,
        }
        if params:
            query_params.update(params)

        r = self.session.get(url, params=query_params)
        r.raise_for_status()
        return r.json()

    def get_titles(self):
        content_id = self.title

        if self.kind == "movie":
            data = self._get_json(
                self.config["endpoints"]["detail"],
                params={"type": "movie", "id": content_id}
            )
            return Title(
                id_=data["id"],
                type_=Title.Types.MOVIE,
                name=data["title"],
                year=int(data.get("releaseDate")[:4]) if data.get("releaseDate") else None,
                original_lang="hi",
                source=self.ALIASES[0],
                service_data=data,
            )

        elif self.kind == "episode":
            data = self._get_json(
                self.config["endpoints"]["detail"],
                params={"type": "episode", "id": content_id}
            )
            season_info = data.get("container") or {}
            show_info = season_info.get("container") or {}

            show_title = show_info.get("title") or data.get("title")
            season_title = season_info.get("title") or "Season 1"
            season_num = 1
            if "season" in season_title.lower():
                try:
                    season_num = int(re.search(r"\d+", season_title).group(0))
                except Exception:
                    pass

            return Title(
                id_=data["id"],
                type_=Title.Types.TV,
                name=show_title,
                season=season_num,
                episode=data.get("sequence") or 1,
                episode_name=data.get("title"),
                original_lang="hi",
                source=self.ALIASES[0],
                service_data=data,
            )

        elif self.kind == "season":
            next_url = ""
            titles = []
            season_num = 1
            show_title = None

            while next_url is not None:
                params = {"type": "season", "id": content_id}
                if next_url:
                    parsed_params = urllib.parse.parse_qs(next_url)
                    for k, v in parsed_params.items():
                        params[k] = v[0]

                episodes_data = self._get_json(
                    self.config["endpoints"]["episodes"],
                    params=params
                )
                items = episodes_data.get("items") or []
                if not items:
                    break

                if not show_title:
                    first_item = items[0]
                    container = first_item.get("container") or {}
                    parent_container = container.get("container") or {}
                    show_title = parent_container.get("title") or episodes_data.get("title") or "Show"
                    season_title = container.get("title") or "Season 1"
                    if "season" in season_title.lower():
                        try:
                            season_num = int(re.search(r"\d+", season_title).group(0))
                        except Exception:
                            pass

                for episode in items:
                    titles.append(
                        Title(
                            id_=episode["id"],
                            type_=Title.Types.TV,
                            name=show_title,
                            season=season_num,
                            episode=episode.get("sequence") or 1,
                            episode_name=episode.get("title"),
                            original_lang="hi",
                            source=self.ALIASES[0],
                            service_data=episode,
                        )
                    )

                next_url = episodes_data.get("next")

            return titles

        elif self.kind == "show":
            seasons_data = self._get_json(
                self.config["endpoints"]["seasons"],
                params={"type": "tv_show", "id": content_id}
            )
            show_title = seasons_data.get("title") or seasons_data.get("name") or "Show"

            titles = []
            for season in seasons_data.get("items") or []:
                season_id = season.get("id")
                season_title = season.get("title") or "Season 1"
                season_num = 1
                if "season" in season_title.lower():
                    try:
                        season_num = int(re.search(r"\d+", season_title).group(0))
                    except Exception:
                        pass

                next_url = ""
                while next_url is not None:
                    params = {"type": "season", "id": season_id}
                    if next_url:
                        parsed_params = urllib.parse.parse_qs(next_url)
                        for k, v in parsed_params.items():
                            params[k] = v[0]

                    episodes_data = self._get_json(
                        self.config["endpoints"]["episodes"],
                        params=params
                    )
                    for episode in episodes_data.get("items") or []:
                        titles.append(
                            Title(
                                id_=episode["id"],
                                type_=Title.Types.TV,
                                name=show_title,
                                season=season_num,
                                episode=episode.get("sequence") or 1,
                                episode_name=episode.get("title"),
                                original_lang="hi",
                                source=self.ALIASES[0],
                                service_data=episode,
                            )
                        )

                    next_url = episodes_data.get("next")

            return titles

    def get_tracks(self, title):
        stream = title.service_data.get("stream") or {}

        # Parse stream URLs
        hls_data = stream.get("hls") or {}
        dash_data = stream.get("dash") or {}
        third_party = stream.get("thirdParty") or {}
        alt_balaji = stream.get("altBalaji") or {}
        mxplay = stream.get("mxplay") or {}

        dash_path = (dash_data.get("high") or dash_data.get("base") or dash_data.get("main")
                     or third_party.get("dashUrl") or alt_balaji.get("dashUrl")
                     or (mxplay.get("dash") or {}).get("high"))

        hls_path = (hls_data.get("high") or hls_data.get("base") or hls_data.get("main")
                    or third_party.get("hlsUrl") or alt_balaji.get("hlsUrl")
                    or (mxplay.get("hls") or {}).get("high"))

        dash_url = None
        if dash_path:
            dash_url = dash_path if dash_path.startswith("http") else f"https://llvod.mxplay.com/{dash_path}"

        hls_url = None
        if hls_path:
            hls_url = hls_path if hls_path.startswith("http") else f"https://llvod.mxplay.com/{hls_path}"

        if dash_url:
            self.log.info("Loading tracks from DASH")
            self.log.debug(f"Loading tracks from DASH: {dash_url}")
            try:
                tracks = Tracks.from_mpd(
                    url=dash_url,
                    session=self.session,
                    source=self.ALIASES[0]
                )
                return tracks
            except Exception as e:
                self.log.warning(f"Failed to load DASH manifest: {e}")

        if hls_url:
            self.log.info("Loading tracks from HLS")
            self.log.debug(f"Loading tracks from HLS: {hls_url}")
            try:
                tracks = Tracks.from_m3u8(
                    m3u8.loads(self.session.get(hls_url).text, hls_url),
                    source=self.ALIASES[0]
                )
                return tracks
            except Exception as e:
                self.log.warning(f"Failed to load HLS manifest: {e}")

        raise ValueError("No video tracks could be loaded for this title.")

    def get_chapters(self, title):
        return []

    def certificate(self, **_):
        return None

    def license(self, challenge, **_):
        return None
