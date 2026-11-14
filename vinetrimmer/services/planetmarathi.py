from __future__ import annotations

import re
import click

from vinetrimmer.objects import Title, Tracks
from vinetrimmer.services.BaseService import BaseService


class PlanetMarathi(BaseService):
    """
    Service code for Planet Marathi (https://www.planetmarathi.com/)
    """

    ALIASES = ["PMARATHI", "planetmarathi", "PlanetMarathi"]
    TITLE_RE = r"https?://(?:www\.)?planetmarathi\.com/(?:(?:movie|series|show|watch)/)?(?P<id>[-\w]+)"

    @staticmethod
    @click.command(name="PlanetMarathi", short_help="https://www.planetmarathi.com/", help=__doc__)
    @click.argument("title", type=str)
    @click.pass_context
    def cli(ctx, **kwargs):
        return PlanetMarathi(ctx, **kwargs)

    def __init__(self, ctx, title):
        self.url = title
        super().__init__(ctx)
        self.parse_title(ctx, self.url)

        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "Referer": "https://www.planetmarathi.com/",
            "Origin": "https://www.planetmarathi.com",
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
        raise ValueError("Planet Marathi playback stream parsing is under development.")

    def get_chapters(self, title):
        return []

    def certificate(self, **_):
        return None

    def license(self, challenge, **_):
        return None
