from __future__ import annotations

import re
import click

from vinetrimmer.objects import Title, Tracks
from vinetrimmer.services.BaseService import BaseService


class EPICON(BaseService):
    """
    Service code for EPIC ON (https://www.epicon.in/)
    """

    ALIASES = ["EPICON", "epicon", "EPICOn"]
    TITLE_RE = r"https?://(?:www\.)?epicon\.in/(?:(?:movie|show|series|watch)/)?(?P<id>[-\w]+)"

    @staticmethod
    @click.command(name="EPICON", short_help="https://www.epicon.in/", help=__doc__)
    @click.argument("title", type=str)
    @click.pass_context
    def cli(ctx, **kwargs):
        return EPICON(ctx, **kwargs)

    def __init__(self, ctx, title):
        self.url = title
        super().__init__(ctx)
        self.parse_title(ctx, self.url)

        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "Referer": "https://www.epicon.in/",
            "Origin": "https://www.epicon.in",
        })

    def get_titles(self):
        return Title(
            id_=self.title,
            type_=Title.Types.MOVIE,
            name=self.title.replace("-", " ").title(),
            source=self.ALIASES[0],
            service_data={},
        )

    def get_tracks(self, title):
        raise ValueError("EPIC ON playback stream parsing is under development.")

    def get_chapters(self, title):
        return []

    def certificate(self, **_):
        return None

    def license(self, challenge, **_):
        return None
