from __future__ import annotations

import re
import urllib.parse
import uuid

import click
import m3u8
from langcodes import Language

from vinetrimmer.objects import AudioTrack, Title, Track, Tracks
from vinetrimmer.services.BaseService import BaseService


class Watcho(BaseService):
    """
    Service code for Watcho (https://www.watcho.com/)

    Watcho is a DishTV-owned OTT aggregator that bundles multiple Indian
    streaming channels (Hoichoi, Chaupal, Shemaroo Me, Sony Liv, Aha, Sun NXT,
    Addatimes, Planet Marathi OTT, ManoramaMAX, Saina Play, NeeStream, Stage,
    Tarang Plus, Aao NXT, Namma Flix, CityShor.TV, Pride East Entertainments,
    ReelDrama, Olly Plus, Tentkotta, Simply South, MovieSaints, Koode, OMTV, etc.)
    under a single subscription.

    Authorization : Cookies  (export from a logged-in browser session)
    Backend API   : https://dishtv-api.revlet.net
    Security      : HLS (AES-128 / clear) or DASH (Widevine)

    How to get cookies:
      1. Log in to https://www.watcho.com in Chrome/Firefox
      2. Export cookies with a browser extension (e.g. "Get cookies.txt LOCALLY")
      3. Save the file to:  vinetrimmer/cookies/watcho/default.txt
    """

    ALIASES = ["WATCHO", "WCO", "watcho", "Watcho", "VZY", "vzy", "Vzy"]

    TITLE_RE = [
        r"https?://(?:www\.)?watcho\.com/(?:[a-zA-Z0-9_-]+/)*(?P<id>[a-zA-Z0-9_-]+)",
        r"https?://(?:www\.)?vzy\.one/(?:[a-zA-Z0-9_-]+/)*(?P<id>\d+)",
        r"^watcho:(?P<id>[a-zA-Z0-9_-]+)$",
        r"^vzy:(?P<id>\d+)$",
    ]

    # Revlet backend base URL
    API_BASE = "https://dishtv-api.revlet.net/service/api/v1"

    # ------------------------------------------------------------------ #
    #  CLI entry point
    # ------------------------------------------------------------------ #

    @staticmethod
    @click.command(name="Watcho", short_help="https://www.watcho.com/ & https://www.vzy.one/", help=__doc__)
    @click.argument("title", type=str)
    @click.pass_context
    def cli(ctx, **kwargs):
        return Watcho(ctx, **kwargs)

    # ------------------------------------------------------------------ #
    #  Constructor
    # ------------------------------------------------------------------ #

    def __init__(self, ctx, title):
        self.url = title
        super().__init__(ctx)
        self.parse_title(ctx, self.url)

        self.is_vzy = "vzy.one" in self.url.lower() or "vzy:" in self.url.lower() or (self.title and self.title.isdigit())

        # These are set properly in _init_session()
        self.box_id = str(uuid.uuid4())   # overwritten from cookie if present
        self.session_id = None

        origin = "https://www.vzy.one" if self.is_vzy else "https://www.watcho.com"
        referer = "https://www.vzy.one/" if self.is_vzy else "https://www.watcho.com/"

        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            "Origin":  origin,
            "Referer": referer,
        })

    # ------------------------------------------------------------------ #
    #  Helpers
    # ------------------------------------------------------------------ #

    def _get_cookie(self, name: str) -> str | None:
        """Return a decoded cookie value, trying both with and without domain."""
        raw = (
            self.session.cookies.get(name, domain="www.watcho.com") or
            self.session.cookies.get(name)
        )
        if raw:
            return urllib.parse.unquote(raw).strip('"')
        return None

    def _api_get(self, endpoint: str, **params) -> dict:
        """Make a GET request to the Revlet API and return the JSON response."""
        url = f"{self.API_BASE}/{endpoint.lstrip('/')}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        r = self.session.get(url)
        r.raise_for_status()
        resp = r.json()

        # Detect expired session and force re-login
        if not resp.get("status") and resp.get("error", {}).get("message") == "RES_ERROR_INVALID_SESSION":
            self.log.warning("Session has expired or is invalid. Re-authenticating...")
            self._init_session(force=True)
            # Retry request with new headers
            r = self.session.get(url)
            r.raise_for_status()
            resp = r.json()

        return resp

    def _parse_subtitle_row(self, metadata: dict) -> tuple[str | None, int | None]:
        """
        Extract (language_code, year) from the subtitle data row.
        Subtitle text format:  "2025 | Hindi | Drama | UA16+"
        """
        lang = None
        year = None
        for row in metadata.get("dataRows") or []:
            for elem in row.get("elements") or []:
                if elem.get("elementSubtype") == "subtitle":
                    text = elem.get("data") or ""
                    # Extract year
                    m = re.search(r"\b(19\d\d|20\d\d)\b", text)
                    if m:
                        year = int(m.group(1))
                    # Extract language (second pipe-separated segment)
                    parts = [p.strip() for p in text.split("|")]
                    if len(parts) > 1 and parts[1]:
                        if parts[1].lower() == "haryanvi":
                            lang = "bgc"
                        else:
                            try:
                                lang = str(Language.find(parts[1]))
                            except Exception:
                                pass
                    return lang, year
        return lang, year

    def _find_play_target(self, metadata: dict) -> str | None:
        """Find the play target path from the watchnow element."""
        for row in metadata.get("dataRows") or []:
            for elem in row.get("elements") or []:
                if elem.get("elementSubtype") == "watchnow":
                    return elem.get("target")
        return None

    # ------------------------------------------------------------------ #
    #  Session initialisation
    # ------------------------------------------------------------------ #

    def _init_session(self, force=False):
        import os
        import json

        token_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "cookies",
            "watcho",
            "token.txt"
        )

        if force:
            self.session_id = None
            self.box_id = None
            if os.path.exists(token_path):
                try:
                    os.remove(token_path)
                    self.log.info("Deleted expired token.txt")
                except Exception as e:
                    self.log.warning(f"Failed to delete expired token.txt: {e}")
            # Clear expired cookies from session
            for cookie in list(self.session.cookies):
                if any(x in cookie.name.lower() for x in ("sessionid", "boxid")):
                    try:
                        self.session.cookies.clear(cookie.domain, cookie.path, cookie.name)
                    except Exception:
                        pass

        if self.session_id:
            return

        if os.path.isfile(token_path) and os.path.getsize(token_path) > 0:
            try:
                with open(token_path, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                if content.startswith("{"):
                    data = json.loads(content)
                    self.session_id = data.get("sessionId", "").strip('"')
                    self.box_id = data.get("boxId", "").strip('"') or self.box_id
                else:
                    lines = [line.strip().strip('"') for line in content.splitlines() if line.strip()]
                    if len(lines) >= 1:
                        self.session_id = lines[0]
                    if len(lines) >= 2:
                        self.box_id = lines[1]
                self.log.debug("Loaded sessionId and boxId from token.txt")
            except Exception as e:
                self.log.warning(f"Failed to read token.txt: {e}")

        if not self.session_id:
            cookie_session = self._get_cookie("_sessionId_v3") or self._get_cookie("sessionId")
            cookie_box = self._get_cookie("_boxId_v3") or self._get_cookie("boxId")
            if cookie_session and cookie_box:
                self.session_id = cookie_session
                self.box_id = cookie_box
                self.log.info("Loaded sessionId and boxId from cookies (default.txt)")

        if not self.session_id:
            self.log.info("=" * 80)
            self.log.info("Watcho Authentication Token Required")
            self.log.info("Attempting browser automation to fetch tokens automatically...")
            self.log.info("=" * 80)

            # Try to automate token extraction using Selenium
            automated = False
            try:
                from selenium import webdriver
                from selenium.webdriver.chrome.options import Options
                import time

                self.log.info("Launching Chrome browser...")
                options = Options()
                options.add_argument("--disable-gpu")
                options.add_argument("--start-maximized")
                
                # Exclude logging noise
                options.add_experimental_option("excludeSwitches", ["enable-logging"])

                driver = webdriver.Chrome(options=options)
                try:
                    self.log.info("Opening watcho.com...")
                    driver.get("https://www.watcho.com/")
                    
                    self.log.info("Please log in to your Watcho account in the opened Chrome window.")
                    self.log.info("Waiting for login detection (timeout: 5 minutes)...")

                    timeout = 300
                    start_time = time.time()
                    while time.time() - start_time < timeout:
                        try:
                            # Read localStorage keys
                            js_script = """
                            return {
                                sessionId: localStorage.getItem('sessionId-v3') || localStorage.getItem('sessionId'),
                                boxId: localStorage.getItem('boxId-v3') || localStorage.getItem('boxId'),
                                isLoggedIn: localStorage.getItem('isloggedin-v3') || localStorage.getItem('isloggedin')
                            };
                            """
                            res = driver.execute_script(js_script)
                            if res and res.get("sessionId") and res.get("boxId") and str(res.get("isLoggedIn")).lower() == "true":
                                self.session_id = res["sessionId"].strip('"')
                                self.box_id = res["boxId"].strip('"')
                                self.log.info("Authentication tokens successfully captured!")
                                automated = True
                                break
                        except Exception:
                            pass
                        time.sleep(1)
                finally:
                    driver.quit()
            except Exception as e:
                self.log.warning(f"Browser automation failed: {e}")

            # Fallback to manual entry if automation failed or timed out
            if not automated:
                self.log.info("=" * 80)
                self.log.info("Browser automation failed or timed out. Falling back to manual entry:")
                self.log.info("  1. Open https://www.watcho.com in your browser and log in.")
                self.log.info("  2. Press F12 (or right-click -> Inspect) to open Developer Tools.")
                self.log.info("  3. Go to the 'Application' tab (Chrome) or 'Storage' tab (Firefox).")
                self.log.info("  4. Expand 'Local Storage' and click 'https://www.watcho.com'.")
                self.log.info("  5. Find and copy the value of 'sessionId' and 'boxId'.")
                self.log.info("=" * 80)

                # Read sessionId
                self.log.info("Enter your sessionId:")
                self.session_id = str(input("> ")).strip().strip('"')

                # Read boxId (try to default from cookie first)
                cookie_box = self._get_cookie("_boxId_v3")
                if cookie_box:
                    self.box_id = cookie_box.strip('"')
                    self.log.info(f"Using boxId from cookies: {self.box_id}")
                else:
                    self.log.info("Enter your boxId:")
                    self.box_id = str(input("> ")).strip().strip('"')

            # Save to token.txt
            if self.session_id and self.box_id:
                try:
                    os.makedirs(os.path.dirname(token_path), exist_ok=True)
                    with open(token_path, "w", encoding="utf-8") as f:
                        f.write(f"{self.session_id}\n{self.box_id}")
                    self.log.info(f"Saved token to {token_path}")
                except Exception as e:
                    self.log.warning(f"Failed to save token.txt: {e}")


        # Apply session identifiers to all subsequent API requests
        self.session.headers.update({
            "session-id": self.session_id,
            "box-id": self.box_id,
            "tenant-code": "dishtv",
        })

    # ------------------------------------------------------------------ #
    #  VZY Implementation (Kaltura OTT + VZY SSO)
    # ------------------------------------------------------------------ #

    VZY_KALTURA_API = "https://5093.dsp1.ott.kaltura.com/api_v3"
    VZY_SSO_API = "https://vzy-sso.dish.watcho.com/v1"

    def _init_vzy_session(self, force=False):
        import os
        import json

        token_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "cookies",
            "watcho",
            "vzy_token.json"
        )

        self.vzy_hz_token = None
        self.vzy_user_id = "9328296446"
        self.vzy_device_id = "web_8d83d61d150410aff8f582459fd2607e_69782067"

        if os.path.isfile(token_path):
            try:
                with open(token_path, "r", encoding="utf-8") as f:
                    vzy_data = json.load(f)
                self.vzy_hz_token = vzy_data.get("hzToken")
                self.vzy_user_id = vzy_data.get("phoneNumber") or self.vzy_user_id
                self.vzy_device_id = vzy_data.get("user_udid") or self.vzy_device_id
                self.log.debug("Loaded VZY credentials from vzy_token.json")
            except Exception as e:
                self.log.warning(f"Failed to read vzy_token.json: {e}")

        if not self.vzy_hz_token:
            self.log.info("=" * 80)
            self.log.info("VZY Authentication Token Required")
            self.log.info("Please enter your hzToken from https://www.vzy.one (Application -> Local Storage -> hzToken):")
            self.vzy_hz_token = str(input("> ")).strip().strip('"')
            if self.vzy_hz_token:
                try:
                    os.makedirs(os.path.dirname(token_path), exist_ok=True)
                    with open(token_path, "w", encoding="utf-8") as f:
                        json.dump({
                            "hzToken": self.vzy_hz_token,
                            "phoneNumber": self.vzy_user_id,
                            "user_udid": self.vzy_device_id
                        }, f, indent=2)
                except Exception as e:
                    self.log.warning(f"Failed to save vzy_token.json: {e}")

        # Obtain / refresh Kaltura Session (KS)
        if not getattr(self, "vzy_ks", None) or force:
            anon_payload = {
                "ClientTag": "KSPWeb-2026.09.02.16.34",
                "apiVersion": "11.0.2.1",
                "partnerId": 5093
            }
            r = self.session.post(
                f"{self.VZY_KALTURA_API}/service/ottuser/action/anonymouslogin",
                json=anon_payload,
                headers={"Origin": "https://www.vzy.one", "Referer": "https://www.vzy.one/"},
                timeout=15
            )
            r.raise_for_status()
            res = r.json()
            self.vzy_ks = res.get("result", {}).get("ks")
            self.log.debug("Acquired Kaltura OTT KS session for VZY")

    def _vzy_kaltura_call(self, endpoint: str, payload: dict) -> dict:
        self._init_vzy_session()
        payload.setdefault("apiVersion", "11.0.2.1")
        payload.setdefault("ks", self.vzy_ks)
        payload.setdefault("ClientTag", "KSPWeb-2026.09.02.16.34")

        url = f"{self.VZY_KALTURA_API}/{endpoint.lstrip('/')}"
        r = self.session.post(
            url,
            json=payload,
            headers={"Origin": "https://www.vzy.one", "Referer": "https://www.vzy.one/"},
            timeout=15
        )
        r.raise_for_status()
        data = r.json()
        err = data.get("result", {}).get("error")
        if err:
            if err.get("code") in ("500016", "500015"):
                self.log.warning("Kaltura KS session expired, renewing...")
                self._init_vzy_session(force=True)
                payload["ks"] = self.vzy_ks
                r = self.session.post(url, json=payload, headers={"Origin": "https://www.vzy.one", "Referer": "https://www.vzy.one/"}, timeout=15)
                r.raise_for_status()
                data = r.json()
            else:
                raise ValueError(f"Kaltura API error: {err.get('message')}")
        return data

    def _vzy_map_partner_to_source(self, partner_name: str) -> str:
        partner_clean = (partner_name or "").lower().replace(" ", "").replace("_", "")
        mapping = {
            "lionsgate": "LGP",
            "lionsgateplay": "LGP",
            "chaupal": "CHTV",
            "laminar": "CHTV",
            "sonyliv": "LIV",
            "zee5": "Z5",
            "shemaroome": "SME",
            "shemaroo": "SME",
            "hoichoi": "HC",
            "hungama": "HM",
            "klikk": "KLK",
            "ohogujarati": "OHO",
            "fancode": "FC",
            "ultrajhakaas": "UJHS",
            "ultrajhakas": "UJHS",
            "watchoexclusive": "WCO",
            "stage": "STAGE",
            "sunnxt": "SUNNXT",
            "aha": "AHA",
            "manoramamax": "MMAX",
            "tarangplus": "TANG",
            "aaonxt": "AAONXT",
            "pocketfilms": "PKTF",
        }
        for k, v in mapping.items():
            if k in partner_clean:
                return v
        return "VZY"

    def _vzy_get_titles(self):
        asset_id = int(self.title)
        resp = self._vzy_kaltura_call("service/asset/action/get", {
            "id": asset_id,
            "assetReferenceType": "media"
        })
        result = resp.get("result", {})
        if not result:
            raise ValueError(f"No asset returned by VZY Kaltura API for ID: {asset_id}")

        name = result.get("name") or "VZY Video"
        metas = result.get("metas") or {}
        year = metas.get("Year", {}).get("value")
        try:
            year = int(year) if year else None
        except Exception:
            year = None

        lang_str = metas.get("DialectLanguageDisplay", {}).get("value") or metas.get("GUILanguage", {}).get("value")
        lang = None
        if lang_str:
            if lang_str.lower() == "haryanvi":
                lang = "bgc"
            else:
                try:
                    lang = str(Language.find(lang_str))
                except Exception:
                    pass

        content_partner = metas.get("ContentPartnerName", {}).get("value") or ""
        source_tag = self._vzy_map_partner_to_source(content_partner)

        asset_type = result.get("type")
        type_desc = (result.get("typeDescription") or "").strip().lower()

        # Check if asset is a trailer/promo, and attempt to resolve the actual Movie or Series asset
        media_files = result.get("mediaFiles") or []
        max_duration = max([mf.get("duration") or 0 for mf in media_files], default=0)

        main_asset = None
        if (asset_type == 679 or type_desc == "trailer"):
            if max_duration >= 900:
                self.log.info(
                    f"Asset {asset_id} is tagged as trailer but has full feature duration ({max_duration}s). "
                    f"Treating as full feature."
                )
                result["typeDescription"] = "Movie"
                type_desc = "movie"
            else:
                self.log.info(f"Asset {asset_id} is a trailer ({max_duration}s). Resolving main feature asset...")

                # Method 1: Check mediaFiles for externalId of the main asset
                for mf in media_files:
                    mf_ext_id = mf.get("externalId")
                    if mf_ext_id and mf_ext_id != result.get("externalId"):
                        try:
                            mf_resp = self._vzy_kaltura_call("service/asset/action/list", {
                                "filter": {
                                    "objectType": "KalturaSearchAssetFilter",
                                    "kSql": f"(and ExternalId = '{mf_ext_id}')"
                                }
                            })
                            mf_objs = mf_resp.get("result", {}).get("objects") or []
                            for obj in mf_objs:
                                obj_partner = (obj.get("metas", {}).get("ContentPartnerName", {}).get("value") or "").lower()
                                obj_drm = obj.get("metas", {}).get("DRMType", {}).get("value")
                                if (not content_partner or obj_partner == content_partner.lower()) and obj_drm != "DeepLink":
                                    main_asset = obj
                                    break
                            if main_asset:
                                break
                        except Exception as e:
                            self.log.debug(f"Failed to lookup asset by ExternalId '{mf_ext_id}': {e}")

                # Method 2: Search Kaltura by Name and match ContentPartnerName
                if not main_asset and name:
                    try:
                        name_clean = name.replace("'", "\\'")
                        srch_resp = self._vzy_kaltura_call("service/asset/action/list", {
                            "filter": {
                                "objectType": "KalturaSearchAssetFilter",
                                "kSql": f"(and Name = '{name_clean}')",
                                "typeIn": "685,681,688"
                            },
                            "pager": {
                                "objectType": "KalturaFilterPager",
                                "pageSize": 30,
                                "pageIndex": 1
                            }
                        })
                        srch_objs = srch_resp.get("result", {}).get("objects") or []
                        matched = [
                            o for o in srch_objs 
                            if (o.get("metas", {}).get("ContentPartnerName", {}).get("value") or "").lower() == content_partner.lower()
                            and o.get("metas", {}).get("DRMType", {}).get("value") != "DeepLink"
                        ]
                        if matched:
                            main_asset = matched[0]
                    except Exception as e:
                        self.log.debug(f"Failed to search asset by Name: {e}")

            if main_asset:
                self.log.info(
                    f"Resolved trailer {asset_id} to {main_asset.get('typeDescription')} "
                    f"asset {main_asset.get('id')} ('{main_asset.get('name')}')"
                )
                asset_id = int(main_asset.get("id"))
                result = main_asset
                name = result.get("name") or name
                metas = result.get("metas") or metas
                year = metas.get("Year", {}).get("value")
                try:
                    year = int(year) if year else None
                except Exception:
                    year = None
                lang_str = metas.get("DialectLanguageDisplay", {}).get("value") or metas.get("GUILanguage", {}).get("value")
                if lang_str:
                    if lang_str.lower() == "haryanvi":
                        lang = "bgc"
                    else:
                        try:
                            lang = str(Language.find(lang_str))
                        except Exception:
                            pass
                content_partner = metas.get("ContentPartnerName", {}).get("value") or content_partner
                source_tag = self._vzy_map_partner_to_source(content_partner)
                asset_type = result.get("type")
                type_desc = (result.get("typeDescription") or "").strip().lower()
            else:
                self.log.warning(f"Could not resolve parent asset for trailer {asset_id}. Proceeding with trailer.")

        # TV Series
        if asset_type == 681 or type_desc == "series":
            series_id = metas.get("SeriesID", {}).get("value") or str(asset_id)
            self.log.info(f"Fetching episodes for series '{name}' (SeriesID: {series_id})...")
            ep_resp = self._vzy_kaltura_call("service/asset/action/list", {
                "filter": {
                    "objectType": "KalturaSearchAssetFilter",
                    "kSql": f"(and SeriesId = '{series_id}')",
                    "typeIn": "688"
                },
                "pager": {
                    "objectType": "KalturaFilterPager",
                    "pageSize": 200,
                    "pageIndex": 1
                }
            })
            ep_objects = ep_resp.get("result", {}).get("objects") or []
            if not ep_objects:
                ep_resp = self._vzy_kaltura_call("service/asset/action/list", {
                    "filter": {
                        "objectType": "KalturaSearchAssetFilter",
                        "kSql": f"(and CreatorChannelID = '{asset_id}')",
                        "typeIn": "688"
                    },
                    "pager": {
                        "objectType": "KalturaFilterPager",
                        "pageSize": 200,
                        "pageIndex": 1
                    }
                })
                ep_objects = ep_resp.get("result", {}).get("objects") or []

            titles = []
            for ep in ep_objects:
                ep_metas = ep.get("metas") or {}
                season_val = ep_metas.get("SeasonNumber", {}).get("value") or 1
                episode_val = ep_metas.get("EpisodeNumber", {}).get("value") or 1
                try:
                    season = int(season_val)
                except Exception:
                    season = 1
                try:
                    episode = int(episode_val)
                except Exception:
                    episode = 1
                ep_name = ep.get("name") or f"Episode {episode}"
                titles.append(Title(
                    id_=str(ep.get("id")),
                    type_=Title.Types.TV,
                    name=name,
                    season=season,
                    episode=episode,
                    episode_name=ep_name,
                    year=year,
                    original_lang=lang,
                    source=source_tag,
                    service_data={"vzy": True, "asset": ep}
                ))
            if titles:
                self.log.info(f"Found {len(titles)} episode(s) for series '{name}'")
                return titles

        # Movie or Single Episode
        title_type = Title.Types.TV if (asset_type == 688 or type_desc == "episode") else Title.Types.MOVIE
        return Title(
            id_=str(asset_id),
            type_=title_type,
            name=name,
            year=year,
            original_lang=lang,
            source=source_tag,
            service_data={"vzy": True, "asset": result}
        )

    def _vzy_get_tracks(self, title):
        self._init_vzy_session()
        asset = (title.service_data or {}).get("asset")
        if not asset:
            resp = self._vzy_kaltura_call("service/asset/action/get", {
                "id": int(title.id),
                "assetReferenceType": "media"
            })
            asset = resp.get("result", {})

        metas = asset.get("metas") or {}
        content_partner = metas.get("ContentPartnerName", {}).get("value") or ""
        ref_id = metas.get("ExternalProviderId", {}).get("value") or ""
        drm_type = metas.get("DRMType", {}).get("value") or ""
        ext_id = asset.get("externalId") or ""
        type_desc = (asset.get("typeDescription") or "").strip().lower()

        # Check if asset is a DeepLink title (e.g. Amazon Prime, SunNXT external links)
        is_deeplink = metas.get("is_Deeplink", {}).get("value") or drm_type == "DeepLink"
        deeplink = metas.get("Deeplink", {}).get("value")
        if is_deeplink:
            partner = content_partner or "an external provider"
            err = f"'{title.name}' is a DeepLink title hosted on {partner.upper()} and cannot be streamed directly via VZY/Watcho."
            if deeplink:
                err += f"\nDeep link URL: {deeplink}"
            raise ValueError(err)

        # Check if asset has a direct non-placeholder mediaFile (e.g. for Free/Trailer streams)
        direct_url = None
        for mf in asset.get("mediaFiles") or []:
            mf_url = mf.get("url") or ""
            if mf_url and "hbomax.com" not in mf_url and (".m3u8" in mf_url or ".mpd" in mf_url):
                direct_url = mf_url
                break

        video_url = None
        license_key = None

        if (drm_type in ("Free", "FreeDynamicUrl") or type_desc == "trailer") and direct_url:
            self.log.info("Using direct media file stream for free/trailer content")
            video_url = direct_url
        else:
            if content_partner and ext_id.startswith(content_partner):
                pid = ext_id[len(content_partner):].lstrip("_")
            else:
                pid = ext_id

            stream_params = {
                "userId": self.vzy_user_id,
                "pid": pid,
                "contentPartner": content_partner,
                "scheme": "widevine",
                "deviceType": "web",
                "deviceId": self.vzy_device_id,
                "isDrm": "true" if drm_type not in ("Free", "FreeDynamicUrl") else "false",
                "entitlementId": "",
                "isLinear": "false",
                "refrenceId": ref_id,
            }

            stream_headers = {
                "Content-Type": "application/json",
                "Authorization": self.vzy_hz_token,
                "Origin": "https://www.vzy.one",
                "Referer": "https://www.vzy.one/",
                "User-Agent": self.session.headers.get("User-Agent")
            }

            stream_url_req = f"{self.VZY_SSO_API}/stream?" + urllib.parse.urlencode(stream_params)
            try:
                r = self.session.get(stream_url_req, headers=stream_headers, timeout=15)
                r.raise_for_status()
                stream_resp = r.json()
                streams = stream_resp.get("streams") or {}
                stream_data = streams.get("streamData") or {}
                video_url = stream_data.get("stream_url")
                keys = streams.get("keys") or {}
                license_key = keys.get("licenseKey")
                params_data = streams.get("params") or {}
                self._drm_token = params_data.get("drmToken") or params_data.get("drmtoken")
                self._token_key = params_data.get("tokenKey") or params_data.get("tokenkey") or "nv-authorizations"
            except Exception as e:
                if direct_url:
                    self.log.warning(f"Failed to fetch stream from SSO API ({e}), falling back to direct media file URL")
                    video_url = direct_url
                else:
                    raise

        if not video_url:
            if direct_url:
                self.log.info("Using direct mediaFile URL from asset metadata")
                video_url = direct_url
            else:
                raise ValueError(f"No stream URL found for VZY asset: {title.id}")

        self.log.debug(f"VZY Stream URL: {video_url}")

        # Attempt to upgrade SD manifest to full HD/1080p master manifest
        if "_sd.mpd" in video_url.lower():
            hd_url = re.sub(r"_sd\.mpd", ".mpd", video_url, flags=re.I)
            try:
                test_r = self.session.head(hd_url, headers=stream_headers, timeout=5)
                if test_r.status_code == 200:
                    video_url = hd_url
            except Exception:
                pass
        elif "_sd.m3u8" in video_url.lower():
            hd_url = re.sub(r"_sd\.m3u8", ".m3u8", video_url, flags=re.I)
            try:
                test_r = self.session.head(hd_url, headers=stream_headers, timeout=5)
                if test_r.status_code == 200:
                    video_url = hd_url
            except Exception:
                pass

        if ".mpd" in video_url.lower():
            self.log.debug("Parsing DASH manifest")
            tracks = Tracks.from_mpd(
                url=video_url,
                session=self.session,
                source=title.source,
            )
        else:
            self.log.info("Parsing HLS manifest")
            r = self.session.get(video_url)
            r.raise_for_status()
            master = m3u8.loads(r.text, uri=video_url)
            tracks = Tracks.from_m3u8(master, source=title.source)

            if not tracks.audios:
                lang_code = title.original_lang or "hin"
                muxed_audio = AudioTrack(
                    id_=uuid.uuid4().hex[:6],
                    source=title.source,
                    url=video_url,
                    codec="aac",
                    language=Language.get(lang_code),
                    bitrate=0,
                    channels="2.0",
                    descriptor=Track.Descriptor.URL,
                    encrypted=False,
                )
                muxed_audio.is_muxed_with_video = True
                tracks.audios.append(muxed_audio)

        # Detect true audio language from URL or metadata
        lang_override = None
        url_upper = video_url.upper()
        if "PUNJABI" in url_upper:
            lang_override = "pan"
        elif "HARYANVI" in url_upper:
            lang_override = "bgc"
        elif "BHOJPURI" in url_upper:
            lang_override = "bho"
        elif "BENGALI" in url_upper:
            lang_override = "ben"
        elif "GUJARATI" in url_upper:
            lang_override = "guj"
        elif "RAJASTHANI" in url_upper:
            lang_override = "raj"
        elif "MARATHI" in url_upper:
            lang_override = "mar"
        elif "TAMIL" in url_upper:
            lang_override = "tam"
        elif "TELUGU" in url_upper:
            lang_override = "tel"
        elif "MALAYALAM" in url_upper:
            lang_override = "mal"
        elif "KANNADA" in url_upper:
            lang_override = "kan"
        elif "ODIA" in url_upper or "ORIYA" in url_upper:
            lang_override = "ori"
        elif "HINDI" in url_upper:
            lang_override = "hin"

        if lang_override:
            title.original_lang = lang_override
            for audio in tracks.audios:
                audio.language = Language.get(lang_override)
        else:
            undef = {Language.get("und"), Language.get("none"), None}
            if title.original_lang and Language.get(title.original_lang) not in {Language.get("eng"), Language.get("en")}:
                undef.add(Language.get("eng"))
                undef.add(Language.get("en"))
            for audio in tracks.audios:
                if audio.language in undef and title.original_lang:
                    audio.language = Language.get(title.original_lang)

        if license_key:
            self._license_url = license_key

        return tracks

    def _vzy_license(self, challenge):
        if not getattr(self, "_license_url", None):
            return None
        import requests
        import base64

        headers = {
            "Content-Type": "application/octet-stream",
            "Origin": "https://www.vzy.one",
            "Referer": "https://www.vzy.one/",
            "User-Agent": self.session.headers.get("User-Agent")
        }

        drm_token = getattr(self, "_drm_token", None)
        token_key = getattr(self, "_token_key", None) or "nv-authorizations"
        if drm_token:
            headers[token_key] = drm_token
            headers["nv-authorizations"] = drm_token

        import time
        max_retries = 3
        last_exception = None
        for attempt in range(1, max_retries + 1):
            try:
                r = requests.post(
                    self._license_url,
                    data=challenge,
                    headers=headers,
                    proxies=self.session.proxies,
                    timeout=15
                )
                r.raise_for_status()
                last_exception = None
                break
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                last_exception = e
                self.log.warning(f"VZY license request attempt {attempt}/{max_retries} failed ({e}). Retrying in 2s...")
                time.sleep(2)
            except requests.HTTPError as e:
                self.log.error(f"VZY License server returned error {r.status_code}: {r.text}")
                raise e

        if last_exception:
            raise last_exception

        if r.headers.get("content-type", "").startswith("application/json") or r.content.startswith(b"{"):
            try:
                data = r.json()
                for k in ("license", "body", "data", "payload"):
                    if k in data:
                        return base64.b64decode(data[k])
            except Exception:
                pass

        return r.content

    # ------------------------------------------------------------------ #
    #  get_titles
    # ------------------------------------------------------------------ #

    def get_titles(self):
        if getattr(self, "is_vzy", False):
            return self._vzy_get_titles()

        self._init_session()

        def extract_episodes(panes, show_name, original_lang):
            titles = []
            for pane in panes:
                episodes = []
                if pane.get("paneType") == "catalog" and "catalog" in pane:
                    catalog = pane["catalog"]
                    episodes = catalog.get("data") or []
                elif pane.get("paneType") == "section" and "section" in pane:
                    section = pane["section"]
                    section_info = section.get("sectionInfo") or {}
                    if section_info.get("dataType") == "entity" or section_info.get("dataSubType") == "video":
                        section_data = section.get("sectionData") or {}
                        episodes = section_data.get("data") or []

                if not episodes:
                    continue

                for idx, ep in enumerate(episodes, start=1):
                    target_obj = ep.get("target") or {}
                    page_attrs = target_obj.get("pageAttributes") or {}
                    display_obj = ep.get("display") or {}

                    ep_id = (
                        ep.get("id") or
                        ep.get("contentId") or
                        page_attrs.get("id") or
                        (target_obj.get("path") or "").split("/")[-1] or
                        f"{show_name}-ep{idx}"
                    )
                    ep_name = (
                        ep.get("title") or
                        ep.get("name") or
                        display_obj.get("title") or
                        f"Episode {idx}"
                    )
                    
                    season_val = ep.get("seasonNumber") or ep.get("seasonIndex") or page_attrs.get("seasonSeqNo") or 1
                    try:
                        season = int(season_val)
                    except (ValueError, TypeError):
                        season = 1

                    ep_val = ep.get("episodeNumber") or ep.get("episodeIndex") or page_attrs.get("episodeSeqNo") or idx
                    try:
                        episode = int(ep_val)
                    except (ValueError, TypeError):
                        episode = idx

                    ep_target = (
                        ep.get("target") if isinstance(ep.get("target"), str) else None
                    ) or target_obj.get("path") or f"movie/play/{ep_id}"

                    titles.append(Title(
                        id_=ep_id,
                        type_=Title.Types.TV,
                        name=show_name,
                        season=season,
                        episode=episode,
                        episode_name=ep_name,
                        original_lang=original_lang,
                        source=self.ALIASES[0],
                        service_data={"play_target": ep_target},
                    ))
                if titles:
                    break
            return titles

        # Build API path from the URL (strip scheme + host)
        parsed = urllib.parse.urlparse(self.url)
        path = parsed.path.strip("/") or self.title

        resp = self._api_get("page/content", path=path)
        if not resp.get("status"):
            raise ValueError(f"Failed to fetch content metadata: {resp}")

        panes = resp.get("response", {}).get("data") or []

        # Check if this is a partner page URL/path
        if "partner/" in path.lower():
            titles = []
            # Extract all item paths first
            item_targets = []
            for pane in panes:
                sec = pane.get("section") or pane.get("catalog") or {}
                items = sec.get("sectionData", {}).get("data") or sec.get("data") or []
                for item in items:
                    target_obj = item.get("target") or {}
                    tpath = target_obj.get("path")
                    if isinstance(tpath, str) and tpath:
                        # Extract name
                        display_obj = item.get("display") or {}
                        item_name = (
                            item.get("title") or
                            item.get("name") or
                            display_obj.get("title") or
                            display_obj.get("name")
                        )
                        if not item_name:
                            for elem in item.get("hover", {}).get("elements") or []:
                                if elem.get("key") == "name":
                                    item_name = elem.get("value")
                                    break
                        if not item_name:
                            item_name = tpath.split("/")[-1]
                        item_targets.append((tpath, item_name))

            # De-duplicate targets by path
            seen_paths = set()
            unique_targets = []
            for tpath, item_name in item_targets:
                if tpath not in seen_paths:
                    seen_paths.add(tpath)
                    unique_targets.append((tpath, item_name))

            self.log.info(f"Partner page contains {len(unique_targets)} items. Fetching metadata for each...")

            for tpath, item_name in unique_targets:
                self.log.info(f" + Fetching: {tpath} ({item_name})")
                try:
                    item_resp = self._api_get("page/content", path=tpath)
                    item_panes = item_resp.get("response", {}).get("data") or []
                    
                    # Check if it is a movie or TV show
                    is_movie = "movie" in tpath.lower()
                    if is_movie:
                        # Find the content metadata pane to get year and original_lang
                        item_metadata = {}
                        for pane in item_panes:
                            if pane.get("paneType") == "content" and "content" in pane:
                                item_metadata = pane["content"]
                                break
                        original_lang, year = self._parse_subtitle_row(item_metadata)
                        play_target = self._find_play_target(item_metadata) or f"movie/play/{tpath.split('/')[-1]}"
                        
                        titles.append(Title(
                            id_=tpath.split('/')[-1],
                            type_=Title.Types.MOVIE,
                            name=item_name,
                            year=year,
                            original_lang=original_lang,
                            source=self.ALIASES[0],
                            service_data={"play_target": play_target},
                        ))
                    else:
                        # TV Show
                        item_metadata = {}
                        for pane in item_panes:
                            if pane.get("paneType") == "content" and "content" in pane:
                                item_metadata = pane["content"]
                                break
                        original_lang, _ = self._parse_subtitle_row(item_metadata)
                        
                        show_eps = extract_episodes(item_panes, item_name, original_lang)
                        titles.extend(show_eps)
                except Exception as e:
                    self.log.warning(f"Failed to fetch metadata for {tpath}: {e}")

            self.log.info(f"Successfully loaded a total of {len(titles)} episodes/movies from the partner page.")
            return titles

        # ── Find the content metadata pane ──────────────────────────────
        metadata = {}
        for pane in panes:
            if pane.get("paneType") == "content" and "content" in pane:
                metadata = pane["content"]
                break

        if not metadata:
            raise ValueError(f"No content pane found in Watcho API response for path: {path}")

        name = (
            metadata.get("title") or
            metadata.get("name") or
            "Watcho Video"
        )
        original_lang, year = self._parse_subtitle_row(metadata)
        play_target = self._find_play_target(metadata) or f"movie/play/{self.title}"

        # ── TV show: look for an episode catalog or section pane ────────
        titles = extract_episodes(panes, name, original_lang)
        if titles:
            self.log.info(f"Found {len(titles)} episode(s) for \"{name}\"")
            return titles

        # ── Movie (or single episode URL) ────────────────────────────────
        return Title(
            id_=self.title,
            type_=Title.Types.MOVIE,
            name=name,
            year=year,
            original_lang=original_lang,
            source=self.ALIASES[0],
            service_data={"play_target": play_target},
        )

    # ------------------------------------------------------------------ #
    #  get_tracks
    # ------------------------------------------------------------------ #

    def get_tracks(self, title):
        if getattr(self, "is_vzy", False) or (title.service_data and title.service_data.get("vzy")):
            return self._vzy_get_tracks(title)

        self._init_session()

        play_target = title.service_data.get("play_target") or f"movie/play/{title.id}"

        resp = self._api_get("page/stream", path=play_target)
        if not resp.get("status"):
            # Surface a useful subscription/access error if available
            page_attrs = resp.get("response", {}).get("pageAttributes") or {}
            error_msg = (
                page_attrs.get("SubscribeErrorMessage") or
                page_attrs.get("RentErrorMessage") or
                page_attrs.get("ContentAccessErrorMessage") or
                str(resp.get("error", {}).get("message") or "No stream available")
            )
            raise ValueError(f"Stream error: {error_msg}")

        page_attrs = resp.get("response", {}).get("pageAttributes") or {}
        network_name = (page_attrs.get("networkName") or page_attrs.get("networkCode") or "").lower().replace(" ", "")
        
        network_mapping = {
            "lionsgate": "LGP",
            "lionsgateplay": "LGP",
            "ultrajhakaas": "UJHS",
            "ultrajhakas": "UJHS",
            "chaupal": "CHTV",
            "laminar": "CHTV",
            "shemaroo": "SME",
            "shemaroome": "SME",
            "zee5": "Z5",
            "sonyliv": "LIV",
            "hungama": "HM",
            "hoichoi": "HC",
            "klikk": "KLK",
            "ohogujarati": "OHO",
            # Additional Regional OTTs
            "aha": "AHA",
            "sunnxt": "SUNNXT",
            "addatimes": "ADDA",
            "planetmarathi": "PM",
            "manoramamax": "MMAX",
            "sainaplay": "SP",
            "neestream": "NEES",
            "stage": "STAGE",
            "tarangplus": "TANG",
            "aaonxt": "AAONXT",
            "nammaflix": "NF",
            "cityshor": "CSHR",
            "prideeast": "PE",
            "reeldrama": "RD",
            "ollyplus": "OP",
            "tentkotta": "TK",
            "simplysouth": "SS",
            "moviesaints": "MS",
            "koode": "KD",
            "omtv": "OMTV",
        }
        
        new_source = None
        for key, alias in network_mapping.items():
            if key in network_name:
                new_source = alias
                break
                
        if new_source:
            title.source = new_source
            title.filename = title.parse_filename()

        streams = resp.get("response", {}).get("streams") or []
        if not streams:
            raise ValueError("No streams returned by Watcho API")

        # Prefer the default stream; otherwise use the first available
        stream = next((s for s in streams if s.get("isDefault")), streams[0])
        video_url = stream.get("url") or ""
        if not video_url:
            raise ValueError(f"Stream object has no URL: {stream}")

        # Update session headers with CDN signature tokens if present
        params = stream.get("params") or {}
        if "streamKey" in params:
            self.session.headers["stream_key"] = params["streamKey"]
        if "streamPolicy" in params:
            self.session.headers["stream_policy"] = params["streamPolicy"]

        drm_info = stream.get("drmInfo") or {}
        license_url = (
            drm_info.get("licenseUrl") or
            drm_info.get("widevine_license_url") or
            stream.get("keys", {}).get("licenseKey")
        )

        self.log.debug(f"Stream URL: {video_url}")

        # ── DASH ─────────────────────────────────────────────────────────
        if ".mpd" in video_url.lower():
            self.log.debug("Parsing DASH manifest")
            tracks = Tracks.from_mpd(
                url=video_url,
                session=self.session,
                source=title.source,
            )

        # ── HLS ──────────────────────────────────────────────────────────
        else:
            self.log.info("Parsing HLS manifest")
            r = self.session.get(video_url)
            r.raise_for_status()
            master = m3u8.loads(r.text, uri=video_url)
            tracks = Tracks.from_m3u8(master, source=title.source)

            # For muxed HLS (no separate audio renditions), create a
            # placeholder audio track so vinetrimmer's pipeline doesn't skip it.
            if not tracks.audios:
                lang_code = title.original_lang or "hin"
                self.log.info(
                    f"No separate audio rendition found — "
                    f"stream is muxed (lang={lang_code})"
                )
                muxed_audio = AudioTrack(
                    id_=uuid.uuid4().hex[:6],
                    source=title.source,
                    url=video_url,       # points at the video; audio is muxed in
                    codec="aac",
                    language=Language.get(lang_code),
                    bitrate=0,
                    channels="2.0",
                    descriptor=Track.Descriptor.URL,
                    encrypted=False,
                )
                muxed_audio.is_muxed_with_video = True
                tracks.audios.append(muxed_audio)

        # ── Fix undefined audio languages ────────────────────────────────
        undef = {Language.get("und"), Language.get("none"), None}
        if title.original_lang and Language.get(title.original_lang) not in {Language.get("eng"), Language.get("en")}:
            undef.add(Language.get("eng"))
            undef.add(Language.get("en"))
        for audio in tracks.audios:
            if audio.language in undef and title.original_lang:
                audio.language = Language.get(title.original_lang)



        # ── Store license URL and params for use in license() ─────────────
        if license_url:
            self._license_url = license_url
        self._license_params = stream.get("params") or {}

        return tracks

    # ------------------------------------------------------------------ #
    #  DRM
    # ------------------------------------------------------------------ #

    def get_chapters(self, title):
        return []

    def certificate(self, **_):
        return None

    def license(self, challenge, **kwargs):
        if not getattr(self, "_license_url", None):
            return None
        if getattr(self, "is_vzy", False):
            return self._vzy_license(challenge)
        import requests

        import base64
        import time

        is_mobiotics = "mobiotics.com" in self._license_url.lower()

        if is_mobiotics:
            if isinstance(challenge, str):
                chal = challenge
            else:
                chal = base64.b64encode(challenge).decode("utf-8")

            # Mobiotics v6 widevine license endpoint requires /widevine suffix
            lic_url = self._license_url
            if not lic_url.endswith("/widevine"):
                lic_url = lic_url.rstrip("/") + "/widevine"

            # Extract fields from the streams params
            params = getattr(self, "_license_params", {})
            json_data = {
                "payload": chal,
                "contentid": params.get("contentId") or params.get("contentid") or "",
                "providerid": params.get("providerId") or params.get("providerid") or "",
                "timestamp": int(time.time()),
                "drmscheme": "widevine",
                "customdata": {
                    "packageid": params.get("packageId") or params.get("packageid") or "",
                    "drmtoken": params.get("drmToken") or params.get("drmtoken") or "",
                },
            }

            headers = {
                "accept": "*/*",
                "content-type": "application/json",
                "origin": "https://www.watcho.com",
                "referer": "https://www.watcho.com/",
                "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
                "x-provider-id": "watcho",
            }

            r = requests.post(
                lic_url,
                json=json_data,
                headers=headers,
                proxies=self.session.proxies,
                timeout=15,
            )
            try:
                r.raise_for_status()
            except requests.HTTPError as e:
                self.log.error(f"License server returned error {r.status_code}: {r.text}")
                raise e
            data = r.json()
            return base64.b64decode(data["body"])

        headers = {
            "Content-Type": "application/octet-stream",
            "Referer": "https://www.watcho.com/",
            "Origin": "https://www.watcho.com",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        }

        # Mobiotics DRM (UltraJhakaas, etc.) license server expects the drmToken
        # to be passed as an Authorization header (Bearer token) or custom headers
        drm_token = getattr(self, "_license_params", {}).get("drmToken")
        if drm_token:
            headers["Authorization"] = f"Bearer {drm_token}"
            headers["dt-custom-data"] = drm_token # some mobiotics endpoints expect this

        r = requests.post(
            self._license_url,
            data=challenge,
            headers=headers,
            proxies=self.session.proxies,
            timeout=15,
        )
        try:
            r.raise_for_status()
        except requests.HTTPError as e:
            self.log.error(f"License server returned error {r.status_code}: {r.text}")
            raise e
        return r.content
