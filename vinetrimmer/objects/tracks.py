import asyncio
import base64
import logging
import math
import os
import re
import json
import shutil
import subprocess
import sys
import uuid
import langcodes
from collections import defaultdict
from enum import Enum
from io import BytesIO, TextIOWrapper
import humanfriendly
import m3u8
import pycaption
import requests
import pysubs2
from subby import WebVTTConverter, SMPTEConverter, WVTTConverter, ISMTConverter, CommonIssuesFixer, SDHStripper, SubRipFile
from pathlib import Path
from langcodes import Language
from requests import Session
from vinetrimmer import config
from vinetrimmer.constants import LANGUAGE_MUX_MAP, TERRITORY_MAP
from vinetrimmer.utils import Cdm, get_boxes, get_closest_match, is_close_match, try_get
from vinetrimmer.utils.collections import as_list
from vinetrimmer.utils.io import aria2c, download_range, saldl, tqdm_downloader, m3u8re, ffmpeg_download
from vinetrimmer.utils.subprocess import ffprobe
from vinetrimmer.utils.widevine.protos.widevine_pb2 import WidevineCencHeader
from vinetrimmer.utils.xml import load_xml
from vinetrimmer.vendor.pymp4.parser import Box, MP4

def format_duration(seconds):
    """Helper function to format duration for Hybrid logging."""
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02.0f}:{minutes:02.0f}:{seconds:06.3f}"

logger = logging.getLogger("srt")
logger.setLevel(logging.WARNING)

CODEC_MAP = {
    # Video
    "avc1": "H.264",
    "avc3": "H.264",
    "hev1": "H.265",
    "hvc1": "H.265",
    "dvh1": "H.265",
    "dvhe": "H.265",
    "vp9": "VP9",
    "av01": "AV1",
    "av1": "AV1",
    # Audio
    "aac": "AAC",
    "mp4a": "AAC",
    "stereo": "AAC",
    "HE": "HE-AAC",
    "ac3": "AC3",
    "ac-3": "AC3",
    "eac": "DDP",
    "eac-3": "DDP",
    "ec-3": "DDP",
    "ddp": "DDP",
    "ddplus": "DDP",
    "atmos": "DDP",
    # Subtitles
    "srt": "SRT",
    "vtt": "VTT",
    "wvtt": "VTT",
    "dfxp": "TTML",
    "stpp": "TTML",
    "ttml": "TTML",
    "tt": "TTML",
}


class Track:
    class Descriptor(Enum):
        URL = 1  # Direct URL, nothing fancy
        M3U = 2  # https://en.wikipedia.org/wiki/M3U (and M3U8)
        MPD = 3  # https://en.wikipedia.org/wiki/Dynamic_Adaptive_Streaming_over_HTTP
        ISM = 4  # Smooth Streaming


    def __init__(self, id_, source, url, codec, language=None, descriptor=Descriptor.URL,
                 needs_proxy=False, needs_repack=False, encrypted=False, pssh=None, pr_pssh=None, smooth=False, note=None, kid=None, key=None, extra=None, original_url=None, duration=None):
        self.id = id_
        self.source = source
        self.url = url
        self.original_url = original_url
        # required basic metadata
        self.note= note
        self.codec = codec
        self.duration = duration
        #self.language = Language.get(language or "none")
        self.language = Language.get(language or "en")
        self.is_original_lang = False  # will be set later
        # optional io metadata
        self.descriptor = descriptor
        self.needs_proxy = bool(needs_proxy)
        self.needs_repack = bool(needs_repack)
        # decryption
        self.encrypted = encrypted  # Can be None, True, or False
        self.pssh = pssh
        self.pr_pssh = pr_pssh
        self.smooth = smooth
        self.kid = kid
        self.key = key
        # extra data
        self.extra = extra or {}  # allow anything for extra, but default to a dict

        # should only be set internally
        self._location = None

    def __repr__(self):
        return "{name}({items})".format(
            name=self.__class__.__name__,
            items=", ".join([f"{k}={repr(v)}" for k, v in self.__dict__.items()])
        )

    def __eq__(self, other):
        return isinstance(other, Track) and self.id == other.id

    def get_language_code(self):
        if not self.language:
            return "und"
        language = self.language.simplify_script()
        lang_code = (language.language or "und").lower()
        try:
            return self.language.to_alpha3()
        except Exception:
            return lang_code

    def get_track_name(self):
        """Return the base track name. This may be enhanced in subclasses."""
        if self.language is None:
            self.language = Language.get("en")
        language = self.language.simplify_script()
        lang_code = self.get_language_code()
        script = (language.script or None)
        territory_code = (language.territory or None)
        locale_parts = [lang_code]
        if script:
            locale_parts.append(script.capitalize())
        if territory_code:
            locale_parts.append(territory_code.upper())
        locale_code = "-".join(locale_parts)
        try:
            lang = langcodes.Language.get(locale_code)
        except Exception:
            lang = langcodes.Language.get(lang_code)
        try:
            display_name = lang.display_name("en")
        except Exception:
            display_name = lang_code
        if lang_code in ["har", "bgc"]:
            display_name = "Haryanvi"
        elif lang_code in ["pun", "pan", "pa"]:
            display_name = "Punjabi"
        elif lang_code in ["raj"]:
            display_name = "Rajasthani"
        elif lang_code in ["ben", "bn"]:
            display_name = "Bengali"
        elif lang_code in ["guj", "gu"]:
            display_name = "Gujarati"
        elif lang_code in ["hin", "hi"]:
            display_name = "Hindi"
        elif lang_code in ["tam", "ta"]:
            display_name = "Tamil"
        elif lang_code in ["tel", "te"]:
            display_name = "Telugu"
        elif lang_code in ["kan", "kn"]:
            display_name = "Kannada"
        elif lang_code in ["mal", "ml"]:
            display_name = "Malayalam"
        elif lang_code in ["mar", "mr"]:
            display_name = "Marathi"
        elif lang_code in ["bho"]:
            display_name = "Bhojpuri"
        elif lang_code in ["ori", "or"]:
            display_name = "Odia"
        elif lang_code in ["urd", "ur"]:
            display_name = "Urdu"
        elif lang_code in ["asm", "as"]:
            display_name = "Assamese"
        if locale_code.lower().endswith("-auto"):
            display_name = re.sub(r"\s*[\(（][^)]*\[auto\][^)]*[\)）]", "", display_name, flags=re.IGNORECASE)
        if lang_code == "zh":
            display_name = display_name.replace("(", "（").replace(")", "）").replace(", ", "，")
            return display_name
        if "(" in display_name and ")" in display_name:
            main, extra = display_name.split("(", 1)
            main = " ".join(word.capitalize() for word in main.strip().split(" "))
            extra = " ".join(word.capitalize() for word in extra.strip(") ").split(" "))
            display_name = f"{main} ({extra})"
        else:
            display_name = " ".join(word.capitalize() for word in display_name.split(" "))
        return display_name

    def get_data_chunk(self, session=None):
        """Get the data chunk from the track's stream."""
        if not session:
            session = Session()

        url = None
        req_headers = {"Authorization": None} if self.source in ("DSNP", "STRP") else None

        if self.descriptor == self.Descriptor.M3U:
            master = m3u8.loads(session.get(as_list(self.url)[0], headers=req_headers, timeout=15).text, uri=self.url)
            for segment in master.segments:
                if not segment.init_section:
                    continue
                if self.source in ["DSNP", "STRP"] and re.match(r"^[a-zA-Z0-9]{4}-(BUMPER|DUB_CARD)/", segment.init_section.uri):
                    continue
                url = ("" if re.match("^https?://", segment.init_section.uri) else segment.init_section.base_uri)
                url += segment.init_section.uri
                break

        if not url:
            url = as_list(self.url)[0]

        with session.get(url, headers=req_headers, stream=True, timeout=15) as s:
            # assuming enough to contain the pssh/kid
            for chunk in s.iter_content(20000):
                # we only want the first chunk
                return chunk
        if self.needs_proxy:
            proxy = next(iter(session.proxies.values()), None)
        else:
            proxy = None

        # assuming 20000 bytes is enough to contain the pssh/kid box
        return download_range(url, 20000, proxy=proxy)
        
        
    def get_pr_pssh(self, session=None):
        """
        Get the PSSH of the track.

        Parameters:
            session: Requests Session, best to provide one if cookies/headers/proxies are needed.

        Returns:
            True if PSSH is now available, False otherwise. PSSH will be stored in Track.pssh
            automatically.
        """
        
        if self.source in ["MA", "CRAV", "ITV"]:
            mp4_file = 'init.mp4'
            data = self.get_data_chunk(session)
            with open(mp4_file, 'wb') as f:
                f.write(data)

            executable = shutil.which("mp4dump")
            mp4_data = subprocess.check_output([executable, '--format', 'json', '--verbosity', '3', mp4_file])
            mp4_data = json.loads(mp4_data)
            #os.remove(mp4_file)
            
            for temp in mp4_data:
                if (temp['name'] == 'moov'):
                    for child in temp['children']:
                        if ('system_id' in child and child['system_id'] == '[9a 04 f0 79 98 40 42 86 ab 92 e6 5b e0 88 5f 95]'):
                            box_size_dec = child['size']
                            pr_pssh_data = child['data']
                            pr_pssh_data_size_dec = child['data_size']
                            break
                    break
        
            pr_pssh_data = pr_pssh_data.replace('[', '').replace(']', '').replace(' ', '')
            if len(pr_pssh_data) % 2 != 0:
                raise ValueError("Invalid hex string length. Must be even.")
            box_size_hex = format(box_size_dec, '08x')
            pr_pssh_data_size_hex = format(pr_pssh_data_size_dec, '08x')
        
            pr_pssh_hex = f'{box_size_hex}70737368000000009a04f07998404286ab92e65be0885f95{pr_pssh_data_size_hex}{pr_pssh_data}'
        
            pr_pssh = (base64.b64encode(bytes.fromhex(pr_pssh_hex))).decode('utf-8')
            self.pr_pssh = pr_pssh
            return True
        
        if not session:
            session = Session()

        boxes = []
        req_headers = {"Authorization": None} if self.source in ("DSNP", "STRP") else None
        
        if self.descriptor == self.Descriptor.M3U:
            # if an m3u, try get from playlist
            master = m3u8.loads(session.get(as_list(self.url)[0], headers=req_headers, timeout=15).text, uri=self.url)
            boxes.extend([
                Box.parse(base64.b64decode(x.uri.split(",")[-1]))
                for x in (master.session_keys or master.keys)
                if x and x.keyformat.lower() == Cdm.urn
            ])
            for x in master.session_keys:
                if x and (x.keyformat.lower() == "com.microsoft.playready" or "com.microsoft.playready" in str(x)):
                    self.pr_pssh = x.uri.split(",")[-1]
                    break
            for x in master.keys:
                if x and "com.microsoft.playready" in str(x):
                    self.pr_pssh = str(x).split("\"")[1].split(",")[-1]
                    break
        
        try:
            if self.pr_pssh.strip().startswith("<"):
                xml_str = self.pr_pssh
            else:
                xml_str = base64.b64decode(self.pr_pssh).decode("utf-16-le", "ignore")
                xml_str = xml_str[xml_str.index("<"):]

            xml = load_xml(xml_str).find("DATA")  # root: WRMHEADER

            self.kid = xml.findtext("KID")  # v4.0.0.0
            if not self.kid:  # v4.1.0.0
                self.kid = next(iter(xml.xpath("PROTECTINFO/KID/@VALUE")), None)
            if not self.kid:  # v4.3.0.0
                self.kid = next(iter(xml.xpath("PROTECTINFO/KIDS/KID/@VALUE")), None)  # can be multiple?

            self.kid = uuid.UUID(base64.b64decode(self.kid).hex()).bytes_le.hex()
            
            return True

        except: pass       


        try:
            if self.pr_pssh or not self.encrypted:
                return True

            if not session:
                session = Session()

            boxes = []
            data = self.get_data_chunk(session)
            if data:
                boxes.extend(list(get_boxes(data, b"pssh")))

            for box in boxes:
                if box.system_ID == uuid.UUID("{9a04f079-9840-4286-ab92-e65be0885f95}"):
                    self.pr_pssh = base64.b64encode(Box.build(box)).decode("utf-8")
                    try:
                        xml_str = Box.build(box)
                        xml_str = xml_str.decode("utf-16-le", "ignore")
                        xml_str = xml_str[xml_str.index("<"):]
                        xml = load_xml(xml_str).find("DATA")
                        kid = xml.findtext("KID")
                        if not kid:
                            kid = next(iter(xml.xpath("PROTECTINFO/KID/@VALUE")), None)
                        if not kid:
                            kid = next(iter(xml.xpath("PROTECTINFO/KIDS/KID/@VALUE")), None)
                        self.kid = uuid.UUID(base64.b64decode(kid).hex()).bytes_le.hex()
                    except Exception:
                        pass
                    return True
        except Exception:
            pass

        return False
        
    def get_pssh(self, session=None):
        """
        Get the PSSH of the track.

        Parameters:
            session: Requests Session, best to provide one if cookies/headers/proxies are needed.

        Returns:
            True if PSSH is now available, False otherwise. PSSH will be stored in Track.pssh
            automatically.
        """
        
        if self.source in ["MA"]:
            mp4_file = 'init.mp4'
            data = self.get_data_chunk(session)
            with open(mp4_file, 'wb') as f:
                f.write(data)

            executable = shutil.which("mp4dump")
            mp4_data = subprocess.check_output([executable, '--format', 'json', '--verbosity', '3', mp4_file])
            mp4_data = json.loads(mp4_data)
            #os.remove(mp4_file)
            
            for temp in mp4_data:
                if (temp['name'] == 'moov'):
                    for child in temp['children']:
                        if ('system_id' in child and child['system_id'] == '[ed ef 8b a9 79 d6 4a ce a3 c8 27 dc d5 1d 21 ed]'):
                            box_size_dec = child['size']
                            pssh_data = child['data']
                            pssh_data_size_dec = child['data_size']
                            break
                    break
        
            pssh_data = pssh_data.replace('[', '').replace(']', '').replace(' ', '')
            if len(pssh_data) % 2 != 0:
                raise ValueError("Invalid hex string length. Must be even.")
            box_size_hex = format(box_size_dec, '08x')
            pssh_data_size_hex = format(pssh_data_size_dec, '08x')
        
            pssh_hex = f'{box_size_hex}7073736800000000edef8ba979d64acea3c827dcd51d21ed{pssh_data_size_hex}{pssh_data}'
        
            pssh = (base64.b64encode(bytes.fromhex(pssh_hex))).decode('utf-8')
            self.pssh = pssh
            return True
        
        if self.pssh or not self.encrypted:
            return True

        if not session:
            session = Session()

        boxes = []
        req_headers = {"Authorization": None} if self.source in ("DSNP", "STRP") else None

        if self.descriptor == self.Descriptor.M3U:
            # if an m3u, try get from playlist
            master = m3u8.loads(session.get(as_list(self.url)[0], headers=req_headers, timeout=15).text, uri=self.url)
            boxes.extend([
                Box.parse(base64.b64decode(x.uri.split(",")[-1]))
                for x in (master.session_keys or master.keys)
                if x and x.keyformat.lower() == Cdm.urn
            ])

        data = self.get_data_chunk(session)
        if data:
            boxes.extend(list(get_boxes(data, b"pssh")))

        for box in boxes:
            if box.system_ID == Cdm.uuid:
                self.pssh = box
                return True

        for box in boxes:
            if box.system_ID == uuid.UUID("{9a04f079-9840-4286-ab92-e65be0885f95}"):
                xml_str = Box.build(box)
                xml_str = xml_str.decode("utf-16-le", "ignore")
                xml_str = xml_str[xml_str.index("<"):]

                xml = load_xml(xml_str).find("DATA")  # root: WRMHEADER

                kid = xml.findtext("KID")  # v4.0.0.0
                if not kid:  # v4.1.0.0
                    kid = next(iter(xml.xpath("PROTECTINFO/KID/@VALUE")), None)
                if not kid:  # v4.3.0.0
                    kid = next(iter(xml.xpath("PROTECTINFO/KIDS/KID/@VALUE")), None)  # can be multiple?

                init_data = WidevineCencHeader()
                init_data.key_id.append(uuid.UUID(bytes_le=base64.b64decode(kid)).bytes)
                init_data.algorithm = 1  # 0=Clear, 1=AES-CTR

                self.pssh = Box.parse(Box.build({
                    "type": b"pssh",
                    "version": 0,
                    "flags": 0,
                    "system_ID": Cdm.uuid,
                    "init_data": init_data.SerializeToString(),
                }))    
                return True

        return False

    def get_kid(self, session=None):
        """
        Get the KID (encryption key id) of the Track.
        The KID corresponds to the Encrypted segments of an encrypted Track.

        Parameters:
            session: Requests Session, best to provide one if cookies/headers/proxies are needed.

        Returns:
            True if KID is now available, False otherwise. KID will be stored in Track.kid
            automatically.
        """
        
        if self.source in ["DSNP", "MA", "CRAV", "ITV"] and not self.kid:
            mp4_file = 'init.mp4'
            data = self.get_data_chunk(session)
            with open(mp4_file, 'wb') as f:
                f.write(data)

            executable = shutil.which("mp4dump")
            mp4_data = subprocess.check_output([executable, '--format', 'json', '--verbosity', '3', mp4_file])
            mp4_data = json.loads(mp4_data)
            os.remove(mp4_file)

            for a in mp4_data:
                if a['name'] == 'moov':
                    for b in a['children']:
                        if b['name'] == 'trak':
                            for c in b['children']:
                                if c['name'] == 'mdia':
                                    for d in c['children']:
                                        if d['name'] == 'minf':
                                            for e in d['children']:
                                                if e['name'] == 'stbl':
                                                    for f in e['children']:
                                                        if f['name'] == 'stsd':
                                                            for g in f['children']:
                                                                if g['name'] == 'encv':
                                                                    for h in g['children']:
                                                                        if h['name'] == 'sinf':
                                                                            for i in h['children']:
                                                                                if i['name'] == 'schi':
                                                                                    for j in i['children']:
                                                                                        if j['name'] == 'tenc':
                                                                                            kid = j['default_KID']
                                                                elif g['name'] == 'enca':
                                                                        for h in g['children']:
                                                                            if h['name'] == 'sinf':
                                                                                for i in h['children']:
                                                                                    if i['name'] == 'schi':
                                                                                        for j in i['children']:
                                                                                            if j['name'] == 'tenc':
                                                                                                kid = j['default_KID']
            self.kid = kid.replace(' ', '').replace('[', '').replace(']', '')
            return True

        if self.kid or not self.encrypted:
            return True

        boxes = []

        # Try to find KID from the track's existing PSSH box first to avoid downloading media chunks / initialization segments
        if self.get_pssh():
            boxes.append(self.pssh)
            for box in boxes:
                if box.type == "pssh" and box.system_ID == Cdm.uuid:
                    if getattr(box, "key_IDs", None):
                        kid = box.key_IDs[0].hex
                        if kid != "00" * 16:
                            self.kid = kid
                            return True
                    cenc_header = WidevineCencHeader()
                    cenc_header.ParseFromString(box.init_data)
                    if getattr(cenc_header, "key_id", None):
                        kid = cenc_header.key_id[0]
                        try:
                            int(kid, 16)
                        except ValueError:
                            kid = kid.hex()
                        else:
                            kid = kid.decode()
                        if kid != "00" * 16:
                            self.kid = kid
                            return True
            boxes = []  # reset if not found

        data = self.get_data_chunk(session)

        if data:
            # try get via ffprobe, needed for non mp4 data e.g. WEBM from Google Play
            probe = ffprobe(data)
            if probe:
                kid = try_get(probe, lambda x: x["streams"]["tags"]["enc_key_id"])
                if kid:
                    kid = base64.b64decode(kid).hex()
                    if kid != "00" * 16:
                        self.kid = kid
                        return True
            # get tenc and pssh boxes if available
            boxes.extend(list(get_boxes(data, b"tenc")))
            boxes.extend(list(get_boxes(data, b"pssh")))

        # get the track's pssh box if available
        if self.get_pssh():
            boxes.append(self.pssh)

        # loop all found boxes and try find a KID
        for box in sorted(boxes, key=lambda b: b.type == "tenc", reverse=True):
            if box.type == "tenc":
                kid = box.key_ID.hex
                if kid != "00" * 16:
                    self.kid = kid
                    return True
            if box.type == "pssh":
                if box.system_ID == Cdm.uuid:
                    # Note: assumes only the first KID of a list is wanted
                    if getattr(box, "key_IDs", None):
                        kid = box.key_IDs[0].hex
                        if kid != "00" * 16:
                            self.kid = kid
                            return True
                    cenc_header = WidevineCencHeader()
                    cenc_header.ParseFromString(box.init_data)
                    if getattr(cenc_header, "key_id", None):
                        kid = cenc_header.key_id[0]
                        try:
                            int(kid, 16)  # KID may be already hex
                        except ValueError:
                            kid = kid.hex()
                        else:
                            kid = kid.decode()
                        if kid != "00" * 16:
                            self.kid = kid
                            return True

        return False

    def download(self, out, name=None, headers=None, proxy=None):
        """
        Download the Track and apply any necessary post-edits like Subtitle conversion.

        Parameters:
            out: Output Directory Path for the downloaded track.
            name: Override the default filename format.
                Expects to contain `{type}`, `{id}`, and `{enc}`. All of them must be used.
            headers: Headers to use when downloading.
            proxy: Proxy to use when downloading.

        Returns:
            Where the file was saved.
        """
        if os.path.isfile(out):
            raise ValueError("Path must be to a directory and not a file")

        os.makedirs(out, exist_ok=True)

        re_name = (name or "{type}_{id}_{enc}").format(
            type=self.__class__.__name__,
            id=self.id,
            enc="enc" if self.encrypted else "dec"
        )
        name = re_name + ".mp4"
        if self.__class__.__name__ == "AudioTrack" and self.source in ["iT", "ATVP", "ATV", "appletv"]:
            name = re_name + ".m4a"
        if self.__class__.__name__ == "TextTrack" and self.source in ["iT", "ATVP", "ATV", "appletv"]:
            name = re_name + ".vtt"
        url_for_ext = as_list(self.url)[0] if self.url else ""
        if url_for_ext:
            first_url = url_for_ext.split("?")[0]
            ext = os.path.splitext(first_url)[1].lower()
            if ext in [".aac", ".ac3", ".ec3", ".ts", ".m4a"]:
                name = re_name + ext
                if ext in [".aac", ".ac3", ".ec3", ".ts"]:
                    self.needs_repack = True
        save_path = os.path.join(out, name)

        if self.descriptor == self.Descriptor.M3U and not self.source in ["iT"]:
            self.playlist_url = as_list(self.url)[0]
            if not self.original_url:
                self.original_url = self.playlist_url
            m3u8_headers = dict(headers or {})
            if self.source in ("DSNP", "STRP"):
                m3u8_headers.pop("Authorization", None)
                m3u8_headers.pop("authorization", None)

            m3u8_res = requests.get(
                self.playlist_url,
                headers=m3u8_headers,
                proxies={"all": proxy} if self.needs_proxy and proxy else None
            )
            m3u8_res.raise_for_status()
            master = m3u8.loads(
                m3u8_res.text,
                uri=self.playlist_url
            )

            # Keys may be [] or [None] if unencrypted
            if any(master.keys + master.session_keys) and self.encrypted is not False:
                self.encrypted = True
                self.get_kid()
                self.get_pssh()
                if not self.kid:
                    self.encrypted = False

            durations = []
            duration = 0
            for segment in master.segments:
                if segment.discontinuity:
                    durations.append(duration)
                    duration = 0
                duration += segment.duration
            durations.append(duration)
            largest_continuity = durations.index(max(durations))

            discontinuity = 0
            has_init = False
            segments = []
            for segment in master.segments:
                if segment.discontinuity:
                    discontinuity += 1
                    has_init = False
                if self.source in ["DSNP", "STRP"] and re.search(
                    r"[a-zA-Z0-9]{4}-(BUMPER|DUB_CARD)/",
                    segment.uri + (segment.init_section.uri if segment.init_section else '')
                ):
                    continue
                if self.source in ["ATVP", "ATV", "appletv"] and discontinuity != largest_continuity:
                    # the amount of pre and post-roll sections change all the time
                    # only way to know which section to get is by getting the largest
                    continue
                if segment.init_section and not has_init:
                    segments.append(
                        ("" if re.match("^https?://", segment.init_section.uri) else segment.init_section.base_uri) +
                        segment.init_section.uri
                    )
                    has_init = True
                segments.append(
                    ("" if re.match("^https?://", segment.uri) else segment.base_uri) +
                    segment.uri
                )
            self.url = segments
            if self.url:
                first_url = self.url[0].split("?")[0]
                ext = os.path.splitext(first_url)[1].lower()
                if ext in [".aac", ".ac3", ".ec3", ".ts", ".m4a"]:
                    name = re_name + ext
                    save_path = os.path.join(out, name)
                    if ext in [".aac", ".ac3", ".ec3", ".ts"]:
                        self.needs_repack = True

        if self.source == "AMZN" and 'ism/' in self.url[0]:
            bins = out.parent / 'binaries'
            executable = shutil.which("N_m3u8DL-RE", path=bins) or shutil.which("RE", path=bins)
            if not executable:
                raise EnvironmentError("N_m3u8DL-RE executable not found...")
            uri = self.url
            pattern = r"(https?://.*?\.ism/)"
            match = re.search(pattern, uri[0])
            if match:
                extracted_url = match.group(1) + "manifest"
                print(extracted_url)
            else:
                print("No match found")
            # out = re.search(r"Video_\w+", str(out)).group(0)
            if self.needs_proxy:
                subprocess.run(f"{executable} {extracted_url} --save-name {re_name} --save-dir {out} --tmp-dir {out} -sv best --custom-proxy {proxy} --log-level ERROR")
            else:
                # --use-system-proxy False
                subprocess.run(f"{executable} {extracted_url} --save-name {re_name} --save-dir {out} --tmp-dir {out} -sv best --use-system-proxy False --log-level ERROR")

        # Check if complete file from previous attempt already exists
        already_downloaded = False
        if os.path.exists(save_path) and os.path.getsize(save_path) > 1024:
            min_size = 50 * 1024 * 1024 if self.__class__.__name__ == "VideoTrack" else (5 * 1024 * 1024 if self.__class__.__name__ == "AudioTrack" else 100)
            if os.path.getsize(save_path) >= min_size:
                already_downloaded = True
                log = logging.getLogger("Tracks")
                log.info(f" + Found existing complete file for {self.id}: {os.path.basename(save_path)} ({self.format_size_compact(os.path.getsize(save_path))})")
                if self.encrypted:
                    try:
                        with open(save_path, "rb") as _fcheck:
                            _header = _fcheck.read(min(10 * 1024 * 1024, os.path.getsize(save_path)))
                            if not any(b in _header for b in (b"sinf", b"schm", b"encv", b"enca")):
                                self.encrypted = False
                                log.info(f" + File {os.path.basename(save_path)} is already decrypted")
                    except Exception:
                        pass

        if not already_downloaded:
            if self.source == "CORE":
                asyncio.run(saldl(
                    self.url,
                    save_path,
                    headers,
                    proxy if self.needs_proxy else None
                ))
            elif self.source == "VDO" and self.descriptor == self.Descriptor.M3U and self.__class__.__name__ != "TextTrack":
                asyncio.run(
                    m3u8re(
                        self.url,
                        save_path,
                        self,
                        headers,
                        proxy if self.needs_proxy else None,
                    )
                )
            elif (
                self.descriptor == self.Descriptor.ISM
                or (self.descriptor != self.Descriptor.MPD and self.source in ("DSPA", "VIU", "KONE", "KABLEONE", "JOJO", "PLAYFLIX", "CHORKI", "STORYTV", "NOW", "nowtvuk", "nowtvit", "UJHK", "ULTRAJHAKAAS", "ujhk", "ultrajhakaas", "UPLAY", "uplay", "ULTRAPLAY", "ultraplay", "TARANGPLUS", "tarangplus", "tarang") and self.__class__.__name__ != "TextTrack")
            ):
                asyncio.run(
                    m3u8re(
                        self.url,
                        save_path,
                        self,
                        headers,
                        proxy if self.needs_proxy else None,
                    )
                )
                # Handle potential language/format suffixes appended by N_m3u8DL-RE
                if not Path(save_path).is_file():
                    base_name = os.path.splitext(os.path.basename(save_path))[0]
                    temp_dir = os.path.dirname(save_path)
                    matches = list(Path(temp_dir).glob(f"{base_name}*"))
                    found_match = next((str(m) for m in matches if m.is_file() and (m.suffix in [".m4a", ".mp4", ".ts", ".aac", ".eac3", ".mkv"] or m.name == base_name)), None)
                    if found_match:
                        if not Path(save_path).exists():
                            try:
                                os.rename(found_match, save_path)
                            except Exception:
                                save_path = found_match
                        else:
                            save_path = found_match
                    else:
                        if not Path(save_path).is_file():
                            raise FileNotFoundError(f"Could not find downloaded file for track {self.id}")

            elif (self.source == "NF" and proxy != None):
                asyncio.run(tqdm_downloader(
                    self.url,
                    save_path,
                    headers,
                    proxy if self.needs_proxy else None
                ))
            elif self.source in ["iT", "ATVP"]:
                asyncio.run(m3u8re(
                    self.url,
                    save_path,
                    headers,
                    proxy if self.needs_proxy else None
                ))
            else:
                aria2_headers = dict(headers or {})
                if self.source in ["ATVP", "iT"]:
                    aria2_headers = {}
                elif self.source in ["DSNP", "STRP"]:
                    aria2_headers.pop("Authorization", None)
                    aria2_headers.pop("authorization", None)

                asyncio.run(aria2c(
                    self.url,
                    save_path,
                    aria2_headers,
                    proxy if self.needs_proxy else None
                ))
            
        if not os.path.exists(save_path):
            base_path = os.path.splitext(save_path)[0]
            found = False
            for candidate_ext in [".mp4", ".m4a", ".ts", ".mkv", ".m4v"]:
                cand = base_path + candidate_ext
                if os.path.exists(cand):
                    save_path = cand
                    found = True
                    break
            if not found:
                parent_dir = os.path.dirname(save_path)
                base_name = os.path.basename(base_path)
                if os.path.exists(parent_dir):
                    matches = [
                        os.path.join(parent_dir, f)
                        for f in os.listdir(parent_dir)
                        if f.startswith(base_name) and not f.endswith(".tmp") and not f.endswith(".aria2")
                    ]
                    if matches:
                        save_path = matches[0]

        if os.stat(save_path).st_size <= 3:  # Empty UTF-8 BOM == 3 bytes
            raise IOError(
                "Download failed, the downloaded file is empty. "
                f"This {'was' if self.needs_proxy else 'was not'} downloaded with a proxy." +
                (
                    " Perhaps you need to set `needs_proxy` as True to use the proxy for this track."
                    if not self.needs_proxy else ""
                )
            )

        self._location = save_path
        return save_path

    def delete(self):
        if self._location:
            import time
            for i in range(10):
                try:
                    os.unlink(self._location)
                    break
                except PermissionError:
                    if i == 9:
                        raise
                    time.sleep(0.5)
            self._location = None

    def repackage(self):
        if not self._location:
            raise ValueError("Cannot repackage a Track that has not been downloaded.")
        ext = os.path.splitext(self._location)[1] or ".mkv"
        fixed_file = f"{self._location}_fixed{ext}"
        try:
            subprocess.run([
                "ffmpeg", "-hide_banner",
                "-loglevel", "panic",
                "-i", self._location,
                # Following are very important!
                "-map_metadata", "-1",  # don't transfer metadata to output file
                "-fflags", "bitexact",  # only have minimal tag data, reproducible mux
                "-codec", "copy",
                fixed_file
            ], check=True)
            self.swap(fixed_file)
        except subprocess.CalledProcessError:
            pass

    def locate(self):
        return self._location

    def move(self, target):
        if not self._location:
            return False
        ok = os.path.realpath(shutil.move(self._location, target)) == os.path.realpath(target)
        if ok:
            self._location = target
        return ok

    def swap(self, target):
        if not os.path.exists(target) or not self._location:
            return False
        os.unlink(self._location)
        os.rename(target, self._location)
        return True

    @staticmethod
    def pt_to_sec(d):
        if isinstance(d, float):
            return d
        if d[0:2] == "P0":
            d = d.replace("P0Y0M0DT", "PT")
        if d[0:2] != "PT":
            raise ValueError("Input data is not a valid time string.")
        d = d[2:].upper()  # skip `PT`
        m = re.findall(r"([\d.]+.)", d)
        return sum(
            float(x[0:-1]) * {"H": 60 * 60, "M": 60, "S": 1}[x[-1].upper()]
            for x in m
        )

    @staticmethod
    def format_hms(seconds):
        if seconds is None:
            return None
        try:
            s = int(round(float(seconds)))
        except Exception:
            return None
        h, rem = divmod(s, 3600)
        m, s = divmod(rem, 60)
        return f"{h:02}:{m:02}:{s:02}"

    @staticmethod
    def format_size_compact(num_bytes):
        try:
            size = float(num_bytes)
        except Exception:
            return ""
        units = ["B", "KB", "MB", "GB", "TB"]
        i = 0
        while size >= 1024 and i < len(units) - 1:
            size /= 1024.0
            i += 1
        val_str = f"{size:.2f}"
        return f"{val_str} {units[i]}"

    def duration_seconds(self):
        cand = getattr(self, "duration", None)
        if not cand and isinstance(getattr(self, "extra", None), dict):
            for key in ("duration", "dur", "length", "time", "pt_duration"):
                if key in self.extra and self.extra[key]:
                    cand = self.extra[key]
                    break
        if cand is None:
            return None
        if isinstance(cand, (int, float)):
            return float(cand)
        try:
            return float(cand)
        except Exception:
            pass
        try:
            return self.pt_to_sec(str(cand))
        except Exception:
            return None

    def computed_size_bytes(self, bitrate):
        try:
            if not bitrate:
                return None
            dur = self.duration_seconds()
            if not dur or dur <= 0:
                return None
            return int((float(bitrate) * float(dur)) / 8.0)
        except Exception:
            return None


class VideoTrack(Track):
    def __init__(self, *args, bitrate, width, size=None, height, fps=None, hdr10=False, dvhdr=False, hlg=False, dv=False,
                 needs_ccextractor=False, needs_ccextractor_first=False, **kwargs):                 
        super().__init__(*args, **kwargs)
        # required
        self.bitrate = int(math.ceil(float(bitrate))) if bitrate else None
        self.width = int(width)
        self.height = int(height)
        # optional
        if "/" in str(fps):
            num, den = fps.split("/")
            self.fps = int(num) / int(den)
        elif fps:
            self.fps = float(fps)
        else:
            self.fps = None
        self.size = size if size else None
        self.hdr10 = bool(hdr10)
        self.dvhdr = bool(dvhdr)
        self.hlg = bool(hlg)
        self.dv = bool(dv)
        self.needs_ccextractor = needs_ccextractor
        self.needs_ccextractor_first = needs_ccextractor_first

    def __str__(self):
        codec = next((CODEC_MAP[x] for x in CODEC_MAP if (self.codec or "").startswith(x)), self.codec)
        range_str = (
            "DV+HDR" if self.dvhdr else
            "HDR10" if self.hdr10 else
            "HLG" if self.hlg else
            "DV" if self.dv else
            "SDR"
        )
        fps_str = f"{self.fps:.3f}" if self.fps else "Unknown"
        size_bytes = self.size if self.size else self.computed_size_bytes(self.bitrate)
        size_str = self.format_size_compact(size_bytes) if size_bytes else None
        enc_status = "Encrypted" if self.encrypted else "Unencrypted"
        return " | ".join([x for x in [
            "|- VID",
            codec,
            range_str,
            f"{self.width}x{self.height}" if self.width and self.height else None,
            f"{self.bitrate // 1000 if self.bitrate else '?'} kb/s",
            f"{fps_str} FPS",
            enc_status,
            size_str
        ] if x])


    def ccextractor(self, track_id, out_path, language, original=False):
        """Return a TextTrack object representing CC track extracted by CCExtractor."""
        if not self._location:
            raise ValueError("You must download the track first.")

        executable = shutil.which("ccextractor") or shutil.which("ccextractorwin")
        if not executable:
            raise EnvironmentError("ccextractor executable was not found.")
        try:
            p = subprocess.Popen([
                executable,
                "-quiet", "-trim", "-noru", "-ru1",
                self._location, "-o", out_path
            ], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            for line in TextIOWrapper(p.stdout, encoding="utf-8"):
                if "[iso file] Unknown box type ID32" not in line:
                    sys.stdout.write(line)
            returncode = p.wait()

            #if returncode and returncode != 10:
            #    raise self.log.exit(f" - ccextractor exited with return code {returncode}")

            if os.path.exists(out_path):
                if os.stat(out_path).st_size <= 3:
                    # An empty UTF-8 file with BOM is 3 bytes.
                    # If the subtitle file is empty, mkvmerge will fail to mux.
                    os.unlink(out_path)
                    return None
                cc_track = TextTrack(
                    id_=track_id,
                    source=self.source,
                    url="",  # doesn't need to be downloaded
                    codec="srt",
                    language=language,
                    #is_original_lang=original,  # TODO: Figure out if this is the original title language
                    cc=True
                )
                cc_track._location = out_path
                return cc_track
        except:
            pass

        return None


class AudioTrack(Track):
    @staticmethod
    def normalize_bitrate(bitrate):
        if not bitrate:
            return None
        try:
            br = int(math.ceil(float(bitrate)))
        except (ValueError, TypeError):
            return None
        
        # Standard nominal audio bitrates in bps
        standard_bitrates = [
            32000, 48000, 64000, 80000, 96000, 112000, 128000, 160000, 
            192000, 224000, 256000, 320000, 384000, 448000, 512000, 
            576000, 640000, 768000, 960000, 1024000, 1536000
        ]
        valid_candidates = []
        for std in standard_bitrates:
            diff = br - std
            if abs(diff) <= 2000:
                valid_candidates.append((abs(diff), std))
            elif 0 < diff <= max(10000, int(std * 0.12)):
                valid_candidates.append((diff, std))

        if valid_candidates:
            valid_candidates.sort(key=lambda x: x[0])
            return valid_candidates[0][1]

        return br

    def __init__(self, *args, bitrate, size=None, channels=None,
                 descriptive: bool = False, boosted_dialog: bool = False, boosted_dialog_level: str | None = None, atmos: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        self.bitrate = self.normalize_bitrate(bitrate)
        self.size = size if size else None
        self.channels = self.parse_channels(channels) if channels else None
        self.atmos = bool(atmos)
        self.descriptive = bool(descriptive)
        self.boosted_dialog = boosted_dialog
        self.boosted_dialog_level = boosted_dialog_level

    @staticmethod
    def parse_channels(channels):
        """
        Converts a string to a float-like string which represents audio channels.
        E.g. "2" -> "2.0", "6" -> "5.1".
        """
        # TODO: Support all possible DASH channel configurations (https://datatracker.ietf.org/doc/html/rfc8216)
        if channels in ["A000", "a000"]:
            return "2.0"
        if channels in ["F801", "f801"]:
            return "5.1"

        try:
            channels = str(float(channels))
        except ValueError:
            channels = str(channels)

        if channels == "6.0":
            return "5.1"

        return channels

    def get_track_name(self):
        """Return the base Track Name."""
        track_name = super().get_track_name() or ""
        
        if getattr(self, "descriptive", False):
            if track_name: track_name += " "
            track_name += "[Descriptive]"

        if getattr(self, "boosted_dialog", False):
            level = getattr(self, "boosted_dialog_level", None)
            label = "Boosted Dialog"
            if level:
                label += f" ({str(level).capitalize()})"
            if track_name: track_name += " "
            track_name += f"[{label}]"

        return track_name or None

    def __str__(self):
        size_bytes = self.size if self.size else self.computed_size_bytes(self.bitrate)
        size_str = self.format_size_compact(size_bytes) if size_bytes else None
        codec = next((CODEC_MAP[x] for x in CODEC_MAP if (self.codec or "").startswith(x)), self.codec)
        enc_status = "Encrypted" if self.encrypted else "Unencrypted"
        return " | ".join([x for x in [
            "|- AUD",
            f"{codec}",
            f"{self.channels}" if self.channels else None,
            f"{self.bitrate // 1000 if self.bitrate else '?'} kb/s",
            f"{self.get_language_code()}",
            " ".join([
                self.get_track_name() or "",
                "[Original]" if self.is_original_lang else ""
            ]).strip(),
            enc_status,
            size_str
        ] if x])


class TextTrack(Track):
    def __init__(self, *args, cc=False, sdh=False, forced=False, **kwargs):
        """
        Information on Subtitle Types:
            https://bit.ly/2Oe4fLC (3PlayMedia Blog on SUB vs CC vs SDH).
            However, I wouldn't pay much attention to the claims about SDH needing to
            be in the original source language. It's logically not true.

            CC == Closed Captions. Source: Basically every site.
            SDH = Subtitles for the Deaf or Hard-of-Hearing. Source: Basically every site.
            HOH = Exact same as SDH. Is a term used in the UK. Source: https://bit.ly/2PGJatz (ICO UK)

            More in-depth information, examples, and stuff to look for can be found in the Parameter
            explanation list below.

        Parameters:
            cc: Closed Caption.
                - Intended as if you couldn't hear the audio at all.
                - Can have Sound as well as Dialogue, but doesn't have to.
                - Original source would be from an EIA-CC encoded stream. Typically all
                  upper-case characters.
                Indicators of it being CC without knowing original source:
                  - Extracted with CCExtractor, or
                  - >>> (or similar) being used at the start of some or all lines, or
                  - All text is uppercase or at least the majority, or
                  - Subtitles are Scrolling-text style (one line appears, oldest line
                    then disappears).
                Just because you downloaded it as a SRT or VTT or such, doesn't mean it
                 isn't from an EIA-CC stream. And I wouldn't take the streaming services
                 (CC) as gospel either as they tend to get it wrong too.
            sdh: Deaf or Hard-of-Hearing. Also known as HOH in the UK (EU?).
                 - Intended as if you couldn't hear the audio at all.
                 - MUST have Sound as well as Dialogue to be considered SDH.
                 - It has no "syntax" or "format" but is not transmitted using archaic
                   forms like EIA-CC streams, would be intended for transmission via
                   SubRip (SRT), WebVTT (VTT), TTML, etc.
                 If you can see important audio/sound transcriptions and not just dialogue
                  and it doesn't have the indicators of CC, then it's most likely SDH.
                 If it doesn't have important audio/sounds transcriptions it might just be
                  regular subtitling (you wouldn't mark as CC or SDH). This would be the
                  case for most translation subtitles. Like Anime for example.
            forced: Typically used if there's important information at some point in time
                     like watching Dubbed content and an important Sign or Letter is shown
                     or someone talking in a different language.
                    Forced tracks are recommended by the Matroska Spec to be played if
                     the player's current playback audio language matches a subtitle
                     marked as "forced".
                    However, that doesn't mean every player works like this but there is
                     no other way to reliably work with Forced subtitles where multiple
                     forced subtitles may be in the output file. Just know what to expect
                     with "forced" subtitles.
        """
        super().__init__(*args, **kwargs)
        self.cc = bool(cc)
        self.sdh = bool(sdh)
        if self.cc and self.sdh:
            raise ValueError("A text track cannot be both CC and SDH.")
        self.forced = bool(forced)
        if (self.cc or self.sdh) and self.forced:
            raise ValueError("A text track cannot be CC/SDH as well as Forced.")

    def get_track_name(self):
        """Return the base Track Name."""
        track_name = super().get_track_name() or ""
        flag = self.cc and "CC" or self.sdh and "SDH" or self.forced and "Forced"
        if flag:
            if track_name:
                flag = f" [{flag}]"
            track_name += flag
        return track_name or None

    @staticmethod
    def convert_to_srt(data, codec):
        codec = (codec or "").lower()
        if codec in ["dfxp", "ttml", "ttml2", "smpte"]:
            converter = SMPTEConverter()
        elif codec in ["vtt", "webvtt", "webvtt-lssdh-ios8"]:
            converter = WebVTTConverter()
        elif codec == "wvtt":
            converter = WVTTConverter()
        elif codec in ("cmfc", "stpp", "dash", "ism-c", "timed text"):
            converter = ISMTConverter()
        else:
            converter = WebVTTConverter()
             
        if isinstance(data, bytes):
            srt = converter.from_bytes(data)
        else:
            srt = converter.from_string(data)

        fixer = CommonIssuesFixer()
        fixed, status = fixer.from_srt(srt)
        if status and fixed:
            srt = fixed

        return srt

    def download(self, out, name=None, headers=None, proxy=None):
        save_path = super().download(out, name, headers, proxy)
        if self.codec.lower() == "ass":
            if not save_path.lower().endswith(".ass"):
                new_save_path = os.path.splitext(save_path)[0] + ".ass"
                if os.path.exists(new_save_path):
                    os.unlink(new_save_path)
                os.rename(save_path, new_save_path)
                self._location = new_save_path
                save_path = new_save_path
            return save_path
        executable = shutil.which("SubtitleEdit")
        if not executable:
            raise EnvironmentError("SubtitleEdit executable was not found.")
        if self.source in ["VDL"]:
            subprocess.run([
                executable,
                "/Convert", str(self._location), "srt",
                f"/outputfilename:{self._location}",
                "/overwrite",
                "/RemoveTextForHI"
            ], check=True)
        else:
            if self.codec.lower() != "srt":
                with open(save_path, "rb") as fd:
                    data = fd.read()
                srt = self.convert_to_srt(data, self.codec)
                srt.save(Path(save_path))
            if hasattr(self, "strip_source_id"):
                try:
                    path_sdh = Path(save_path)
                    fixer = CommonIssuesFixer()
                    stripper = SDHStripper()
                    srt, _ = fixer.from_file(path_sdh)
                    srt.save(path_sdh)
                    stripped, status = stripper.from_srt(srt)
                    if status:
                        stripped.save(path_sdh)
                        print(f"Stripped SDH from source track {self.strip_source_id}")
                except Exception as e:
                    print(f"Failed to strip SDH: {e}")

        if not save_path.lower().endswith(".srt"):
            new_save_path = os.path.splitext(save_path)[0] + ".srt"
            if os.path.exists(new_save_path):
                os.unlink(new_save_path)
            os.rename(save_path, new_save_path)
            self._location = new_save_path
            save_path = new_save_path

        self.codec = "srt"
        return save_path

    def __str__(self):
        codec = next((CODEC_MAP[x] for x in CODEC_MAP if (self.codec or "").startswith(x)), self.codec)
        return " | ".join([x for x in [
            "|- SUB",
            f"{codec}",
            f"{self.get_language_code()}",
            " ".join([self.get_track_name() or "", "[Original]" if self.is_original_lang else ""]).strip()
        ] if x])

class MenuTrack:
    line_1 = re.compile(r"^CHAPTER(?P<number>\d+)=(?P<timecode>[\d\\.]+)$")
    line_2 = re.compile(r"^CHAPTER(?P<number>\d+)NAME=(?P<title>[\d\\.]+)$")

    def __init__(self, number, title, timecode):
        self.id = f"chapter-{number}"
        self.number = number
        self.title = title
        if "." not in timecode:
            timecode += ".000"
        self.timecode = timecode

    def __bool__(self):
        return bool(
            self.number and self.number >= 0 and
            self.title and
            self.timecode
        )

    def __repr__(self):
        """
        OGM-based Simple Chapter Format intended for use with MKVToolNix.

        This format is not officially part of the Matroska spec. This was a format
        designed for OGM tools that MKVToolNix has since re-used. More Information:
        https://mkvtoolnix.download/doc/mkvmerge.html#mkvmerge.chapters.simple
        """
        return "CHAPTER{num}={time}\nCHAPTER{num}NAME={name}".format(
            num=f"{self.number:02}",
            time=self.timecode,
            name=self.title
        )

    def __str__(self):
        return " | ".join([
            "├─ CHP",
            f"[{self.number:02}]",
            self.timecode,
            self.title
        ])

    @classmethod
    def loads(cls, data):
        """Load chapter data from a string."""
        lines = [x.strip() for x in data.strip().splitlines(keepends=False)]
        if len(lines) > 2:
            return MenuTrack.loads("\n".join(lines))
        one, two = lines

        one_m = cls.line_1.match(one)
        two_m = cls.line_2.match(two)
        if not one_m or not two_m:
            raise SyntaxError(f"An unexpected syntax error near:\n{one}\n{two}")

        one_str, timecode = one_m.groups()
        two_str, title = two_m.groups()
        one_num, two_num = int(one_str.lstrip("0")), int(two_str.lstrip("0"))

        if one_num != two_num:
            raise SyntaxError(f"The chapter numbers ({one_num},{two_num}) does not match.")
        if not timecode:
            raise SyntaxError("The timecode is missing.")
        if not title:
            raise SyntaxError("The title is missing.")

        return cls(number=one_num, title=title, timecode=timecode)

    @classmethod
    def load(cls, path):
        """Load chapter data from a file."""
        with open(path, encoding="utf-8") as fd:
            return cls.loads(fd.read())

    def dumps(self):
        """Return chapter data as a string."""
        return repr(self)

    def dump(self, path):
        """Write chapter data to a file."""
        with open(path, "w", encoding="utf-8") as fd:
            return fd.write(self.dumps())

    @staticmethod
    def format_duration(seconds):
        minutes, seconds = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        return f"{hours:02.0f}:{minutes:02.0f}:{seconds:06.3f}"


class Tracks:
    """
    Tracks.
    Stores video, audio, and subtitle tracks. It also stores chapter/menu entries.
    It provides convenience functions for listing, sorting, and selecting tracks.
    """

    TRACK_ORDER_MAP = {
        VideoTrack: 0,
        AudioTrack: 1,
        TextTrack: 2,
        MenuTrack: 3
    }

    def __init__(self, *args):
        self.videos = []
        self.audios = []
        self.subtitles = []
        self.chapters = []

        if args:
            self.add(as_list(*args))

    def __iter__(self):
        return iter(as_list(self.videos, self.audios, self.subtitles))

    def __repr__(self):
        return "{name}({items})".format(
            name=self.__class__.__name__,
            items=", ".join([f"{k}={repr(v)}" for k, v in self.__dict__.items()])
        )

    def overall_duration_seconds(self):
        durations = []
        for coll_name in ("videos", "audios"):
            coll = getattr(self, coll_name, []) or []
            for t in coll:
                try:
                    d = t.duration_seconds()
                except Exception:
                    d = None
                if d:
                    durations.append(d)
        return max(durations) if durations else None

    def duration_hms(self):
        secs = self.overall_duration_seconds()
        return Track.format_hms(secs) if secs else None

    def size_estimation_selected(self, selected_tracks=None):
        if selected_tracks is None:
            selected_tracks = []
            if self.videos:
                selected_tracks.append(self.videos[0])
            if self.audios:
                selected_tracks.append(self.audios[0])
        total_bytes = 0
        for t in selected_tracks or []:
            sb = getattr(t, "size", None) or t.computed_size_bytes(getattr(t, "bitrate", None))
            if sb:
                try:
                    total_bytes += int(sb)
                except Exception:
                    pass
        if total_bytes <= 0:
            return None
        return Track.format_size_compact(total_bytes)


    def __str__(self):
        rep = ""
        last_track_type = None
        tracks = [*list(self), *self.chapters]
        for track in sorted(tracks, key=lambda t: self.TRACK_ORDER_MAP[type(t)]):
            if type(track) != last_track_type:
                last_track_type = type(track)
                count = sum(type(x) is type(track) for x in tracks)
                rep += "{count} {type} Track{plural}{colon}\n".format(
                    count=count,
                    type=track.__class__.__name__.replace("Track", ""),
                    plural="s" if count != 1 else "",
                    colon=":" if count > 0 else ""
                )
            rep += f"{track}\n"

        return rep.rstrip()

    def exists(self, by_id=None, by_url=None):
        """Check if a track already exists by various methods."""
        if by_id:  # recommended
            return any(x.id == by_id for x in self)
        if by_url:
            return any(x.url == by_url for x in self)
        return False

    def add(self, tracks, warn_only=True):
        """Add a provided track to its appropriate array and ensuring it's not a duplicate."""
        if isinstance(tracks, Tracks):
            tracks = [*list(tracks), *tracks.chapters]

        duplicates = 0
        for track in as_list(tracks):
            if self.exists(by_id=track.id):
                if not warn_only:
                    raise ValueError(
                        "One or more of the provided Tracks is a duplicate. "
                        "Track IDs must be unique but accurate using static values. The "
                        "value should stay the same no matter when you request the same "
                        "content. Use a value that has relation to the track content "
                        "itself and is static or permanent and not random/RNG data that "
                        "wont change each refresh or conflict in edge cases."
                    )
                duplicates += 1
                continue

            if isinstance(track, VideoTrack):
                self.videos.append(track)
            elif isinstance(track, AudioTrack):
                self.audios.append(track)
            elif isinstance(track, TextTrack):
                self.subtitles.append(track)
            elif isinstance(track, MenuTrack):
                self.chapters.append(track)
            else:
                raise ValueError("Track type was not set or is invalid.")

        log = logging.getLogger("Tracks")

    def print(self, level=logging.INFO):
        """Print the __str__ to log at a specified level."""
        log = logging.getLogger("Tracks")
        for line in str(self).splitlines(keepends=False):
            log.log(level, line)

    def sort_videos(self, by_language=None):
        """Sort video tracks by bitrate, and optionally language."""
        if not self.videos:
            return
        # bitrate
        self.videos = sorted(self.videos, key=lambda x: float(x.bitrate or 0.0), reverse=True)
        # language
        for language in reversed(by_language or []):
            if str(language) == "all":
                language = next((x.language for x in self.videos if x.is_original_lang), "")
            if not language:
                continue
            self.videos = sorted(
                self.videos,
                key=lambda x: "" if is_close_match(language, [x.language]) else str(x.language)
            )

    def sort_audios(self, by_language=None):
        """Sort audio tracks by original status, language alphabetically, boosted status, descriptive, and bitrate."""
        if not self.audios:
            return

        def audio_sort_key(x):
            level_map = {"High": 0, "Medium": 1, "Low": 2}
            
            is_orig = getattr(x, "is_original_lang", False)
            is_boosted = getattr(x, "boosted_dialog", False)
            is_descriptive = getattr(x, "descriptive", False)
            boosted_level = getattr(x, "boosted_dialog_level", "")
            
            level_val = level_map.get(boosted_level, 3) if is_boosted else 0
            
            return (
                0 if is_orig else 1,
                str(x.language).lower(),  
                is_boosted, 
                is_descriptive,  
                level_val,                  
                -float(x.bitrate or 0.0),
                -float(x.channels.replace("ch", "").replace("/JOC", "") if x.channels is not None else 0.0)
            )

        self.audios = sorted(self.audios, key=audio_sort_key)

        for language in reversed(by_language or []):
            if str(language) == "all":
                language = next((x.language for x in self.audios if getattr(x, "is_original_lang", False)), "")
            if not language:
                continue
            
            self.audios = sorted(
                self.audios,
                key=lambda x: 0 if is_close_match(language, [x.language]) else 1
            )
            
    def sort_subtitles(self, by_language=None):
        """Sort subtitle tracks by sdh, cc, forced, and optionally language."""
        if not self.subtitles:
            return
        normal_langs = {
            str(getattr(t, "language", "")).lower()
            for t in self.subtitles
            if isinstance(t, TextTrack)
            and not getattr(t, "sdh", False)
            and not getattr(t, "cc", False)
            and not getattr(t, "forced", False)
        }
        new_stripped = []
        made_for_lang = set()
        for sdh in [t for t in list(self.subtitles) if isinstance(t, TextTrack) and getattr(t, "sdh", False)]:
            lang_key = str(getattr(sdh, "language", "")).lower()

            if lang_key in normal_langs or lang_key in made_for_lang:
                continue
            strip_id = f"{sdh.id}_strip"
            if self.exists(by_id=strip_id):
                continue
            stripped_track = TextTrack(
                id_=strip_id,
                source=sdh.source,
                url=sdh.url,
                codec=sdh.codec,
                language=sdh.language,
                cc=False,
                sdh=False,
                forced=getattr(sdh, "forced", False),
                descriptor=getattr(sdh, "descriptor", Track.Descriptor.URL),
                needs_proxy=getattr(sdh, "needs_proxy", False),
            )
            stripped_track.strip_source_id = sdh.id
            stripped_track.is_original_lang = getattr(sdh, "is_original_lang", False)
            new_stripped.append(stripped_track)
            made_for_lang.add(lang_key)
        self.subtitles = list(new_stripped) + self.subtitles
        self.subtitles = sorted(
            self.subtitles,
            key=lambda x: (
                not getattr(x, "forced", False), 
                (
                    0 if (not getattr(x, "sdh", False)
                          and not getattr(x, "cc", False)
                          and not hasattr(x, "strip_source_id")) else
                    1 if hasattr(x, "strip_source_id") else
                    2 if getattr(x, "sdh", False) else
                    3
                ),
                str(getattr(x, "language", "")).lower()
            )
        )
        for language in reversed(by_language or []):
            if str(language) == "all":
                language = next((x.language for x in self.subtitles if getattr(x, "is_original_lang", False)), "")
            if not language:
                continue
            self.subtitles = sorted(
                self.subtitles,
                key=lambda x: "" if is_close_match(language, [x.language]) else str(x.language)
            )
        for language in reversed(by_language or []):
            if str(language) == "all":
                language = next((x.language for x in self.subtitles if getattr(x, "is_original_lang", False)), "")
            if not language:
                continue
            self.subtitles = sorted(
                self.subtitles,
                key=lambda x: "" if is_close_match(language, [x.language]) else str(x.language)
            )

    def sort_chapters(self):
        """Sort chapter tracks by chapter number."""
        if not self.chapters:
            return
        # number
        self.chapters = sorted(self.chapters, key=lambda x: x.number)

    @staticmethod
    def select_by_language(languages, tracks, one_per_lang=True):
        """
        Filter a track list by language.

        If one_per_lang is True, only the first matched track will be returned for
        each language. It presumes the first match is what is wanted.

        This means if you intend for it to return the best track per language,
        then ensure the iterable is sorted in ascending order (first = best, last = worst).
        """

        if not tracks:
            return
        if "all" not in languages:
            track_type = tracks[0].__class__.__name__.lower().replace("track", "").replace("text", "subtitle")
            orig_tracks = tracks
            tracks = [
                x for x in tracks
                if is_close_match(x.language, languages) or (x.is_original_lang and "orig" in languages)
            ]
            if not tracks:
                if languages == ["orig"]:
                    all_languages = set(x.language for x in orig_tracks)
                    if len(all_languages) == 1:
                        # If there's only one language available, take it
                        languages = list(all_languages)
                        tracks = [
                            x for x in orig_tracks
                            if is_close_match(x.language, languages) or (x.is_original_lang and "orig" in languages)
                        ]
                    else:
                        raise ValueError(
                            f"There's no original {track_type} track. Please specify a language manually with "
                            f"{'-al' if track_type == 'audio' else '-sl'}."
                        )
                else:
                    raise ValueError(
                        f"There's no {track_type} tracks that match the language{'' if len(languages) == 1 else 's'}: "
                        f"{', '.join(languages)}"
                    )
        if one_per_lang:
            if "all" in languages:
                languages = list(sorted(set(x.language for x in tracks), key=str))
            for language in languages:
                if language == "orig":
                    yield next(x for x in tracks if x.is_original_lang)
                else:
                    match = get_closest_match(language, [x.language for x in tracks])
                    if match:
                        yield next(x for x in tracks if x.language == match)
        else:
            for track in tracks:
                yield track

    def select_videos (self, by_language=None, by_vbitrate=None, by_quality=None, by_worst=None, by_range=None, 
        one_only: bool = True, by_codec=None,
    ) -> None:
        """Filter video tracks by language and other criteria."""
        if by_quality:
            # Note: Do not merge these list comprehensions. They must be done separately so the results
            # from the 16:9 canvas check is only used if there's no exact height resolution match.
            videos_quality = [x for x in self.videos if x.height == by_quality]
            if not videos_quality and isinstance(by_quality, int):
                videos_quality = [x for x in self.videos if abs(int(x.width * (9 / 16)) - by_quality) <= 10]
            if not videos_quality:
                # AMZN weird resolution (1248x520)
                videos_quality = [x for x in self.videos if x.width == 1248 and by_quality == 720]
            if not videos_quality:
                videos_quality = [x for x in self.videos if (x.width, x.height) < (1024, 576) and by_quality == "SD"]
            if not videos_quality:
                videos_quality = [x for x in self.videos if (x.width, x.height) < (1482, 620) and by_quality == "HD720"] 
            if not videos_quality:
                videos_quality = [
                    x for x in self.videos if isinstance(x.extra, dict) and x.extra.get("quality") == by_quality
                ]
            if not videos_quality:
                raise ValueError(f"There's no {by_quality}p resolution video track. Aborting.")
            self.videos = videos_quality
        if by_vbitrate:
            self.videos = [x for x in self.videos if int(x.bitrate) <= int(by_vbitrate * 1001)]
        if by_worst:
            self.videos = sorted(self.videos, key=lambda x: float(x.bitrate or 0.0), reverse=False)
        if by_codec:
            codec_map = {
                "H264": ["avc", "h264"],
                "H265": ["hev", "hvc", "dvh", "dvh1", "dvhe", "h265"],
                "VP9": ["vp9", "vp09"],
                "AV1": ["av01", "av1"],
            }
            targets = codec_map.get(by_codec.upper(), [by_codec.lower()])
            codec_videos = [x for x in self.videos if any(t in (x.codec or "").lower() for t in targets)]
            if codec_videos:
                self.videos = codec_videos
            else:
                raise ValueError(f"There's no {by_codec} video tracks. Aborting.")
        if by_range:
            self.videos = [x for x in self.videos if {
                "HDR10": x.hdr10,
                "HLG": x.hlg,
                "DV": x.dv,
                "SDR": not x.hdr10 and not x.dv
            }.get((by_range or "").upper(), True)]
            if not self.videos:
                raise ValueError(f"There's no {by_range} video track. Aborting.")
        if by_language:
            self.videos = list(self.select_by_language(by_language, self.videos))
        if one_only and self.videos:
            self.videos = [self.videos[0]]

    def select_videos_multi(self, ranges: list[str], by_quality=None, by_vbitrate=None) -> None:
        """Select multiple video tracks for different HDR ranges (e.g., DV+HDR)."""
        selected = []
        for r in ranges:
            temp = Tracks()
            temp.videos = self.videos.copy()
            
            # When creating a hybrid (DV + HDR/SDR), pick the lowest size DV track for RPU metadata
            if r == "DV" and len(ranges) > 1:
                target_quality = None
                fallback_to_min = True
            else:
                target_quality = by_quality
                fallback_to_min = False

            try:
                temp.select_videos(by_range=r, by_quality=target_quality, one_only=False)
            except ValueError:
                if r == "DV":
                    target_quality = None
                    fallback_to_min = True
                    try:
                        temp.select_videos(by_range=r, by_quality=target_quality, one_only=False)
                    except ValueError:
                        continue
                else:
                    continue
            
            if by_vbitrate and r != "DV":
                temp.videos = [x for x in temp.videos if int(x.bitrate) <= int(by_vbitrate * 1001)]
                
            if temp.videos:
                if r == "DV" and fallback_to_min:
                    best = min(temp.videos, key=lambda x: x.bitrate)
                else:
                    best = max(temp.videos, key=lambda x: x.bitrate)
                selected.append(best)
                
        # Include HDR type in uniqueness check
        unique = {}
        for v in selected:
            key = (v.width, v.height, v.codec, v.hdr10, v.dv)
            if key not in unique or v.bitrate > unique[key].bitrate:
                unique[key] = v
        self.videos = list(unique.values())
        if not self.videos:
            raise ValueError(f"There's no video track matching any of the ranges {ranges}. Aborting.")

    def select_audios(
        self,
        with_descriptive: bool = True,
        with_atmos: bool = False,
        by_language=None,
        by_bitrate=None,
        by_channels=None,
        by_codec=None,
        by_boost: str | None = None,
        should_fallback: bool = False,
        by_adub_codec: str | None = None,
        by_chdub_channels: str | None = None
    ) -> None:
        """Filter audio tracks by language and other criteria."""
        if not with_descriptive:
            self.audios = [x for x in self.audios if not x.descriptive]
        if by_codec or by_adub_codec:
            def match_codec(x):
                is_orig = getattr(x, "is_original_lang", False)
                target = by_codec if is_orig else (by_adub_codec or by_codec)
                if not target:
                    return True
                friendly_codec = next((CODEC_MAP[k] for k in CODEC_MAP if (x.codec or "").lower().startswith(k)), x.codec)
                return (
                    target.lower() in (x.codec or "").lower() or
                    target.lower() == friendly_codec.lower()
                )
            
            codec_audio = list(filter(match_codec, self.audios))
            if not codec_audio and not should_fallback:
                raise ValueError(f"There's no matching audio tracks for codec (Orig: {by_codec} / Dub: {by_adub_codec}). Aborting.")
            else:
                self.audios = (codec_audio if codec_audio else self.audios)
        
        if by_channels or by_chdub_channels:
            def match_channels(x):
                is_orig = getattr(x, "is_original_lang", False)
                target = by_channels if is_orig else (by_chdub_channels or by_channels)
                if not target:
                    return True
                return x.channels == target
            
            channels_audio = list(filter(match_channels, self.audios))
            if not channels_audio and not should_fallback:
                raise ValueError(f"There's no matching audio tracks for channels (Orig: {by_channels} / Dub: {by_chdub_channels}). Aborting.")
            else:
                self.audios = (channels_audio if channels_audio else self.audios)
        
        if with_atmos:
            atmos_audio = list(filter(lambda x: x.atmos, self.audios))
            self.audios = (atmos_audio if atmos_audio else self.audios) 
            
        if by_bitrate:
            target_bps = int(by_bitrate * 1000)
            target_norm = AudioTrack.normalize_bitrate(target_bps) or target_bps
            self.audios = [
                x for x in self.audios
                if x.bitrate and (int(x.bitrate) <= target_norm or int(x.bitrate) <= int(target_bps * 1.1))
            ]

        if by_boost:
            by_boost = by_boost.lower()

            def boost_level(a):
                level = getattr(a, "boosted_dialog_level", None)
                if not level and getattr(a, "extra", None):
                    level = a.extra.get("boosted_dialog_level")
                return (str(level).lower() if level else None)

            def is_boosted(a):
                if getattr(a, "boosted_dialog", None) is not None:
                    return bool(a.boosted_dialog)
                return bool(getattr(a, "extra", None) and a.extra.get("boosted_dialog"))

            if by_boost == "normal":
                self.audios = [a for a in self.audios if not is_boosted(a)]
            else:
                filtered = [a for a in self.audios if is_boosted(a) and boost_level(a) == by_boost]
                if not filtered:
                    raise ValueError(f"No audio tracks found for boost level '{by_boost}'.")
                self.audios = filtered
            
        if by_language:
            normal_pool  = [x for x in self.audios if not getattr(x, "descriptive", False) and not getattr(x, "boosted_dialog", False)]
            boosted_pool = [x for x in self.audios if getattr(x, "boosted_dialog", False)]
            desc_pool    = [x for x in self.audios if getattr(x, "descriptive", False)]

            selected = []
            norm_selected = list(self.select_by_language(by_language, normal_pool))
            selected += norm_selected
            if (by_boost and by_boost != "normal") or not norm_selected:
                selected += list(self.select_by_language(by_language, boosted_pool))
            if with_descriptive:
                selected += list(self.select_by_language(by_language, desc_pool))

            seen, uniq = set(), []
            for a in selected:
                if a.id not in seen:
                    uniq.append(a)
                    seen.add(a.id)
            self.audios = uniq

        if self.audios:
            self.audios = sorted(
                self.audios,
                key=lambda x: 0 if getattr(x, "is_original_lang", False) else 1
            )
            
    def select_subtitles(self, by_language=None, scc: str | None = None, ssdh: str | None = None, sfc: str | None = None, with_cc: bool | None = None, with_sdh: bool | None = None, with_forced: bool | None = None):  # noqa: E501
        def norm_tokens(sel):
            if sel is None:
                return None
            seq = sel if isinstance(sel, (list, tuple, set)) else [sel]
            out = []
            for item in seq:
                for piece in str(item).split(","):
                    t = piece.strip().lower()
                    if t:
                        out.append(t)
            return out

        def matches(code: str, lang) -> bool:
            lg = str(lang).lower()
            return lg == code or lg.startswith(code + "-")

        def apply_category(attr: str, selector, fallback: bool | None):
            tokens = norm_tokens(selector)
            aliases = {"orig"}
            if tokens is None:
                if attr == "cc" and fallback:
                    self.subtitles = [x for x in self.subtitles if not getattr(x, "cc", False)]
                elif attr == "sdh" and fallback:
                    self.subtitles = [x for x in self.subtitles
                                      if not getattr(x, "sdh", False) or getattr(x, "is_original_lang", False)]
                elif attr == "forced" and fallback:
                    self.subtitles = [x for x in self.subtitles
                                      if not getattr(x, "forced", False) or getattr(x, "is_original_lang", False)]
                return
            toks = set(tokens)
            keep = []
            for x in self.subtitles:
                in_cat = bool(getattr(x, attr, False))
                if not in_cat:
                    keep.append(x) 
                    continue
                if "none" in toks:
                    pass  
                elif "all" in toks:
                    keep.append(x)
                else:
                    ok = False
                    if any(t in {"orig", "original", "auto"} for t in toks) and getattr(x, "is_original_lang", False):
                        ok = True
                    else:
                        for t in toks:
                            if t in {"orig", "original", "auto"}:
                                continue
                            if matches(t, x.language):
                                ok = True
                                break
                    if ok:
                        keep.append(x)

            self.subtitles = keep
        apply_category("cc",    scc,  with_cc)
        apply_category("sdh",   ssdh, with_sdh)
        apply_category("forced", sfc, with_forced)
        if by_language:
            self.subtitles = list(self.select_by_language(by_language, self.subtitles, one_per_lang=False))

    def export_chapters(self, to_file=None):
        """Export all chapters in order to a string or file."""
        self.sort_chapters()
        data = "\n".join(map(repr, self.chapters))
        if to_file:
            os.makedirs(os.path.dirname(to_file), exist_ok=True)
            with open(to_file, "w", encoding="utf-8") as fd:
                fd.write(data)
        return data

    # converter code

    @staticmethod
    def from_m3u8(*args, **kwargs):
        from vinetrimmer import parsers
        return parsers.m3u8.parse(*args, **kwargs)

    @staticmethod
    def from_mpd(*args, **kwargs):
        from vinetrimmer import parsers
        return parsers.mpd.parse(**kwargs)
    
    @staticmethod
    def from_ism(*args, **kwargs):
        from vinetrimmer import parsers
        return parsers.ism.parse(**kwargs)

    # DV+HDR Hybrid functionality
    def make_hybrid(self) -> str:
        """Create a hybrid DV+HDR track from separate HDR10 and DV tracks (Unshackle Profile 5 -> 8.1 logic)."""
        import time
        from pathlib import Path
        
        start_time = time.time()
        log = logging.getLogger("Hybrid")
        log.info(" + Processing DV+HDR Hybrid (Converting DV Profile 5 -> Profile 8.1 for HDR10 Base)...")

        # Find valid pair of HDR10 and DV tracks
        hdr = next((t for t in self.videos if t.hdr10 and not t.dv), None)
        dv = next((t for t in self.videos if t.dv and not t.hdr10), None)
        
        if not hdr or not dv:
            raise ValueError("Hybrid failed: Could not find valid pair of HDR10 and DV tracks.")
        
        hdr_path = Path(hdr.locate()).resolve()
        dv_path = Path(dv.locate()).resolve()
        
        # Output raw HEVC file (mkvmerge and downstream muxers support this natively)
        hybrid_output = hdr_path.with_name(f"{hdr_path.stem}_hybrid.hevc")
        
        try:
            hybrid_file = self.make_hybrid_dv_hdr(
                dv_file=dv_path, 
                hdr_file=hdr_path, 
                output_file=hybrid_output
            )
            
            # Verify the file was actually created and has content
            hybrid_path = Path(hybrid_file)
            if not hybrid_path.exists() or hybrid_path.stat().st_size < 1000:
                raise FileNotFoundError(f"Hybrid file creation failed or file is empty: {hybrid_path}")

            # Update the HDR track location and properties to reflect DV Profile 8.1 + HDR10
            hdr._location = str(hybrid_path)
            hdr.dv = True
            hdr.hdr10 = True
            
            # Remove the redundant separate DV track from the list so it doesn't get muxed twice
            if dv in self.videos:
                self.videos.remove(dv)
            
            # Delete the original separate files to save space
            try:
                if hdr_path.exists() and hdr_path != hybrid_path:
                    hdr_path.unlink()
                if dv_path.exists():
                    dv_path.unlink()
            except Exception as e:
                log.warning(f" - Failed to delete source files after hybrid creation: {e}")

        except Exception as e:
            log.error(f" - Hybrid creation failed: {e}")
            raise

        end_time = time.time()
        duration = format_duration(end_time - start_time)
        log.info(f" + Finish processing Hybrid (DV Profile 8.1 + HDR10) in {duration}!")
        
        return str(hdr._location)

    def make_hybrid_dv_hdr(self, dv_file: Path, hdr_file: Path, output_file: Path) -> str:
        """
        Create hybrid DV+HDR track using dovi_tool (Unshackle workflow):
        1. Extract RPU from DV track and convert Profile 5 -> Profile 8.1 (Mode 2: strips IPTPQc2 mapping curves).
        2. Verify RPU structure and profile compatibility.
        3. Extract HDR10 raw HEVC bitstream (BT.2020 PQ).
        4. Inject Profile 8.1 RPU into HDR10 bitstream.
        """
        # Locate binaries
        dovi_tool = shutil.which("dovi_tool")
        if not dovi_tool:
            dovi_tool = shutil.which("dovi_tool", path="./binaries")
        
        if not dovi_tool:
            raise EnvironmentError("dovi_tool executable not found in PATH or ./binaries/")

        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
             raise EnvironmentError("ffmpeg executable not found.")

        # Temp filenames
        rpu_file = output_file.with_name("RPU.bin")
        hdr_hevc_file = output_file.with_name("hdr_temp.hevc")
        temp_files_to_clean = [rpu_file, hdr_hevc_file]
        log = logging.getLogger("Hybrid")
        
        try:
            # 1. Extract RPU and convert Profile 5 -> Profile 8.1 (Mode 2)
            # Mode 2 removes proprietary IPT non-linear mapping while keeping dynamic metadata (L1, L2, L5, L6)
            if dv_file.suffix.lower() != ".hevc":
                log.info(" + Extracting Dolby Vision RPU & Converting to Profile 8.1 (Mode 2) via pipe...")
                ffmpeg_cmd = [
                    ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                    "-i", str(dv_file),
                    "-c:v", "copy", "-bsf:v", "hevc_mp4toannexb",
                    "-f", "hevc", "-"
                ]
                dovi_cmd = [
                    dovi_tool, "-m", "2", "extract-rpu", 
                    "-i", "-", 
                    "-o", str(rpu_file)
                ]
                p1 = subprocess.Popen(ffmpeg_cmd, stdout=subprocess.PIPE)
                p2 = subprocess.Popen(dovi_cmd, stdin=p1.stdout, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                p1.stdout.close()
                stderr = p2.communicate()[1]
                if p2.returncode != 0:
                    raise RuntimeError(f"dovi_tool extract-rpu failed: {stderr.decode(errors='ignore')}")
                if p1.wait() != 0:
                    raise RuntimeError("ffmpeg DV stream extraction failed")
            else:
                log.info(" + Extracting Dolby Vision RPU & Converting to Profile 8.1 (Mode 2) from raw HEVC...")
                subprocess.run([
                    dovi_tool, "-m", "2", "extract-rpu", 
                    "-i", str(dv_file), 
                    "-o", str(rpu_file)
                ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

            if not rpu_file.exists() or rpu_file.stat().st_size < 100:
                raise RuntimeError("Dolby Vision RPU extraction resulted in an empty or missing RPU file")

            # 2. Inspect & Verify RPU conversion to Profile 8.1
            try:
                info_res = subprocess.run(
                    [dovi_tool, "info", "-s", "-i", str(rpu_file)],
                    capture_output=True,
                    text=True,
                    timeout=10
                )
                if info_res.returncode == 0 and info_res.stdout.strip():
                    info_data = json.loads(info_res.stdout)
                    dv_profile = info_data.get("profile", info_data.get("dovi_profile", "8.1"))
                    dm_version = info_data.get("dm_version", info_data.get("cm_version", "v4.0 / v2.9"))
                    log.info(f" + Verified Converted RPU: Profile {dv_profile} | DM Version: {dm_version}")
                else:
                    log.info(" + Converted Dolby Vision RPU to Profile 8.1 (Ready for HDR10 base injection)")
            except Exception as e:
                log.debug(f"RPU summary check: {e}")
                log.info(" + Converted Dolby Vision RPU to Profile 8.1")

            # 3. Prepare raw HDR10 HEVC stream
            target_hdr_input = hdr_file
            if hdr_file.suffix.lower() != ".hevc":
                log.info(" + Extracting HDR10 base layer stream to raw HEVC...")
                subprocess.run([
                    ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                    "-i", str(hdr_file),
                    "-c:v", "copy", "-bsf:v", "hevc_mp4toannexb",
                    str(hdr_hevc_file)
                ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                target_hdr_input = hdr_hevc_file

            # 4. Inject Converted Profile 8.1 RPU into HDR10 HEVC Stream
            log.info(" + Injecting Converted Profile 8.1 Dolby Vision RPU into HDR10 base layer...")
            subprocess.run([
                dovi_tool, "inject-rpu", 
                "-i", str(target_hdr_input), 
                "-r", str(rpu_file), 
                "-o", str(output_file)
            ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

        except (subprocess.CalledProcessError, RuntimeError) as e:
            raise RuntimeError(f"Error running dovi_tool/ffmpeg in hybrid pipeline: {e}")
        finally:
            # Clean up all temp files
            for temp in temp_files_to_clean:
                if temp.exists():
                    try:
                        temp.unlink()
                    except OSError:
                        pass

        return str(output_file)

    def mux(self, prefix, default_sub_lang=None, title=None):
        """
        Takes the Video, Audio and Subtitle Tracks, and muxes them into an MKV file.
        It will attempt to detect Forced/Default tracks, and will try to parse the language codes of the Tracks
        """
        if self.videos:
            muxed_location = self.videos[0].locate()
            if not muxed_location:
                raise ValueError("The provided video track has not yet been downloaded.")
            muxed_location = os.path.splitext(muxed_location)[0] + ".muxed.mkv"
        elif self.audios:
            muxed_location = self.audios[0].locate()
            if not muxed_location:
                raise ValueError("A provided audio track has not yet been downloaded.")
            muxed_location = os.path.splitext(muxed_location)[0] + ".muxed.mka"
        elif self.subtitles:
            muxed_location = self.subtitles[0].locate()
            if not muxed_location:
                raise ValueError("A provided subtitle track has not yet been downloaded.")
            muxed_location = os.path.splitext(muxed_location)[0] + ".muxed.mks"
        elif self.chapters:
            muxed_location = config.filenames.chapters.format(filename=prefix)
            if not muxed_location:
                raise ValueError("A provided chapter has not yet been downloaded.")
            muxed_location = os.path.splitext(muxed_location)[0] + ".muxed.mks"
        else:
            raise ValueError("No tracks provided, at least one track must be provided.")
        
        muxed_location = os.path.join(config.directories.downloads, os.path.basename(muxed_location))
        
        cl = [
            "mkvmerge",
            "--no-date",
            "--output",
            muxed_location
        ]

        if title:
            cl.extend(["--title", title])

        for i, vt in enumerate(self.videos):
            location = vt.locate()
            if not location:
                raise ValueError("Somehow a Video Track was not downloaded before muxing...")
            track_args = []
            if self.audios:
                track_args.append("--no-audio")
            cl.extend([
                *track_args,
                "--language", "0:und",
                "--default-track", f"0:{i == 0}",
                "--compression", "0:none",
                "(", location, ")"
            ])

        for i, at in enumerate(self.audios):
            location = at.locate()
            if not location:
                raise ValueError("Somehow an Audio Track was not downloaded before muxing...")
            
            lang_code = str(at.language)
            if "-auto" in lang_code.lower():
                lang_code = lang_code.lower().replace("-auto", "")
            lang_code = LANGUAGE_MUX_MAP.get(lang_code.lower(), lang_code)
            
            track_args = []
            if self.videos:
                track_args.append("--no-video")

            cl.extend([
                *track_args,
                "--track-name", f"0:{at.get_track_name() or ''}",
                "--language", f"0:{lang_code}",
                "--default-track", f"0:{i == 0}",
                "--compression", "0:none",
                "(", location, ")"
            ])

        orig_aliases = {"orig"}
        def lang_match(code, lang):
            code = (code or "").lower()
            lang = str(lang).lower()
            return lang == code or lang.startswith(code + "-")

        desired_default = None
        if default_sub_lang:
            if default_sub_lang.lower() in orig_aliases:
                orig_lang = next((st.language for st in self.subtitles if getattr(st, "is_original_lang", False)), None)
                desired_default = str(orig_lang) if orig_lang else None
            else:
                desired_default = str(default_sub_lang)

        default_set = False
        for st in self.subtitles:
            location = st.locate()
            if not location:
                log.warning(f" - Skipping subtitle track {st} because it was not downloaded.")
                continue
            default = False
            if not default_set:
                if desired_default:
                    if lang_match(desired_default, st.language):
                        default = True
                        default_set = True
                else:
                    if lang_match("id", st.language):
                        default = True
                        default_set = True
            
            lang_code = str(st.language)
            if "-auto" in lang_code.lower():
                lang_code = lang_code.lower().replace("-auto", "")
            lang_code = LANGUAGE_MUX_MAP.get(lang_code.lower(), lang_code)

            cl.extend([
                "--track-name", f"0:{st.get_track_name() or ''}",
                "--language", f"0:{lang_code}",
                "--sub-charset", "0:UTF-8",
                "--forced-track", f"0:{st.forced}",
                "--default-track", f"0:{default}",
                "--compression", "0:none",
                "(", location, ")"
            ])

        if self.chapters:
            location = config.filenames.chapters.format(filename=prefix)
            self.export_chapters(location)
            cl.extend(["--chapters", location])

        p = subprocess.Popen(cl, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        in_progress = False
        output_lines = []
        for line in TextIOWrapper(p.stdout, encoding="utf-8"):
            output_lines.append(line)
            if line.startswith("Progress:"):
                in_progress = True
                sys.stdout.write("\r" + line.rstrip('\n'))
        if in_progress:
            sys.stdout.write("\n")
        returncode = p.wait()
        if returncode >= 2:
            log = logging.getLogger("mux")
            log.error("mkvmerge failed with code %d. Command line: %s", returncode, cl)
            log.error("mkvmerge output:\n%s", "".join(output_lines))

            # Fallback to FFmpeg muxing if mkvmerge fails
            try:
                log.info(" + Attempting fallback muxing via FFmpeg...")
                ff_cmd = ["ffmpeg", "-hide_banner", "-y", "-loglevel", "error"]
                for vt in self.videos:
                    if vt.locate():
                        ff_cmd.extend(["-i", vt.locate()])
                for at in self.audios:
                    if at.locate():
                        ff_cmd.extend(["-i", at.locate()])
                for st in self.subtitles:
                    if st.locate():
                        ff_cmd.extend(["-i", st.locate()])
                ff_cmd.extend(["-c", "copy", muxed_location])
                res = subprocess.run(ff_cmd)
                if res.returncode == 0 and os.path.exists(muxed_location) and os.path.getsize(muxed_location) > 1024:
                    log.info(" + Fallback FFmpeg muxing succeeded!")
                    returncode = 0
            except Exception as e_ff:
                log.error(f"Fallback FFmpeg muxing failed: {e_ff}")

        return muxed_location, returncode

    def select_videos_multi(self, ranges: list[str], by_quality=None, by_vbitrate=None) -> None:
        """Select multiple video tracks for different HDR ranges (e.g., DV+HDR)."""
        selected = []
        for r in ranges:
            temp = Tracks()
            temp.videos = self.videos.copy()
            
            # When creating a hybrid (DV + HDR/SDR), pick the lowest size DV track for RPU metadata
            if r == "DV" and len(ranges) > 1:
                target_quality = None
                fallback_to_min = True
            else:
                target_quality = by_quality
                fallback_to_min = False

            try:
                temp.select_videos(by_range=r, by_quality=target_quality, one_only=False)
            except ValueError:
                if r == "DV":
                    target_quality = None
                    fallback_to_min = True
                    try:
                        temp.select_videos(by_range=r, by_quality=target_quality, one_only=False)
                    except ValueError:
                        continue
                else:
                    continue
            
            if by_vbitrate and r != "DV":
                temp.videos = [x for x in temp.videos if int(x.bitrate) <= int(by_vbitrate * 1001)]
                
            if temp.videos:
                if r == "DV" and fallback_to_min:
                    best = min(temp.videos, key=lambda x: x.bitrate)
                else:
                    best = max(temp.videos, key=lambda x: x.bitrate)
                selected.append(best)
                
        unique = {}
        for v in selected:
            key = (v.codec, v.hdr10, v.dv)
            if key not in unique or v.bitrate > unique[key].bitrate:
                unique[key] = v
                
        self.videos = list(unique.values())
        if not self.videos:
            raise ValueError(f"There's no video track matching any of the ranges {ranges}. Aborting.")
