from __future__ import annotations

import base64
import json
import re
import os
import click
import requests
import m3u8

from vinetrimmer.objects import Title, Tracks, Track
from vinetrimmer.services.BaseService import BaseService
from vinetrimmer.utils.io import get_ip_info


class LGP(BaseService):
    """
    Service code for Lionsgate Play (https://lionsgateplay.com/)
    """

    ALIASES = ["LGP", "lgp", "lionsgate", "lionsgateplay", "LionsgatePlay"]
    GEOFENCE = []

    TITLE_RE = [
        r"https?://(?:www\.)?lionsgateplay\.com/shows/[^/]+/(?P<id>[a-zA-Z0-9_]+)",
        r"https?://(?:www\.)?lionsgateplay\.com/movies/[^/]+/(?P<id>[a-zA-Z0-9_]+)",
        r"^(?P<id>https?://\S+\.(?:mpd|m3u8)(?:\?\S*)?)$",
        r"^(?P<id>[a-zA-Z0-9_]+)$",
    ]

    @staticmethod
    @click.command(name="LGP", short_help="https://lionsgateplay.com/", help=__doc__)
    @click.argument("title", type=str)
    @click.pass_context
    def cli(ctx, **kwargs):
        return LGP(ctx, **kwargs)

    def __init__(self, ctx, title):
        self.url = title
        super().__init__(ctx)
        self.parse_title(ctx, self.url)

        # Retrieve current IP country to set correct API catalog region
        try:
            self.country = get_ip_info().get("country_code", "IN").upper()
        except Exception:
            self.country = "IN"

        self.access_token = None
        if self.credentials:
            self.access_token = getattr(self.credentials, "token", None) or getattr(self.credentials, "access_token", None)

        if not self.access_token and self.cookies:
            # Check if cookies or txt contain FlutterSecureStorage token
            for cookie in self.cookies:
                if "access_token" in cookie.name:
                    self.access_token = cookie.value
                    break

        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://lionsgateplay.com",
            "Referer": "https://lionsgateplay.com/",
            "x-country": self.country,
            "x-platform": "web"
        })

        if self.access_token:
            clean_token = self.access_token.strip().replace("Bearer ", "")
            self.session.headers["Authorization"] = f"Bearer {clean_token}"
            self.nv_authorizations = clean_token

    def get_titles(self) -> Title | list[Title]:
        # If it's a direct manifest URL, pass it directly
        if re.search(r"\.(?:mpd|m3u8)", self.title, re.IGNORECASE):
            self.log.debug(" + Using direct manifest URL: %s", self.title)
            name = "LionsgatePlay_Video"
            path_part = self.title.split("?")[0].rstrip("/")
            match_segment = re.search(r"([^/]+)\.(?:mpd|m3u8)", path_part, re.IGNORECASE)
            if match_segment:
                name = match_segment.group(1)

            return Title(
                id_=name,
                type_=Title.Types.MOVIE,
                name=name,
                source=self.ALIASES[0],
                service_data={"manifest_url": self.title},
            )

        api_url = "https://api-prod.lionmultiverse.com/cms/graphql"
        self.log.info("Requesting content type from GraphQL API: %s", api_url)

        # Step 1: Query asset type
        type_query = """
        query GetAssetType($filters: AssetFiltersInput) {
          assets(filters: $filters) {
            Asset_Type
            Asset_ID
            Asset_Name
          }
        }
        """

        variables = {
            "filters": {
                "Asset_ID": {
                    "eq": self.title
                }
            }
        }

        try:
            r = self.session.post(api_url, json={"query": type_query, "variables": variables})
            r.raise_for_status()
            res = r.json()
        except Exception as e:
            raise self.log.exit(f" - Failed to fetch metadata from GraphQL API: {e}")

        assets = res.get("data", {}).get("assets") or []
        if not assets:
            raise self.log.exit(f" - Content not found or geo-blocked in {self.country}: {self.title}")

        asset = assets[0]
        asset_type = asset.get("Asset_Type")
        asset_name = asset.get("Asset_Name") or f"LionsgatePlay_{self.title}"

        if asset_type == "Series":
            # Step 2 for Series: Query Asset_Details (plural)
            series_query = """
            query GetSeriesDetails($filters: AssetFiltersInput) {
              assets(filters: $filters) {
                Asset_Type
                Asset_ID
                Asset_Name
                Asset_Details {
                  Seasons {
                    Season_Number
                    Season_Name
                    Season_ID
                    Episodes {
                      Episode_ID
                      Episode_Name
                      Episode_Number
                      DRM_Video_Subtitle {
                        Video_URLs {
                          HLS_M3U8_URL
                          DASH_MPD_URL
                          Language {
                            Name
                          }
                        }
                      }
                    }
                  }
                }
              }
            }
            """
            try:
                r = self.session.post(api_url, json={"query": series_query, "variables": variables})
                r.raise_for_status()
                res = r.json()
                # Save to scratch for debugging
                os.makedirs("scratch", exist_ok=True)
                with open("scratch/lgp_get_titles_response.json", "w", encoding="utf-8") as f:
                    json.dump(res, f, indent=2)
            except Exception as e:
                raise self.log.exit(f" - Failed to fetch Series details from GraphQL API: {e}")

            assets = res.get("data", {}).get("assets") or []
            if not assets or not assets[0].get("Asset_Details"):
                raise self.log.exit(f" - No Series details found for: {self.title}")

            titles = []
            seasons = assets[0]["Asset_Details"].get("Seasons") or []
            for season in seasons:
                season_num = season.get("Season_Number")
                episodes = season.get("Episodes") or []
                for ep in episodes:
                    ep_num = ep.get("Episode_Number")
                    ep_name = ep.get("Episode_Name")
                    ep_id = ep.get("Episode_ID")
                    
                    manifest_url = None
                    urls = ep.get("DRM_Video_Subtitle", {}).get("Video_URLs") or []
                    for u in urls:
                        # Prefer DASH (.mpd) over HLS (.m3u8) for stability
                        manifest_url = u.get("DASH_MPD_URL") or u.get("HLS_M3U8_URL")
                        if manifest_url:
                            break

                    titles.append(Title(
                        id_=ep_id,
                        type_=Title.Types.TV,
                        name=asset_name,
                        season=season_num,
                        episode=ep_num,
                        episode_name=ep_name,
                        source=self.ALIASES[0],
                        service_data={"manifest_url": manifest_url},
                    ))
            return titles

        else:
            # Step 2 for Movie: Query Asset_Detail (singular)
            movie_query = """
            query GetMovieDetails($filters: AssetFiltersInput) {
              assets(filters: $filters) {
                Asset_Type
                Asset_ID
                Asset_Name
                Asset_Detail {
                  DRM_Video_Subtitle {
                    Video_URLs {
                      HLS_M3U8_URL
                      DASH_MPD_URL
                      Language {
                        Name
                      }
                    }
                  }
                }
              }
            }
            """
            try:
                r = self.session.post(api_url, json={"query": movie_query, "variables": variables})
                r.raise_for_status()
                res = r.json()
                # Save to scratch for debugging
                os.makedirs("scratch", exist_ok=True)
                with open("scratch/lgp_get_titles_response.json", "w", encoding="utf-8") as f:
                    json.dump(res, f, indent=2)
            except Exception as e:
                raise self.log.exit(f" - Failed to fetch Movie details from GraphQL API: {e}")

            assets = res.get("data", {}).get("assets") or []
            if not assets or not assets[0].get("Asset_Detail"):
                raise self.log.exit(f" - No Movie details found for: {self.title}")

            manifest_url = None
            urls = assets[0]["Asset_Detail"].get("DRM_Video_Subtitle", {}).get("Video_URLs") or []
            for u in urls:
                manifest_url = u.get("DASH_MPD_URL") or u.get("HLS_M3U8_URL")
                if manifest_url:
                    break

            return Title(
                id_=self.title,
                type_=Title.Types.MOVIE,
                name=asset_name,
                source=self.ALIASES[0],
                service_data={"manifest_url": manifest_url},
            )

    def get_tracks(self, title: Title) -> Tracks:
        manifest_url = title.service_data.get("manifest_url")

        if not manifest_url:
            raise self.log.exit(
                " - No playback stream URL found for this content.\n"
                "   Make sure you have valid credentials/cookies configured or that the content is fully released."
            )

        self.log.info(f"Loading tracks from manifest: {manifest_url}")

        if ".mpd" in manifest_url.lower():
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

    def license(self, challenge: bytes, title: Title = None, track: Track = None, **_) -> bytes:
        license_url = self.config["endpoints"]["license"]
        self.log.info(" + Routing license request to Lionsgate Play DRM server: %s", license_url)

        headers = {
            "Content-Type": "application/octet-stream",
            "Origin": "https://lionsgateplay.com",
            "Referer": "https://lionsgateplay.com/",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        }

        # Check for Nagra DRM nv-authorizations token
        nv_token = getattr(self, "nv_authorizations", None)
        if not nv_token and self.credentials:
            nv_token = getattr(self.credentials, "token", None) or getattr(self.credentials, "nv_authorizations", None)

        if nv_token:
            headers["nv-authorizations"] = nv_token.strip()
            self.log.debug(" + Using nv-authorizations token for license request")

        r = self.session.post(license_url, data=challenge, headers=headers)
        r.raise_for_status()
        return r.content
