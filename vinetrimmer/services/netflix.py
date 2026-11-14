import base64
import json
import os
import re
import sys
import time
import random
import click
import jsonpickle
import requests
from langcodes import Language
from datetime import timedelta
from uuid import UUID
from itertools import zip_longest                      
from vinetrimmer.objects import AudioTrack, MenuTrack, TextTrack, Title, Tracks, VideoTrack
from vinetrimmer.services.BaseService import BaseService
from vinetrimmer.utils.collections import as_list, flatten
from vinetrimmer.utils.MSL import MSL
from vinetrimmer.utils.MSL.schemes import KeyExchangeSchemes
from vinetrimmer.utils.MSL.schemes.UserAuthentication import UserAuthentication
from vinetrimmer.utils.widevine.cdm import Cdm
from vinetrimmer.utils.widevine.device import LocalDevice
from vinetrimmer.vendor.pymp4.parser import Box
from vinetrimmer.utils.gen_esn import chrome_esn_generator

class Netflix(BaseService):

    ALIASES = ["NF", "netflix"]
    TITLE_RE = [
        r"^(?:https?://(?:www\.)?netflix\.com(?:/[a-z0-9]{2})?/(?:title/|watch/|.+jbv=))?(?P<id>\d+)",
        r"^https?://(?:www\.)?unogs\.com/title/(?P<id>\d+)",
    ]

    NF_LANG_MAP = {
        "es": "es-419",
        "pt": "pt-PT",
    }

    @staticmethod
    @click.command(name="Netflix", short_help="https://netflix.com")
    @click.argument("title", type=str, required=False)
    @click.option("-drm", "--drm-system", type=click.Choice(["widevine", "playready"], case_sensitive=False),
                  default="playready",
                  help="which drm system to use")
    @click.option("-p", "--profile", type=click.Choice(["MPL", "HPL", "QC", "MPL+HPL", "MPL+HPL+QC", "MPL+QC"], case_sensitive=False),
                  default="MPL+HPL+QC",
                  help="H.264 profile to use. Default is best available.")
    @click.option("--meta-lang", type=str, help="Language to use for metadata")
    @click.option("--hybrid", is_flag=True, default=False, help="Fetch both DV and HDR10 profiles to enable DV+HDR hybrid creation.")
    @click.option("--single", is_flag=True, default=False, help="Single title mode. Must use for trailers.")
    @click.option("--hydrate", is_flag=True, default=False, help="Hydrate missing tracks (audio/text) from the manifest.")
    @click.pass_context
    def cli(ctx, **kwargs):
        return Netflix(ctx, **kwargs)

    def __init__(self, ctx, title, drm_system, profile, meta_lang, single, hybrid, hydrate):
        super().__init__(ctx)
        self.parse_title(ctx, title)
        self.profile = profile
        self.meta_lang = meta_lang
        self.single = single
        self.hydrate = hydrate
        
        # Force Widevine when using proxy for better compatibility
        if ctx.parent.params["proxy"] and len("".join(i for i in ctx.parent.params["proxy"] if not i.isdigit())) == 2:
            self.GEOFENCE.append(ctx.parent.params["proxy"])
            self.proxy_country = ctx.parent.params["proxy"]
            # Switch to Widevine when using proxy
            if drm_system == "playready":
                self.log.info(f"Switching from PlayReady to Widevine for proxy compatibility")
                drm_system = "widevine"
        else:
            self.proxy_country = None
            
        self.drm_system = drm_system
        self.hybrid = hybrid
        if self.hybrid:
            ctx.parent.params["range_"] = "DV+HDR"
        self.vcodec = ctx.parent.params["vcodec"]
        self.acodec = ctx.parent.params["acodec"]
        self.range = ctx.parent.params["range_"]
        self.quality = ctx.parent.params["quality"]
        self.audio_only = ctx.parent.params["audio_only"]
        self.subs_only = ctx.parent.params["subs_only"]
        self.chapters_only = ctx.parent.params["chapters_only"]
        self.cdm = ctx.obj.cdm
        self.download_proxied = len(self.GEOFENCE) > 0
        self.profiles = []
        self.profiles_global = []
        self.msl = None
        self.esn = None
        self.userauthdata = None
        self.react_context = {}
        self.session_id = None
        self.is_live = False
        self.configure()

    def get_titles(self):
        # Verify proxy is working by checking apparent IP
        if self.proxy_country:
            try:
                ip_check = self.session.get("https://api.ipify.org?format=json", timeout=10).json()
                apparent_ip = ip_check.get('ip', 'unknown')
                self.log.info(f" + Apparent IP through proxy: {apparent_ip}")
                
                # Override detected country - force Pakistan headers regardless of actual IP
                self.log.info(f" + Forcing region headers for: {self.proxy_country}")
            except Exception as e:
                self.log.warning(f" + Could not verify proxy IP: {e}")
        
        metadata = self.get_metadata(self.title)["video"]
        self.log.debug(f" + Metadata keys: {list(metadata.keys())}")
        if metadata.get("type") in ("movie", "live", "liveEvent") or metadata.get("isLive") or metadata.get("live") or self.single:
            if metadata.get("type") in ("live", "liveEvent") or metadata.get("isLive") or metadata.get("live"):
                self.is_live = True
            titles = [Title(
                id_=self.title,
                type_=Title.Types.MOVIE,
                name=metadata["title"],
                year=metadata.get("year"),
                source=self.ALIASES[0],
                service_data=metadata
            )]
        else:
            episodes = [episode for season in [
                [dict(x, **{"season": season["seq"]}) for x in season["episodes"]]
                for season in metadata["seasons"]
            ] for episode in season]
            for ep in episodes:
                if ep.get("isLive") or ep.get("live") or ep.get("type") in ("live", "liveEvent") or "live" in str(ep).lower():
                    self.is_live = True
                    break
            titles = [Title(
                id_=self.title,
                type_=Title.Types.TV,
                name=metadata["title"],
                season=episode.get("season"),
                episode=episode.get("seq"),
                episode_name=episode.get("title"),
                source=self.ALIASES[0],
                service_data=episode
            ) for episode in episodes]
        manifest = self.get_manifest(titles[0], self.profiles)
        original_language = self.get_original_language(manifest)
        for title in titles:
            title.original_lang = original_language
        return titles

    def hydrate_track_internal(self, audio_track_id, text_track_id, manifest, viewable_id):
        params = {
            "type": "standard",
            "viewableId": viewable_id,
            "profiles": self.profiles_global,
            "flavor": "STANDARD",
            "drmType": self.drm_system,
            "drmVersion": "30" if self.drm_system == "playready" else self.config["configuration"].get("drm_version", "0"),
            "usePsshBox": True,
            "isBranching": False,
            "useHttpsStreams": True,
            "supportsUnequalizedDownloadables": True,
            "imageSubtitleHeight": 1080,
            "uiVersion": self.react_context["playerModel"]["data"]["config"]["ui"]["initParams"]["uiVersion"],
            "uiPlatform": self.react_context["playerModel"]["data"]["config"]["ui"]["initParams"]["uiPlatform"],
            "clientVersion": "6.0026.291.011",
            "requiredAudioTrackId": audio_track_id,
            "requiredTextTrackId": text_track_id,
            "supportsPreReleasePin": True,
            "supportsWatermark": True,
            "showAllDubTracks": True,
            "showAllSubDubTracks": True,
            "supportsPartialHydration": True,
            "videoOutputInfo": [{
                "type": "DigitalVideoOutputDescriptor",
                "outputType": "unknown",
                "supportedHdcpVersions": self.config["configuration"]["supported_hdcp_versions"],
                "isHdcpEngaged": self.config["configuration"]["is_hdcp_engaged"]
            }],
            "titleSpecificData": {
                str(viewable_id): {"unletterboxed": True}
            },
            "preferAssistiveAudio": False,
            "isUIAutoPlay": False,
            "isNonMember": False,
            "challenge": self.config["payload_challenge_pr"] if self.drm_system == "playready" else self.config["payload_challenge"],
        }

        payload = {
            "version": 2,
            "url": "/manifest",
            "id": int(time.time() * 1000) + random.randint(1, 99999), 
            "esn": self.esn,
            "uiVersion": self.react_context["playerModel"]["data"]["config"]["ui"]["initParams"]["uiVersion"],
            "clientVersion": "6.0026.291.011",
            "params": params
        }

        try:
            _, payload_chunks = self.msl.send_message(
                endpoint=self.config["endpoints"]["manifest"],
                params={
                    "reqAttempt": 1,
                    "reqPriority": 10,
                    "reqName": "manifest",
                    "clienttype": self.react_context["playerModel"]["data"]["config"]["ui"]["initParams"]["uimode"],
                    "uiversion": self.react_context["serverDefs"]["data"]["BUILD_IDENTIFIER"],
                },
                application_data=payload,
                userauthdata=self.userauthdata
            )
        except Exception:
            return manifest

        if "errorDetails" in payload_chunks:
            raise Exception(f"Manifest call failed: {payload_chunks['errorDetails']}")
        for audio_track in payload_chunks["audio_tracks"]:
            if audio_track["new_track_id"] == audio_track_id:
                desired_track = audio_track
                break
        for index, audio_track in enumerate(manifest["audio_tracks"]):
            if audio_track["new_track_id"] == audio_track_id:
                manifest["audio_tracks"][index] = desired_track
                break
        for text_track in payload_chunks["timedtexttracks"]:
            if text_track["new_track_id"] == text_track_id:
                desired_track = text_track
                break
        for index, text_track in enumerate(manifest["timedtexttracks"]):
            if text_track["new_track_id"] == text_track_id:
                manifest["timedtexttracks"][index] = desired_track
                break

        return manifest

    def hydrate_tracks(self, manifest, title):
        viewable_id = title.service_data.get("episodeId", title.service_data["id"])
        audio_track_ids = [t["new_track_id"] for t in manifest.get("audio_tracks", []) if not t.get("hydrated")]
        text_track_ids = [t["new_track_id"] for t in manifest.get("timedtexttracks", []) if not t.get("hydrated") and not t.get("isNoneTrack")]
        if not audio_track_ids and not text_track_ids:
            return manifest
        pairs = list(zip_longest(audio_track_ids, text_track_ids, fillvalue=None))
        max_len = len(pairs)
        for i, (audio_id, text_id) in enumerate(pairs):
            if audio_id is None and audio_track_ids:
                audio_id = audio_track_ids[0]
            if text_id is None and text_track_ids:
                text_id = text_track_ids[0]
            current_time = time.strftime("%Y-%m-%d %H:%M:%S")
            prefix = f"{current_time} [I] NF : "
            sys.stdout.write(f"\r{prefix}Fetching missing audio and text tracks [{i+1}/{max_len}]")
            sys.stdout.flush()
            manifest = self.hydrate_track_internal(audio_id, text_id, manifest, viewable_id)            
        sys.stdout.write("\n")
        return manifest

    def get_tracks(self, title):
        has_hydrated = False
        hydrated_audio = []
        hydrated_text = []

        if self.vcodec == "H264":
            tracks = Tracks()
            self.config["profiles"]["video"]["H264"]["MPL+HPL+QC"] = (
                self.config["profiles"]["video"]["H264"]["MPL"] + 
                self.config["profiles"]["video"]["H264"]["HPL"] + 
                self.config["profiles"]["video"]["H264"]["QC"]
            )            
            if self.audio_only or self.subs_only or self.chapters_only:
                profiles = ["MPL+HPL+QC"]
            else:
                profiles = self.profile.split("+")
            for profile in profiles:
                try:
                    manifest = self.get_manifest(title, self.config["profiles"]["video"]["H264"][profile])
                except:
                    manifest = self.get_manifest(title, self.config["profiles"]["video"]["H264"]["MPL"] + self.config["profiles"]["video"]["H264"]["HPL"])                
                if self.hydrate:
                    if not has_hydrated:
                        manifest = self.hydrate_tracks(manifest, title)
                        hydrated_audio = manifest.get("audio_tracks", [])
                        hydrated_text = manifest.get("timedtexttracks", [])
                        has_hydrated = True
                    else:
                        manifest["audio_tracks"] = hydrated_audio
                        manifest["timedtexttracks"] = hydrated_text                
                manifest_tracks = self.manifest_as_tracks(manifest)
                license_url = manifest["links"]["license"]["href"]                
                if self.cdm.device.security_level == 3 and self.cdm.device.type == LocalDevice.Types.CHROME:
                    max_quality = max(x.height for x in manifest_tracks.videos) if manifest_tracks.videos else 0
                    if profile == "HPL" and max_quality >= 1080:
                        if manifest.get("video_tracks") and "SEGMENT_MAP_2KEY" in manifest["video_tracks"][0]["streams"][0]["tags"]:
                            manifest_720 = self.get_manifest(
                                title, [x for x in self.config["profiles"]["video"]["H264"]["HPL"] if "l40" not in x]
                            )
                            license_url = manifest_720["links"]["license"]["href"]
                        else:
                            continue

                for track in manifest_tracks:
                    if track.encrypted:
                        track.extra["license_url"] = license_url
                
                self.duration_tracks(manifest_tracks, manifest, title)
                tracks.add(manifest_tracks, warn_only=True)
            return tracks
            
        elif self.vcodec == "H265" and self.range == "SDR":
            tracks = Tracks()
            self.config["profiles"]["video"]["H265"]["SDR"]["P1+P2"] = (
                self.config["profiles"]["video"]["H265"]["SDR"]["P1"] + 
                self.config["profiles"]["video"]["H265"]["SDR"]["P2"] 
            )
            
            if self.audio_only or self.subs_only or self.chapters_only:
                profiles_to_fetch = ["P1", "P2"]
            else:
                profiles_to_fetch = self.profile.split("+")
                
                if any(p in ["MPL", "HPL", "QC"] for p in profiles_to_fetch):
                    profiles_to_fetch = ["P1", "P2"]
            
            for p_key in profiles_to_fetch:
                try:
                    manifest = self.get_manifest(title, self.config["profiles"]["video"]["H265"]["SDR"][p_key])
                except KeyError:
                    self.log.warning(f" - Profile {p_key} not found in H265 SDR config, skipping.")
                    continue
                
                if self.hydrate:
                    if not has_hydrated:
                        manifest = self.hydrate_tracks(manifest, title)
                        hydrated_audio = manifest.get("audio_tracks", [])
                        hydrated_text = manifest.get("timedtexttracks", [])
                        has_hydrated = True
                    else:
                        manifest["audio_tracks"] = hydrated_audio
                        manifest["timedtexttracks"] = hydrated_text

                manifest_tracks = self.manifest_as_tracks(manifest)
                license_url = manifest["links"]["license"]["href"]
                for track in manifest_tracks:
                    if track.encrypted:
                        track.extra["license_url"] = license_url
                self.duration_tracks(manifest_tracks, manifest, title)
                tracks.add(manifest_tracks, warn_only=True)
            return tracks

        elif self.vcodec == "H265" and self.range == "DV+HDR":
            tracks = Tracks()
            
            manifest_hdr = self.get_manifest(title, self.config["profiles"]["video"]["H265"]["HDR10"])
            if self.hydrate:
                if not has_hydrated:
                    manifest_hdr = self.hydrate_tracks(manifest_hdr, title)
                    hydrated_audio = manifest_hdr.get("audio_tracks", [])
                    hydrated_text = manifest_hdr.get("timedtexttracks", [])
                    has_hydrated = True
                else:
                    manifest_hdr["audio_tracks"] = hydrated_audio
                    manifest_hdr["timedtexttracks"] = hydrated_text
            
            manifest_tracks_hdr = self.manifest_as_tracks(manifest_hdr)
            license_url_hdr = manifest_hdr["links"]["license"]["href"]
            for track in manifest_tracks_hdr:
                if track.encrypted:
                    track.extra["license_url"] = license_url_hdr
                if isinstance(track, VideoTrack):
                    track.hdr10 = track.codec.split("-")[1] in ["hdr", "hdr10plus"] if track.codec.startswith(("av1", "hevc", "vp9")) else False
                    track.dv = track.codec.startswith("hevc-dv")
            self.duration_tracks(manifest_tracks_hdr, manifest_hdr, title)
            tracks.add(manifest_tracks_hdr, warn_only=True)

            dv_sub_profiles = list(self.config["profiles"]["video"]["H265"]["DV"].keys())
            for p_key in dv_sub_profiles:
                try:
                    profile_list = self.config["profiles"]["video"]["H265"]["DV"].get(p_key)
                    if not profile_list:
                        continue
                    manifest_dv = self.get_manifest(title, profile_list)
                    
                    if self.hydrate:
                        manifest_dv["audio_tracks"] = hydrated_audio
                        manifest_dv["timedtexttracks"] = hydrated_text

                    manifest_tracks_dv = self.manifest_as_tracks(manifest_dv)
                    license_url_dv = manifest_dv["links"]["license"]["href"]
            
                    for track in manifest_tracks_dv:
                        if track.encrypted:
                            track.extra["license_url"] = license_url_dv
                        if isinstance(track, VideoTrack):
                            track.id = f"{p_key}-{track.id}"
                            track.hdr10 = False
                            track.dv = True
                            track.extra["dv_profile"] = p_key
            
                    self.duration_tracks(manifest_tracks_dv, manifest_dv, title)
                    tracks.add(manifest_tracks_dv, warn_only=True)
            
                except Exception as e:
                    self.log.error(f" - Error fetching DV {p_key}: {e}")

            return tracks

        else:
            manifest = self.get_manifest(title, self.profiles)
            if self.hydrate and not has_hydrated:
                manifest = self.hydrate_tracks(manifest, title)
            
            manifest_tracks = self.manifest_as_tracks(manifest)
            license_url = manifest["links"]["license"]["href"]
            for track in manifest_tracks:
                if track.encrypted:
                    track.extra["license_url"] = license_url
                if isinstance(track, VideoTrack):
                    track.hdr10 = track.codec.split("-")[1] in ["hdr", "hdr10plus"] if track.codec.startswith(("av1", "hevc", "vp9")) else False
                    track.dv = track.codec.startswith("hevc-dv")
            self.duration_tracks(manifest_tracks, manifest, title)
            return manifest_tracks

    def get_chapters(self, title):
        metadata = self.get_metadata(title.id)["video"]
        if metadata["type"] == "movie" or self.single:
            episode = metadata
        else:
            season = next(x for x in metadata["seasons"] if x["seq"] == title.season)
            episode = next(x for x in season["episodes"] if x["seq"] == title.episode)
        if not (episode.get("skipMarkers") and episode.get("creditsOffset")):
            return []
        chapters = {}
        for item in episode["skipMarkers"]:
            chapters[item] = {"start": 0, "end": 0}
            if not episode["skipMarkers"][item]:
                continue
            if episode["skipMarkers"][item]["start"] is None:
                chapters[item]["start"] = 0
            else:
                chapters[item]["start"] = (episode["skipMarkers"][item]["start"] / 1000)
            if episode["skipMarkers"][item]["end"] is None:
                chapters[item]["end"] = 0
            else:
                chapters[item]["end"] = (episode["skipMarkers"][item]["end"] / 1000)
        cc, intro = 1, 0
        chaps = [MenuTrack(
            number=1,
            title=f"Part {cc:02}",
            timecode="00:00:00.000",
        )]
        for item in chapters:
            if chapters[item]["start"] != 0:
                if intro == 0:
                    cc += 1
                    chaps.append(MenuTrack(
                        number=cc,
                        title="Intro",
                        timecode=MenuTrack.format_duration(max(0, chapters[item]["start"] - 0.5)),
                    ))
                    cc += 1
                    chaps.append(MenuTrack(
                        number=cc,
                        title=f"Part {(cc - 1):02}",
                        timecode=MenuTrack.format_duration(max(0, chapters[item]["end"] + 0.250)),
                    ))
                else:
                    cc += 1
                    chaps.append(MenuTrack(
                        number=cc,
                        title=f"Part {cc:02}",
                        timecode=MenuTrack.format_duration(max(0, chapters[item]["start"] - 0.5)),
                    ))
                    cc += 1
                    chaps.append(MenuTrack(
                        number=cc,
                        title=f"Part {cc:02}",
                        timecode=MenuTrack.format_duration(max(0, chapters[item]["end"] + 0.250)),
                    ))
                    cc += 1

        credits_offset = episode.get("creditsOffset", 0) or 0
        if credits_offset > 0:
            credits_sec = max(0, (credits_offset / 1000) if credits_offset > 10000 else credits_offset - 1)
            if cc == 1:
                chaps.append(MenuTrack(
                    number=2,
                    title="Credits",
                    timecode=MenuTrack.format_duration(credits_sec),
                ))
            else:
                chaps.append(MenuTrack(
                    number=cc,
                    title="Credits",
                    timecode=MenuTrack.format_duration(credits_sec),
                ))
        return chaps

    def certificate(self, **_):
        return self.config["certificate"]

    def license(self, challenge, track, session_id, **_):
        if not self.msl:
            raise self.log.exit(" - Cannot get license, MSL client has not been created yet.")
        header, payload_data = self.msl.send_message(
            endpoint=self.config["endpoints"]["licence"],
            params={
                "reqAttempt": 1,
                "reqName": "license",
            },
            application_data={
                "version": 2,
                "url": track.extra["license_url"],
                "id": int(time.time() * 10000),
                "esn": self.esn,
                "languages": ["en-US"],
                "uiVersion": self.react_context["serverDefs"]["data"]["uiVersion"],
                "clientVersion": "6.0026.291.011",
                "params": [{
                    "sessionId": base64.b64encode(session_id).decode("utf-8"),
                    "clientTime": int(time.time()),
                    "challengeBase64": base64.b64encode(challenge).decode("utf-8"),
                    "xid": str(int((int(time.time()) + 0.1612) * 1000)),
                }],
                "echo": "sessionId"
            },
            userauthdata=self.userauthdata
        )
        if not payload_data:
            raise self.log.exit(f" - Failed to get license: {header['message']} [{header['code']}]")
        if "error" in payload_data[0]:
            error = payload_data[0]["error"]
            error_display = error.get("display")
            error_detail = re.sub(r" \(E3-[^)]+\)", "", error.get("detail", ""))

            if error_display:
                self.log.critical(f" - {error_display}")
            if error_detail:
                self.log.critical(f" - {error_detail}")

            if not (error_display or error_detail):
                self.log.critical(f" - {error}")
            sys.exit(1)
        return payload_data[0]["licenseResponseBase64"]

    def configure(self):
        # Enhanced headers to bypass proxy detection
        user_agents = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        ]
        
        self.session.headers.update({
            "User-Agent": random.choice(user_agents),
            "Origin": "https://www.netflix.com",
            "Referer": "https://www.netflix.com/browse",
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9,ur;q=0.8,hi;q=0.7",
            "Accept-Encoding": "gzip, deflate, br",
            "DNT": "1",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "Sec-CH-UA": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
            "Sec-CH-UA-Mobile": "?0",
            "Sec-CH-UA-Platform": '"Windows"',
            "X-Netflix-Request-Routing": '{"path":"/pathEvaluator","requestId":"","sessionId":""}',
            "X-Netflix.clientType": "akira",
            "X-Netflix.uiVersion": "v8a4f1a9a",
        })
        
        # Clear MSL cache when using proxy to force fresh handshake
        if self.proxy_country:
            self.log.info(f"Using proxy for region: {self.proxy_country}")
            # Delete MSL cache to force new handshake through proxy
            cache_pattern = self.get_cache("msl_*.json")
            import glob
            cache_dir = os.path.dirname(cache_pattern)
            if os.path.exists(cache_dir):
                for cache_file in glob.glob(os.path.join(cache_dir, "msl_*.json")):
                    try:
                        os.remove(cache_file)
                        self.log.info(f"Cleared MSL cache: {os.path.basename(cache_file)}")
                    except:
                        pass
            time.sleep(random.uniform(0.5, 1.2))
        
        self.profiles = self.get_profiles()
        self.log.info("Initializing a Netflix MSL client")
        
        msl_cdm = self.cdm
        is_playready = False
        try:
            if self.cdm.device.__class__.__name__ == "PLAYREADY" or self.cdm.device.type == LocalDevice.Types.PLAYREADY:
                is_playready = True
        except Exception:
            is_playready = True
            
        if is_playready:
            from vinetrimmer.commands.dl import get_cdm
            from vinetrimmer.utils.widevine.cdm import Cdm
            # Load the Widevine CDM specifically for the MSL handshake
            msl_device = get_cdm("Netflix", profile=self.profile, cdm_name='Decrypt Labs - KeyXtractor API [widevine]')
            msl_cdm = Cdm(msl_device)

        self.react_context = self.get_react_context()

        if msl_cdm.device.type == LocalDevice.Types.CHROME:
            # Prefer Chrome ESN from react_context if available, otherwise generate a modern NFCDCH-02- ESN
            esn_candidate = self.react_context.get("esnAccessor", {}).get("data", {}).get("esn")
            if esn_candidate and esn_candidate.startswith("NFCDCH-"):
                self.esn = esn_candidate
            else:
                self.esn = chrome_esn_generator()
        else:
            esn_map = self.config.get("esn_map", {})
            self.esn = esn_map.get(msl_cdm.device.system_id) or esn_map.get(str(msl_cdm.device.system_id))
        if not self.esn:
            raise self.log.exit(" - No ESN specified")
        self.log.info(f" + ESN: {self.esn}")
        scheme = {
            LocalDevice.Types.CHROME: KeyExchangeSchemes.AsymmetricWrapped,
            LocalDevice.Types.ANDROID: KeyExchangeSchemes.Widevine
        }[msl_cdm.device.type]
        self.log.info(f" + Scheme: {scheme}")
        self.msl = MSL.handshake(
            scheme=scheme,
            session=self.session,
            endpoint=self.config["endpoints"]["manifest"],
            sender=self.esn,
            cdm=msl_cdm,
            msl_keys_path=self.get_cache("msl_{id}_{esn}_{scheme}.json".format(
                id=msl_cdm.device.system_id,
                esn=self.esn,
                scheme=scheme
            ))
        )
        if not self.session.cookies:
            raise self.log.exit(" - No cookies provided, cannot log in.")
        cookie_dict = self.session.cookies.get_dict()
        if "NetflixId" in cookie_dict and "SecureNetflixId" in cookie_dict:
            self.userauthdata = UserAuthentication.NetflixIDCookies(
                netflixid=self.session.cookies.get_dict()["NetflixId"],
                securenetflixid=self.session.cookies.get_dict()["SecureNetflixId"]
            )
        else:
            if not self.credentials:
                raise self.log.exit(" - Credentials are required for Android CDMs, and none were provided.")
            self.userauthdata = UserAuthentication.EmailPassword(
                email=self.credentials.username,
                password=self.credentials.password
            )


    def get_profiles(self):
        if self.range in ("HDR10", "DV", "DV+HDR") and self.vcodec not in ("H265", "VP9", "AV1"):
            self.vcodec = "H265"
        profiles = self.config["profiles"]["video"][self.vcodec]
        
        if self.range == "DV+HDR":
            hybrid_profiles = []
            if "HDR10" in profiles:
                hybrid_profiles.extend(profiles["HDR10"])
            if "DV" in profiles:
                for dv_list in profiles["DV"].values():
                    hybrid_profiles.extend(dv_list)
            return hybrid_profiles
            
        if self.range and self.range in profiles:
            return profiles[self.range]
        return profiles

    def duration(self, manifest, title):
        def _normalize(v):
            if v is None:
                return None
            try:
                v = float(v)
                return v / 1000.0 if v > 10000 else v 
            except Exception:
                return None

        for k in ("duration", "runtime", "movie_duration", "length",
                  "playbackDuration", "totalDuration", "presentation_duration"):
            val = manifest.get(k)
            sec = _normalize(val)
            if sec:
                return int(sec)

        for vt in manifest.get("video_tracks", []) or []:
            for k in ("duration", "presentationDuration", "length", "runtime"):
                sec = _normalize(vt.get(k))
                if sec:
                    return int(sec)
            for st in vt.get("streams", []) or []:
                for k in ("duration", "length", "runtime", "presentationDuration"):
                    sec = _normalize(st.get(k))
                    if sec:
                        return int(sec)

        try:
            meta = self.get_metadata(title.id)["video"]
        except Exception:
            meta = None

        if isinstance(meta, dict):
            for k in ("duration", "runtime", "runtimeInMs", "durationInMs", "runningTime", "runningTimeInMs"):
                sec = _normalize(meta.get(k))
                if sec:
                    return int(sec)

        return None

    def duration_tracks(self, tracks_obj, manifest, title):
        duration_s = self.duration(manifest, title)
        if not duration_s:
            return
        target = None
        if getattr(tracks_obj, "videos", None) and tracks_obj.videos:
            target = tracks_obj.videos[0]
        elif getattr(tracks_obj, "audios", None) and tracks_obj.audios:
            target = tracks_obj.videos[0]
        if target:
            if target.extra is None:
                target.extra = {}
            target.extra["duration"] = int(duration_s)

    def get_react_context(self):
        cache_loc = self.get_cache("web_data.json")

        if not os.path.isfile(cache_loc):
            src = self.session.get("https://www.netflix.com/browse").text
            match = re.search(r"netflix\.reactContext\s*=\s*({.*?});\s*</script>", src, re.DOTALL)
            if not match:
                raise self.log.exit(" - Failed to retrieve reactContext data, cookies might be outdated.")
            react_context_raw = match.group(1)
            react_context = json.loads(re.sub(r"\\x", r"\\u00", react_context_raw))["models"]
            react_context["requestHeaders"]["data"] = {
                re.sub(r"\B([A-Z])", r"-\1", k): str(v) for k, v in react_context["requestHeaders"]["data"].items()
            }
            react_context["abContext"]["data"]["headers"] = {
                k: str(v) for k, v in react_context["abContext"]["data"]["headers"].items()
            }
            react_context["requestHeaders"]["data"] = {
                k: str(v) for k, v in react_context["requestHeaders"]["data"].items()
            }
            os.makedirs(os.path.dirname(cache_loc), exist_ok=True)
            with open(cache_loc, "w", encoding="utf-8") as fd:
                fd.write(jsonpickle.encode(react_context))

            return react_context

        with open(cache_loc, encoding="utf-8") as fd:
            return jsonpickle.decode(fd.read())

    def get_metadata(self, title_id):
        # AGGRESSIVE: Always spoof Pakistan IP headers when using pk proxy, regardless of actual IP
        # Using ONLY real Pakistan residential IP ranges
        regional_ips = {
            "pk": [
                # Pakistan Telecom residential ranges
                "39.32.178.45", "39.33.215.78", "39.34.156.92",
                "103.11.220.67", "103.12.198.45", "103.13.156.89",
                "101.50.75.123", "101.51.88.156", "101.52.99.178",
                "119.73.98.234", "119.74.156.89", "119.75.178.123",
                "182.176.45.89", "182.177.98.156", "182.178.123.89",
                "182.191.123.45", "182.190.156.78", "182.189.178.92",
                # PTCL residential IPs
                "58.65.178.45", "58.66.198.89", "58.67.156.123",
                "110.93.178.45", "110.94.156.89", "110.95.198.123"
            ],
            "bn": ["27.96.45.78", "103.92.123.89", "202.51.195.123", "118.67.195.67", "45.64.123.89"],
            "in": ["106.51.123.89", "117.239.67.123", "49.205.89.123", "103.77.165.89", "152.58.123.89"],
            "us": ["24.123.89.45", "208.67.222.222", "8.8.8.8"]
        }
        
        if hasattr(self, 'proxy_country') and self.proxy_country in regional_ips:
            # Use multiple IPs to appear as different users
            fake_ip = regional_ips[self.proxy_country][random.randint(0, len(regional_ips[self.proxy_country])-1)]
            
            # CRITICAL: Set Accept-Language to match region
            region_languages = {
                "pk": "ur-PK,ur;q=0.9,en-US;q=0.8,en;q=0.7",
                "bn": "bn-BD,bn;q=0.9,en-US;q=0.8,en;q=0.7",
                "in": "hi-IN,hi;q=0.9,en-US;q=0.8,en;q=0.7",
                "us": "en-US,en;q=0.9"
            }
            
            # Aggressive header spoofing - FORCE Pakistan headers
            headers_to_add = {
                "X-Forwarded-For": fake_ip,
                "CF-Connecting-IP": fake_ip,
                "True-Client-IP": fake_ip,
                "X-Real-IP": fake_ip,
                "X-Client-IP": fake_ip,
                "Forwarded": f"for={fake_ip}",
                "X-ProxyUser-Ip": fake_ip,
                "Via": f"1.1 {fake_ip}",
                "Accept-Language": region_languages.get(self.proxy_country, "en-US,en;q=0.9"),
                # Add timezone hint for region
                "X-Timezone": "Asia/Karachi" if self.proxy_country == "pk" else "UTC",
            }
            self.session.headers.update(headers_to_add)
            self.log.info(f" + Spoofing Pakistan IP: {fake_ip}")
            # Add random delay to avoid timing patterns
            time.sleep(random.uniform(0.2, 0.7))
        
        # Add jitter to metadata requests
        time.sleep(random.uniform(0.1, 0.3))
        
        try:
            metadata = self.session.get(
                self.config["endpoints"]["metadata"].format(build_id="release"),
                params={
                    "movieid": title_id,
                    "drmSystem": self.config["configuration"]["drm_system"],
                    "isWatchlistEnabled": False,
                    "isShortformEnabled": False,
                    "languages": self.meta_lang
                }
            ).json()
        except requests.HTTPError as e:
            if e.response.status_code == 500:
                self.log.warning(
                    " - Recieved a HTTP 500 error while getting metadata, deleting cached reactContext data"
                )
                if os.path.isfile(self.get_cache("web_data.json")):
                    os.unlink(self.get_cache("web_data.json"))
                return self.get_metadata(title_id)
            raise
        except json.JSONDecodeError:
            raise self.log.exit(" - Failed to get metadata, title might not be available in your region.")
        else:
            if isinstance(metadata, dict) and "video" not in metadata:
                self.log.warning(f" - Metadata response: {metadata}")
            if "status" in metadata and metadata["status"] == "error":
                raise self.log.exit(
                    f" - Failed to get metadata, cookies might be expired. ({metadata['message']})"
                )
            return metadata

    def get_manifest(self, title, video_profiles):
        if isinstance(video_profiles, dict):
            def _flatten_dict(d):
                for v in d.values():
                    if isinstance(v, dict):
                        yield from _flatten_dict(v)
                    elif isinstance(v, list):
                        yield from v
                    else:
                        yield v
            video_profiles = list(_flatten_dict(video_profiles))
        if self.quality == 720:
            video_profiles = [x for x in video_profiles if "l40" not in x]
        audio_profiles = self.config["profiles"]["audio"]
        if self.acodec:
            audio_profiles = audio_profiles[self.acodec]
        if isinstance(audio_profiles, dict):
            audio_profiles = list(audio_profiles.values())
        

        profiles = sorted(set(flatten(as_list(
            self.config["profiles"]["video"]["H264"]["BPL"],
            video_profiles,
            audio_profiles,
            self.config["profiles"]["subtitles"],
        ))))
        
        self.profiles_global = profiles
        self.log.debug("Profiles:\n\t" + "\n\t".join(profiles))
        
        # Regional language hints when using proxy
        regional_langs = {
            "pk": ["ur-PK", "en-US", "hi-IN"],
            "bn": ["bn-BD", "en-US", "hi-IN"],
            "in": ["hi-IN", "en-US", "te-IN", "ta-IN", "kn-IN", "ml-IN"],
            "br": ["pt-BR", "en-US"],
            "us": ["en-US"]
        }
        manifest_languages = ["en-US"]
        if hasattr(self, 'proxy_country') and self.proxy_country in regional_langs:
            manifest_languages = regional_langs[self.proxy_country]
            self.log.info(f" + Using region-specific languages: {manifest_languages}")
        
        # Add longer jitter before manifest call when using proxy - simulate user browsing
        if hasattr(self, 'proxy_country') and self.proxy_country:
            time.sleep(random.uniform(0.8, 2.0))

        if self.drm_system == "playready":
            params = {}
            if self.cdm.device.type == LocalDevice.Types.CHROME:
                params = {
                    "reqAttempt": 1,
                    "reqPriority": 10,
                    "reqName": "manifest",
                    "clienttype": self.react_context["playerModel"]["data"]["config"]["ui"]["initParams"]["uimode"],
                    "uiversion": self.react_context["serverDefs"]["data"]["BUILD_IDENTIFIER"],
                }
            
            # Add random jitter before manifest request
            if self.proxy_country:
                time.sleep(random.uniform(0.2, 0.6))

            _, payload_chunks = self.msl.send_message(
                endpoint=self.config["endpoints"]["manifest"],
                params=params,
                application_data={
                    "version": 2,
                    "url": "/manifest",
                    "id": int(time.time() * 1000) + random.randint(1, 99999),
                    "esn": self.esn,
                    "languages": manifest_languages,
                    "uiVersion": self.react_context["playerModel"]["data"]["config"]["ui"]["initParams"]["uiVersion"],
                    "clientVersion": "6.0026.291.011",
                    "params": {
                        "type": "live" if self.is_live else "standard", 
                        "viewableId": title.service_data.get("episodeId", title.service_data["id"]),
                        "profiles": profiles,
                        "flavor": "PRO", 
                        "drmType": "playready",
                        "deviceSecurityLevel": "3000",
                        "drmVersion": "30",
                        "usePsshBox": True,
                        "isBranching": False, 
                        "useHttpsStreams": True,
                        "supportsLiveStreaming": self.is_live,
                        "supportsUnequalizedDownloadables": True, 
                        "imageSubtitleHeight": 2160,
                        "uiVersion": self.react_context["playerModel"]["data"]["config"]["ui"]["initParams"]["uiVersion"],
                        "uiPlatform": self.react_context["playerModel"]["data"]["config"]["ui"]["initParams"]["uiPlatform"],
                        "clientVersion": "6.0026.291.011",
                        "supportsPreReleasePin": True,
                        "supportsWatermark": True,
                        "showAllDubTracks": True, 
                        "showAllSubDubTracks": True,
                        "requestSegmentVmaf": True,
                        "forceProfiles": True,
                        "requestEligibleABTests": True,
                        "maximumBitrate": 30000,
                        "preferredBitrate": 30000,
                        "desiredVmaf": 100,
                        "desiredComplexity": "high",
                        "supportsPartialHydration": self.hydrate, 
                        "videoOutputInfo": [{
                            "type": "DigitalVideoOutputDescriptor",
                            "outputType": "HDMI",
                            "supportedHdcpVersions": ["2.2", "2.3"],
                            "isHdcpEngaged": True,
                            "displayWidth": 3840,
                            "displayHeight": 2160,
                            "maxBitrate": 30000
                        }],
                        "titleSpecificData": {
                            title.service_data.get("episodeId", title.service_data["id"]): {
                                "unletterboxed": True,
                                "requestHighBitrate": True,
                                "preferHighestQuality": True
                            }
                        },
                        "preferAssistiveAudio": False,
                        "isUIAutoPlay": False,
                        "isNonMember": False,
                        "challenge": self.config["payload_challenge_pr"],
                    }
                },
                userauthdata=self.userauthdata
            )
            if "errorDetails" in payload_chunks:
                error_msg = payload_chunks['errorDetails']
                # Check if it's a geo-restriction error
                if "playapi-410" in str(error_msg).lower() or "availability window" in str(error_msg).lower():
                    self.log.warning(f" - Geo-restriction detected: {error_msg}")
                    self.log.warning(" - This content may not be available in the proxy region.")
                    self.log.warning(" - Netflix has detected the proxy/VPN. Consider using:")
                    self.log.warning("   1. A residential proxy instead of datacenter IP")
                    self.log.warning("   2. Downloading without proxy if content is available in your region")
                raise Exception(f"Manifest call failed: {error_msg}")
            return payload_chunks
            
        elif self.drm_system == "widevine":
            params = {}
            if self.cdm.device.type == LocalDevice.Types.CHROME:
                params = {
                    "reqAttempt": 1,
                    "reqPriority": 10,
                    "reqName": "manifest",
                    "clienttype": self.react_context["playerModel"]["data"]["config"]["ui"]["initParams"]["uimode"],
                    "uiversion": self.react_context["serverDefs"]["data"]["BUILD_IDENTIFIER"],
                }
            
            # Add random jitter before manifest request
            if self.proxy_country:
                time.sleep(random.uniform(0.2, 0.6))

            _, payload_chunks = self.msl.send_message(
                endpoint=self.config["endpoints"]["manifest"],
                params=params,
                application_data={
                    "version": 2,
                    "url": "/manifest",
                    "id": int(time.time() * 1000) + random.randint(1, 99999),
                    "esn": self.esn,
                    "languages": manifest_languages,
                    "uiVersion": self.react_context["playerModel"]["data"]["config"]["ui"]["initParams"]["uiVersion"],
                    "clientVersion": "6.0026.291.011",
                    "params": {
                        "type": "live" if self.is_live else "standard",
                        "viewableId": title.service_data.get("episodeId", title.service_data["id"]),
                        "profiles": profiles,
                        "flavor": "STANDARD",
                        "drmType": "widevine",
                        "drmVersion": "25",
                        "usePsshBox": True,
                        "isBranching": False, 
                        "useHttpsStreams": True,
                        "supportsLiveStreaming": self.is_live,
                        "supportsUnequalizedDownloadables": True,
                        "imageSubtitleHeight": 1080,
                        "uiVersion": self.react_context["playerModel"]["data"]["config"]["ui"]["initParams"]["uiVersion"],
                        "uiPlatform": self.react_context["playerModel"]["data"]["config"]["ui"]["initParams"]["uiPlatform"],
                        "clientVersion": "6.0026.291.011",
                        "supportsPreReleasePin": True, 
                        "supportsWatermark": True, 
                        "showAllDubTracks": True, 
                        "showAllSubDubTracks": True,
                        "supportsPartialHydration": self.hydrate, 
                        "videoOutputInfo": [{
                            "type": "DigitalVideoOutputDescriptor",
                            "outputType": "unknown",
                            "supportedHdcpVersions": self.config["configuration"]["supported_hdcp_versions"],
                            "isHdcpEngaged": self.config["configuration"]["is_hdcp_engaged"]
                        }],
                        "titleSpecificData": {
                            title.service_data.get("episodeId", title.service_data["id"]): {"unletterboxed": True}
                        },
                        "preferAssistiveAudio": False,
                        "isUIAutoPlay": False,
                        "isNonMember": False,
                        "challenge": self.config["payload_challenge"],
                    }
                },
                userauthdata=self.userauthdata
            )
            if "errorDetails" in payload_chunks:
                error_msg = payload_chunks['errorDetails']
                # Check if it's a geo-restriction error
                if "playapi-410" in str(error_msg).lower() or "availability window" in str(error_msg).lower():
                    self.log.warning(f" - Geo-restriction detected: {error_msg}")
                    self.log.warning(" - This content may not be available in the proxy region.")
                    self.log.warning(" - Netflix has detected the proxy/VPN. Consider using:")
                    self.log.warning("   1. A residential proxy instead of datacenter IP")
                    self.log.warning("   2. Downloading without proxy if content is available in your region")
                raise Exception(f"Manifest call failed: {error_msg}")
            return payload_chunks
        
    def manifest_as_tracks(self, manifest):
        audio_tracks_raw = manifest.get("audio_tracks", [])
        audio_tracks = []
        for t in audio_tracks_raw:
            if "streams" in t and t["streams"]:
                for d in t["streams"]:
                    combined = t.copy()
                    combined.update(d)
                    audio_tracks.append(combined)
            else:
                audio_tracks.append(t)
        manifest["audio_tracks"] = audio_tracks

        videos = []
        for vt in manifest.get("video_tracks", []):
            for stream in vt.get("streams", []):
                if not stream.get("urls") or not isinstance(stream["urls"], list) or "url" not in stream["urls"][0]: 
                    continue
                
                kid = stream.get("drmHeaderId") if stream.get("isDrm") and self.drm_system == "widevine" else None
                pssh = None
                if stream.get("isDrm") and stream.get("drmHeaderId"):
                    try:
                        pssh = Box.parse(Box.build(dict(
                            type=b"pssh", version=0, flags=0, system_ID=Cdm.uuid,
                            init_data=b"\x12\x10" + UUID(hex=stream["drmHeaderId"]).bytes
                        )))
                    except Exception:
                        pass

                videos.append(VideoTrack(
                    id_=stream.get("downloadable_id", stream.get("new_track_id", "vid_unknown")),
                    source=self.ALIASES[0],
                    url=stream["urls"][0]["url"],
                    codec=stream.get("content_profile", ""),
                    bitrate=stream.get("bitrate", 0) * 1000,
                    size=stream.get("size", 0),
                    width=stream.get("res_w", 0),
                    height=stream.get("res_h", 0),
                    fps=(float(stream["framerate_value"]) / stream["framerate_scale"]) if "framerate_value" in stream and "framerate_scale" in stream else None,
                    needs_proxy=self.download_proxied,
                    needs_repack=False,
                    encrypted=stream.get("isDrm", False),
                    pssh=pssh,
                    kid=kid
                ))

        audios = []
        for x in manifest.get("audio_tracks", []):
            if not x.get("urls") or not isinstance(x["urls"], list) or "url" not in x["urls"][0]: 
                continue
            
            kid = x.get("drmHeaderId") if x.get("isDrm") else None
            pssh = None
            if x.get("isDrm") and x.get("drmHeader") and "bytes" in x["drmHeader"]:
                try:
                    pssh = Box.parse(base64.b64decode(x["drmHeader"]["bytes"]))
                except Exception:
                    pass

            audios.append(AudioTrack(
                id_=x.get("downloadable_id", x.get("new_track_id", "aud_unknown")),
                source=self.ALIASES[0],
                url=x["urls"][0]["url"],
                codec=x.get("content_profile", ""),
                language=self.NF_LANG_MAP.get(x.get("language"), x.get("language", "und")),
                bitrate=x.get("bitrate", 0) * 1000,
                size=x.get("size", 0),
                channels=x.get("channels", 2),
                descriptive=x.get("rawTrackType", "").lower() == "assistive",
                needs_proxy=self.download_proxied,
                needs_repack=False,
                encrypted=x.get("isDrm", False),
                pssh=pssh,
                kid=kid
            ))

        texts = []
        for x in manifest.get("timedtexttracks", []):
            if x.get("isNoneTrack"): 
                continue

            url = None
            codec = None
            if x.get("ttDownloadables"):
                for dl_codec, dl_data in x["ttDownloadables"].items():
                    if dl_data and isinstance(dl_data, dict) and dl_data.get("downloadUrls"):
                        urls_dict = dl_data["downloadUrls"]
                        if urls_dict:
                            url_list = list(urls_dict.values())
                            if url_list:
                                url = url_list[0]
                                codec = dl_codec
                                break
            
            if not url: 
                continue 
            
            track_id = x.get("new_track_id", "txt_unknown")
            if x.get("downloadableIds") and isinstance(x["downloadableIds"], dict):
                ids_values = list(x["downloadableIds"].values())
                if ids_values:
                    track_id = ids_values[0]
            elif x.get("downloadable_id"):
                track_id = x.get("downloadable_id")

            texts.append(TextTrack(
                id_=track_id,
                source=self.ALIASES[0],
                url=url,
                codec=codec,
                language=self.NF_LANG_MAP.get(x.get("language"), x.get("language", "und")),
                forced=x.get("isForcedNarrative", False),
                needs_proxy=self.download_proxied,
                cc=x.get("trackVariant"),
                sdh=x.get("rawTrackType", "") == "closedcaptions"
            ))

        return Tracks(videos, audios, texts)

    @staticmethod
    def get_original_language(manifest):
        for language in manifest.get("audio_tracks", []):
            if language.get("languageDescription", "").endswith(" [Original]"):
                return Language.get(language.get("language", "en"))
        
        default_track = manifest.get("defaultTrackOrderList", [])
        if default_track and "mediaId" in default_track[0]:
            try:
                return Language.get(default_track[0]["mediaId"].split(";")[2])
            except IndexError:
                pass
        return Language.get("en")
