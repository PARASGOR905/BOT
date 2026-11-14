import logging
import re
import math
import requests
import base64
import uuid
import sys
from io import BytesIO

from vinetrimmer.utils.xml import load_xml
from vinetrimmer.objects import VideoTrack, AudioTrack, TextTrack, Tracks, Track
from vinetrimmer.utils import Cdm
from vinetrimmer.vendor.pymp4.parser import Box

def parse(*, url=None, data=None, source, session=None):
    """
    Convert a Smooth Streaming ISM manifest to a Tracks object.
    
    :param url: URL of the ISM manifest.
    :param data: The ISM manifest content as string.
    :param source: Source tag for the returned tracks.
    :param session: Requests session for downloading.
    """
    if not data:
        if not url:
            raise ValueError("Neither a URL nor a document was provided to Tracks.from_ism")
        data = (session or requests).get(url).text

    root = load_xml(data)
    if root.tag != "SmoothStreamingMedia":
        raise ValueError("Non-ISM document provided to tracks_from_ism")
    
    # Helper to resolve relative URLs
    def resolve_url(path):
        if url and not re.match(r"^https?://", path, re.IGNORECASE):
            from urllib.parse import urljoin
            return urljoin(url, path)
        return path

    tracks = []
    
    # Check for protection
    protection = root.find("Protection")
    pssh = None
    pr_pssh = None
    kid = None
    
    if protection is not None:
        protection_header = protection.find("ProtectionHeader")
        if protection_header is not None:
            # SystemID for PlayReady
            sys_id = protection_header.get("SystemID")
            if sys_id and sys_id.lower() == "9a04f079-9840-4286-ab92-e65be0885f95":
                pr_data = protection_header.text
                if pr_data:
                    pr_data = pr_data.strip()  # Remove any whitespace
                    try:
                        # Decode base64 then utf-16-le to get the XML
                        pr_bytes = base64.b64decode(pr_data)
                        pr_xml = pr_bytes.decode("utf-16-le")
                        # Extract just the XML part
                        if "<" in pr_xml:
                            xml_str = pr_xml[pr_xml.index("<"):]
                        else:
                            xml_str = pr_xml
                        
                        try:
                            from pyplayready.system.pssh import _PlayreadyPSSHStructs
                            xml_bytes = xml_str.encode("utf-16-le")
                            record = dict(type=1, length=len(xml_bytes), data=xml_bytes)
                            record_bytes = _PlayreadyPSSHStructs.PlayreadyObject.build(record)
                            pro_length = 4 + 2 + len(record_bytes)
                            pro = dict(length=pro_length, records=[record])
                            pro_bytes = _PlayreadyPSSHStructs.PlayreadyHeader.build(pro)
                            pr_pssh = base64.b64encode(pro_bytes).decode("utf-8")
                        except Exception as pssh_err:
                            logging.getLogger("ism").warning(f"Failed to generate PlayReady PRO from XML: {pssh_err}")
                            pr_pssh = xml_str
                            
                        # Try to extract KID from WRMHEADER logic
                        import xml.etree.ElementTree as ET
                        try:
                            # Parse the XML to find KID
                            wrm_root = ET.fromstring(xml_str)
                            kid_element = wrm_root.find(".//{http://schemas.microsoft.com/DRM/2007/03/PlayReadyHeader}KID")
                            if kid_element is None:
                                kid_element = wrm_root.find(".//KID")
                            
                            if kid_element is not None and kid_element.text:
                                kid_bytes = base64.b64decode(kid_element.text)
                                kid = uuid.UUID(bytes_le=kid_bytes).hex
                        except Exception as e:
                            logging.getLogger("ism").warning(f"Failed to extract KID: {e}")
                            
                    except Exception as e:
                        logging.getLogger("ism").warning(f"Failed to parse ProtectionHeader: {e}")



    # Synthesize Widevine PSSH from KID if available (crucial for valid Track object)
    if kid and not pssh:
        try:
            # Construct a Widevine PSSH box using the KID
            # Default to v0 PSSH
            pssh_box = Box.build(dict(
                type=b"pssh",
                version=0,
                flags=0,
                system_ID=Cdm.uuid,
                init_data=b"\x12\x10" + bytes.fromhex(kid) 
                # This is a minimal init_data (key_id only), sufficient for many players/CDMs
                # or we might need full provider/content ID, but usually key ID is enough for license req
            ))
            pssh = Box.parse(pssh_box)
        except Exception as e:
            logging.getLogger("ism").warning(f"Failed to create Widevine PSSH from KID: {e}")

    # Get global TimeScale and Duration
    time_scale = int(root.get("TimeScale") or 10000000)
    duration_ticks = int(root.get("Duration") or 0)
    global_duration_sec = duration_ticks / time_scale if duration_ticks else 0

    for stream_index in root.findall("StreamIndex"):
        type_ = stream_index.get("Type")
        url_template = stream_index.get("Url")
        name = stream_index.get("Name")
        chunks = stream_index.findall("c")
        
        # StreamIndex level TimeScale (overrides global for chunks, but usually consistent)
        stream_time_scale = int(stream_index.get("TimeScale") or time_scale)
        
        # Calculate timeline
        # Timeline is a list of timestamps, one for each fragment
        timeline = []
        current_time = 0
        
        # Sometimes chunks have 't' (start time), 'd' (duration), 'r' (repeat count)
        for c in chunks:
            if c.get("t"):
                current_time = int(c.get("t"))
            
            d = int(c.get("d") or 0)
            r = int(c.get("r") or 1)
            
            # The 't' attribute specifies the start time of the *first* fragment in the series.
            # Subsequent fragments (if r > 1) are relative to the previous one + d.
            
            # However, for constructing URLs, we usually need the start time of the fragment.
            # Url template: .../Fragments(video={start time})
            
            for _ in range(r):
                timeline.append(current_time)
                current_time += d
        
        # If global duration is 0, try to calculate from timeline
        if global_duration_sec == 0 and timeline and chunks:
            # Last fragment start + last fragment duration
            last_chunk = chunks[-1]
            last_d = int(last_chunk.get("d") or 0)
            # We need to know how many repeats the last chunk has to find the absolute end
            # usage of 'current_time' after loop might be the end time?
            # actually current_time after loop is (last_start + last_duration)
            global_duration_sec = current_time / stream_time_scale

        # Now process each QualityLevel
        for quality_level in stream_index.findall("QualityLevel"):
            bitrate = quality_level.get("Bitrate")
            fourcc = quality_level.get("FourCC")
            index = quality_level.get("Index")
            
            # Construct fragment URLs
            fragment_urls = []
            
            # Replace {bitrate} (and {Bitrate} just in case)
            current_ql_url = url_template.replace("{bitrate}", str(bitrate)).replace("{Bitrate}", str(bitrate))
            current_ql_url = current_ql_url.replace("{start_time}", "{start time}") # Normalize just in case
            
            for t in timeline:
                # Replace {start time} with the timestamp
                u = current_ql_url.replace("{start time}", str(t))
                fragment_urls.append(resolve_url(u))
            
            track_id = f"{type_}_{bitrate}_{index}"
            
            # Common attributes
            common_args = {
                "id_": track_id,
                "source": source,
                "url": fragment_urls,
                "bitrate": bitrate,
                "encrypted": protection is not None,
                "pssh": pssh,
                "pr_pssh": pr_pssh,
                "kid": kid,
                "descriptor": Track.Descriptor.ISM,
                "extra": (stream_index, quality_level)
            }
            
            if type_ == "video":
                width = int(quality_level.get("MaxWidth") or quality_level.get("Width") or 0)
                height = int(quality_level.get("MaxHeight") or quality_level.get("Height") or 0)
                codec = fourcc or "H264"
                
                # frameRate calculation?
                # chunks 'd' is duration in ticks. 
                # fps = stream_time_scale / (d / number_of_frames_in_chunk) -> Unknown
                
                # Check if QualityLevel has FrameRate attribute (some ISM have it)
                frame_rate = quality_level.get("FrameRate") or stream_index.get("FrameRate")
                if not frame_rate:
                    frame_rate = quality_level.get("MaxFrameRate") or stream_index.get("MaxFrameRate")

                tracks.append(VideoTrack(
                    **common_args,
                    codec=codec,
                    width=width,
                    height=height,
                    duration=global_duration_sec,
                    fps=frame_rate
                ))
                
            elif type_ == "audio":
                channels = quality_level.get("Channels")
                sampling_rate = quality_level.get("SamplingRate")
                codec = fourcc or "AAC"
                
                lang = stream_index.get("Language") or "und"
                
                # Heuristic for Atmos/JOC: EC-3 with >400kbps and 6 channels
                # Exclude 640kbps which is often just high bitrate 5.1
                is_ec3 = codec.lower() in ["ec-3", "eac3", "eac-3"]
                is_51 = str(channels) == "6"
                bitrate_int = int(bitrate) if bitrate else 0
                is_atmos = is_ec3 and is_51 and bitrate_int > 400000 and bitrate_int != 640000

                tracks.append(AudioTrack(
                    **common_args,
                    codec=codec,
                    language=lang,
                    channels=channels,
                    duration=global_duration_sec,
                    atmos=is_atmos
                ))
            
            elif type_ == "text":
                tracks.append(TextTrack(
                    **common_args,
                    codec=fourcc or "ttml",
                    language=stream_index.get("Language") or "und"
                ))
                
    tracks_obj = Tracks()
    tracks_obj.add(tracks, warn_only=True)
    return tracks_obj
