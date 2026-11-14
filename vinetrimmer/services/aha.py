from __future__ import annotations

import re
import click

from vinetrimmer.objects import Title, Tracks
from vinetrimmer.services.BaseService import BaseService


class Aha(BaseService):
    """
    Service code for Aha Video (https://www.aha.video/)
    """

    ALIASES = ["AHA", "aha", "Aha"]
    TITLE_RE = r"https?://(?:www\.)?aha\.video/(?:web/)?(?:movie|player/movie|player/episode|webseries)/[a-z0-9-]+-(?P<id>[a-z0-9]+)"

    @staticmethod
    @click.command(name="Aha", short_help="https://www.aha.video/", help=__doc__)
    @click.argument("title", type=str)
    @click.pass_context
    def cli(ctx, **kwargs):
        return Aha(ctx, **kwargs)

    def __init__(self, ctx, title):
        self.url = title
        super().__init__(ctx)
        self.parse_title(ctx, self.url)

        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "Referer": "https://www.aha.video/",
            "Origin": "https://www.aha.video",
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
        raise ValueError("Aha Video playback stream parsing is under development.")

    def get_chapters(self, title):
        return []

    def certificate(self, **_):
        return None

    def license(self, challenge, **_):
        return None
