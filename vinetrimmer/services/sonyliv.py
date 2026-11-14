import os
import re
import time
import json
import m3u8
import base64
import requests

import click

from vinetrimmer.objects import TextTrack, Title, Tracks
from vinetrimmer.services.BaseService import BaseService

class Sonyliv(BaseService):
    """
    SonyLiv India streaming service (https://sonyliv.com).
    \b
    Authorization: Cookies + accessToken(from Browser Local Storage)
    Security: UHD@L3, doesn't seem to care about releases.
    
    Script By https://telegram.me/divine_404
    """

    ALIASES = ["SLIV", "sonyliv"]

    TITLE_RE = r"^(?:https?://(?:www\.)?sonyliv.com/(?P<type>movies|shows)/[a-z0-9-]+-)?(?P<id>\d+)"

    @staticmethod
    @click.command(name="Sonyliv", short_help="https://sonyliv.com")
    @click.argument("title", type=str, required=False)
    @click.option("-d", "--device", default="chrome",
                type=click.Choice(["chrome", "android", "safari"], case_sensitive=False),
                help="Device to use for requesting manifest.")
    @click.pass_context
    def cli(ctx, **kwargs):
        return Sonyliv(ctx, **kwargs)
    
    def __init__(self, ctx, title, device):
        super().__init__(ctx)
        self.m = self.parse_title(ctx, title)
        self.manifestDevice = device

        self.vcodec = ctx.parent.params["vcodec"] or "H264"
        self.acodec = ctx.parent.params["acodec"] or "EC3"
        self.range = ctx.parent.params["range_"] or "SDR"
        self.quality = ctx.parent.params.get("quality") or 1080

        self.profile = ctx.obj.profile

        self.device_id = None
        self.app_version = None
        self.accessToken = None
        self.securityToken = None
        self.license_api = None
        self.cacheData = None

        self.configure()

    def get_titles(self):
        tempHeaders = self.session.headers.copy()
        tempHeaders.update({
            "Host": "apiv2.sonyliv.com",
            "Security_token": self.securityToken,
            "Session_id": self.session.cookies.get('sessionId', None, 'apiv2.sonyliv.com'),
            "Device_id": self.device_id,
            "App_version": self.app_version,
        })


        r = requests.get(
            url = self.config['endpoints']['title'].format(id=self.m['id']),
            headers = tempHeaders,
            cookies = self.reqCookies,
            proxies=self.session.proxies
        )
        try:
            titleRes = json.loads(r.content.decode())
        except json.JSONDecodeError:
            raise ValueError(f"Received Irrelevant Title API Response: {r.text}")

        for correct in titleRes['resultObj']['containers']:
            if int(correct['id']) == int(self.m['id']):
                titleRes = correct.copy()

        if titleRes['metadata']['objectSubtype'] == 'MOVIE_BUNDLE' or titleRes['metadata']['objectSubtype'] == 'MOVIE':
            return Title(
                id_=self.m['id'],
                type_=Title.Types.MOVIE,
                name=titleRes['metadata']['title'],
                year=titleRes['metadata']['emfAttributes']['release_year'] if 'release_year' in titleRes['metadata']['emfAttributes'] else titleRes['metadata']['emfAttributes']['release_date'].split('-')[0],
                original_lang=titleRes['metadata']['language'],
                source=self.ALIASES[0],
                service_data=titleRes,
            )
        
        elif (titleRes['layout'] == "BUNDLE_ITEM"):
            bucket = []

            if (titleRes['metadata']['objectSubtype'] == "EPISODIC_SHOW"):
                ep_count = titleRes['episodeCount']
                r = requests.get(
                    url = self.config['endpoints']['season'].format(id=titleRes['id'], ep_start=0, ep_end=ep_count-1),
                    headers = tempHeaders,
                    cookies = self.reqCookies,
                    proxies=self.session.proxies
                )

                try:
                    seasonRes = json.loads(r.content.decode())
                except json.JSONDecodeError:
                    raise ValueError(f"Received Irrelevant Season API Response: {r.text}")
                
                for episode in seasonRes['resultObj']['containers'][0]['containers']:
                    bucket.append({
                        "episode_id": episode['id'],
                        "series_name": titleRes['metadata']['title'],
                        "season_number": titleRes['metadata']['season'] if ('season' in titleRes['metadata'].keys()) else "1",
                        "episode_number": episode['metadata']['episodeNumber'],
                        "episode_name": episode['metadata']['episodeTitle'],
                        "episode_org_lang": episode['metadata']['language'],
                        "service_data": episode
                    })

            if (titleRes['metadata']['objectSubtype'] == "SHOW"):
                for season in titleRes['containers']:
                    if season['metadata']['objectSubtype'] == "SEASON" and int(season['parents'][0]['parentId']) == int(self.m['id']):
                        ep_count = season['episodeCount']
                        r = requests.get(
                            url = self.config['endpoints']['season'].format(id=season['id'], ep_start=0, ep_end=ep_count-1),
                            headers = tempHeaders,
                            cookies = self.reqCookies,
                            proxies=self.session.proxies
                        )
                        try:
                            seasonRes = json.loads(r.content.decode())
                        except json.JSONDecodeError:
                            raise ValueError(f"Received Irrelevant Season API Response: {r.text}")

                        for episode in seasonRes['resultObj']['containers'][0]['containers']:
                            bucket.append({
                                "episode_id": episode['id'],
                                "series_name": titleRes['metadata']['title'],
                                "season_number": season['metadata']['season'],
                                "episode_number": episode['metadata']['episodeNumber'],
                                "episode_name": episode['metadata']['episodeTitle'],
                                "episode_org_lang": episode['metadata']['language'],
                                "service_data": episode
                            })
                    
                    elif season['metadata']['objectSubtype'] == "EPISODE_RANGE" and int(season['parents'][0]['parentId']) == int(self.m['id']):
                        ep_count = season['episodeCount']
                        r = requests.get(
                            url = self.config['endpoints']['season'].format(id=season['id'], ep_start=0, ep_end=ep_count-1),
                            headers = tempHeaders,
                            cookies = self.reqCookies,
                            proxies=self.session.proxies
                        )

                        try:
                            seasonRes = json.loads(r.content.decode())
                        except json.JSONDecodeError:
                            raise ValueError(f"Received Irrelevant Season API Response: {r.text}")
                        
                        for episode in seasonRes['resultObj']['containers'][0]['containers']:
                            bucket.append({
                                "episode_id": episode['id'],
                                "series_name": titleRes['metadata']['title'],
                                "season_number": season['metadata']['season'],
                                "episode_number": episode['metadata']['episodeNumber'],
                                "episode_name": episode['metadata']['episodeTitle'],
                                "episode_org_lang": episode['metadata']['language'],
                                "service_data": episode
                            })
            
            if not bucket == []:
                return [Title(
                    id_=b['episode_id'],
                    type_=Title.Types.TV,
                    name=b['series_name'],
                    season=b['season_number'],
                    episode=b['episode_number'],
                    episode_name=b['episode_name'],
                    original_lang=b['episode_org_lang'],
                    source=self.ALIASES[0],
                    service_data=b['service_data']
                ) for b in bucket]
        else:
            self.log.exit(" - Title unsupported.")
                                 
    def get_tracks(self, title):
        
        # Build client hint strings for TV and WEB profiles
        web_client = '{"os_name":"Mac OS","os_version":"10.15.7","device_make":"none","device_model":"none","display_res":"1470","viewport_res":"894","conn_type":"4g","supp_codec":"H264,AV1,AAC","client_throughput":"16000","td_user_agent":"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36","hdr_decoder":"UNKNOWN","audio_decoder":"STEREO"}'

        # Define endpoints to request
        manifest_endpoint = self.config['endpoints']['manifest']
        endpoints_to_fetch = []
        
        range_upper = (self.range or "").upper()

        if self.vcodec == 'H265':
            ranges_to_fetch = []
            if "DV" in range_upper:
                ranges_to_fetch.append("DOLBY_VISION")
            if "HDR" in range_upper:
                ranges_to_fetch.append("HDR10")
            if not ranges_to_fetch:
                ranges_to_fetch.append("HLG")  # default fallback

            for r_type in ranges_to_fetch:
                tv_client = r'{"device_make":"Amazon","device_model":"AFTMM","display_res":"2160","viewport_res":"2160","supp_codec":"HEVC,H264,AAC,EAC3,AC3,ATMOS","audio_decoder":"EAC3,AAC,AC3,ATMOS","hdr_decoder":"' + r_type + r'","td_user_useragent":"com.onemainstream.sonyliv.android\/8.95 (Android 7.1.2; en_IN; AFTMM; Build\/NS6281 )"}'
                endpoints_to_fetch.append({
                    "url": manifest_endpoint,
                    "client": tv_client,
                    "type": f"TV_{r_type}"
                })
        else:
            # For H264, fetch WEB manifest for video, and TV manifest for Dolby audio
            endpoints_to_fetch.append({
                "url": manifest_endpoint.replace("SONY_ANDROID_TV", "WEB"),
                "client": web_client,
                "type": "WEB"
            })
            if self.acodec != "AAC":
                tv_client = r'{"device_make":"Amazon","device_model":"AFTMM","display_res":"2160","viewport_res":"2160","supp_codec":"HEVC,H264,AAC,EAC3,AC3,ATMOS","audio_decoder":"EAC3,AAC,AC3,ATMOS","hdr_decoder":"HLG","td_user_useragent":"com.onemainstream.sonyliv.android\/8.95 (Android 7.1.2; en_IN; AFTMM; Build\/NS6281 )"}'
                endpoints_to_fetch.append({
                    "url": manifest_endpoint,
                    "client": tv_client,
                    "type": "TV"
                })

        tempHeaders = self.session.headers.copy()
        if "X-Playback-Session-Id" in tempHeaders.keys():
            tempHeaders.pop("X-Playback-Session-Id")
        tempHeaders.update({
            "Host": "apiv2.sonyliv.com",
            "Content-Type": "application/json",
            "X-Via-Device": "true",
            "Security_token": self.securityToken,
            "App_version": self.app_version,
            "Device_id": self.device_id,
            "Session_id": self.session.cookies.get('sessionId', None, 'apiv2.sonyliv.com'),
            "Authorization": "Bearer " + self.accessToken,
        })

        if str(title.type) == "Types.MOVIE":
            if 'containers' not in title.service_data.keys():
                _id_ = title.service_data['metadata']['contentId']
            else:
                for mov in title.service_data['containers']:
                    if mov['metadata']['contentSubtype'] == "MOVIE":
                        _id_ = mov['id']
        else:
            _id_ = title.service_data['metadata']['contentId']

        combined_tracks = None

        for ep in endpoints_to_fetch:
            self.log.debug(f"Requesting manifest for {ep['type']} profile...")
            tempHeaders["Td_client_hints"] = ep["client"]
            
            manifestRes = None
            max_retries = 3
            for attempt in range(max_retries):
                r = requests.post(
                    url = ep["url"].format(id=_id_, bid=self.cacheData['contactId']['id']),
                    headers = tempHeaders,
                    cookies = self.reqCookies,
                    json = {
                        "actionType": "play",
                        "browser": 'chrome',
                        "deviceId": self.device_id,
                        "hasLAURLEnabled": True,
                        "os": "Mac OS",
                        "platform": "web",
                        "adsParams":{
                            "idtype": "uuid",
                            "rdid": self.device_id,
                            "is_lat": 0,
                            "ppid": self.cacheData['contactId']['PPID']
                        }
                    },
                    proxies=self.session.proxies
                )
                try:
                    manifestRes = json.loads(r.content.decode())
                    if manifestRes['resultCode'] != "OK":
                        if "concurrent stream" in str(manifestRes['message']).lower():
                            if attempt < max_retries - 1:
                                self.log.warning(
                                    f"Reached concurrent stream limit. Retrying in 60 seconds "
                                    f"(attempt {attempt + 1}/{max_retries})..."
                                )
                                time.sleep(60)
                                continue
                        if len(endpoints_to_fetch) > 1 and ep["type"].startswith("TV"):
                            self.log.warning(
                                f"Failed to fetch TV manifest (Dolby tracks will be unavailable): "
                                f"{manifestRes['message']}"
                            )
                            break
                        raise ValueError(manifestRes['message'])
                    break
                except json.JSONDecodeError:
                    raise ValueError(f"Received Irrelevant Manifest API Response: {r.text}")
            
            if manifestRes is None or manifestRes['resultCode'] != "OK":
                continue
            
            mpd_url = manifestRes['resultObj']['videoURL']

            try:
                self.license_api = manifestRes['resultObj']['LA_Details']['laURL']
            except Exception as e:
                pass

            self.session.headers.update({
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/115.0",
                "Accept": "*/*",
                "Accept-Language": "en-US,en;q=0.5",
                "Accept-Encoding": "gzip, deflate",
                "Referer": "https://www.sonyliv.com/",
                "X-Playback-Session-Id": self.device_id,
                "Origin": "https://www.sonyliv.com",
            })

            r_mpd = self.session.get(mpd_url)

            if not '.m3u8' in str(mpd_url):
                tracks = Tracks.from_mpd(
                    url = mpd_url,
                    data = r_mpd.content.decode(),
                    session = self.session,
                    source = self.ALIASES[0],
                )
            else:
                tracks = Tracks.from_m3u8(
                    m3u8.loads(r_mpd.text, uri=mpd_url),
                    source=self.ALIASES[0],
                    url=mpd_url,
                )

            # Check SDR/HDR/DV and suffix video track IDs
            for video in tracks.videos:
                video.hdr10 = False
                video.dv = False
                video.hlg = False

            av_range_ = manifestRes['resultObj']['additionalDataJson']['video_quality']
            if av_range_ == "HDR":
                for video in tracks.videos:
                    video.hdr10 = True
                    video.id = f"{video.id}-HDR10"
            elif av_range_ == "DOLBY_VISION":
                for video in tracks.videos:
                    video.dv = True
                    video.id = f"{video.id}-DV"
            elif av_range_ == "HLG":
                for video in tracks.videos:
                    video.hlg = True
                    video.id = f"{video.id}-HLG"

            if combined_tracks is None:
                combined_tracks = tracks
                
                # Adding subtitle tracks
                for sub in manifestRes['resultObj']['subtitle']:
                    combined_tracks.add(TextTrack(
                        id_= sub['subtitleId'],
                        source = self.ALIASES[0],
                        url = sub['subtitleUrl'],
                        codec = "vtt", #hardcoded
                        language = sub['subtitleLanguageName'],
                    ), warn_only=True)
            else:
                # Merge video tracks
                if self.vcodec == 'H265':
                    for video in tracks.videos:
                        combined_tracks.add(video)

                # Merge logic
                if ep["type"] == "TV" and self.vcodec == "H264":
                    self.log.debug("Merging Dolby audio tracks from TV profile into H264 profile...")
                    for audio in tracks.audios:
                        try:
                            channels = float(getattr(audio, "channels", 2) or 2)
                        except (ValueError, TypeError):
                            channels = 2.0
                        is_dolby = any(c in str(audio.codec).upper() for c in ("EAC3", "AC3", "EC3", "EC-3")) or channels > 2
                        if is_dolby:
                            if audio.id not in [a.id for a in combined_tracks.audios]:
                                audio.id = f"{audio.id}-5.1"
                                combined_tracks.add(audio)

        for track in combined_tracks:
            track.needs_proxy = True
        return combined_tracks

    def get_chapters(self, title):
        return []

    def certificate(self, **_):
        return None

    def license(self, challenge: bytes, title: Title, **_):

        if self.license_api == None: 

            licHeaders = self.session.headers.copy()
            if "X-Playback-Session-Id" in licHeaders.keys():
                licHeaders.pop("X-Playback-Session-Id")
            licHeaders.update({
                "Host": "apiv2.sonyliv.com",
                "Content-Type": "application/json",
                "Security_token": self.securityToken,
                "Device_id": self.device_id,
                "X-Via-Device": "true",
                "Authorization": "Bearer " + self.accessToken
            })
            r = requests.post(
                url = self.config['endpoints']['license'],
                headers = licHeaders,
                json = {
                    "platform": self.config['device'][self.manifestDevice]['platform'],
                    "deviceId": self.device_id,
                    "actionType": "play",
                    "browser": self.manifestDevice,
                    "assetId": title.service_data['metadata']['contentId'],
                    "os": self.manifestDevice
                }
            )

            try:
                licRes = json.loads(r.content.decode())
            except json.JSONDecodeError:
                raise ValueError(f"Irrelevant License API Response: {r.text}")
            
            self.license_api = licRes['resultObj']['laURL']

            return requests.post(
                url = self.license_api,
                data=challenge,
                # proxies=self.session.proxies,
            ).content  
        
        else:
            return requests.post(
                url = self.license_api,
                data=challenge,
                # proxies=self.session.proxies,
            ).content


    def configure(self):
        self.session.headers.update({
            "User-Agent": self.config['device'][self.manifestDevice]['user-agent'],
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.5",
            "Accept-Encoding": "gzip, deflate",
            "Referer": "https://www.sonyliv.com/",
            "Origin": "https://www.sonyliv.com",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-site",
        })

        if not self.cookies:
            raise self.log.exit(" - Please add cookies")
        self.reqCookies = {
            "_abck": self.session.cookies.get('_abck', None, '.sonyliv.com'),
            "ak_bmsc": self.session.cookies.get('ak_bmsc', None, '.sonyliv.com'),
            "bm_sz": self.session.cookies.get('bm_sz', None, '.sonyliv.com'),
        }

        self.device_id = self.config['device'][self.manifestDevice]['device_id']
        self.app_version = self.config['device'][self.manifestDevice]['app_version']
        self.prepToken()
  
 
    def prepToken(self):
        cache_path = self.get_cache("{profile}_ProfileCache.json".format(profile=self.profile))
        token_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "cookies", "sonyliv", "token.txt")

        if not os.path.isfile(cache_path):
            self.cacheData = {"vt_profile": self.profile}
            self.log.info(" + Generating Cache...")
            
            if os.path.isfile(token_path) and os.path.getsize(token_path) > 0:
                with open(token_path, "r", encoding="utf-8") as tf:
                    self.accessToken = tf.read().strip()
                self.log.info("Read access_token from cookies/sonyliv/token.txt")
            else:
                self.log.info("Enter your access_token from Browser (Dev Tools -> Application -> Local Storage -> 'https://www.sonyliv.com' -> accessToken):")
                self.accessToken = str(input(">")).strip()
                os.makedirs(os.path.dirname(token_path), exist_ok=True)
                with open(token_path, "w", encoding="utf-8") as tf:
                    tf.write(self.accessToken)
                self.log.info("Saved access_token to cookies/sonyliv/token.txt")
                
            self.cacheData["accessToken"] = { 
                "rawToken": self.accessToken,
                "data": json.loads(base64.b64decode(self.accessToken.split('.')[1] + '==='))
            }
            if int(self.cacheData['accessToken']['data']['exp']) < int(time.time()):
                raise self.log.exit(f" - Provided access_token is expired.")

            self.log.info("Getting security_token...")
            self.securityToken = self.getSecurityToken()
            userData = self.refresh()

            self.cacheData["securityToken"] = {
                "rawToken": self.securityToken,
                "data": json.loads(base64.b64decode(self.securityToken.split('.')[1] + '===')) if self.securityToken else {}
            }
            self.cacheData["contactId"] = {
                "id": userData['resultObj']['contactMessage'][0]['contactID'],
                "PPID": self.getHash(userData['resultObj']['contactMessage'][0]['contactID'])
            }

            os.makedirs(os.path.dirname(cache_path), exist_ok=True)
            with open(cache_path, "w", encoding="utf-8") as fd:
                json.dump(self.cacheData, fd, indent = 2)
        
        else:
            with open(cache_path, "r+", encoding="utf-8") as fd:
                self.cacheData = json.loads(fd.read())
                if int(self.cacheData['accessToken']['data']['exp']) < int(time.time()):
                    self.log.info("- access_token expired. Checking cookies/sonyliv/token.txt for a new one...")
                    if os.path.isfile(token_path) and os.path.getsize(token_path) > 0:
                        with open(token_path, "r", encoding="utf-8") as tf:
                            new_token = tf.read().strip()
                        if new_token and new_token != self.cacheData['accessToken']['rawToken']:
                            self.accessToken = new_token
                            self.cacheData["accessToken"] = { 
                                "rawToken": self.accessToken,
                                "data": json.loads(base64.b64decode(self.accessToken.split('.')[1] + '==='))
                            }
                            if int(self.cacheData['accessToken']['data']['exp']) < int(time.time()):
                                raise self.log.exit("- The access_token in token.txt is also expired. Please update it.")
                            self.log.info("Successfully loaded new access_token from token.txt")
                        else:
                            raise self.log.exit("- access_token expired. Please update cookies/sonyliv/token.txt and delete cache file.")
                    else:
                        raise self.log.exit("- access_token expired. Delete cache file and update cookies.")
                else:
                    self.accessToken = self.cacheData['accessToken']['rawToken']

                if self.cacheData['securityToken'].get('rawToken') and int(self.cacheData['securityToken']['data'].get('exp', 0)) < int(time.time()):
                    self.log.info("security_token expired, Getting a new one...")
                    self.securityToken = self.getSecurityToken()
                    userData = self.refresh()

                    self.cacheData["securityToken"] = {
                        "rawToken": self.securityToken,
                        "data": json.loads(base64.b64decode(self.securityToken.split('.')[1] + '===')) if self.securityToken else {}
                    }
                    self.cacheData["contactId"] = {
                        "id": userData['resultObj']['contactMessage'][0]['contactID'],
                        "PPID": self.getHash(userData['resultObj']['contactMessage'][0]['contactID'])
                    }
                else:
                    self.securityToken = self.cacheData['securityToken'].get('rawToken', "")
                    userData = self.refresh()
                    self.cacheData["contactId"] = {
                        "id": userData['resultObj']['contactMessage'][0]['contactID'],
                        "PPID": self.getHash(userData['resultObj']['contactMessage'][0]['contactID'])
                    }
                    self.log.info("Using Account Tokens from Cache.")
                fd.seek(0)
                fd.truncate()
                json.dump(self.cacheData, fd, indent = 2)
        return
        
    def getSecurityToken(self):
        sec_token_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "cookies", "sonyliv", "security_token.txt")
        if os.path.isfile(sec_token_path) and os.path.getsize(sec_token_path) > 0:
            with open(sec_token_path, "r", encoding="utf-8") as sf:
                manual_sec = sf.read().strip()
            if manual_sec:
                try:
                    data = json.loads(base64.b64decode(manual_sec.split('.')[1] + '==='))
                    if int(data.get('exp', 0)) > int(time.time()):
                        self.log.info("Read security_token from cookies/sonyliv/security_token.txt")
                        return manual_sec
                except Exception:
                    pass

        # Primary: Official API endpoint (fast and reliable)
        get_token_url = self.config.get('endpoints', {}).get(
            'security_token', 'https://apiv2.sonyliv.com/AGL/1.4/A/ENG/WEB/ALL/GETTOKEN'
        )
        try:
            api_headers = {
                'Accept': 'application/json, text/plain, */*',
                'Referer': 'https://www.sonyliv.com/',
                'Origin': 'https://www.sonyliv.com',
                'User-Agent': self.config['device'][self.manifestDevice]['user-agent']
            }
            r = requests.get(get_token_url, headers=api_headers, proxies=self.session.proxies, timeout=10)
            if r.status_code == 200:
                scToken = r.json().get('resultObj', "")
                if scToken:
                    json.loads(base64.b64decode(scToken.split('.')[1] + '==='))
                    return scToken
        except Exception as e:
            self.log.debug(f"Direct GETTOKEN API failed: {e}")

        # Fallback: scrape homepage with timeout
        tempHeaders = self.session.headers.copy()
        tempHeaders.update({
            "Host": "www.sonyliv.com",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Site": "none",
        })

        try:
            resp = requests.get(
                url="https://www.sonyliv.com",
                headers=tempHeaders,
                cookies=self.reqCookies,
                proxies=self.session.proxies,
                timeout=10
            )
            resp = str(resp.content.decode())
            import re
            match = re.search(r'securityToken:\{.*?resultObj:"([^"]*)"', resp)
            scToken = match.group(1) if match else ""
            if scToken:
                json.loads(base64.b64decode(scToken.split('.')[1] + '==='))
            return scToken
        except Exception as e:
            self.log.exit(f"Failed to obtain security_token: {e}")

    def refresh(self):
        tempHeaders = self.session.headers.copy()
        tempHeaders.update({
            "Host": "apiv2.sonyliv.com",
            "X-Via-Device": "true",
            "Security_token": self.securityToken,
            "App_version": self.app_version,
            "Device_id": self.device_id,
            "Session_id": self.session.cookies.get('sessionId', None, 'apiv2.sonyliv.com'),
            "Authorization": self.accessToken,
        })

        self.reqCookies.update({
            "sessionId": self.session.cookies.get('sessionId', None, 'apiv2.sonyliv.com'),
            "bm_sv": self.session.cookies.get('bm_sv', None, '.sonyliv.com'),
            "AKA_A2": self.session.cookies.get('AKA_A2', None, '.sonyliv.com'),
            "bm_mi": self.session.cookies.get('bm_mi', None, '.sonyliv.com'),
        })

        userData = requests.get(
            url = self.config['endpoints']['refresh'],
            headers = tempHeaders,
            cookies = self.reqCookies,
            proxies=self.session.proxies
        )
        userData = json.loads(userData.content.decode())
        if userData['resultCode'] == "OK" and userData['message'] == "SUCCESS":
            return userData
        else:
            self.log.error(userData)
            self.log.exit("Unintended API Response.")
        
    def getHash(self, contactId):
        tempHeaders = self.session.headers.copy()
        tempHeaders.update({
            "Host": "apiv2.sonyliv.com",
            "Content-Type": "application/json",
            "X-Via-Device": "true",
            "Security_token": self.securityToken,
            "App_version": self.app_version,
            "Device_id": self.device_id,
            "Session_id": self.session.cookies.get('sessionId', None, 'apiv2.sonyliv.com'),
            "Authorization": self.accessToken,            
        })
        hashData = requests.post(
            url = self.config['endpoints']['hash'],
            headers = tempHeaders,
            json = {
                "baseId": contactId
            },
            cookies = self.reqCookies,
            proxies=self.session.proxies
        )
        hashData = json.loads(hashData.content.decode())
        if hashData['resultCode'] == "OK":
            return hashData['resultObj']['ppId']
        else:
            self.log.error(hashData)
            self.log.exit("Unintended API Response.")