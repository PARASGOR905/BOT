import click
import requests
import urllib.parse
from typing import List, Union, Any

from vinetrimmer.objects import Title, Tracks
from vinetrimmer.services.BaseService import BaseService


class AtresPlayer(BaseService):
    """
    \b
    Service code for Atresplayer streaming service (https://www.atresplayer.com/).

    \b
    Author: Antigravity
    Authorization: Credentials (username & password)
    """

    ALIASES = ["AtresPlayer", "atresplayer", "atp", "atres"]
    TITLE_RE = r"^(?:https?://(?:www\.)?atresplayer\.com)?(?P<id>/(?:[^/?#]+/)*[a-zA-Z0-9_\-]+/?)"

    @staticmethod
    @click.command(name="AtresPlayer", short_help="https://www.atresplayer.com")
    @click.argument("title", type=str)
    @click.pass_context
    def cli(ctx, **kwargs):
        return AtresPlayer(ctx, **kwargs)

    def __init__(self, ctx, title: str):
        super().__init__(ctx)
        self.parse_title(ctx, title)
        self.configure()

    def configure(self):
        if self.credentials:
            self.log.info(" + Logging into Atresplayer...")
            data = {
                "username": self.credentials.username,
                "password": self.credentials.password,
            }
            res = self.session.post(
                "https://account.atresplayer.com/auth/v1/login",
                data=data
            )
            if res.status_code != 200:
                raise ValueError(f"Failed to log in to Atresplayer: {res.text}")
            self.log.info(" + Login successful!")

    def get_titles(self) -> List[Title]:
        path = self.title
        if not path.startswith("/"):
            path = "/" + path

        url_res = self.session.get("https://api.atresplayer.com/client/v1/url", params={"href": path})
        if url_res.status_code != 200:
            raise ValueError(f"Failed to resolve URL '{path}': {url_res.text}")

        page_info = url_res.json()
        if "href" not in page_info:
            raise ValueError(f"No API endpoint found for path '{path}'")

        meta = self.session.get(page_info["href"]).json()
        page_type = page_info.get("pageType") or meta.get("pageType")

        titles = []

        if page_type == "EPISODE":
            titles.append(Title(
                id_=meta.get("id"),
                type_=Title.Types.TV,
                name=meta.get("format", {}).get("title") or meta.get("title"),
                season=int(meta.get("currentSeason", {}).get("seasonNumber") or 1),
                episode=int(meta.get("numberOfEpisode") or 1),
                episode_name=meta.get("title"),
                original_lang="es",
                source=self.ALIASES[0],
                service_data=meta,
            ))
        elif page_type in ("FORMAT", "SEASON"):
            format_id = meta.get("id")
            episodes_found = []

            if format_id:
                search_url = f"https://api.atresplayer.com/client/v1/row/search?entityType=ATPEpisode&formatId={format_id}"
                row_res = self.session.get(search_url).json()
                episodes_found = row_res.get("items") or row_res.get("itemRows") or []

            for idx, ep in enumerate(episodes_found, 1):
                ep_link = ep.get("link", {})
                ep_href = ep_link.get("href")
                if ep_href:
                    titles.append(Title(
                        id_=ep.get("contentId") or ep_href.split("/")[-1],
                        type_=Title.Types.TV,
                        name=ep.get("subTitle") or meta.get("title"),
                        season=1,
                        episode=idx,
                        episode_name=ep.get("title"),
                        original_lang="es",
                        source=self.ALIASES[0],
                        service_data={"href": ep_href, "raw": ep},
                    ))
        else:
            titles.append(Title(
                id_=meta.get("id"),
                type_=Title.Types.MOVIE,
                name=meta.get("title"),
                original_lang="es",
                source=self.ALIASES[0],
                service_data=meta,
            ))

        return titles

    def get_tracks(self, title: Title) -> Tracks:
        service_data = title.service_data
        url_video = service_data.get("urlVideo")
        if not url_video and "href" in service_data:
            ep_meta = self.session.get(service_data["href"]).json()
            url_video = ep_meta.get("urlVideo")

        if not url_video:
            raise ValueError(f"Could not find urlVideo endpoint for title '{title.name}'")

        res = self.session.get(url_video)
        if res.status_code == 403:
            raise ValueError(f"Playback forbidden for '{title.name}' (403): {res.text}")

        v_data = res.json()
        sources = v_data.get("sources", [])

        candidate_urls = []
        for s in sources:
            stype = s.get("type", "")
            src = s.get("src", "")
            if stype == "application/dash+hevc" or "hevc" in src.lower():
                candidate_urls.insert(0, ("mpd", src))
            elif "dash" in stype or src.endswith(".mpd"):
                candidate_urls.append(("mpd", src))
            elif "mpegurl" in stype or "hls" in stype or src.endswith(".m3u8"):
                candidate_urls.append(("m3u8", src))

        combined_tracks = None
        for format_type, url in candidate_urls:
            try:
                t = None
                if format_type == "mpd":
                    res = self.session.get(url)
                    text = res.text.strip()
                    if text.startswith("<"):
                        t = Tracks.from_mpd(data=text, url=url, session=self.session, source=self.ALIASES[0])
                elif format_type == "m3u8":
                    res = self.session.get(url)
                    import m3u8
                    master = m3u8.loads(res.text, uri=url)
                    t = Tracks.from_m3u8(master, source=self.ALIASES[0], url=url)

                if t and (t.videos or t.audios):
                    if combined_tracks is None:
                        combined_tracks = t
                    else:
                        combined_tracks.add(t, warn_only=True)
            except Exception as e:
                self.log.warning(f"Failed to load {format_type} manifest from {url}: {e}")

        if not combined_tracks:
            raise ValueError(f"No valid tracks could be extracted for '{title.name}'")

        return combined_tracks

    def get_chapters(self, title: Title):
        return []

    def certificate(self, **_):
        return None

    def license(self, challenge: bytes, **_):
        return None
