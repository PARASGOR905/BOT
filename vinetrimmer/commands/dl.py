import base64
import html
import json
import logging
import os
import shutil
import subprocess
import sys
import traceback
from zlib import crc32
from pathlib import Path
from datetime import datetime, timedelta
from http.cookiejar import MozillaCookieJar
import random
import time
import click
import requests
from appdirs import AppDirs
from langcodes import Language
from pymediainfo import MediaInfo
from Crypto.Random import get_random_bytes
import math
from vinetrimmer import services
from vinetrimmer.config import Config, config, credentials, directories, filenames
from vinetrimmer.objects import AudioTrack, Credential, TextTrack, Title, Titles, VideoTrack, Track
from vinetrimmer.objects.vaults import InsertResult, Vault, Vaults
from vinetrimmer.utils import Cdm, is_close_match
from vinetrimmer.utils.click import (AliasedGroup, ContextData, acodec_param, language_param, quality_param,
                                     range_param, vcodec_param, wanted_param, channels_param)
from vinetrimmer.utils.collections import as_list, merge_dict
from vinetrimmer.utils.io import load_yaml
from vinetrimmer.utils.widevine.device import LocalDevice, RemoteDevice
from pyplayready.cdm import Cdm as Cdm_PR
from pyplayready.device import Device as Device_PR
from pyplayready.system.pssh import PSSH as PSSH_PR
from pyplayready.crypto.ecc_key import ECCKey
from pyplayready.system.bcert import CertificateChain, Certificate
from vinetrimmer.vendor.pymp4.parser import Box
from construct import Container
from pyplayready.crypto.ecc_key import ECCKey
from pyplayready.system.bcert import CertificateChain, Certificate
from Crypto.Random import get_random_bytes
from pathlib import Path
import time

def reprovision_device(prd_path: Path) -> None:
    """
    Reprovision a Playready Device (.prd) by creating a new leaf certificate and new encryption/signing keys.
    Will override the device if an output path or directory is not specified

    Only works on PRD Devices of v3 or higher
    """
    if not prd_path.is_file():
        raise Exception("prd_path: Not a path to a file, or it doesn't exist.")

    device = Device_PR.load(prd_path)

    if device.group_key is None:
        raise Exception("Device does not support reprovisioning, re-create it or use a Device with a version of 3 or higher")

    device.group_certificate.remove(0)

    encryption_key = ECCKey.generate()
    signing_key = ECCKey.generate()

    device.encryption_key = encryption_key
    device.signing_key = signing_key

    new_certificate = Certificate.new_leaf_cert(
        cert_id=get_random_bytes(16),
        security_level=device.group_certificate.get_security_level(),
        client_id=get_random_bytes(16),
        signing_key=signing_key,
        encryption_key=encryption_key,
        group_key=device.group_key,
        parent=device.group_certificate
    )
    device.group_certificate.prepend(new_certificate)

    prd_path.parent.mkdir(parents=True, exist_ok=True)
    prd_path.write_bytes(device.dumps())

def create_device(device_dir, force_refresh=False) -> None:
    """Create a Playready Device (.prd) file from an ECC private group key and group certificate chain"""
    group_key = device_dir / 'zgpriv.dat'
    group_certificate = device_dir / 'bgroupcert.dat'
    infofile = device_dir / 'PR.json'

    if not group_key.is_file():
        raise TypeError("group_key: Not a path to a file, or it doesn't exist.")
    if not group_certificate.is_file():
        raise TypeError("group_certificate: Not a path to a file, or it doesn't exist.")
    
    if force_refresh:
        if infofile.is_file():
            infofile.unlink()
        device_files = list(device_dir.glob("*.prd"))
        for device_file in device_files:
            device_file.unlink()
        logging.warning(" + Forcing device refresh...")
    elif infofile.is_file():
        with open(infofile, 'r') as file:
            info = json.loads(file.read())
        if "device" in info:
            device_prd = Path(device_dir / info["device"])
            if device_prd.is_file():
                logging.info(" + Loading existing device: %s", info["device"])
                return device_prd
        else:
            infofile.unlink()
            logging.warning(" + Invalid info file, refreshing device...")

    encryption_key = ECCKey.generate()
    signing_key = ECCKey.generate()

    group_key = ECCKey.load(group_key)
    certificate_chain = CertificateChain.load(group_certificate)

    new_certificate = Certificate.new_leaf_cert(
        cert_id=get_random_bytes(16),
        security_level=certificate_chain.get_security_level(),
        client_id=get_random_bytes(16),
        signing_key=signing_key,
        encryption_key=encryption_key,
        group_key=group_key,
        parent=certificate_chain
    )
    certificate_chain.prepend(new_certificate)

    device = Device_PR(
        group_key=group_key.dumps(),
        encryption_key=encryption_key.dumps(),
        signing_key=signing_key.dumps(),
        group_certificate=certificate_chain.dumps(),
    )
    
    expiry = (datetime.now() + timedelta(days=3650)).isoformat()
    prd_bin = device.dumps()
    out_path = device_dir / f"{device.get_name()}_{crc32(prd_bin).to_bytes(4, 'big').hex()}.prd"

    if out_path.exists():
        with open(infofile, 'w') as file:
            json.dump({
                "expiry": expiry, 
                "device": out_path.name, 
                "SecurityLevel": device.security_level,
                "created": datetime.now().isoformat()
            }, file)
        return out_path

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(device.dumps())

    logging.info(" + Created Playready Device (.prd) file, %s", out_path.name)
    logging.info(" + Security Level: %s", device.security_level)
    logging.info(" + Device expiry: %s (10 years from now)", expiry)

    with open(infofile, 'w') as file:
        json.dump({
            "expiry": expiry, 
            "device": out_path.name, 
            "SecurityLevel": device.security_level,
            "created": datetime.now().isoformat()
        }, file)

    return out_path

def get_cdm(service, profile=None, cdm_name=None):
    """
    Get CDM Device (either remote or local) for a specified service.
    Raises a ValueError if there's a problem getting a CDM.
    """
    if not cdm_name:
        cdm_name = next((v for k, v in config.cdm.items() if k.lower() == service.lower()), None) or config.cdm.get("default")
        if not cdm_name and service.lower() in ("cr", "crunchyroll") and not get_credentials(service, profile or "default"):
            cdm_name = next((x["name"] for x in config.cdm_api if x.get("type") == "CHROME"), None)
    if not cdm_name:
        raise ValueError("A CDM to use wasn't listed in the vinetrimmer.yml config")
    if isinstance(cdm_name, dict):
        if not profile:
            raise ValueError("CDM config is mapped for profiles, but no profile was chosen")
        cdm_name = cdm_name.get(profile) or config.cdm.get("default")
        if not cdm_name:
            raise ValueError(f"A CDM to use was not mapped for the profile {profile}")
    
    try:
        device_dir = Path(directories.devices) / cdm_name
        if not device_dir.is_dir():
            for d in Path(directories.devices).iterdir():
                if d.is_dir() and d.name.lower() == cdm_name.lower():
                    device_dir = d
                    break
        
        is_amazon_us = service.lower() in ["AMZN", "amazon"]
        is_sl3000 = "sl3000" in cdm_name.lower() or "hisense" in cdm_name.lower()
        
        if device_dir.is_dir() and (device_dir / 'zgpriv.dat').is_file() and (device_dir / 'bgroupcert.dat').is_file():
            if is_amazon_us and is_sl3000:
                # Look for existing .prd device
                prd_files = list(device_dir.glob("*.prd"))
                if prd_files:
                    # Use the most recent
                    latest_prd = max(prd_files, key=lambda x: x.stat().st_mtime)
                    # Check if reprovisioning is needed (after ~2 days)
                    if (int(time.time()) - int(os.path.getmtime(latest_prd))) > 172800:  # 2 days in seconds
                        try:
                            reprovision_device(latest_prd)
                            logging.info(f" + Reprovisioned Playready Device (.prd) file, {latest_prd.name}")
                        except Exception as e:
                            logging.warning(f"Reprovision Failed - {e}")
                    logging.info(f" + Using existing PlayReady device for Amazon US: {latest_prd.name}")
                    return Device_PR.load(latest_prd)
            
            device_path = create_device(device_dir)
            return Device_PR.load(device_path)
        
        # Check if it's a direct path to a PlayReady device file
        if "sl2000" in cdm_name.lower() or "sl3000" in cdm_name.lower():
            prd_path = os.path.join(directories.devices, f"{cdm_name}.prd")
            if not os.path.exists(prd_path):
                matches = list(Path(directories.devices).rglob(f"*{cdm_name}*.prd"))
                if matches:
                    prd_path = str(matches[0])
            if os.path.exists(prd_path):
                # Check if reprovisioning is needed (after ~2 days)
                if (int(time.time()) - int(os.path.getmtime(prd_path))) > 172800:  # 2 days in seconds
                    try:
                        reprovision_device(Path(prd_path))
                        logging.info(f" + Reprovisioned Playready Device (.prd) file, {cdm_name}")
                    except Exception as e:
                        logging.warning(f"Reprovision Failed - {e}")
                return Device_PR.load(prd_path)
            else:
                # If it doesn't exist, create a new one
                if device_dir.is_dir():
                    device_path = create_device(device_dir)
                    return Device_PR.load(device_path)
        else:
            # Otherwise, treat as a Widevine device
            wvd_path = os.path.join(directories.devices, f"{cdm_name}.wvd")
            if not os.path.exists(wvd_path):
                matches = list(Path(directories.devices).rglob(f"*{cdm_name}*.wvd"))
                if matches:
                    wvd_path = str(matches[0])
            return LocalDevice.load(wvd_path)
    
    except FileNotFoundError:
        dirs = [
            os.path.join(directories.devices, cdm_name),
            os.path.join(AppDirs("pywidevine", False).user_data_dir, "devices", cdm_name),
            os.path.join(AppDirs("pywidevine", False).site_data_dir, "devices", cdm_name),
        ]

        for d in dirs:
            try:
                # Try to treat as a Widevine device directory
                return LocalDevice.from_dir(d)
            except FileNotFoundError:
                pass

        cdm_api = next(iter(x for x in config.cdm_api if x["name"] == cdm_name), None)
        if cdm_api:
            if cdm_api.get("type") == "PLAYREADY":
                from vinetrimmer.utils.widevine.device import CRDRemoteDevice
                return CRDRemoteDevice(**cdm_api)
            return RemoteDevice(**cdm_api)

        raise ValueError(f"Device {cdm_name!r} not found")

def get_service_config(service):
    """Get both service config and service secrets as one merged dictionary."""
    service_config = load_yaml(filenames.service_config.format(service=service.lower()))

    user_config = (load_yaml(filenames.user_service_config.format(service=service.lower()))
                   or load_yaml(filenames.user_service_config.format(service=service)))

    if user_config:
        merge_dict(service_config, user_config)

    return service_config


def get_profile(service):
    """
    Get the default profile for a service from the config.
    """
    profile = config.profiles.get(service)
    if profile is False:
        return None  # auth-less service if `false` in config
    if not profile:
        profile = config.profiles.get("default")
    if not profile:
        raise ValueError(f"No profile has been defined for '{service}' in the config.")

    return profile


def get_cookie_jar(service, profile):
    """Get the profile's cookies if available."""
    cookie_file = os.path.join(directories.cookies, service.lower(), f"{profile}.txt")
    if not os.path.isfile(cookie_file):
        cookie_file = os.path.join(directories.cookies, service, f"{profile}.txt")
    if not os.path.isfile(cookie_file):
        aliases = []
        for key, service_aliases in services.SERVICE_MAP.items():
            if key.lower() == service.lower():
                aliases = list(service_aliases)
                break
        if "nowtv" in service.lower() and "nowtv" not in [a.lower() for a in aliases]:
            aliases.append("nowtv")
        
        candidates = {service.lower()} | {a.lower() for a in aliases}
        if os.path.isdir(directories.cookies):
            for entry in os.listdir(directories.cookies):
                if entry.lower() in candidates:
                    path = os.path.join(directories.cookies, entry, f"{profile}.txt")
                    if os.path.isfile(path):
                        cookie_file = path
                        break
    if os.path.isfile(cookie_file):
        cookie_jar = MozillaCookieJar(cookie_file)
        with open(cookie_file, "r+", encoding="utf-8") as fd:
            unescaped = html.unescape(fd.read())
            fd.seek(0)
            fd.truncate()
            fd.write(unescaped)
        cookie_jar.load(ignore_discard=True, ignore_expires=True)
        return cookie_jar
    return None


def get_credentials(service, profile="default"):
    """Get the profile's credentials if available."""
    cred = credentials.get(service, {})

    if isinstance(cred, dict):
        cred = cred.get(profile)
    elif profile != "default":
        return None

    if cred:
        if isinstance(cred, list):
            return Credential(*cred)
        else:
            return Credential.loads(cred)


@click.group(name="dl", short_help="Download from a service.", cls=AliasedGroup, context_settings=dict(
    help_option_names=["-?", "-h", "--help"],
    max_content_width=116,  # max PEP8 line-width, -4 to adjust for initial indent
    default_map=config.arguments
))
@click.option("--debug", is_flag=True, hidden=True)  # Handled by vinetrimmer.py
@click.option("-p", "--profile", type=str, default=None,
              help="Profile to use when multiple profiles are defined for a service.")
@click.option("-q", "--quality", callback=quality_param, default=None,
              help="Download Resolution, defaults to best available.")
@click.option("-v", "--vcodec", callback=vcodec_param, default="H264",
              help="Video Codec, defaults to H264.")
@click.option("-a", "--acodec", callback=acodec_param, default=None,
              help="Audio Codec")
@click.option("-vb", "--vbitrate", "vbitrate", type=int,
              default=None,
              help="Video Bitrate, defaults to Max.")
@click.option("-ab", "--abitrate", "abitrate", type=int,
              default=None,
              help="Audio Bitrate, defaults to Max.")              
@click.option("-aa", "--atmos", is_flag=True, default=False,
              help="Prefer Atmos Audio")
@click.option("-ch", "--channels", help="Audio Channels")
@click.option("-r", "--range", "range_", callback=range_param, default="SDR",
              help="Video Color Range, defaults to SDR.")
@click.option("-w", "--wanted", callback=wanted_param, default=None,
              help="Wanted episodes, e.g. `S01-S05,S07`, `S01E01-S02E03`, `S02-S02E03`, e.t.c, defaults to all.")
@click.option("-al", "--alang", callback=language_param, default="orig",
              help="Language wanted for audio.")
@click.option("-sl", "--slang", callback=language_param, default="all",
              help="Language wanted for subtitles.")
@click.option("--delay", type=int, default=None,
              help="Delay between title processing")
@click.option("--proxy", type=str, default=None,
              help="Proxy URI to use. If a 2-letter country is provided, it will try get a proxy from the config.")
@click.option("-A", "--audio-only", is_flag=True, default=False,
              help="Only download audio tracks.")
@click.option("-S", "--subs-only", is_flag=True, default=False,
              help="Only download subtitle tracks.")
@click.option("-C", "--chapters-only", is_flag=True, default=False,
              help="Only download chapters.")
@click.option("-ns", "--no-subs", is_flag=True, default=False,
              help="Do not download subtitle tracks.")
@click.option("-na", "--no-audio", is_flag=True, default=False,
              help="Do not download audio tracks.")
@click.option("-nv", "--no-video", is_flag=True, default=False,
              help="Do not download video tracks.")
@click.option("-nc", "--no-chapters", is_flag=True, default=False,
              help="Do not download chapters tracks.")
@click.option("-ad", "--audio-description", is_flag=True, default=False,
              help="Download audio description tracks.")
@click.option("--list", "list_", is_flag=True, default=False,
              help="Skip downloading and list available tracks and what tracks would have been downloaded.")
@click.option("--selected", is_flag=True, default=False,
              help="List selected tracks and what tracks are downloaded.")
@click.option("--cdm", type=str, default=None,
              help="Override the CDM that will be used for decryption.")
@click.option("--keys", is_flag=True, default=False,
              help="Skip downloading, retrieve the decryption keys (via CDM or Key Vaults) and print them.")
@click.option("--cache", is_flag=True, default=False,
              help="Disable the use of the CDM and only retrieve decryption keys from Key Vaults. "
                   "If a needed key is unable to be retrieved from any Key Vaults, the title is skipped.")
@click.option("--no-cache", is_flag=True, default=False,
              help="Disable the use of Key Vaults and only retrieve decryption keys from the CDM.")
@click.option("--no-proxy", is_flag=True, default=False,
              help="Force disable all proxy use.")
@click.option("--force-proxy", is_flag=True, default=False,
              help="Force using proxy even if current region matches.")
@click.option("-nm", "--no-mux", is_flag=True, default=False,
              help="Do not mux the downloaded and decrypted tracks.")
@click.option("--mux", is_flag=True, default=False,
              help="Force muxing when using --audio-only/--subs-only/--chapters-only.")
@click.option("--worst", is_flag=True, default=False,
              help="Choose the worst available video tracks rather than the best")
@click.option("-nf", "--no-forced", is_flag=True, default=False,
              help="Do not download forced subtitle tracks.")
@click.pass_context
def dl(ctx, profile, cdm, *_, **__):
    log = logging.getLogger("dl")

    service = ctx.params.get("service_name") or services.get_service_key(ctx.invoked_subcommand)
    if not service:
        log.exit(" - Unable to find service")

    profile = profile or get_profile(service)
    service_config = get_service_config(service)
    vaults = []
    for vault in config.key_vaults:
        try:
            vaults.append(Config.load_vault(vault))
        except Exception as e:
            log.error(f" - Failed to load vault {vault['name']!r}: {e}")
    vaults = Vaults(vaults, service=service)
    local_vaults = sum(v.type == Vault.Types.LOCAL for v in vaults)
    remote_vaults = sum(v.type == Vault.Types.REMOTE for v in vaults)
    http_vaults = sum(v.type == Vault.Types.HTTP for v in vaults)
    log.info(f" + {local_vaults} Local Vault{'' if local_vaults == 1 else 's'}")
    log.info(f" + {remote_vaults} Remote Vault{'' if remote_vaults == 1 else 's'}")
    log.info(f" + {http_vaults} HTTP Vault{'' if http_vaults == 1 else 's'}")

    try:
        device = get_cdm(service, profile, cdm)
    except ValueError as e:
        raise log.exit(f" - {e}")
    
    # Add this check after device is loaded
    if hasattr(device, 'type') and device.type == LocalDevice.Types.PLAYREADY:
        # For PlayReady devices, check if reprovisioning is needed on every run
        if hasattr(device, '_path') and device._path:  # Check if device has a file path
            prd_path = Path(device._path)
            if prd_path.exists():
                if (int(time.time()) - int(os.path.getmtime(prd_path))) > 172800:  # 2 days
                    try:
                        reprovision_device(prd_path)
                        log.info(f" + Reprovisioned Playready Device (.prd) file")
                        # Reload the device after reprovisioning
                        device = Device_PR.load(prd_path)
                    except Exception as e:
                        log.warning(f"Reprovision Failed - {e}")
    
    device_name = ""
    if isinstance(device, Device_PR):
        device_name = device.get_name()
    elif isinstance(device, (LocalDevice, RemoteDevice)):
        device_name = str(device.system_id)
    else:
        device_name = "Unknown Device"
    log.info(f" + Loaded {device.__class__.__name__}: {device_name} (L{device.security_level})")
    if isinstance(device, Device_PR):
        cdm = Cdm_PR.from_device(device)
        cdm.device = device
        if not hasattr(device, "type"):
            device.type = LocalDevice.Types.PLAYREADY
    else:
        cdm = Cdm(device)

    if profile:
        cookies = get_cookie_jar(service, profile)
        credentials = get_credentials(service, profile)
        if not cookies and not credentials and service_config.get("needs_auth", True):
            raise log.exit(f" - Profile {profile!r} has no cookies or credentials")
    else:
        cookies = get_cookie_jar(service, "default")
        credentials = get_credentials(service, "default")
        if not cookies and not credentials:
            cookies = None
            credentials = None


    resolved_proxy = ctx.params.get("proxy")

    ctx.obj = ContextData(
        config=service_config,
        full_config=config,      # ← ADD THIS  
        vaults=vaults,
        cdm=cdm,
        profile=profile,
        cookies=cookies,
        credentials=credentials,
        proxy=resolved_proxy,   # ← ADD THIS
    )


@dl.result_callback()
@click.pass_context
def result(ctx, service, quality, vcodec, range_, wanted, alang, slang, audio_only, subs_only, chapters_only, audio_description,
           list_, keys, cache, no_cache, no_subs, no_audio, no_video, no_chapters, atmos, vbitrate: int, abitrate: int, channels, no_mux, worst, mux, delay, selected, no_forced, *_, **__):
    def ccextractor():
        log.info("Extracting EIA-608 captions from stream with CCExtractor")
        track_id = f"ccextractor-{track.id}"
        # TODO: Is it possible to determine the language of EIA-608 captions?
        cc_lang = track.language
        try:
            cc = track.ccextractor(
                track_id=track_id,
                out_path=filenames.subtitles.format(id=track_id, language_code=cc_lang),
                language=cc_lang,
                original=False,
            )
        except EnvironmentError:
            log.warning(" - CCExtractor not found, cannot extract captions")
        else:
            if cc:
                title.tracks.add(cc)
                log.info(" + Extracted")
            else:
                log.info(" + No captions found")

    log = service.log

    service_name = service.__class__.__name__
    cdm_service_name = getattr(service.__class__, "CDM_SERVICE_NAME", service_name)

    log.info("Retrieving Titles")
    try:
        titles = Titles(as_list(service.get_titles()))
    except requests.HTTPError as e:
        log.debug(traceback.format_exc())
        raise log.exit(f" - HTTP Error {e.response.status_code}: {e.response.reason}")
    if not titles:
        raise log.exit(" - No titles returned!")
    titles.order()
    titles.print()
    
    first = True    

    for title in titles.with_wanted(wanted):
        if not first and delay:
            d = delay + random.randint(math.floor(int(-delay/5.0)), math.floor((delay/5.0))) 
            log.info(f"Delaying for {d}s before getting next title...")
            time.sleep(d)

        first = False

        if title.type == Title.Types.TV:
            log.info("Getting tracks for {} S{:02}E{:02}{} [{}]".format(
                title.name,
                title.season,
                title.episode,
                f" - {title.episode_name}" if title.episode_name else "",
                title.id
            ))
        else:
            log.info("Getting tracks for {}{} [{}]".format(
                title.name,
                f" ({title.year})" if title.year else "",
                title.id
            ))

        try:
            title.tracks.add(service.get_tracks(title), warn_only=True)
            title.tracks.add(service.get_chapters(title))
            #if not next((x for x in title.tracks.videos if x.language.language in (v_lang or lang)), None) and title.tracks.videos:
             #   lang = [title.tracks.videos[0].language["language"]]
              #  log.info("Defaulting language to {lang} for tracks".format(lang=lang[0].upper()))
        except requests.HTTPError as e:
            log.debug(traceback.format_exc())
            err_text = e.response.text if e.response is not None else ""
            raise log.exit(f" - HTTP Error {e.response.status_code}: {e.response.reason}\n   Response: {err_text}")
        title.tracks.sort_videos()
        title.tracks.sort_audios(by_language=alang)
        title.tracks.sort_subtitles(by_language=slang)
        title.tracks.sort_chapters()

        for track in title.tracks:
            if track.language == Language.get("none"):
                track.language = title.original_lang
            track.is_original_lang = is_close_match(track.language, [title.original_lang])        

        if not list(title.tracks):
            log.error(" - No tracks returned!")
            continue
        if not selected:            
            log.info("> All Tracks:")
            title.tracks.print()

        try:
            if title.tracks.videos:
                if range_ == "DV+HDR":
                    title.tracks.select_videos_multi(["HDR10", "DV"], by_quality=quality, by_vbitrate=vbitrate)
                else:
                    title.tracks.select_videos(by_quality=quality, by_vbitrate=vbitrate, by_range=range_, one_only=True, by_worst=worst, by_codec=vcodec)
            title.tracks.select_audios(by_language=alang, by_bitrate=abitrate, with_atmos=atmos, with_descriptive=audio_description, by_channels=channels)
            # title.tracks.select_audios(by_language=alang, with_descriptive=audio_description)
            title.tracks.select_subtitles(by_language=slang, with_forced=False if no_forced else True)
        except ValueError as e:
            log.error(f" - {e}")
            continue

        if no_video:
            title.tracks.videos.clear()
            
        if no_audio:
            title.tracks.audios.clear()
            
        if no_subs:
            title.tracks.subtitles.clear()
            
        if no_chapters:
            title.tracks.chapters.clear()    
        
        if audio_only or subs_only or chapters_only:
            title.tracks.videos.clear()
            if audio_only:
                if not subs_only:
                    title.tracks.subtitles.clear()
                if not chapters_only:
                    title.tracks.chapters.clear()
            elif subs_only:
                if not audio_only:
                    title.tracks.audios.clear()
                if not chapters_only:
                    title.tracks.chapters.clear()
            elif chapters_only:
                if not audio_only:
                    title.tracks.audios.clear()
                if not subs_only:
                    title.tracks.subtitles.clear()

            if not mux:
                no_mux = True

        log.info("> Selected Tracks:")
        title.tracks.print()

        if list_:
            continue  # only wanted to see what tracks were available and chosen

        skip_title = False
        # get keys
        for track in title.tracks:
                                                 
            if (service_name in ("AppleTVPlus", "iTunes", "AppleTV", "ATV")) and "VID" in str(track):
                track.encrypted = True
            if track.encrypted:
                if track.source=="DSNP":
                    track.pr_pssh=None
                    track.pssh=None
                log.info(f"Licensing: {str(track).replace('├─ ', '')}")
                    

                is_playready = isinstance(ctx.obj.cdm, Cdm_PR) or (hasattr(ctx.obj.cdm, "device") and getattr(ctx.obj.cdm.device, "type", None) == LocalDevice.Types.PLAYREADY)
                if is_playready:
                    if not track.get_pr_pssh(service.session):
                        raise log.exit(" - Failed to get PR_PSSH")
                else:
                    if not track.get_pssh(service.session):
                        raise log.exit(" - Failed to get PSSH")
                if is_playready: 
                    log.info(f" + PR_PSSH: {track.pr_pssh}")
                else:
                    try:
                        log.info(f" + WV_PSSH: {base64.b64encode(Box.build(track.pssh)).decode()}")
                    except:
                        log.info(f" + WV_PSSH: {track.pssh}")

                if not track.get_kid(service.session):
                    raise log.exit(" - Failed to get KID")
                log.info(f" + KID: {track.kid}")
        
            if track.encrypted:
                if not hasattr(title, "all_keys"):
                    title.all_keys = {}
                try:
                    title.all_keys.update(ctx.obj.vaults.get_all_for_title(title.id))
                except Exception:
                    pass

                if track.key:
                    title.all_keys[track.kid.lower()] = track.key.lower()
                    log.info(f" + KEY: {track.key} (Static)")
                elif not no_cache:
                    track.key, vault_used = ctx.obj.vaults.get(track.kid, title.id)
                    if track.key:
                        title.all_keys[track.kid.lower()] = track.key.lower()
                        log.info(f" + KEY: {track.key} (From {vault_used.name} {vault_used.type.name} Key Vault)")
                        for vault in ctx.obj.vaults.vaults:
                            if vault == vault_used:
                                continue

                            try:
                                result = ctx.obj.vaults.insert_key(
                                    vault, service_name.lower(), track.kid, track.key, title.id, commit=True
                                )
                                if result == InsertResult.SUCCESS:
                                    log.info(f" + Cached to {vault} vault")
                                elif result == InsertResult.ALREADY_EXISTS:
                                    log.info(f" + Already exists in {vault} vault")
                            except:
                                pass
                                                                                                 
                if not track.key:
                    if cache:
                        skip_title = True
                        break
                    
                    is_playready = isinstance(ctx.obj.cdm, Cdm_PR) or (hasattr(ctx.obj.cdm, "device") and ctx.obj.cdm.device.__class__.__name__ == "PLAYREADY")
                    if is_playready:
                        try:
                            session_id = ctx.obj.cdm.open()

                            # Step 1: Normalise pr_pssh → raw bytes
                            _pr_raw = track.pr_pssh
                            if isinstance(_pr_raw, Container):
                                _pr_raw = Box.build(_pr_raw)

                            # Step 2: If it is a Widevine PSSH, convert to PlayReady PSSH
                            if isinstance(_pr_raw, bytes):
                                try:
                                    import uuid as _uuid, hashlib as _hashlib
                                    from vinetrimmer.utils.widevine.protos import widevine_pb2 as _wv
                                    _box = Box.parse(_pr_raw)
                                    _sys = _box.system_ID
                                    if isinstance(_sys, str):
                                        _sys = _uuid.UUID(_sys)
                                    elif isinstance(_sys, bytes):
                                        _sys = _uuid.UUID(bytes=_sys)
                                    if _sys == _uuid.UUID("edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"):
                                        # Widevine → extract KID
                                        _idata = _box.init_data
                                        _kid = None
                                        if _idata.startswith(b"\x12\x10") and len(_idata) == 18:
                                            _kid = _idata[2:18]
                                        else:
                                            try:
                                                _cenc = _wv.WidevineCencHeader()
                                                _cenc.ParseFromString(_idata)
                                                if _cenc.key_id:
                                                    _kid = _cenc.key_id[0]
                                            except Exception:
                                                pass
                                        if _kid:
                                            _ku = _uuid.UUID(bytes=_kid)
                                            _kb64 = base64.b64encode(_ku.bytes_le).decode()
                                            _ck = base64.b64encode(_hashlib.sha256(_ku.bytes_le).digest()[:7]).decode()
                                            _xml = (
                                                f'<WRMHEADER version="4.0.0.0" '
                                                f'xmlns="http://schemas.microsoft.com/DRM/2007/03/PlayReadyHeader">'
                                                f'<DATA><PROTECTINFO><KEYLEN>16</KEYLEN><ALGID>AESCTR</ALGID>'
                                                f'</PROTECTINFO><KID>{_kb64}</KID><CHECKSUM>{_ck}</CHECKSUM>'
                                                f'</DATA></WRMHEADER>'
                                            )
                                            _wb = _xml.encode("utf-16-le")
                                            _rec = (1).to_bytes(2,"little") + len(_wb).to_bytes(2,"little") + _wb
                                            _pro = (6+len(_rec)).to_bytes(4,"little") + (1).to_bytes(2,"little") + _rec
                                            _pr_raw = Box.build(dict(
                                                type=b"pssh", version=0, flags=0,
                                                system_ID=_uuid.UUID("{9a04f079-9840-4286-ab92-e65be0885f95}"),
                                                init_data=_pro
                                            ))
                                except Exception:
                                    pass  # fall through with original bytes

                            # Step 3: PSSH_PR expects a base64 string of the PlayReady PSSH box
                            _pr_b64 = base64.b64encode(_pr_raw).decode() if isinstance(_pr_raw, bytes) else str(_pr_raw)
                            wrm_header = PSSH_PR(_pr_b64).wrm_headers[0]
                            challenge = ctx.obj.cdm.get_license_challenge(session_id, wrm_header).encode()
                            license_res = service.license(
                                challenge=challenge,
                                title=title,
                                track=track,
                                session_id=session_id
                            )
                            if isinstance(license_res, bytes):
                                try:
                                    if b"<License>" in license_res:
                                        license_res = base64.b64encode(license_res).decode()
                                except Exception:
                                    pass
                            if isinstance(license_res, bytes):
                                license_res = base64.b64encode(license_res).decode()
                            try:
                                license_res = base64.b64decode(license_res).decode()
                            except Exception:
                                pass
                            ctx.obj.cdm.parse_license(session_id, license_res)
                            content_keys = [
                                (x.key_id.hex, x.key.hex()) for x in ctx.obj.cdm.get_keys(session_id)
                            ]
                            ctx.obj.cdm.close(session_id)
                        except Exception as e:
                            raise log.exit(f" - Error {e}")

                    else:
                        if isinstance(ctx.obj.cdm, Cdm_PR) or (hasattr(ctx.obj.cdm, "device") and getattr(ctx.obj.cdm.device, "type", None) == LocalDevice.Types.PLAYREADY):
                         
                                                                                  
                            session_id = ctx.obj.cdm.open(track.pr_pssh,raw=True)
                        else:
                            session_id = ctx.obj.cdm.open(track.pssh, service_name=cdm_service_name.lower())
                        try:
                            ctx.obj.cdm.set_service_certificate(
                                session_id,
                                service.certificate(
                                    challenge=ctx.obj.cdm.service_certificate_challenge,
                                    title=title,
                                    track=track,
                                    session_id=session_id
                                ) or ctx.obj.cdm.common_privacy_cert
                            )
                            ctx.obj.cdm.parse_license(
                                session_id,
                                service.license(
                                    challenge=ctx.obj.cdm.get_license_challenge(session_id, cdm_service_name),
                                    title=title,
                                    track=track,
                                    session_id=session_id,
                                    service_name=service_name
                                )
                            )
                            
                        except requests.HTTPError as e:
                            log.debug(traceback.format_exc())
                            raise log.exit(f" - HTTP Error {e.response.status_code}: {e.response.reason}")
                                                             
                                                                                                          
                        content_keys = [
                            (x.kid.hex(), x.key.hex()) for x in ctx.obj.cdm.get_keys(session_id, content_only=True)
                                    
                                                                                                   
                        ]
             
                    if not content_keys:
                        raise log.exit(" - No content keys were returned by the CDM!")
                    cdm_class_name = ctx.obj.cdm.__class__.__name__ if isinstance(ctx.obj.cdm, Cdm_PR) else ctx.obj.cdm.device.__class__.__name__
                    log.info(f" + Obtained content keys from the {cdm_class_name} CDM")

                    if not hasattr(title, "all_keys"):
                        title.all_keys = {}

                    for kid, key in content_keys:
                        if kid == "b770d5b4bb6b594daf985845aae9aa5f":
                            # Amazon HDCP test key
                            continue
                        title.all_keys[kid.lower()] = key.lower()
                        log.info(f" + {kid}:{key}")

                    # cache keys into all key vaults
                    for vault in ctx.obj.vaults.vaults:
                        try:
                            log.info(f"Caching to {vault} vault")
                            cached = 0
                            already_exists = 0
                            for kid, key in content_keys:
                                result = ctx.obj.vaults.insert_key(vault, service_name.lower(), kid, key, title.id)
                                if result == InsertResult.FAILURE:
                                    log.warning(f" - Failed, table {service_name.lower()} doesn't exist in the vault.")
                                elif result == InsertResult.SUCCESS:
                                    cached += 1
                                elif result == InsertResult.ALREADY_EXISTS:
                                    already_exists += 1

                            ctx.obj.vaults.commit(vault)
                            log.info(f" + Cached {cached}/{len(content_keys)} keys")
                            if already_exists:
                                log.info(f" + {already_exists}/{len(content_keys)} keys already existed in vault")
                            if cached + already_exists < len(content_keys):
                                log.warning(f"    Failed to cache {len(content_keys) - cached - already_exists} keys")
                        except:
                            pass
                    # use matching content key for the tracks key id
                    track.key = next((key for kid, key in content_keys if kid == track.kid), None)
                    if track.key:
                        log.info(f" + KEY: {track.key} (From CDM)")
                    else:
                        raise log.exit(f" - No content key with the key ID \"{track.kid}\" was returned")

        if skip_title:
            for track in title.tracks:
                track.delete()
            continue
        if keys:
            continue

        # download and decrypt
        for track in title.tracks:
            decrypter_to_use = None
            if not keys:
                if isinstance(track, AudioTrack) and getattr(track, "is_muxed_with_video", False):
                    log.info(f"Extracting audio from muxed video track: {track}")
                    video_track = next((v for v in title.tracks.videos if v.locate()), None)
                    if not video_track:
                        raise log.exit(" - Video track not found or not downloaded yet, cannot extract audio")
                    video_path = video_track.locate()
                    
                    re_name = "{type}_{id}_{enc}".format(
                        type=track.__class__.__name__,
                        id=track.id,
                        enc="enc" if track.encrypted else "dec"
                    )
                    save_path = os.path.join(directories.temp, re_name + ".mp4")
                    try:
                        subprocess.run([
                            "ffmpeg", "-hide_banner", "-y",
                            "-loglevel", "panic",
                            "-i", video_path,
                            "-vn",
                            "-acodec", "copy",
                            save_path
                        ], check=True)
                    except subprocess.CalledProcessError as e:
                        raise log.exit(f" - Failed to extract audio: {e}")
                    
                    track._location = save_path
                    log.info(" + Extracted")
                    continue

                log.info(f"Downloading: {track}")
                if track.needs_proxy:
                    proxy = next(iter(service.session.proxies.values()), None)
                else:
                    proxy = None
                track.download(directories.temp, headers=service.session.headers, proxy=proxy)
                log.info(" + Downloaded")
            if isinstance(track, VideoTrack) and track.needs_ccextractor_first and not no_subs:
                ccextractor()
            if track.encrypted:
                log.info("Decrypting...")
                if track.key:
                    log.info(f" + KEY: {track.key} (Static)")
                # elif not no_cache: # keys are already in track.key, so no need
                #     track.key, vault_used = ctx.obj.vaults.get(track.kid, title.id)
                #     if track.key:
                #         log.info(f" + KEY: {track.key} (From {vault_used.name} {vault_used.type.name} Key Vault)")
                #         for vault in ctx.obj.vaults.vaults:
                #             if vault == vault_used:
                #                 continue
                #             result = ctx.obj.vaults.insert_key(
                #                 vault, service_name.lower(), track.kid, track.key, title.id, commit=True
                #             )
                #             if result == InsertResult.SUCCESS:
                #                 log.info(f" + Cached to {vault} vault")
                #             elif result == InsertResult.ALREADY_EXISTS:
                #                 log.info(f" + Already exists in {vault} vault")
                # TODO: Move decryption code to Track
                decrypter_to_use = config.decrypter
                dec = os.path.splitext(track.locate())[0] + ".dec.mp4"

                if decrypter_to_use == "packager":
                    platform = {"win32": "win", "darwin": "osx"}.get(sys.platform, sys.platform)
                    names = ["shaka-packager", "packager", f"packager-{platform}"]
                    executable = next((x for x in (shutil.which(x) or shutil.which(x, path="./binaries") for x in names) if x), None)
                    if not executable:
                        log.debug("Packager binary not found, falling back to mp4decrypt.")
                        decrypter_to_use = "mp4decrypt"
                    else:
                        title_keys = getattr(title, "all_keys", {})
                        keys_list = []
                        label_idx = 0
                        if track.kid and track.key:
                            keys_list.append(f"label={label_idx}:key_id={track.kid.lower()}:key={track.key.lower()}")
                            label_idx += 1
                        for k_id, k_val in title_keys.items():
                            if track.kid and k_id.lower() == track.kid.lower():
                                continue
                            keys_list.append(f"label={label_idx}:key_id={k_id.lower()}:key={k_val.lower()}")
                            label_idx += 1
                        if track.key and not any(k.endswith(f":key={track.key.lower()}") and "00000000000000000000000000000000" in k for k in keys_list):
                            keys_list.append(f"label={label_idx}:key_id=00000000000000000000000000000000:key={track.key.lower()}")
                            label_idx += 1

                        # Amazon (and some other services) embed a different KID in the
                        # file's tenc box than the KID in the PSSH that was used for
                        # licensing.  Shaka Packager looks up the key by the tenc KID,
                        # finds nothing, and aborts.  mp4decrypt brute-forces all keys
                        # so it succeeds anyway — but we'd rather keep packager working.
                        # Fix: parse the downloaded file, collect every tenc KID, and add
                        # an alias entry (same key, different KID) for any KID not already
                        # covered in keys_list.
                        if track.key:
                            try:
                                _covered_kids = set()
                                for _k in keys_list:
                                    for _part in _k.split(":"):
                                        if _part.startswith("key_id="):
                                            _covered_kids.add(_part[7:].lower())
                                with open(track.locate(), "rb") as _fh:
                                    _data = _fh.read(min(5 * 1024 * 1024, os.path.getsize(track.locate())))
                                # Scan for tenc boxes. _idx points to the 't' of "tenc".
                                # tenc is a FullBox:
                                #   [4B "tenc"][1B version][3B flags]   = 8 bytes so far
                                #   [1B reserved][1B reserved/crypt+skip] = 10 bytes
                                #   [1B default_isProtected][1B default_IV_size] = 12 bytes
                                #   [16B default_KID] starts at _idx + 12
                                _pos = 0
                                while _pos < len(_data) - 28:
                                    _idx = _data.find(b"tenc", _pos)
                                    if _idx < 4 or _idx < 0:
                                        if _idx < 0:
                                            break
                                        _pos = _idx + 1
                                        continue
                                    # Sanity: preceding 4 bytes should be the box size (big-endian uint32 > 0)
                                    _box_size = int.from_bytes(_data[_idx - 4:_idx], "big")
                                    if _box_size < 28 or _box_size > 256:
                                        # Not a valid tenc box (too small or too large for a tenc)
                                        _pos = _idx + 1
                                        continue
                                    # KID is 16 bytes at offset +12 from the "tenc" type field
                                    _kid_bytes = _data[_idx + 12:_idx + 28]
                                    if len(_kid_bytes) == 16:
                                        _tenc_kid = _kid_bytes.hex().lower()
                                        if _tenc_kid != "0" * 32 and _tenc_kid not in _covered_kids:
                                            keys_list.append(f"label={label_idx}:key_id={_tenc_kid}:key={track.key.lower()}")
                                            label_idx += 1
                                            _covered_kids.add(_tenc_kid)
                                            log.debug(f" + Packager: added tenc KID alias {_tenc_kid} → {track.key.lower()}")
                                    _pos = _idx + 1

                            except Exception as _e:
                                log.debug(f" + Packager: tenc KID scan failed ({_e}), continuing without aliases")


                        try:
                            os.makedirs(directories.temp, exist_ok=True)
                            subprocess.run([
                                executable,
                                "input={},stream={},output={}".format(
                                    track.locate(),
                                    track.__class__.__name__.lower().replace("track", ""),
                                    dec
                                ),
                                "--enable_raw_key_decryption", "--keys",
                                ",".join(keys_list),
                                "--temp_dir", directories.temp
                            ], check=True)
                        except subprocess.CalledProcessError:
                            log.warning("Packager decryption failed, trying mp4decrypt fallback...")
                            if os.path.exists(dec):
                                os.remove(dec)
                            decrypter_to_use = "mp4decrypt"

                if decrypter_to_use == "mp4decrypt":
                    executable = shutil.which("mp4decrypt") or shutil.which("mp4decrypt", path="./binaries")
                    ffmpeg_exe = shutil.which("ffmpeg") or shutil.which("ffmpeg", path="./binaries")
                    if not executable and not ffmpeg_exe:
                        raise log.exit(" - Unable to find mp4decrypt or ffmpeg binary")

                    title_keys = getattr(title, "all_keys", {})
                    ffmpeg_success = False

                    # --- Tier 1: FFmpeg ---
                    # FFmpeg handles Apple's CBCS scheme and broken moov boxes correctly,
                    # where both shaka-packager and mp4decrypt produce garbled output.
                    # Note: FFmpeg -decryption_key -c copy cannot decrypt standard CENC/CTR streams.
                    is_cbcs = (
                        service_name in ["ATV", "AppleTV", "iTunes", "iT"]
                        or "cbcs" in getattr(track, "codec", "").lower()
                        or (hasattr(track, "extra") and isinstance(track.extra, dict) and "cbcs" in str(track.extra).lower())
                    )
                    if ffmpeg_exe and track.key and is_cbcs:
                        try:
                            subprocess.run([
                                ffmpeg_exe, "-y",
                                "-decryption_key", track.key.lower(),
                                "-i", track.locate(),
                                "-c", "copy",
                                dec
                            ], check=True, capture_output=True)
                            if os.path.exists(dec) and os.path.getsize(dec) > 1024:
                                ffmpeg_success = True
                                log.info(" + Decrypted via FFmpeg")
                        except subprocess.CalledProcessError:
                            log.warning(" - FFmpeg decryption failed, trying mp4decrypt...")
                            if os.path.exists(dec):
                                os.remove(dec)

                    if not ffmpeg_success:
                        if not executable:
                            raise log.exit(" - Unable to find mp4decrypt binary")

                        # --- Tier 2: mp4fragment + mp4decrypt ---
                        # Pre-fragment to normalize Apple's non-standard fragment structure.
                        mp4fragment_exe = shutil.which("mp4fragment") or shutil.which("mp4fragment", path="./binaries")
                        enc_input = track.locate()
                        if mp4fragment_exe and (service_name in ["ATV", "AppleTV", "iTunes", "iT"] or is_cbcs):
                            frag_tmp = os.path.splitext(track.locate())[0] + ".frag.mp4"
                            try:
                                subprocess.run([mp4fragment_exe, enc_input, frag_tmp], check=True, capture_output=True)
                                if os.path.exists(frag_tmp) and os.path.getsize(frag_tmp) > 0:
                                    enc_input = frag_tmp
                            except subprocess.CalledProcessError:
                                if os.path.exists(frag_tmp):
                                    os.remove(frag_tmp)

                        cmd = [executable, "--show-progress"]
                        if track.kid and track.key:
                            cmd.extend(["--key", f"{track.kid.lower()}:{track.key.lower()}"])
                        for k_id, k_val in title_keys.items():
                            if track.kid and k_id.lower() == track.kid.lower():
                                continue
                            cmd.extend(["--key", f"{k_id.lower()}:{k_val.lower()}"])
                        cmd.extend([enc_input, dec])
                        try:
                            subprocess.run(cmd, check=True)
                        except subprocess.CalledProcessError:
                            raise log.exit(" - Failed!")
                        finally:
                            if mp4fragment_exe and enc_input != track.locate() and os.path.exists(enc_input):
                                os.remove(enc_input)

                if os.path.exists(dec):
                    enc_path = track.locate()
                    enc_size = os.path.getsize(enc_path) if os.path.exists(enc_path) else 0
                    dec_size = os.path.getsize(dec)
                    if enc_size > 10 * 1024 * 1024 and dec_size < enc_size * 0.5:
                        log.error(f" - Decrypted file is severely truncated! ({dec_size / (1024*1024):.2f} MB vs {enc_size / (1024*1024):.2f} MB encrypted)")
                        raise log.exit(f" - Decryption truncated ({dec_size} < {enc_size}) for {track}")
                    track.swap(dec)
                    log.info(" + Decrypted")
                else:
                    raise log.exit(f" - Decryption failed for {track}")
  

            if track.needs_repack or (decrypter_to_use == "mp4decrypt" and isinstance(track, (VideoTrack, AudioTrack))):
                log.info("Repackaging stream with FFmpeg (to fix malformed streams)")
                track.repackage()
                log.info(" + Repackaged")

            if isinstance(track, VideoTrack) and track.needs_ccextractor and not no_subs:
                ccextractor()

                    
        if range_ == "DV+HDR":
            try:
                # Check if we already have a DV+HDR track
                dvhdr_tracks = [t for t in title.tracks.videos if t.dv and t.hdr10]
                if dvhdr_tracks:
                    log.info(" + Track is already DV+HDR, no processing needed")
                else:
                    hybrid_path = title.tracks.make_hybrid()
                    log.info(f" + Hybrid DV+HDR created: {hybrid_path}")
            except Exception as e:
                log.warning(f" - Skipped Hybrid DV+HDR: {e}")

        if not list(title.tracks) and not title.tracks.chapters:
            continue
        if no_mux:
            if title.tracks.chapters:
                final_file_path = directories.downloads
                if title.type == Title.Types.TV:
                    final_file_path = os.path.join(final_file_path, title.parse_filename(folder=True))
                os.makedirs(final_file_path, exist_ok=True)
                chapters_loc = filenames.chapters.format(filename=title.filename)
                title.tracks.export_chapters(chapters_loc)
                shutil.move(chapters_loc, os.path.join(final_file_path, os.path.basename(chapters_loc)))
            for track in title.tracks:
                media_info = MediaInfo.parse(track.locate())
                final_file_path = directories.downloads
                if title.type == Title.Types.TV:
                    final_file_path = os.path.join(
                        final_file_path, title.parse_filename(folder=True)
                    )
                os.makedirs(final_file_path, exist_ok=True)
                filename = title.parse_filename(media_info=media_info)
                if isinstance(track, (AudioTrack, TextTrack)):
                    filename += f".{track.language}"
                extension = track.codec if isinstance(track, TextTrack) else os.path.splitext(track.locate())[1][1:]
                if isinstance(track, AudioTrack) and extension == "mp4":
                    extension = "m4a"
                track.move(os.path.join(final_file_path, f"{filename}.{track.id}.{extension}"))
        else:
            log.info("Muxing tracks into an MKV container")
            muxed_location, returncode = title.tracks.mux(title.filename)
            if returncode == 1:
                log.warning(" - mkvmerge had at least one warning, will continue anyway...")
            elif returncode >= 2:
                raise log.exit(" - Failed to mux tracks into MKV file")
            log.info(" + Muxed")
            for track in title.tracks:
                track.delete()
            if title.tracks.chapters:
                try:
                    os.unlink(filenames.chapters.format(filename=title.filename))
                except FileNotFoundError:
                    pass
            media_info = MediaInfo.parse(muxed_location)
            final_file_path = directories.downloads
            if title.type == Title.Types.TV:
                final_file_path = os.path.join(
                    final_file_path, title.parse_filename(media_info=media_info, folder=True)
                )
            os.makedirs(final_file_path, exist_ok=True)
            # rename muxed mkv file with new data from mediainfo data of it
            if audio_only:
                extension = "mka"
            elif subs_only:
                extension = "mks"
            else:
                extension = "mkv"
            dest_path = os.path.join(final_file_path, f"{title.parse_filename(media_info=media_info)}.{extension}")
            if os.path.exists(dest_path):
                try:
                    os.remove(dest_path)
                except Exception:
                    pass
            try:
                shutil.move(muxed_location, dest_path)
            except Exception:
                base, ext = os.path.splitext(dest_path)
                dest_path = f"{base}_new{ext}"
                if os.path.exists(dest_path):
                    try:
                        os.remove(dest_path)
                    except Exception:
                        pass
                shutil.move(muxed_location, dest_path)
            try:
                size_bytes = os.path.getsize(dest_path)
                units = ["B", "KB", "MB", "GB", "TB"]
                size = float(size_bytes)
                i = 0
                while size >= 1024 and i < len(units) - 1:
                    size /= 1024.0
                    i += 1
                size_str = f"{size:.2f} {units[i]}"
                log.info(f" + Saved to: {dest_path} ({size_str})")
            except Exception:
                pass
            

    log.info("Processed all titles!")


def load_services():
    for service in services.__dict__.values():
        if callable(getattr(service, "cli", None)):
            dl.add_command(service.cli)


load_services()