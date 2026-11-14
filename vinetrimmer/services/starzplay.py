import json
import re
import click

from vinetrimmer.objects import Title, Tracks
from vinetrimmer.services.BaseService import BaseService


class STARZPLAY(BaseService):
    """
    Service code for STARZPLAY streaming service (https://starzplay.com).
    """

    ALIASES = ["STARZPLAY", "starzplay", "starz"]
    GEOFENCE = ["ae"]
    TITLE_RE = [
        r"^(?:https?://(?:www\.)?starzplay\.com/(?:[a-z]{2}/)?(?:movies|series|freetv|live)/)(?P<slug>[a-zA-Z0-9_-]+)/(?P<id>\d+)",
        r"^(?:https?://(?:www\.)?starzplay\.com/(?:[a-z]{2}/)?(?:movies|series|freetv|live)/)?(?P<id>\d+)",
    ]

    @staticmethod
    @click.command(name="STARZPLAY", short_help="https://starzplay.com")
    @click.argument("title", type=str, required=False)
    @click.pass_context
    def cli(ctx, **kwargs):
        return STARZPLAY(ctx, **kwargs)

    def __init__(self, ctx, title):
        self.raw_title = title or ctx.parent.params.get("title") or ""
        super().__init__(ctx)
        self.parse_title(ctx, title)

        self.license_url = self.config.get("endpoints", {}).get("license", "https://widevine-license.vudrm.tech/proxy")
        cookie_dict = {c.name: c.value for c in self.session.cookies}
        self.spx_token = cookie_dict.get("spx-token")
        self.jwt_token = cookie_dict.get("starz-jwt")
        self.country = cookie_dict.get("sash_loc_co", "AE")

        profile_auth = cookie_dict.get("selectedProfileAuthTokens")
        if profile_auth:
            try:
                auth_data = json.loads(profile_auth)
                self.spx_token = auth_data.get("spxToken") or self.spx_token
                self.jwt_token = auth_data.get("jwtToken") or self.jwt_token
            except Exception:
                pass

        self.session.headers.update({
            "Origin": "https://starzplay.com",
            "Referer": "https://starzplay.com/",
            "Authorization": f"Bearer {self.spx_token}" if self.spx_token else "",
        })

    def get_titles(self):
        target = self.raw_title or str(self.title)
        match = re.search(r"/(?:movies|series|freetv|live)/[a-zA-Z0-9_-]+/(\d+)", target)
        if match:
            content_id = match.group(1)
        else:
            id_match = re.search(r"\b(\d{10,})\b", target)
            content_id = id_match.group(1) if id_match else self.title

        catalog_endpoint = f"{self.config['endpoints']['catalog']}/{content_id}"
        params = {
            "parentalControl": 18,
            "extendInfo": "true",
            "lang": "en,ar,fr",
            "mediaAssetTypes": "dash_widevine_spa",
            "byMediaAvailabilityTags": "unpackagedTrailer|packagedTrailer|noTrailer",
            "country": self.country,
        }

        res = self.session.get(catalog_endpoint, params=params)
        res.raise_for_status()
        data = res.json()

        titles_data = data.get("titles", [])
        if not titles_data:
            raise ValueError(f"No title found in mediaCatalog for ID: {content_id}")

        item = titles_data[0]
        show_title = item.get("title") or item.get("sortTitle") or "Untitled"
        content_type = item.get("contentType", "series")
        year = int(item.get("year", 2026))

        media_list = item.get("media", [])
        titles = []

        if content_type == "movie" or len(media_list) == 1 and content_type != "series":
            mpd_url = self._extract_mpd(media_list[0]) if media_list else None
            titles.append(
                Title(
                    id_=content_id,
                    type_=Title.Types.MOVIE,
                    name=show_title,
                    year=year,
                    original_lang="en",
                    source=self.ALIASES[0],
                    service_data={
                        "content_id": content_id,
                        "mpd_url": mpd_url,
                    },
                )
            )
        else:
            for idx, media_item in enumerate(media_list, start=1):
                guid = media_item.get("mediaGuid", "")
                ep_title = media_item.get("mediaTitle") or f"Episode {idx}"

                # Try parsing Season and Episode numbers from GUID (e.g. ...S01E001)
                season_num, ep_num = 1, idx
                guid_match = re.search(r"S(\d+)E(\d+)", guid, re.IGNORECASE)
                if guid_match:
                    season_num = int(guid_match.group(1))
                    ep_num = int(guid_match.group(2))
                else:
                    ep_match = re.search(r"Episode\s*(\d+)", ep_title, re.IGNORECASE)
                    if ep_match:
                        ep_num = int(ep_match.group(1))

                mpd_url = self._extract_mpd(media_item)

                titles.append(
                    Title(
                        id_=guid or f"{content_id}_{season_num}_{ep_num}",
                        type_=Title.Types.TV,
                        name=show_title,
                        season=season_num,
                        episode=ep_num,
                        episode_name=ep_title,
                        year=year,
                        original_lang="en",
                        source=self.ALIASES[0],
                        service_data={
                            "content_id": content_id,
                            "guid": guid,
                            "mpd_url": mpd_url,
                            "season": season_num,
                            "episode": ep_num,
                        },
                    )
                )

        return titles

    def _extract_mpd(self, media_item):
        for c in media_item.get("content", []):
            url = c.get("streamingUrl")
            if url and ("dash" in str(c.get("assetTypes", [])).lower() or c.get("format") == "MPEG-DASH"):
                # Akamai (starzplayarabia.com) blocks VPN IP pools (Reference #18)
                # Fall back to PlayCo AWS CDN origin which serves the identical streams without blocking
                url = re.sub(r"https?://[a-zA-Z0-9_\-\.]*starzplayarabia\.com", "http://mena-jit-cdn-lb.aws.playco.com", url)
                return url
        return None

    def get_tracks(self, title):
        mpd_url = title.service_data.get("mpd_url")
        if not mpd_url:
            raise ValueError(f"No DASH MPD streamingUrl found for {title.name} {title.id}")

        tracks = Tracks.from_mpd(
            url=mpd_url,
            session=self.session,
            source=self.ALIASES[0],
        )
        return tracks

    def certificate(self, challenge, title, track, session_id):
        return self.license(challenge, title, track, session_id)

    def license(self, challenge, title, track, session_id):
        headers = {
            "Content-Type": "application/octet-stream",
            "x-vudrm-token": "9c89f7d3-2fe7-4986-aaaf-dacaf8f37b93",
            "X-VUDRM-TOKEN": "9c89f7d3-2fe7-4986-aaaf-dacaf8f37b93",
        }
        if self.spx_token:
            headers["x-spx-token"] = self.spx_token

        try:
            res = self.session.post(
                self.license_url,
                data=challenge,
                headers=headers,
            )
            res.raise_for_status()
            return res.content
        except Exception:
            # Fallback to VUDRM JSON payload format
            payload = {
                "token": "9c89f7d3-2fe7-4986-aaaf-dacaf8f37b93",
                "drm_info": list(challenge),
            }
            res = self.session.post(
                self.license_url,
                json=payload,
                headers={"Content-Type": "application/json"},
            )
            res.raise_for_status()
            return res.content
