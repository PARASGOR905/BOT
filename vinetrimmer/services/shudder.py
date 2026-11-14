import click

from vinetrimmer.objects import Title, Tracks
from vinetrimmer.services.BaseService import BaseService


class Shudder(BaseService):
    """
    Service code for Shudder (https://www.shudder.com/).
    
    \b
    Authorization: Required
    Security: HD@L3
    """

    ALIASES = ["SHUDDER", "shudder"]
    
    # Matches shudder URLs like: https://watch.shudder.com/video/991645 or https://www.shudder.com/movies/watch/...
    TITLE_RE = (
        r"^(?:https?://(?:[a-z0-9-]+\.)?shudder\.com(?:/[a-z]{2})?)?"
        r"/(?P<type>video|movies|series)(?:/watch/[^/?#]+)?/"
        r"(?P<id>[0-9a-zA-Z-]+)(?:[?#].*)?$"
    )

    @staticmethod
    @click.command(name="Shudder", short_help="https://www.shudder.com")
    @click.argument("title", type=str, required=False)
    @click.pass_context
    def cli(ctx, **kwargs):
        return Shudder(ctx, **kwargs)

    def __init__(self, ctx, title):
        super().__init__(ctx)
        parsed = self.parse_title(ctx, title) or {}
        self.title_type = parsed.get("type")
        self.configure()

    def get_titles(self):
        """
        Fetch the title(s) metadata (Movies or Episodes) from the Shudder API.
        """
        self.log.exit(
            " - Shudder playback is not implemented in this service. "
            "Use Shudder's official web or app playback."
        )

    def get_tracks(self, title):
        """
        Get the MPD/M3U8 url and extract the tracks.
        """
        self.log.exit(
            " - Shudder playback is not implemented in this service. "
            "Use Shudder's official web or app playback."
        )

    def get_chapters(self, title):
        return []

    def certificate(self, **kwargs):
        # Shudder usually doesn't need a specific cert, just hit the license URL, or hardcode it
        return None

    def license(self, challenge, **kwargs):
        """
        Send the license challenge to the Shudder Widevine license server.
        """
        return self.session.post(
            url=self.config["endpoints"]["license"],
            data=challenge,
            # headers={"Authorization": f"Bearer {self.config.credentials.token}"} # Adjust as needed
        ).content

    # Service specific functions
    def configure(self):
        """
        Authenticate or configure headers needed for Shudder API calls.
        """
        # TODO: Load credentials, authenticate, set session headers
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
        })
