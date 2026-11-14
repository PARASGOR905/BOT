from __future__ import annotations

import re
import click

from vinetrimmer.objects import Title, Tracks
from vinetrimmer.services.BaseService import BaseService


class ManoramaMAX(BaseService):
    """
    Service code for ManoramaMAX (https://www.manoramamax.com/)
    """

    ALIASES = ["MMAX", "manoramamax", "ManoramaMAX"]
    TITLE_RE = r"https?://(?:www\.)?manoramamax\.com/(?:(?:movie|show|series|watch)/)?(?P<id>[-\w]+)"

    @staticmethod
    @click.command(name="ManoramaMAX", short_help="https://www.manoramamax.com/", help=__doc__)
    @click.argument("title", type=str)
    @click.pass_context
    def cli(ctx, **kwargs):
        return ManoramaMAX(ctx, **kwargs)

    def __init__(self, ctx, title):
        self.url = title
        super().__init__(ctx)
        self.parse_title(ctx, self.url)

        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "Referer": "https://www.manoramamax.com/",
            "Origin": "https://www.manoramamax.com",
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
        raise ValueError("ManoramaMAX playback stream parsing is under development.")

    def get_chapters(self, title):
        return []

    def certificate(self, **_):
        return None

    def license(self, challenge, **_):
        return None
