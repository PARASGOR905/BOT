import json
import re
import sys

import click
import requests

from vinetrimmer.objects import Title, Tracks
from vinetrimmer.services.BaseService import BaseService


class AmcPlus(BaseService):
    """
    Service code for AMC+ (https://www.amcplus.com).

    \b
    Authorization: Cookies
    Security: FHD@L3 (Widevine via Brightcove)

    \b
    URL formats supported:
      https://www.amcplus.com/movies/{slug}--{nid}
      https://www.amcplus.com/shows/{slug}--{nid}
      https://www.amcplus.com/shows/{slug}/episodes--{season_nid}
      https://www.amcplus.com/shows/{slug}/episodes/{ep_slug}--{ep_nid}
    """

    ALIASES = ["AMCP", "amcplus", "amc+"]
    GEOFENCE = ["us"]

    # We capture a broad NID from the trailing --<digits>, but we also keep
    # the full URL in self.raw_title for routing in get_titles().
    TITLE_RE = [
        r"^(?:https?://(?:www\.)?amcplus\.com)?/shows/[^/]+/episodes/[^/]+-+(?P<id>\d+)$",
        r"^(?:https?://(?:www\.)?amcplus\.com)?/shows/[^/]+/episodes-+(?P<id>\d+)$",
        r"^(?:https?://(?:www\.)?amcplus\.com)?/shows/[^/]+-+(?P<id>\d+)$",
        r"^(?:https?://(?:www\.)?amcplus\.com)?/movies/[^/]+-+(?P<id>\d+)$",
    ]

    @staticmethod
    @click.command(name="AmcPlus", short_help="https://amcplus.com")
    @click.argument("title", type=str, required=False)
    @click.pass_context
    def cli(ctx, **kwargs):
        return AmcPlus(ctx, **kwargs)

    def __init__(self, ctx, title):
        super().__init__(ctx)
        # Save the raw URL before parse_title strips it to just the NID
        self.raw_title = title or ctx.parent.params.get("title", "")
        self.parse_title(ctx, title)

        self.user_token = None
        self.widevine_license_url = None

        self.configure()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def configure(self):
        """Load user_token from cookies and set AMC API headers on session."""
        for cookie in self.session.cookies:
            if cookie.name == "user_token":
                self.user_token = cookie.value
                break

        if not self.user_token:
            self.log.exit(
                " - No user_token cookie found. "
                "Export AMC+ cookies to vinetrimmer/cookies/AmcPlus/default.txt"
            )

        svc_headers = self.config.get("headers", {})
        self.session.headers.update({
            "Accept": "application/json",
            "Authorization": f"Bearer {self.user_token}",
            **svc_headers,
        })

    def _fetch_initial_data(self, path):
        """GET an amcplus.com page and parse window.initialData JSON."""
        url = f"https://www.amcplus.com{path}"
        self.log.info(f" + Fetching: {url}")
        resp = self.session.get(url)
        m = re.search(
            r"window\.initialData\s*=\s*JSON\.parse\(String\.raw`(\{.*?\})`\);",
            resp.text, re.DOTALL
        )
        if not m:
            self.log.exit(f" - Could not parse window.initialData from {url}")
        return json.loads(m.group(1))

    @staticmethod
    def _find_cards(node):
        """Recursively collect all card-type nodes in an initialData tree."""
        cards = []
        if isinstance(node, dict):
            if node.get("type") == "card":
                cards.append(node)
            for v in node.values():
                cards.extend(AmcPlus._find_cards(v))
        elif isinstance(node, list):
            for item in node:
                cards.extend(AmcPlus._find_cards(item))
        return cards

    @staticmethod
    def _page_title(init_data):
        """Best-effort show/movie name from initialData properties."""
        p = init_data.get("initialData", {}).get("properties", {})
        raw = p.get("title", "") or p.get("pageTitle", "")
        return re.sub(r"\s*\|.*$", "", raw).strip() or raw.strip()

    def _discover_season_nids(self, show_slug, show_nid):
        """
        Fetch the show page and extract all episode-list page NIDs, one per season.
        Returns list of (season_number, season_episodes_nid) in order.
        """
        init_data = self._fetch_initial_data(f"/shows/{show_slug}--{show_nid}")
        text = json.dumps(init_data)
        matches = re.findall(r"/shows/[^\"\s]+/episodes-+(\d+)", text)
        seen = []
        for nid in matches:
            if nid not in seen:
                seen.append(nid)
        return [(i + 1, nid) for i, nid in enumerate(seen)]

    def _episodes_for_season(self, show_slug, season_nid, show_name, season_number):
        """Fetch an episode-list page and return a list of TV Title objects."""
        path = f"/shows/{show_slug}/episodes--{season_nid}"
        init_data = self._fetch_initial_data(path)
        cards = self._find_cards(init_data.get("initialData", {}))

        titles = []
        for card in cards:
            props = card.get("properties", {})
            if props.get("contentType") != "episode":
                continue
            analytics = props.get("analyticsObject", {})
            meta = props.get("meta", {})
            text_obj = props.get("text", {})

            ep_nid = str(meta.get("nid") or analytics.get("nid", ""))
            ep_num = analytics.get("contentEpisode", 0)
            ep_name = text_obj.get("title", "")

            if not ep_nid:
                continue

            titles.append(Title(
                id_=ep_nid,
                type_=Title.Types.TV,
                name=show_name,
                season=season_number,
                episode=ep_num,
                episode_name=ep_name,
                source=self.ALIASES[0],
                service_data={"nid": ep_nid},
            ))
        return titles

    # ------------------------------------------------------------------
    # BaseService abstract methods
    # ------------------------------------------------------------------

    def get_titles(self):
        raw = self.raw_title.strip()

        # ---- Single episode: /shows/.../episodes/ep-slug--NID ----
        m = re.match(
            r"(?:https?://(?:www\.)?amcplus\.com)?(/shows/([^/]+)/episodes/[^/]+-+(\d+))/?$",
            raw
        )
        if m:
            path = m.group(1)
            show_slug = m.group(2)
            ep_nid = m.group(3)
            init_data = self._fetch_initial_data(path)
            show_name = self._page_title(init_data)

            # Pull season/episode numbers from analyticsObject in page data
            text = json.dumps(init_data)
            season_num, ep_num, ep_name = 1, 1, ""
            for j_str in re.findall(r'"analyticsObject"\s*:\s*(\{[^}]+\})', text):
                try:
                    a = json.loads(j_str)
                    if a.get("contentSeason", -1) >= 1:
                        season_num = a.get("contentSeason", 1)
                        ep_num = a.get("contentEpisode", 1)
                        ep_name = a.get("contentTitle", "")
                        break
                except Exception:
                    pass

            return [Title(
                id_=ep_nid,
                type_=Title.Types.TV,
                name=show_name,
                season=season_num,
                episode=ep_num,
                episode_name=ep_name,
                source=self.ALIASES[0],
                service_data={"nid": ep_nid},
            )]

        # ---- Season episodes list: /shows/.../episodes--NID ----
        m = re.match(
            r"(?:https?://(?:www\.)?amcplus\.com)?(/shows/([^/]+)/episodes-+(\d+))/?$",
            raw
        )
        if m:
            path = m.group(1)
            show_slug = m.group(2)
            season_nid = m.group(3)
            init_data = self._fetch_initial_data(path)
            show_name = self._page_title(init_data)
            sn_m = re.search(r"[Ss]eason\s+(\d+)", json.dumps(init_data))
            season_number = int(sn_m.group(1)) if sn_m else 1
            return self._episodes_for_season(show_slug, season_nid, show_name, season_number)

        # ---- Show landing: /shows/slug--NID ----
        m = re.match(
            r"(?:https?://(?:www\.)?amcplus\.com)?(/shows/([^/]+))/?$",
            raw
        )
        if m and "episodes" not in raw:
            path = m.group(1)
            slug_and_nid = m.group(2)
            sn_m = re.match(r"(.+)-+(\d+)$", slug_and_nid)
            if not sn_m:
                self.log.exit(f" - Could not parse show slug/NID from: {slug_and_nid}")
            show_slug = sn_m.group(1)
            show_nid = sn_m.group(2)
            init_data = self._fetch_initial_data(path)
            show_name = self._page_title(init_data)
            season_pairs = self._discover_season_nids(show_slug, show_nid)
            titles = []
            for season_number, season_nid in season_pairs:
                titles.extend(
                    self._episodes_for_season(show_slug, season_nid, show_name, season_number)
                )
            return titles

        # ---- Movie: /movies/slug--NID ----
        m = re.match(
            r"(?:https?://(?:www\.)?amcplus\.com)?(/movies/[^/]+-+(\d+))/?$",
            raw
        )
        if m:
            path = m.group(1)
            movie_nid = m.group(2)
            init_data = self._fetch_initial_data(path)
            movie_name = self._page_title(init_data)
            return [Title(
                id_=movie_nid,
                type_=Title.Types.MOVIE,
                name=movie_name,
                source=self.ALIASES[0],
                service_data={"nid": movie_nid},
            )]

        # ---- Fallback: bare NID ----
        nid = self.title  # set by parse_title()
        self.log.warning(f" - Treating as bare NID: {nid}")
        return [Title(
            id_=nid,
            type_=Title.Types.MOVIE,
            name="Unknown",
            source=self.ALIASES[0],
            service_data={"nid": nid},
        )]

    def get_tracks(self, title):
        nid = title.service_data.get("nid") or title.id
        playback_url = self.config["endpoints"]["playback"].format(nid=nid)

        body = {
            "adobeShortMediaToken": "",
            "hba": False,
            "adtags": {
                "lat": 0,
                "url": "https://www.amcplus.com",
                "playerWidth": 1920,
                "playerHeight": 1080,
                "ppid": 1,
                "mode": "on-demand",
                "uid2": "",
            },
            "useLowResVideo": False,
        }

        resp = self.session.post(playback_url, json=body)
        resp_data = resp.json()

        if not resp_data.get("success"):
            error = resp_data.get("error", "Unknown error")
            self.log.exit(f" - Playback API error for NID {nid}: {error}")

        pb_json = resp_data["data"]["playbackJsonData"]

        # Find HTTPS DASH + Widevine source
        dash_source = None
        for src in pb_json.get("sources", []):
            if (
                src.get("type") == "application/dash+xml"
                and src.get("src", "").startswith("https")
                and "com.widevine.alpha" in src.get("key_systems", {})
            ):
                dash_source = src
                break

        if not dash_source:
            self.log.exit(f" - No HTTPS DASH/Widevine source found for NID {nid}")

        self.widevine_license_url = (
            dash_source["key_systems"]["com.widevine.alpha"]["license_url"]
        )
        mpd_url = dash_source["src"]

        self.log.info(f" + MPD: {mpd_url[:80]}...")
        mpd_resp = self.session.get(mpd_url)

        return Tracks.from_mpd(
            data=mpd_resp.text,
            url=mpd_url,
            source=self.ALIASES[0],
        )

    def get_chapters(self, title):
        return []

    def certificate(self, challenge, **_):
        return self.license(challenge)

    def license(self, challenge, **_):
        if not self.widevine_license_url:
            self.log.exit(" - No Widevine license URL. Call get_tracks() first.")

        resp = requests.post(
            self.widevine_license_url,
            data=challenge,
            headers={
                "User-Agent": (
                    self.session.headers.get("User-Agent", "")
                    or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/128.0.0.0 Safari/537.36"
                ),
                "Origin": "https://www.amcplus.com",
                "Referer": "https://www.amcplus.com/",
            },
            timeout=30,
        )
        resp.raise_for_status()
        return resp.content
