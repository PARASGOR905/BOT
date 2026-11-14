from __future__ import annotations

import logging
import re
import m3u8
import click
from hashlib import md5

from pystorytv.client import StoryTVClient
from pystorytv.config import Session as StorySession

from vinetrimmer.objects import Title, Track, Tracks, VideoTrack, AudioTrack, TextTrack
from vinetrimmer.services.BaseService import BaseService


class StoryTV(BaseService):
    """
    Service code for StoryTV (https://app.storytv.asia).

    Authorization: pystorytv session token (optional, premium requires login)
    Security: Unencrypted HLS streams
    """

    ALIASES = ["STORYTV", "storytv", "story"]
    GEOFENCE = []
    TITLE_RE = [
        r"^https?://(?:www\.)?(?:app\.)?storytv\.asia/shows/(?P<slug>[^/]+)-(?P<id>\d+)(?:/(?P<episode>\d+))?(?:\?.*)?$",
        r"^https?://(?:www\.)?(?:app\.)?storytv\.asia/shows/(?P<id>\d+)(?:/(?P<episode>\d+))?(?:\?.*)?$",
        r"^(?P<id>\d+)$",
    ]

    @staticmethod
    @click.command(name="StoryTV", short_help="https://app.storytv.asia")
    @click.argument("title", type=str)
    @click.pass_context
    def cli(ctx, **kwargs):
        return StoryTV(ctx, **kwargs)

    def __init__(self, ctx, title: str):
        super().__init__(ctx)
        assert ctx.parent is not None

        parsed = self.parse_title(ctx, title) or {}
        self.content_id = parsed.get("id")
        if not self.content_id:
            self.log.exit(f" - Could not extract show ID from '{title}'")

        self.wanted = ctx.parent.params.get("wanted")
        self.vcodec = ctx.parent.params.get("vcodec") or "H264"

        # Load session using pystorytv config loader
        session_obj = StorySession.load()
        if not session_obj:
            self.log.warning("No active StoryTV session found. Let's log in to your StoryTV account.")
            try:
                from pystorytv.auth import interactive_login
                otpless_token, id_token = interactive_login()
                temp_client = StoryTVClient(session=None)
                auth_resp = temp_client.verify_otpless(otpless_token, id_token)
                session_obj = StorySession(
                    jwt=auth_resp.jwt,
                    rft=auth_resp.rft,
                    user_id=auth_resp.user_id,
                    session_id=auth_resp.session_id,
                    mobile=auth_resp.mobile,
                    sub_stat=auth_resp.sub_stat,
                )
                session_obj.save()
                self.log.info("Successfully logged in and saved session!")
            except Exception as e:
                self.log.exit(f" - Login failed: {e}. An active session is required to fetch details.")

        self.client = StoryTVClient(session=session_obj)

    def get_titles(self) -> list[Title]:
        try:
            info = self.client.get_episode_list(self.content_id)
        except Exception as e:
            self.log.exit(f" - StoryTV API returned error: {e}")

        show_title = info.show_title or f"StoryTV-{self.content_id}"
        logging.getLogger("Titles").info(f"Title: {show_title}")

        titles = []
        for ep in info.episodes:
            ep_order = ep.index
            if self.wanted and str(ep_order) not in self.wanted and f"S01E{ep_order:02}" not in ",".join(self.wanted or []):
                continue

            titles.append(Title(
                id_=str(ep.index),
                type_=Title.Types.TV,
                name=show_title,
                season=1,
                episode=ep_order,
                episode_name=ep.ep_title,
                original_lang="hin",
                source=self.ALIASES[0],
                service_data={
                    "index": ep.index,
                    "show_id": self.content_id,
                    "show_title": show_title,
                }
            ))

        if not titles:
            self.log.exit(" - No StoryTV episodes matched or found.")
        return titles

    def get_tracks(self, title: Title) -> Tracks:
        meta = title.service_data
        show_id = meta["show_id"]
        ep_index = meta["index"]

        # Fetch signed HLS metadata for the show's episodes
        try:
            episodes_meta = self.client.get_all_episode_metadata(show_id)
        except Exception as e:
            self.log.exit(f" - Failed to get episode metadata: {e}")

        matched_ep = next((e for e in episodes_meta if e.index == ep_index), None)
        if not matched_ep:
            self.log.exit(f" - Episode index {ep_index} metadata/URL not found or not returned.")

        m3u8_url = matched_ep.eps_url
        if not m3u8_url:
            if matched_ep.lock:
                self.log.exit(f" - Episode {ep_index} is locked (Premium). Please log in using pystorytv first.")
            else:
                self.log.exit(f" - Episode {ep_index} stream URL is empty.")

        self.log.debug(" + HLS URL: %s", m3u8_url[:120])

        resp = self.session.get(m3u8_url, timeout=10)
        resp.raise_for_status()
        master = m3u8.loads(resp.text, uri=resp.url)

        # Map to vinetrimmer Tracks
        tracks = Tracks.from_m3u8(master, source=self.ALIASES[0])

        # Fill default bitrates if not populated in m3u8
        for track in tracks.audios:
            if not track.bitrate or track.bitrate == 0:
                track.bitrate = 128000  # Default 128 kbps

        # Force unencrypted (StoryTV does not use DRM, just HLS)
        for track in tracks.videos + tracks.audios + tracks.subtitles:
            track.encrypted = False

        return tracks

    def get_chapters(self, title):
        return []
