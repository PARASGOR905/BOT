from __future__ import annotations

import base64
import hashlib
import re
import click
import m3u8

from vinetrimmer.objects import Title, Tracks, TextTrack
from vinetrimmer.services.BaseService import BaseService


class Playflix(BaseService):
    """
    Service code for Playflix (https://www.playflix.app/)
    """

    ALIASES = ["PF", "playflix", "Playflix"]

    TITLE_RE = [
        r"https?://(?:www\.)?playflix\.app/content/(?P<id>[a-zA-Z0-9+/=]+)",
        r"^playflix:(?P<id>[a-zA-Z0-9+/=]+)$",
    ]

    @staticmethod
    @click.command(name="Playflix", short_help="https://www.playflix.app/", help=__doc__)
    @click.argument("title", type=str)
    @click.pass_context
    def cli(ctx, **kwargs):
        return Playflix(ctx, **kwargs)

    def __init__(self, ctx, title):
        self.url = title
        super().__init__(ctx)
        self.parse_title(ctx, self.url)

        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "Accept": "*/*",
            "Origin": "https://www.playflix.app",
            "Referer": "https://www.playflix.app/",
        })

    def get_titles(self):
        # Decode base64 title ID to get the content ID
        raw_id = self.title
        try:
            # Add padding if missing
            padding = len(raw_id) % 4
            if padding:
                raw_id += "=" * (4 - padding)
            decoded_bytes = base64.b64decode(raw_id)
            content_id = decoded_bytes.decode("utf-8")
        except Exception:
            content_id = raw_id

        self.log.debug(f"Loading Playflix page for Content ID: {content_id}")
        r = self.session.get(self.url)
        r.raise_for_status()
        html = r.text

        # Extract title name from HTML (look for h4 or title tag)
        match_h4 = re.search(r'<h4[^>]*>(.*?)</h4>', html, re.DOTALL | re.IGNORECASE)
        if match_h4:
            title_text = re.sub(r'\s+', ' ', match_h4.group(1)).strip()
        else:
            match_title = re.search(r'<title>(.*?)</title>', html, re.IGNORECASE)
            title_text = match_title.group(1) if match_title else "Playflix Video"
            # Clean title text
            title_text = (
                title_text.replace("Watch ", "")
                .replace(" Full Movie Online on Playflix", "")
                .replace(" on Playflix", "")
                .strip()
            )

        # Check if the page is prompting for login
        if "login-form" in html or "login" in html.lower() and "btn" in html:
            # Double check if we actually have cookies
            if not self.session.cookies.get("laravel_session"):
                self.log.warning(
                    "You do not seem to be logged in. Fresh cookies from playflix.app "
                    "should be placed in vinetrimmer/cookies/playflix/default.txt"
                )

        # Parse TV show details if title matches show format
        match_ep = re.search(
            r'^(?P<show_name>.+?)(?:\s*\([^)]+\))?:\s*(?:Episode|Ep\.?)\s*(?P<ep_num>\d+)',
            title_text,
            re.I
        )
        if match_ep:
            show_name = match_ep.group("show_name").strip()
            # Strip standard dub languages
            show_name = re.sub(
                r'\s*\((?:Korean|Hindi|Tamil|Telugu|English|Spanish|French|German|Japanese|Chinese|Malayalam|Kannada|Marathi|Punjabi|Bengali)\)',
                '',
                show_name,
                flags=re.I
            ).strip()
            episode = int(match_ep.group("ep_num"))

            # Parse all episodes from the playlist slider container
            titles = []
            slider_match = re.search(r'id\s*=\s*["\']playlist_slider["\'][^>]*>(.*?)(?:<script|<footer>)', html, re.DOTALL)
            if slider_match:
                slider_html = slider_match.group(1)
                # Parse horizontal-thumbnail-container playTrailer div blocks
                ep_blocks = re.findall(r'<div[^>]*class\s*=\s*["\'][^"\']*playTrailer[^"\']*["\'][^>]*>(.*?)</div>', slider_html, re.DOTALL)
                for block in ep_blocks:
                    find_match = re.search(r'data-find\s*=\s*["\']([a-zA-Z0-9+/=]+)["\']', block)
                    name_match = re.search(r'data-name\s*=\s*["\']([^"\']+)["\']', block)
                    if find_match and name_match:
                        b64_id = find_match.group(1)
                        name = name_match.group(1)
                        try:
                            padding = len(b64_id) % 4
                            if padding:
                                b64_id += "=" * (4 - padding)
                            ep_content_id = base64.b64decode(b64_id).decode('utf-8')
                        except Exception:
                            ep_content_id = b64_id
                            
                        ep_match = re.search(r'\d+', name)
                        ep_num = int(ep_match.group(0)) if ep_match else 1
                        
                        titles.append(Title(
                            id_=ep_content_id,
                            type_=Title.Types.TV,
                            name=show_name,
                            season=1,
                            episode=ep_num,
                            episode_name=name,
                            source=self.ALIASES[0],
                            service_data={"content_id": ep_content_id},
                        ))
            
            # Fallback if slider is empty or not found
            if not titles:
                titles.append(Title(
                    id_=content_id,
                    type_=Title.Types.TV,
                    name=show_name,
                    season=1,
                    episode=episode,
                    episode_name=f"Episode {episode}",
                    source=self.ALIASES[0],
                    service_data={"content_id": content_id},
                ))
            return titles
        else:
            cleaned_title = re.sub(
                r'\s*\((?:Korean|Hindi|Tamil|Telugu|English|Spanish|French|German|Japanese|Chinese|Malayalam|Kannada|Marathi|Punjabi|Bengali)\)',
                '',
                title_text,
                flags=re.I
            ).strip()
            return Title(
                id_=content_id,
                type_=Title.Types.MOVIE,
                name=cleaned_title,
                source=self.ALIASES[0],
                service_data={"content_id": content_id},
            )

    def get_tracks(self, title):
        content_id = title.service_data["content_id"]
        b64_id = base64.b64encode(str(content_id).encode("utf-8")).decode("utf-8")
        watch_url = f"https://www.playflix.app/watch/{b64_id}"
        self.log.debug(f"Loading Playflix Watch page: {watch_url}")
        
        r = self.session.get(watch_url)
        r.raise_for_status()
        watch_html = r.text

        video_url = None
        # Extract base64-encoded m3u8 URL robustly
        for m in re.finditer(r'["\']([a-zA-Z0-9+/=]{20,})["\']', watch_html):
            try:
                val = m.group(1)
                padding = len(val) % 4
                if padding:
                    val += "=" * (4 - padding)
                decoded = base64.b64decode(val).decode('utf-8')
                if decoded.startswith('http') and '.m3u8' in decoded:
                    video_url = decoded
                    break
            except Exception:
                continue

        # Plaintext fallback
        if not video_url:
            m3u8_matches = re.findall(r'https?://[^\s"\'`<>]+?\.m3u8[^\s"\'`<>]*', watch_html)
            if m3u8_matches:
                video_url = m3u8_matches[0].replace("&amp;", "&")

        if not video_url:
            if "Pick Your Subscription" in watch_html or "subscribeNow" in watch_html:
                self.log.warning(
                    f"Episode {title.episode} is locked (requires subscription). "
                    f"Skipping since your session is not subscribed."
                )
            elif "login-form" in watch_html or ("login" in watch_html.lower() and "btn" in watch_html):
                self.log.warning(
                    f"Not logged in or session expired when accessing watch page for Episode {title.episode}. "
                    f"Please check your cookies in vinetrimmer/cookies/playflix/default.txt"
                )
            else:
                self.log.warning(
                    f"Could not find HLS stream URL on the watch page for Episode {title.episode}."
                )
            return Tracks()

        self.log.debug(f"Found Playflix HLS Stream URL: {video_url}")

        playlist_res = self.session.get(video_url)
        playlist_res.raise_for_status()
        
        master = m3u8.loads(playlist_res.text, uri=video_url)
        tracks = Tracks.from_m3u8(master, source=self.ALIASES[0])

        # Mark tracks as unencrypted (clear/standard AES-128 HLS)
        for track in tracks.videos + tracks.audios + tracks.subtitles:
            track.encrypted = False

        return tracks

    def get_chapters(self, title):
        return []

    def certificate(self, **_):
        return None

    def license(self, challenge, **_):
        return None
