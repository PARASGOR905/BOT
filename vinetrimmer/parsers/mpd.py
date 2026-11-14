import xmltodict
import asyncio
import base64
import json
import math
import os
import re
import urllib.parse
import uuid
from copy import copy
from hashlib import md5

import requests
from langcodes import Language
from langcodes.tag_parser import LanguageTagError

from vinetrimmer import config
from vinetrimmer.objects import AudioTrack, TextTrack, Track, Tracks, VideoTrack
from vinetrimmer.utils import Cdm
from vinetrimmer.utils.io import aria2c
from vinetrimmer.utils.xml import load_xml
from vinetrimmer.vendor.pymp4.parser import Box


def parse(*, url=None, data=None, source, session=None, downloader=None):
    """
    Convert an MPEG-DASH MPD (Media Presentation Description) document to a Tracks object
    with video, audio and subtitle track objects where available.
    """
    tracks = []
    if not data:
        if not url:
            raise ValueError("Neither a URL nor a document was provided to Tracks.from_mpd")
        base_url = url.rsplit('/', 1)[0] + '/'
        if downloader is None:
            res = (session or requests).get(url)
            res.raise_for_status()
            data = res.text
            if not data or not data.strip():
                raise ValueError(f"Received empty MPD document from {url}")
        elif downloader == "aria2c":
            out = os.path.join(config.directories.temp, url.split("/")[-1])
            asyncio.run(aria2c(url, out))

            with open(out, encoding="utf-8") as fd:
                data = fd.read()

            try:
                os.unlink(out)
            except FileNotFoundError:
                pass
        else:
            raise ValueError(f"Unsupported downloader: {downloader}")

    try:
        root = load_xml(data)
    except Exception as e:
        raise ValueError(f"Failed to parse MPD XML document: {e}") from e
    if root.tag != "MPD":
        raise ValueError("Non-MPD document provided to Tracks.from_mpd")

    # Global durations
    mpd_duration = root.get("mediaPresentationDuration")
    if mpd_duration:
        mpd_duration = Track.pt_to_sec(mpd_duration)

    periods = root.findall("Period")

    for period in periods:
        if source == "HULU" and next(iter(period.xpath("SegmentType/@value")), "content") != "content":
            continue

        period_duration = period.get("duration")
        if period_duration:
            period_duration = Track.pt_to_sec(period_duration)

        # Skip bumper / pre-roll / interstitial periods in multi-period manifests
        if len(periods) > 1:
            period_id = str(period.get("id") or "").lower()
            if period_id.startswith("seq:-") or period_id in ("bumper", "preroll", "ad"):
                continue
            if period_duration and period_duration <= 15:
                has_longer_period = any(
                    (Track.pt_to_sec(other_p.get("duration")) if other_p.get("duration") else 0) > 60
                    for other_p in periods
                )
                if has_longer_period:
                    continue

        final_duration = period_duration or mpd_duration


        period_base_url = period.findtext("BaseURL") or root.findtext("BaseURL")
        if url and (not period_base_url or not re.match("^https?://", period_base_url.lower())):
            period_base_url = urllib.parse.urljoin(url, period_base_url)
            period_base_url = period_base_url.replace('fly.eu.prd.media.max.com', 'akm.eu.prd.media.max.com')
            period_base_url = period_base_url.replace('gcp.eu.prd.media.max.com', 'akm.eu.prd.media.max.com')
            period_base_url = period_base_url.replace('fly.latam.prd.media.max.com', 'akm.latam.prd.media.max.com')
            period_base_url = period_base_url.replace('gcp.latam.prd.media.max.com', 'akm.latam.prd.media.max.com')         

        for adaptation_set in period.findall("AdaptationSet"):
            if any(x.get("schemeIdUri") == "http://dashif.org/guidelines/trickmode"
                   for x in adaptation_set.findall("EssentialProperty")
                   + adaptation_set.findall("SupplementalProperty")):
                continue

            for rep in adaptation_set.findall("Representation"):
                try:
                    content_type = next(x for x in [
                        rep.get("contentType"),
                        rep.get("mimeType"),
                        adaptation_set.get("contentType"),
                        adaptation_set.get("mimeType")
                    ] if bool(x))
                except StopIteration:
                    raise ValueError("No content type value could be found")
                else:
                    content_type = content_type.split("/")[0]
                
                if content_type.startswith("image"):
                    continue 

                codecs = rep.get("codecs") or adaptation_set.get("codecs")
                supplementalcodecs = rep.get("{urn:scte:dash:scte214-extensions}supplementalCodecs") or adaptation_set.get("{urn:scte:dash:scte214-extensions}supplementalCodecs")
                
                if content_type in ("text", "application"):
                    mime = adaptation_set.get("mimeType")
                    if mime and not mime.endswith("/mp4"):
                        codecs = mime.split("/")[1]

                track_lang = None
                for lang in [rep.get("lang"), adaptation_set.get("lang")]:
                    lang = (lang or "").strip()
                    if not lang:
                        continue
                    try:
                        t = Language.get(lang.split("-")[0])
                        if t == Language.get("und") or not t.is_valid():
                            raise LanguageTagError()
                    except LanguageTagError:
                        continue
                    else:
                        track_lang = Language.get(lang)
                        break

                protections = rep.findall("ContentProtection") + adaptation_set.findall("ContentProtection")
                encrypted = bool(protections)
                pssh = None
                kid = None
                pr_pssh = None
                for protection in protections:
                    kid = protection.get("default_KID")
                    if kid:
                        kid = uuid.UUID(kid).hex
                    else:
                        kid = protection.get("kid")
                        if kid:
                            kid = uuid.UUID(bytes_le=base64.b64decode(kid)).hex
                            
                    if "9a04f079-9840-4286-ab92-e65be0885f95" in protection.get("schemeIdUri", "").lower():
                        pr_pssh = protection.findtext("pro") if source in ["STAN", "RKTN", "CR"] else protection.findtext("pssh")
                    if (protection.get("schemeIdUri") or "").lower() != Cdm.urn:
                        continue
                    pssh = protection.findtext("pssh")
                    if pssh:
                        pssh = base64.b64decode(pssh)
                        try:
                            pssh = Box.parse(pssh)
                        except Exception:
                            pssh = Box.parse(Box.build(dict(
                                type="pssh",
                                data=dict(
                                    version=0,
                                    flags=0,
                                    system_ID=Cdm.uuid,
                                    init_data=pssh
                                )
                            )))

                rep_base_url = rep.findtext("BaseURL")
                if rep_base_url and not re.match("^https?://", rep_base_url.lower()):
                    rep_base_url = urllib.parse.urljoin(period_base_url, rep_base_url)
                elif not rep_base_url:
                    rep_base_url = period_base_url
                segment_list = rep.find("SegmentList")
                if segment_list is None:
                    segment_list = adaptation_set.find("SegmentList")

                segment_template_rep = rep.find("SegmentTemplate")
                segment_template_set = adaptation_set.find("SegmentTemplate")
                if segment_template_rep is not None:
                    segment_template = copy(segment_template_rep)
                    if segment_template_set is not None:
                        for k, v in segment_template_set.attrib.items():
                            if k not in segment_template.attrib:
                                segment_template.set(k, v)
                        if segment_template.find("SegmentTimeline") is None:
                            timeline_set = segment_template_set.find("SegmentTimeline")
                            if timeline_set is not None:
                                segment_template.append(copy(timeline_set))
                else:
                    segment_template = segment_template_set

                if segment_list is not None:
                    segment_urls = segment_list.findall("SegmentURL")
                    is_byte_range_single_file = bool(segment_urls) and all(
                        not s.get("media") and bool(s.get("mediaRange")) for s in segment_urls
                    )
                    if is_byte_range_single_file:
                        if not re.match("^https?://", rep_base_url.lower()):
                            rep_base_url = urllib.parse.urljoin(period_base_url, rep_base_url)
                        query = urllib.parse.urlparse(url).query if url else ""
                        if query and not urllib.parse.urlparse(rep_base_url).query:
                            rep_base_url += "?" + query
                        track_url = rep_base_url
                    else:
                        track_url = []
                        init_node = segment_list.find("Initialization")
                        query = urllib.parse.urlparse(url).query if url else ""
                        if init_node is not None:
                            init_url = init_node.get("sourceURL")
                            if not init_url and init_node.get("range"):
                                init_url = rep_base_url
                            if init_url:
                                full_init_url = urllib.parse.urljoin(rep_base_url, init_url)
                                if query and "?" not in full_init_url:
                                    full_init_url += "?" + query
                                track_url.append(full_init_url)
                        
                        for seg_url in segment_urls:
                            media_url = seg_url.get("media")
                            if not media_url and seg_url.get("mediaRange"):
                                media_url = rep_base_url
                            if media_url:
                                full_url = urllib.parse.urljoin(rep_base_url, media_url)
                                if query and "?" not in full_url:
                                    full_url += "?" + query
                                track_url.append(full_url)

                elif segment_template is not None:
                    segment_template = copy(segment_template)

                    for item in ("initialization", "media"):
                        if not segment_template.get(item):
                            continue
                        segment_template.set(
                            item, segment_template.get(item).replace("$RepresentationID$", rep.get("id"))
                        )
                        query = urllib.parse.urlparse(url).query
                        if query and not urllib.parse.urlparse(segment_template.get(item)).query:
                            segment_template.set(item, segment_template.get(item) + "?" + query)
                        if not re.match("^https?://", segment_template.get(item).lower()):
                            segment_template.set(item, urllib.parse.urljoin(
                                period_base_url if not rep_base_url else rep_base_url, segment_template.get(item)
                            ))

                    track_url = []

                    def replace_fields(url_str, **kwargs):
                        for field, value in kwargs.items():
                            url_str = url_str.replace(f"${field}$", str(value))
                            m = re.search(fr"\${re.escape(field)}%([a-z0-9]+)\$", url_str, flags=re.I)
                            if m:
                                url_str = url_str.replace(m.group(), f"{value:{m.group(1)}}")
                        return url_str

                    initialization = segment_template.get("initialization")
                    if initialization:
                        track_url.append(replace_fields(
                            initialization,
                            Bandwidth=rep.get("bandwidth"),
                            RepresentationID=rep.get("id")
                        ))

                    start_number = int(segment_template.get("startNumber") or 1)
                    segment_timeline = segment_template.find("SegmentTimeline")
                    if segment_timeline is not None:
                        seg_time_list = []
                        current_time = 0
                        for s in segment_timeline.findall("S"):
                            if s.get("t"):
                                current_time = int(s.get("t"))
                            for _ in range(1 + (int(s.get("r") or 0))):
                                seg_time_list.append(current_time)
                                current_time += int(s.get("d"))
                        seg_num_list = list(range(start_number, len(seg_time_list) + start_number))
                        track_url += [
                            replace_fields(
                                segment_template.get("media"),
                                Bandwidth=rep.get("bandwidth"),
                                Number=n,
                                RepresentationID=rep.get("id"),
                                Time=t
                            )
                            for t, n in zip(seg_time_list, seg_num_list)
                        ]
                    else:
                        calc_duration = period_duration or mpd_duration
                        segment_duration = (
                            float(segment_template.get("duration")) / float(segment_template.get("timescale") or 1)
                        )
                        total_segments = math.ceil(calc_duration / segment_duration)
                        track_url += [
                            replace_fields(
                                segment_template.get("media"),
                                Bandwidth=rep.get("bandwidth"),
                                Number=s,
                                RepresentationID=rep.get("id"),
                                Time=s
                            )
                            for s in range(start_number, start_number + total_segments)
                        ]

                elif rep_base_url and (
                    source not in ["DSCP", "DSNY"]
                ):
                    if not re.match("^https?://", rep_base_url.lower()):
                        rep_base_url = urllib.parse.urljoin(period_base_url, rep_base_url)
                    query = urllib.parse.urlparse(url).query
                    if query and not urllib.parse.urlparse(rep_base_url).query:
                        rep_base_url += "?" + query
                    track_url = rep_base_url
                else:
                    raise ValueError("Couldn't find a SegmentTemplate, SegmentList or fallback BaseURL for a Representation.")

                track_id_str = "{codec}-{lang}-{bitrate}-{extra}".format(
                    codec=codecs,
                    lang=track_lang,
                    bitrate=rep.get("bandwidth") or 0,
                    extra=(adaptation_set.get("audioTrackId") or "") + (rep.get("id") or ""),
                )
                track_id = md5(track_id_str.encode()).hexdigest()


                def get_track_size(track_repr):
                    segment_list = track_repr.findall('SegmentList')
                    if segment_list:
                        urls_with_range = [s for s in segment_list[0].findall('SegmentURL') if s.get('mediaRange')]
                        
                        if urls_with_range:
                            file_size = sorted(urls_with_range, key=lambda s: int(s.get('mediaRange').split('-')[1]), reverse=True)
                            return int(file_size[0].get('mediaRange').split('-')[1])
                    return None
                
                if content_type == "video":
                    tracks.append(VideoTrack(
                        id_=track_id,
                        source=source,
                        url=track_url,
                        size=get_track_size(track_repr=rep),
                        codec=(codecs or "").split(".")[0],
                        language=track_lang,
                        bitrate=rep.get("bandwidth"),
                        width=int(rep.get("width") or 0) or adaptation_set.get("width"),
                        height=int(rep.get("height") or 0) or adaptation_set.get("height"),
                        fps=rep.get("frameRate") or adaptation_set.get("frameRate"),
                        hdr10=any(
                            x.get("schemeIdUri") == "urn:mpeg:mpegB:cicp:TransferCharacteristics"
                            and x.get("value") == "16"
                            for x in adaptation_set.findall("SupplementalProperty")
                        ) or any(
                            x.get("schemeIdUri") == "http://dashif.org/metadata/hdr"
                            and x.get("value") == "SMPTE2094-40"
                            for x in adaptation_set.findall("SupplementalProperty")
                        ),
                        hlg=any(
                            x.get("schemeIdUri") == "urn:mpeg:mpegB:cicp:TransferCharacteristics"
                            and x.get("value") == "18"
                            for x in adaptation_set.findall("SupplementalProperty")
                        ),
                        dvhdr=(
                            (isinstance(codecs, str) and codecs.startswith(("dvhe.08", "dvh1.08"))) or
                            (isinstance(supplementalcodecs, str) and "dvh1.08" in supplementalcodecs)
                        ),
                        dv=codecs and codecs.startswith(("dvhe", "dvh1")),
                        descriptor=Track.Descriptor.MPD,
                        encrypted=encrypted,
                        pssh=pssh,
                        pr_pssh=pr_pssh,
                        kid=kid,
                        duration=final_duration,
                        original_url=url,
                        extra=(rep, adaptation_set)
                    ))
                elif content_type == "audio":
                    subtype = adaptation_set.get("audioTrackSubtype", "").lower()
                    is_boosted = "boosteddialog" in subtype
                    
                    boost_level = None
                    if is_boosted:
                        if any(x in subtype for x in ["medium", "med", "mid"]):
                            boost_level = "Medium"
                        elif "low" in subtype: 
                            boost_level = "Low"
                        elif any(x in subtype for x in ["high", "hi"]):
                            boost_level = "High"
                        else:
                            boost_level = "High"

                    tracks.append(AudioTrack(
                        id_=track_id,
                        source=source,
                        url=track_url,
                        size=get_track_size(track_repr=rep),
                        codec=(codecs or "").split(".")[0],
                        language=track_lang,
                        bitrate=rep.get("bandwidth"),
                        channels=next(iter(
                            rep.xpath("AudioChannelConfiguration/@value")
                            or adaptation_set.xpath("AudioChannelConfiguration/@value")
                        ), None),
                        descriptive=any(
                            (x.get("schemeIdUri") == "urn:mpeg:dash:role:2011" and x.get("value") == "description")
                            or (x.get("schemeIdUri") == "urn:tva:metadata:cs:AudioPurposeCS:2007" and x.get("value") == "1")
                            for x in adaptation_set.findall("Accessibility")
                        ),
                        atmos=any(
                            prop.get("schemeIdUri") == "tag:dolby.com,2018:dash:EC3_ExtensionType:2018" and prop.get("value") == "JOC"
                            for prop in rep.findall("SupplementalProperty")
                        ),
                        boosted_dialog=is_boosted,
                        boosted_dialog_level=boost_level,
                        
                        descriptor=Track.Descriptor.MPD,
                        encrypted=encrypted,
                        pssh=pssh,
                        pr_pssh=pr_pssh,
                        kid=kid,
                        duration=final_duration,
                        original_url=url,
                        extra=(rep, adaptation_set)
                    ))
                elif content_type in ("text", "application"):
                    if source == '#HMAX':
                        st = rep.find("SegmentTemplate")
                        sub_path_url = rep.findtext("BaseURL")
                        if not sub_path_url:
                            sub_path_url = st.get('media')
                        try:
                            path = re.search(r'(t\/.+?\/)t', sub_path_url).group(1)
                        except AttributeError:
                            path = 't/sub/'
                        
                        is_normal = any(x.get("value") == "subtitle" for x in adaptation_set.findall("Role"))
                        is_sdh = any(x.get("value") == "caption" for x in adaptation_set.findall("Role"))
                        is_forced = any(x.get("value") == "forced-subtitle" for x in adaptation_set.findall("Role"))

                        if is_normal:
                            track_url = [base_url + path + adaptation_set.get('lang') + '_sub.vtt']
                        elif is_sdh:
                            track_url = [base_url + path + adaptation_set.get('lang') + '_sdh.vtt']
                        elif is_forced:
                            track_url = [base_url + path + adaptation_set.get('lang') + '_forced.vtt']

                        tracks.append(TextTrack(
                            id_=track_id,
                            source=source,
                            url=track_url,
                            codec=(codecs or "").split(".")[0],
                            language=track_lang,
                            forced=is_forced,
                            sdh=is_sdh,
                            descriptor=Track.Descriptor.MPD,
                            extra=(rep, adaptation_set)
                        ))
                    else:
                        tracks.append(TextTrack(
                            id_=track_id,
                            source=source,
                            url=track_url,
                            codec=(codecs or "").split(".")[0],
                            language=track_lang,
                            descriptor=Track.Descriptor.MPD,
                            extra=(rep, adaptation_set)
                        ))

    tracks_obj = Tracks()
    tracks_obj.add(tracks, warn_only=True)

    return tracks_obj
