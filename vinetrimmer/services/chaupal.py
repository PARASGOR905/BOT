import json
import re
import urllib.parse
from urllib.parse import urlparse
import uuid
import click
import m3u8
import requests
from langcodes import Language

from vinetrimmer.config import directories
from vinetrimmer.objects import Title
from vinetrimmer.objects.tracks import Tracks
from vinetrimmer.services.BaseService import BaseService


class Chaupal(BaseService):
    """
    Service class for Chaupal (chaupal.com).
    """

    ALIASES = ["CHTV", "CHAUPAL", "chaupal", "Chaupal"]

    TITLE_RE = [
        r"https?://(?:www\.)?chaupal\.(?:com|tv)/(?:movie|tvshow)/(?P<id>[^/?#]+)",
        r"https?://tvapps\.chaupal\.com/(?:#/)?(?:movie|tvshow)/(?:play/)?(?P<id>[^/?#]+)",
        r"^chaupal:(?P<id>.+)$",
    ]

    BASE_API = "https://chaupalapi.revlet.net"
    USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"

    @staticmethod
    @click.command(name="Chaupal", short_help="https://www.chaupal.com/", help=__doc__)
    @click.argument("title", type=str)
    @click.pass_context
    def cli(ctx, **kwargs):
        return Chaupal(ctx, **kwargs)

    def __init__(self, ctx, title):
        self.url = title
        super().__init__(ctx)
        self.parse_title(ctx, self.url)

    def resolve_path_from_html(self, url):
        """Scrape webpage HTML to extract playback path from __NEXT_DATA__ metadata."""
        if not url.startswith("http"):
            return None
        try:
            headers = {
                "User-Agent": self.USER_AGENT,
                "Origin": "https://www.chaupal.com",
                "Referer": "https://www.chaupal.com/",
            }
            r = requests.get(url, headers=headers, timeout=15)
            if r.status_code != 200:
                return None

            match = re.search(r'<script\s+id="__NEXT_DATA__"\s+type="application/json">(.*?)</script>', r.text, re.DOTALL)
            if not match:
                return None

            data = json.loads(match.group(1))
            meta_tags = (
                data.get("props", {})
                .get("pageProps", {})
                .get("seodata", {})
                .get("response", {})
                .get("metaTags", [])
            )
            for tag in meta_tags:
                if tag.get("tagName") == "videoschema":
                    content_str = tag.get("content", "")
                    try:
                        schema = json.loads(content_str)
                        content_url = schema.get("contentUrl")
                        if content_url:
                            parsed = urlparse(content_url)
                            return parsed.path.lstrip("/")
                    except Exception:
                        pass
        except Exception as e:
            self.log.debug(f"Failed to resolve path from html: {e}")
        return None

    @staticmethod
    def _parse_year(*sources):
        for src in sources:
            if not src:
                continue
            if isinstance(src, dict):
                for key in ("release_year", "releaseYear", "year", "yearOfRelease", "productionYear", "release_date", "releaseDate", "uploadDate", "dateCreated", "datePublished"):
                    val = src.get(key)
                    if val:
                        res = Chaupal._parse_year(val)
                        if res:
                            return res
                for nested_key in ("metadata", "page_info", "attributes", "content", "display"):
                    val = src.get(nested_key)
                    if isinstance(val, dict):
                        res = Chaupal._parse_year(val)
                        if res:
                            return res
            elif isinstance(src, (list, tuple, set)):
                for item in src:
                    res = Chaupal._parse_year(item)
                    if res:
                        return res
            elif isinstance(src, (int, str)):
                val_str = str(src).strip()
                m = re.search(r"\b(19\d\d|20\d\d)\b", val_str)
                if m:
                    try:
                        return int(m.group(1))
                    except Exception:
                        pass
        return None

    def parse_page_content(self, data, slug, original_path=None):
        """Parse titles from page/content response format."""
        titles = []
        response_data = data.get("response", {}) or data.get("data", {})
        panes = response_data.get("data", [])

        # 1. Try to find the show/movie name
        name = slug
        content_type = "movie" if "movie/" in self.url else "tvshow"
        content_id = None
        year = None

        for pane in panes:
            pt = pane.get("paneType")
            cc = str(pane.get("contentCode", ""))
            if pt == "content" and ("movie" in cc or "tvshow" in cc or "info" in cc or "detail" in cc):
                content = pane.get("content", {})
                name = content.get("title") or name
                content_id = content.get("id")
                year = self._parse_year(content, pane)
                if "movie" in cc:
                    content_type = "movie"
                elif "tvshow" in cc:
                    content_type = "tvshow"
                break

        if not year:
            year = self._parse_year(response_data, panes)

        if name:
            name = re.sub(r"-[a-zA-Z0-9]{5,8}$", "", name)
            name = name.replace("-", " ").replace("_", " ")
            name = ' '.join(word.capitalize() for word in name.split())

        # 2. Parse seasons and episodes
        for pane in panes:
            if pane.get("paneType") == "section":
                section = pane.get("section", {})
                sec_info = section.get("sectionInfo", {})
                sec_name = sec_info.get("name", "")

                # Check if it is a season section
                if "season" in sec_name.lower():
                    content_type = "tvshow"
                    season_num = 1
                    try:
                        m = re.search(r"\d+", sec_name)
                        if m:
                            season_num = int(m.group(0))
                    except Exception:
                        pass

                    sec_data = section.get("sectionData", {})
                    items = sec_data.get("data", [])

                    for idx, item in enumerate(items, 1):
                        display = item.get("display", {})
                        target = item.get("target", {})
                        page_attrs = target.get("pageAttributes", {})

                        ep_id = page_attrs.get("contentId") or page_attrs.get("id")
                        if not ep_id:
                            continue

                        ep_title = (
                            display.get("title")
                            or item.get("hover", {}).get("elements", [{}])[1].get("value")
                            or f"Episode {idx}"
                        )

                        # Try to find the correct path in hover elements first (important for premium/locked content)
                        ep_path = None
                        hover = item.get("hover", {})
                        elements = hover.get("elements", [])
                        for el in elements:
                            if el.get("key") == "favouriteTargetPath":
                                ep_path = el.get("value")
                                break
                        if not ep_path:
                            for el in elements:
                                if el.get("key") == "targetPath":
                                    val = el.get("value")
                                    if val and "play/" not in val:
                                        parts = val.split("/")
                                        if len(parts) >= 2:
                                            ep_path = f"{parts[0]}/play/{parts[1]}"
                                        else:
                                            ep_path = f"tvshow/play/{val}"
                                    else:
                                        ep_path = val
                                    break

                        if not ep_path or ep_path in ("buy/packages-list", "signin"):
                            ep_path = target.get("path")

                        if ep_path and not ep_path.startswith("/"):
                            ep_path = f"/{ep_path}"

                        # Parse episode number
                        ep_num = idx
                        if page_attrs.get("episodeSeqNo"):
                            try:
                                ep_num = int(page_attrs.get("episodeSeqNo"))
                            except Exception:
                                pass

                        titles.append(Title(
                            id_=ep_id,
                            type_=Title.Types.TV,
                            name=name,
                            year=year,
                            season=season_num,
                            episode=ep_num,
                            episode_name=ep_title,
                            source=self.ALIASES[0],
                            service_data={"content_id": ep_id, "path": ep_path},
                        ))

        if not titles and (content_type == "movie" or "movie/" in self.url):
            # It's a movie
            movie_path = original_path or f"/movie/{slug}"
            if not movie_path.startswith("/"):
                movie_path = f"/{movie_path}"
            titles.append(Title(
                id_=content_id or slug,
                type_=Title.Types.MOVIE,
                name=name,
                year=year,
                source=self.ALIASES[0],
                service_data={"content_id": content_id or slug, "path": movie_path},
            ))

        return titles

    def get_titles(self):
        """Fetch media metadata and return list of Title objects."""
        slug = self.title
        self.authenticate()

        # Clean slug by stripping random trailing 5-8 char hash (e.g. judaa-b3gbjw -> judaa)
        clean_slug = re.sub(r"-[a-zA-Z0-9]{5,8}$", "", slug) if slug else slug

        detail_url = f"{self.BASE_API}/service/api/v1/page/stream"
        content_url = f"{self.BASE_API}/service/api/v1/page/content"
        
        if "movie/" in self.url:
            path = f"/movie/{slug}"
        elif "tvshow/" in self.url:
            path = f"/tvshow/{slug}"
        else:
            path = f"/{slug}" if not slug.startswith("/") else slug

        path = path.lstrip("/")

        # 1. Try page/content first to get rich metadata (clean title, year, episodes)
        params = {'path': path, 'tenant_code': 'chaupal'}
        try:
            r_content = self.session.get(content_url, params=params, hooks={"response": []})
            if r_content.status_code == 200:
                content_data = r_content.json()
                if content_data.get("status"):
                    self.log.info(f"Successfully fetched content metadata via page/content API for path: {path}")
                    titles = self.parse_page_content(content_data, slug, original_path=path)
                    if titles:
                        return titles
        except Exception as e:
            self.log.debug(f"Failed page/content API: {e}")

        # 2. Try page/stream if page/content didn't return titles
        r = self.session.get(detail_url, params=params, hooks={"response": []})
        
        data = {}
        if r.status_code == 200:
            try:
                data = r.json()
            except Exception:
                data = {}

        # 3. Fallback page/content check with original path if stream failed
        if not data.get("status"):
            self.log.info(f"Page stream failed. Attempting page/content API fallback for path: {path}")
            r_content = self.session.get(content_url, params={'path': path, 'tenant_code': 'chaupal'}, hooks={"response": []})
            if r_content.status_code == 200:
                try:
                    content_data = r_content.json()
                    if content_data.get("status"):
                        self.log.info("Successfully fetched show metadata via page/content API fallback")
                        return self.parse_page_content(content_data, slug, original_path=path)
                except Exception as e:
                    self.log.debug(f"Failed page/content fallback for original path: {e}")

            # 4. Attempt HTML fallback resolution
            self.log.info(f"Path '{path}' not found or unauthorized via API. Attempting HTML fallback resolution...")
            resolved_path = self.resolve_path_from_html(self.url)
            if resolved_path and resolved_path != path:
                self.log.info(f"Successfully resolved playback path to '{resolved_path}'")
                path = resolved_path
                params = {'path': path, 'tenant_code': 'chaupal'}
                
                # Try page/stream for resolved path
                r = self.session.get(detail_url, params=params, hooks={"response": []})
                if r.status_code == 200:
                    try:
                        data = r.json()
                    except Exception:
                        data = {}
                
                # 5. Try page/content fallback for resolved path if page/stream still failed
                if not data.get("status"):
                    self.log.info(f"Page stream failed for resolved path. Attempting page/content fallback for resolved path: {path}")
                    r_content = self.session.get(content_url, params=params, hooks={"response": []})
                    if r_content.status_code == 200:
                        try:
                            content_data = r_content.json()
                            if content_data.get("status"):
                                self.log.info("Successfully fetched show metadata via page/content API fallback (resolved path)")
                                return self.parse_page_content(content_data, slug, original_path=path)
                        except Exception as e:
                            self.log.debug(f"Failed page/content fallback for resolved path: {e}")

            if not data.get("status"):
                error_msg = data.get("error", {}).get("message") or data.get("message") or "Unknown error"
                self.log.error(f"Chaupal API returned error: {error_msg}")
                return []

        # Parse titles
        titles = []
        response_data = data.get("response", {}) or data.get("data", {})
        page_info = response_data.get("page_info", {})
        metadata = page_info.get("metadata", {})
        
        content_type = metadata.get("content_type", "").lower()
        year = self._parse_year(metadata, page_info, response_data)
        
        if content_type == "movie" or "movie" in path:
            content_id = metadata.get("content_id")
            name = metadata.get("title") or clean_slug
            if name:
                name = re.sub(r"-[a-zA-Z0-9]{5,8}$", "", name)
                name = name.replace("-", " ").replace("_", " ")
                name = ' '.join(word.capitalize() for word in name.split())
            titles.append(Title(
                id_=content_id,
                type_=Title.Types.MOVIE,
                name=name,
                year=year,
                source=self.ALIASES[0],
                service_data={"content_id": content_id, "path": path},
            ))
            return titles
            
        # TV Show parsing
        seasons = response_data.get("related_catalog", [])
        show_name = metadata.get("title") or clean_slug
        if show_name:
            show_name = re.sub(r"-[a-zA-Z0-9]{5,8}$", "", show_name)
            show_name = show_name.replace("-", " ").replace("_", " ")
            show_name = ' '.join(word.capitalize() for word in show_name.split())
            titles.append(Title(
                id_=content_id,
                type_=Title.Types.MOVIE,
                name=name,
                year=year,
                source=self.ALIASES[0],
                service_data={"content_id": content_id, "path": path},
            ))
            return titles
            
        # TV Show parsing
        seasons = response_data.get("related_catalog", [])
        show_name = metadata.get("title", slug)
        if show_name:
            show_name = show_name.replace("-", " ").replace("_", " ")
            show_name = ' '.join(word.capitalize() for word in show_name.split())
        
        for block in seasons:
            if block.get("layout_type") == "portrait":
                items = block.get("catalog_list_items", [])
                for idx, item in enumerate(items, 1):
                    ep_meta = item.get("metadata", {})
                    ep_id = ep_meta.get("content_id")
                    if not ep_id:
                        continue
                    
                    ep_title = ep_meta.get("title", f"Episode {idx}")
                    ep_year = self._parse_year(ep_meta, year)
                    
                    ep_path = item.get("path") or item.get("url") or item.get("slug")
                    if not ep_path:
                        ep_path = ep_meta.get("path") or ep_meta.get("url") or ep_meta.get("slug")
                    if not ep_path:
                        ep_path = f"/tvshow/{slug}/{ep_id}"
                    
                    titles.append(Title(
                        id_=ep_id,
                        type_=Title.Types.TV,
                        name=show_name,
                        year=ep_year,
                        season=1,
                        episode=idx,
                        episode_name=ep_title,
                        source=self.ALIASES[0],
                        service_data={"content_id": ep_id, "path": ep_path},
                    ))
        
        if not titles:
            content_id = metadata.get("content_id")
            titles.append(Title(
                id_=content_id,
                type_=Title.Types.TV,
                name=show_name,
                year=year,
                season=1,
                episode=1,
                episode_name="Episode 1",
                source=self.ALIASES[0],
                service_data={"content_id": content_id, "path": path},
            ))
            
        return titles

    def authenticate(self):
        """Fetch Chaupal token using session cookies if available, or fallback to guest token."""
        if hasattr(self, 'session_id') and self.session_id:
            return

        session_id_cookie = self.session.cookies.get("_sessionId_v3")
        box_id_cookie = self.session.cookies.get("_boxId_v3")

        has_session = False

        # 1. Check for token JSON file (e.g. exported from tvapps.chaupal.com or app)
        token_dir = directories.cookies / "Chaupal"
        token_files = [token_dir / "chaupal_token.json", token_dir / "tvapps_token.json"]
        for tf in token_files:
            if tf.exists():
                try:
                    with open(tf, "r", encoding="utf-8") as f:
                        tdata = json.load(f)
                    sid = tdata.get("sessionId") or tdata.get("session_id")
                    bid = tdata.get("boxId") or tdata.get("box_id")
                    did = str(tdata.get("deviceId") or tdata.get("device_id") or "93")
                    api_base = tdata.get("apiBase") or tdata.get("api_base") or "https://chaupal-testapp.revlet.net"
                    if sid and bid:
                        self.session_id = sid.strip('"')
                        self.box_id = bid.strip('"')
                        self.device_id = did
                        self.BASE_API = api_base.rstrip("/")
                        self.log.info(f"Using Chaupal session credentials from {tf.name}")
                        if self.validate_session():
                            has_session = True
                            self.log.info("Chaupal session from token file is valid and active")
                            break
                        else:
                            self.log.warning(f"Chaupal session from {tf.name} is expired or invalid!")
                except Exception as e:
                    self.log.warning(f"Failed to load Chaupal token from {tf}: {e}")

        # 2. Check session cookies
        if not has_session and session_id_cookie and box_id_cookie:
            self.session_id = urllib.parse.unquote(session_id_cookie).strip('"')
            self.box_id = urllib.parse.unquote(box_id_cookie).strip('"')
            self.device_id = "5"
            self.log.info("Using Chaupal session credentials from cookies")

            if self.validate_session():
                has_session = True
                self.log.info("Chaupal session cookie is valid and active")
            else:
                self.log.warning("Chaupal session from cookies is expired or invalid!")

        if not has_session:
            if session_id_cookie or box_id_cookie:
                self.log.warning("Falling back to guest session token...")
            else:
                self.log.info("No Chaupal session cookies found. Using guest session token...")

            self.box_id = str(uuid.uuid4())
            self.device_id = "5"
            self.session_id = None

            self.session.headers.update({
                "User-Agent": self.USER_AGENT,
                "Origin": "https://www.chaupal.com",
                "Referer": "https://www.chaupal.com/",
                "Accept": "application/json, text/plain, */*"
            })

            timezone = "Asia/Kolkata"
            token_url = (
                f"{self.BASE_API}/service/api/v1/get/token"
                f"?tenant_code=chaupal&box_id={self.box_id}&product=chaupal"
                f"&device_id={self.device_id}&display_lang_code=ENG&device_sub_type=browser&timezone={timezone}"
            )

            self.log.debug("Fetching Chaupal session token...")
            r = self.session.get(token_url, hooks={"response": []})
            if r.status_code != 200:
                self.log.error(f"Failed to fetch token, status: {r.status_code}")
                return

            try:
                data = r.json()
            except ValueError:
                self.log.error(f"Invalid JSON in token response: {r.text[:100]}")
                return

            if not data.get("status"):
                self.log.error(f"Failed to get a valid token response from Chaupal: {data}")
                return

            token = data.get("response", {}).get("sessionId")
            if not token:
                self.log.error(f"Failed to get sessionId from Chaupal: {data}")
                return

            self.session_id = token
            self.log.debug(f"Got Session ID: {self.session_id}")

        origin = "https://tvapps.chaupal.com" if "testapp" in self.BASE_API else "https://www.chaupal.com"
        referer = "https://tvapps.chaupal.com/" if "testapp" in self.BASE_API else "https://www.chaupal.com/"
        self.session.headers.update({
            "Authorization": f"Bearer {self.session_id}",
            "x-auth-token": self.session_id,
            "Session-Id": self.session_id,
            "Box-Id": self.box_id,
            "Tenant-Code": "chaupal",
            "Origin": origin,
            "Referer": referer,
            "User-Agent": self.USER_AGENT,
        })

    def validate_session(self):
        """Validate if the current session cookie is authenticated by checking user info endpoint."""
        origin = "https://tvapps.chaupal.com" if "testapp" in self.BASE_API else "https://www.chaupal.com"
        referer = "https://tvapps.chaupal.com/" if "testapp" in self.BASE_API else "https://www.chaupal.com/"
        headers = {
            "Session-Id": self.session_id,
            "Box-Id": self.box_id,
            "Tenant-Code": "chaupal",
            "Authorization": f"Bearer {self.session_id}",
            "x-auth-token": self.session_id,
            "Origin": origin,
            "Referer": referer,
            "User-Agent": self.USER_AGENT,
        }
        user_info_url = f"{self.BASE_API}/service/api/auth/user/info?tenant_code=chaupal&boxId={self.box_id}"
        try:
            r = self.session.get(user_info_url, headers=headers, hooks={"response": []})
            if r.status_code == 200:
                data = r.json()
                if data.get("status") is True:
                    user_data = data.get("response", {})
                    name = user_data.get("name") or user_data.get("phoneNumber") or "User"
                    pack = user_data.get("attributes", {}).get("activePackName") or user_data.get("cardStatus", "Subscribed")
                    self.log.info(f"Chaupal User: {name} (Plan: {pack})")
                    return True
        except Exception as e:
            self.log.debug(f"Failed to validate session: {e}")
        return False

    def get_language_override(self, url):
        if not url:
            return None
        url_upper = url.upper()
        if "HARYANVI" in url_upper:
            return "bgc"
        elif "PUNJABI" in url_upper:
            return "pan"
        elif "BHOJPURI" in url_upper:
            return "bho"
        elif "RAJASTHANI" in url_upper:
            return "raj"
        elif "BENGALI" in url_upper:
            return "ben"
        elif "GUJARATI" in url_upper:
            return "guj"
        return None

    def get_tracks(self, title):
        """Fetch media details and return Tracks."""
        self.authenticate()
        tracks = Tracks()
        
        path = None
        if title and hasattr(title, 'service_data') and isinstance(title.service_data, dict):
            path = title.service_data.get("path")
            
        if not path:
            parsed_url = urlparse(self.url)
            path = parsed_url.path
            
        if not path or path == "/":
            slug = self.title
            if "movie/" in self.url:
                path = f"/movie/{slug}"
            elif "tvshow/" in self.url:
                path = f"/tvshow/{slug}"
            else:
                path = f"/{slug}" if not slug.startswith("/") else slug
                
        path = path.lstrip("/")
        self.log.info(f"Fetching Chaupal content for path: {path}")

        # Try formatted play_path first
        play_path = path
        if "/play/" not in play_path:
            if play_path.startswith("movie/"):
                play_path = play_path.replace("movie/", "movie/play/")
            elif play_path.startswith("tvshow/"):
                play_path = play_path.replace("tvshow/", "tvshow/play/")

        stream_url = f"{self.BASE_API}/service/api/v1/page/stream?path={play_path}&tenant_code=chaupal&device_supported_features=HD,SD"
        r_st = self.session.get(stream_url, hooks={"response": []})

        # Fallback to original path if play_path failed
        try:
            if r_st.status_code != 200 or not r_st.json().get("status"):
                stream_url_fallback = f"{self.BASE_API}/service/api/v1/page/stream?path={path}&tenant_code=chaupal"
                r_fallback = self.session.get(stream_url_fallback, hooks={"response": []})
                if r_fallback.status_code == 200 and r_fallback.json().get("status"):
                    r_st = r_fallback
        except Exception:
            pass

        try:
            st_data = r_st.json()
        except Exception:
            self.log.error(f"Invalid JSON from stream API: {r_st.text[:100]}")
            return tracks

        if not st_data.get("status"):
            error_details = st_data.get('error', {})
            err_msg = error_details.get('message', 'Unknown Error')
            self.log.error(f"Stream API Error: {err_msg}")
            if any(k in err_msg.lower() for k in ["login", "limit", "subscribe", "subscription", "screen", "unauthorized"]):
                self.log.error("Please provide active subscription cookies in vinetrimmer/cookies/Chaupal/default.txt")
            return tracks

        response = st_data.get("response", {})
        streams = response.get("streams", [])

        if not streams:
            self.log.error("No streams found in response.")
            return tracks

        # Parse Widevine and HLS streams
        for stream in streams:
            stream_type = stream.get("streamType", "").lower()
            url = stream.get("url")
            if not url:
                continue

            if stream_type == "widevine":
                keys = stream.get("keys", {})
                license_url = keys.get("licenseKey")
                self.log.info("Found Widevine MPD")
                self.log.debug(f"Found Widevine MPD URL: {url}")
                self.license_url = license_url
                manifest_tracks = Tracks.from_mpd(
                    url=url,
                    session=self.session,
                    source=self.ALIASES[0]
                )
                lang_override = self.get_language_override(url)
                for track in manifest_tracks:
                    if track.__class__.__name__ == "AudioTrack":
                        if lang_override:
                            track.language = Language.get(lang_override)
                    elif track.__class__.__name__ == "TextTrack":
                        if not track.language or str(track.language).lower() in ("und", "none", "mis", "mul", ""):
                            track.language = Language.get("eng")
                tracks.add(manifest_tracks)
            elif stream_type == "hls" or url.endswith(".m3u8"):
                self.log.info("Found HLS Stream")
                self.log.debug(f"Found HLS Stream URL: {url}")
                r = self.session.get(url, hooks={"response": []})
                master = m3u8.loads(r.text, uri=url)
                manifest_tracks = Tracks.from_m3u8(
                    master=master,
                    source=self.ALIASES[0]
                )
                lang_override = self.get_language_override(url)
                for track in manifest_tracks:
                    if track.__class__.__name__ == "AudioTrack":
                        if lang_override:
                            track.language = Language.get(lang_override)
                    elif track.__class__.__name__ == "TextTrack":
                        if not track.language or str(track.language).lower() in ("und", "none", "mis", "mul", ""):
                            track.language = Language.get("eng")
                tracks.add(manifest_tracks)

        for track in tracks.subtitles:
            if not track.language or str(track.language).lower() in ("und", "none", "mis", "mul", ""):
                track.language = Language.get("eng")

        return tracks

    def license(self, challenge, title, track, session_id, **_):
        """Get Widevine DRM license from Chaupal license server."""
        license_url = getattr(self, "license_url", None)
        if not license_url:
            raise ValueError("No Widevine license URL available for Chaupal")

        headers = {
            "User-Agent": self.USER_AGENT,
            "Origin": "https://www.chaupal.com",
            "Referer": "https://www.chaupal.com/",
            "Session-Id": self.session_id,
            "Box-Id": self.box_id,
            "Tenant-Code": "chaupal",
            "Authorization": f"Bearer {self.session_id}",
            "x-auth-token": self.session_id,
        }

        r = self.session.post(license_url, data=challenge, headers=headers)
        r.raise_for_status()
        return r.content

