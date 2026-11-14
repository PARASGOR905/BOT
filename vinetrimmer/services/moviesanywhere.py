import re
import click
import requests

from vinetrimmer.objects import Title, Tracks
from vinetrimmer.services.BaseService import BaseService


class MoviesAnywhere(BaseService):
    """
    Service code for US streaming service MoviesAnywhere (https://moviesanywhere.com).

    Authorization: Cookies
    Security: SD-HD@L3, FHD SDR@L1 (any active device), FHD-UHD HDR-DV@L1 (whitelisted devices).
    """
    ALIASES = ["MA", "moviesanywhere"]

    TITLE_RE = r"https://moviesanywhere\.com(?P<id>.+)"

    VIDEO_CODEC_MAP = {
        "H264": ["avc"],
        "H265": ["hvc", "hev", "dvh"]
    }
    AUDIO_CODEC_MAP = {
        "AAC": ["mp4a"],
        "AC3": ["ac-3", "ac3"],
        "EC3": ["ec-3", "ec3"]
    }

    @staticmethod
    @click.command(name="MoviesAnywhere", short_help="moviesanywhere.com")
    @click.argument("title", type=str)
    @click.pass_context
    def cli(ctx, **kwargs):
        return MoviesAnywhere(ctx, **kwargs)

    def __init__(self, ctx, title):
        super().__init__(ctx)
        self.parse_title(ctx, title)
        self.configure()
        self.vcodec = ctx.parent.params.get("vcodec") if (ctx and ctx.parent) else None
        self.acodec = ctx.parent.params.get("acodec") if (ctx and ctx.parent) else None
        self.range = ctx.parent.params.get("range_") if (ctx and ctx.parent) else "SDR"
        self.quality = ctx.parent.params.get("quality") if (ctx and ctx.parent) else None

        # Automatically switch to H265 when HDR / Dolby Vision or 4K/2160p resolution is requested
        if (self.range != "SDR" or self.quality == 2160) and self.vcodec == "H264":
            self.log.info(f" + Switched video codec to H265 for {self.range} / {self.quality}p")
            self.vcodec = "H265"

    def get_titles(self):
        slug = self.title
        if not slug.startswith("/"):
            slug = "/" + slug
        if not slug.startswith("/movie/"):
            slug = "/movie" + slug

        platform = self.config.get("platform", "android_tv")
        payload = {
            "platform": platform,
            "variables": {"slug": slug},
            "extensions": '{"persistedQuery":{"sha256Hash":"a25439c554231a8bdd7225b6d4bc132fff270f51cc0fb309bdc63d59ba674565","version":1}}',
        }

        # Ensure Android TV User-Agent and X-MA-Platform headers are explicitly sent to the GraphQL API.
        headers = {
            "Content-Type": "application/json",
            "User-Agent": self.config.get("headers", {}).get(
                "User-Agent",
                "Dalvik/2.1.0 (Linux; U; Android 11; SHIELD Android TV Build/RQ1A.210105.003)"
            ),
            "X-MA-Platform": platform,
        }
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        if self.install_id:
            headers["install-id"] = self.install_id

        timeout = self.config.get("timeout", 15)
        graphql_url = self.config.get("endpoints", {}).get(
            "graphql", "https://gateway.moviesanywhere.com/graphql"
        )

        try:
            res = self.session.post(
                url=graphql_url,
                json=payload,
                headers=headers,
                timeout=timeout,
            )
            res.raise_for_status()
        except requests.exceptions.Timeout:
            self.log.exit(" - Request to MoviesAnywhere GraphQL API timed out")
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else "unknown"
            self.log.exit(f" - MoviesAnywhere GraphQL API HTTP error ({status}): {e}")
        except requests.exceptions.RequestException as e:
            self.log.exit(f" - MoviesAnywhere GraphQL API network error: {e}")

        try:
            self.content = res.json()
        except (ValueError, requests.exceptions.JSONDecodeError):
            self.log.exit(f" - Unable to parse JSON response from MoviesAnywhere GraphQL API: {res.text[:200]}")

        if not isinstance(self.content, dict):
            self.log.exit(" - Invalid response structure from MoviesAnywhere GraphQL API")

        errors = self.content.get("errors")
        if errors:
            if isinstance(errors, list) and len(errors) > 0:
                first_err = errors[0]
                err_msg = first_err.get("message") if isinstance(first_err, dict) else str(first_err)
            else:
                err_msg = str(errors)
            self.log.exit(f" - MoviesAnywhere GraphQL API Error: {err_msg}")

        data = self.content.get("data")
        if not isinstance(data, dict):
            self.log.exit(" - No data object returned from MoviesAnywhere GraphQL API")

        page_data = data.get("page")
        if not isinstance(page_data, dict) or "components" not in page_data:
            self.log.exit(" - Title page not found on MoviesAnywhere")

        components_raw = page_data.get("components")
        if not isinstance(components_raw, list):
            self.log.exit(" - Invalid or missing components list in MoviesAnywhere page data")

        components = [c for c in components_raw if isinstance(c, dict)]

        play_comp = next(
            (c for c in components if c.get("__typename") in ("MovieMarqueeComponent", "HeroComponent")
             and isinstance(c.get("mainAction"), dict)
             and c.get("mainAction", {}).get("__typename") == "PlayAction"),
            None
        )
        store_comp = next(
            (c for c in components if c.get("__typename") in ("MovieMarqueeComponent", "HeroComponent")
             and isinstance(c.get("mainAction"), dict)
             and c.get("mainAction", {}).get("__typename") == "StoreAction"),
            None
        )

        metadata_comp = play_comp or store_comp or next(
            (c for c in components if c.get("__typename") in ("MovieMarqueeComponent", "HeroComponent")),
            None
        )
        if not metadata_comp:
            self.log.exit(" - Movie metadata component (MovieMarqueeComponent or HeroComponent) not found on page")

        title_name = re.sub(r" \(.+?\)", "", metadata_comp.get("title") or self.title.strip("/").replace("-", " ").title())
        title_year = metadata_comp.get("year", 0)

        main_action = metadata_comp.get("mainAction") if isinstance(metadata_comp.get("mainAction"), dict) else {}
        action_type = main_action.get("__typename")

        if action_type == "StoreAction" and not play_comp:
            self.log.exit(f" - Account does not own title '{title_name}' ({title_year}) on MoviesAnywhere (StoreAction / Buy required)")

        target_action = play_comp.get("mainAction", {}) if play_comp and isinstance(play_comp.get("mainAction"), dict) else main_action
        playable_id = None
        try:
            playable_id = target_action["playerData"]["playable"]["id"]
        except (KeyError, TypeError):
            self.log.exit(f" - Account does not seem to own title '{title_name}' ({title_year})")

        try:
            self.selected_player_data = target_action["playerData"]["playable"]
        except (KeyError, TypeError):
            self.selected_player_data = {}

        return Title(
            id_=playable_id,
            type_=Title.Types.MOVIE,
            name=title_name,
            year=title_year,
            original_lang="en",
            source=self.ALIASES[0],
            service_data=page_data,
        )

    def get_tracks(self, title):
        player_data = getattr(self, "selected_player_data", {}) or {}
        dash_assets = (player_data.get("videoAssets") or {}).get("dash") or {}

        all_tracks = Tracks()
        videos = []
        audios = []
        processed_urls = set()

        for cr in dash_assets.values():
            if not cr or not isinstance(cr, list):
                continue
            for manifest in cr:
                if not isinstance(manifest, dict):
                    continue
                m_url = manifest.get("url")
                if not m_url or m_url in processed_urls:
                    continue
                processed_urls.add(m_url)

                try:
                    parsed_tracks = Tracks.from_mpd(
                        url=m_url,
                        source=self.ALIASES[0],
                        session=self.session,
                    )
                except Exception as exc:
                    self.log.warning(f" - Failed to parse MPD manifest from {m_url}: {exc}")
                    continue

                license_url = manifest.get("widevineLaUrl") or self.config.get(
                    "endpoints", {}
                ).get("license", "https://licensing.moviesanywhere.com/licensing/v1/widevine/license")

                for video in parsed_tracks.videos:
                    video.license_url = license_url
                    if "dash_hevc_hdr" in (video.url or "") or "dash_hevc_hdr" in m_url:
                        video.hdr10 = True
                    if "dash_hevc_dolbyvision" in (video.url or "") or "dash_hevc_dolbyvision" in m_url:
                        video.dv = True
                    videos.append(video)

                for audio in parsed_tracks.audios:
                    audio.license_url = license_url
                    if "atmos" in (audio.url or "").lower() or "ec-3" in (audio.codec or "").lower():
                        audio.atmos = True
                    audios.append(audio)

        # Deduplicate video tracks across manifests
        unique_videos = []
        seen_video_keys = set()
        for v in videos:
            key = (
                v.width,
                v.height,
                v.bitrate,
                (v.codec or "").lower(),
                getattr(v, "hdr10", False),
                getattr(v, "dv", False)
            )
            if key not in seen_video_keys:
                seen_video_keys.add(key)
                unique_videos.append(v)

        all_tracks.videos = unique_videos

        # Apply video codec filter ONLY if specified
        if self.vcodec:
            if self.vcodec not in self.VIDEO_CODEC_MAP:
                self.log.exit(f" - Unsupported video codec {self.vcodec!r}. Supported: {', '.join(self.VIDEO_CODEC_MAP.keys())}")
            allowed_prefixes = self.VIDEO_CODEC_MAP[self.vcodec]
            filtered_videos = [x for x in all_tracks.videos if any((x.codec or "").lower().startswith(p.lower()) for p in allowed_prefixes)]

            if unique_videos and not filtered_videos:
                available = sorted({(v.codec or "unknown")[:4] for v in unique_videos})
                self.log.warning(
                    f" - No {self.vcodec} video tracks found for this title. "
                    f"Available codec(s): {', '.join(available)}."
                )
            elif filtered_videos:
                all_tracks.videos = filtered_videos

        # Deduplicate audio tracks
        unique_audios = []
        seen_audio_keys = set()
        for a in audios:
            key = (a.id, getattr(a, "language", None), getattr(a, "codec", None), getattr(a, "bitrate", None))
            if key not in seen_audio_keys:
                seen_audio_keys.add(key)
                unique_audios.append(a)

        # Apply audio codec filter ONLY if specified
        if self.acodec:
            if self.acodec not in self.AUDIO_CODEC_MAP:
                self.log.exit(f" - Unsupported audio codec {self.acodec!r}. Supported: {', '.join(self.AUDIO_CODEC_MAP.keys())}")
            allowed_acodecs = self.AUDIO_CODEC_MAP[self.acodec]
            all_tracks.audios = [
                x for x in unique_audios
                if any((x.codec or "").lower().startswith(c.lower()) for c in allowed_acodecs)
            ]
        else:
            all_tracks.audios = unique_audios

        return all_tracks

    def get_chapters(self, title):
        return []

    def certificate(self, **_):
        return None

    def license(self, challenge: bytes, track: Tracks, **_) -> bytes:
        timeout = self.config.get("timeout", 15)
        license_url = getattr(track, "license_url", None) or self.config.get("endpoints", {}).get(
            "license", "https://licensing.moviesanywhere.com/licensing/v1/widevine/license"
        )

        headers = {
            "Content-Type": "application/octet-stream",
            "User-Agent": self.config.get("headers", {}).get(
                "User-Agent",
                "Dalvik/2.1.0 (Linux; U; Android 11; SHIELD Android TV Build/RQ1A.210105.003)"
            ),
            "X-MA-Platform": self.config.get("platform", "android_tv"),
        }
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        if self.install_id:
            headers["install-id"] = self.install_id

        try:
            res = self.session.post(
                url=license_url,
                data=challenge,
                headers=headers,
                timeout=timeout,
            )
        except requests.exceptions.Timeout:
            self.log.exit(" - License request timed out")
        except requests.exceptions.HTTPError as e:
            res = e.response
            err_text = ""
            if res is not None:
                try:
                    err_text = res.content.decode("utf-8", errors="ignore")[:200]
                except Exception:
                    err_text = str(e)
            else:
                err_text = str(e)
            status = res.status_code if res is not None else "unknown"
            self.log.exit(f" - License request HTTP error ({status}): {err_text}")
        except requests.exceptions.RequestException as e:
            self.log.exit(f" - License request failed: {e}")

        if not res.content:
            self.log.exit(" - License request returned an empty response")

        content_type = res.headers.get("Content-Type", "").lower()
        if "json" in content_type or res.content.startswith(b"{"):
            try:
                err_data = res.json()
                err_msg = (
                    err_data.get("message")
                    or err_data.get("errorCode")
                    or err_data.get("error")
                    or err_data.get("errors")
                    or res.text
                )
            except Exception:
                err_msg = res.text
            self.log.exit(f" - License request returned error: {err_msg}")

        if "html" in content_type or res.content.startswith(b"<"):
            self.log.exit(f" - License request returned unexpected HTML response: {res.text[:200]}")

        return res.content

    def configure(self):
        access_token = None
        install_id = None
        for cookie in (self.cookies or []):
            if cookie.name == "secure_access_token":
                access_token = cookie.value
            elif cookie.name == "install_id":
                install_id = cookie.value

        self.access_token = access_token
        self.install_id = install_id

        if "headers" in self.config:
            self.session.headers.update(self.config["headers"])

        if self.access_token:
            self.session.headers["Authorization"] = f"Bearer {self.access_token}"
        if self.install_id:
            self.session.headers["install-id"] = self.install_id
