"""Environment-backed settings for the theme agent. No secret appears in source.

Its own class rather than fields on `tv_avatar.config.Settings`: that one requires
the Nebius, SLNG and Anam keys, and a proxy for a background video has no use for
them. FAL_KEY is the name fal's own SDKs read, so an existing .env line keeps working.
"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class ThemeAgentSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Blank starts the server anyway and answers 503 on the proxy route, the way
    # create_app() starts with no .env; the health route says which it is.
    fal_key: str = ""
    # The only model this proxy will spend the key on.
    theme_agent_endpoint: str = "minimax/h3-max/director"
    # Comma-separated origins allowed besides localhost: a TV build's, or the app opened
    # by a LAN address. Blank by default.
    theme_agent_origins: str = ""
    # Localhost on any port. Vite takes the next free port whenever 5173 is busy, and a
    # fixed list of dev origins refused every one it moved to. Switch off anywhere that
    # is not a developer's own machine.
    theme_agent_allow_localhost: bool = True
    # A Director session bills at least 60 s at $0.08/s. The route is reachable
    # from any browser on the allowed origins, so the day's total is capped here.
    theme_agent_max_sessions_per_day: int = 10

    @property
    def fal_key_problem(self) -> str | None:
        """Why the key cannot be sent, or None if it can. Worded for a person.

        The key goes into an HTTP header, which is ASCII and cannot hold whitespace. A
        .env line typed with quotes, on a layout where the quote key is a dead key,
        ends up wrapped in diaereses, which used to crash the proxy on its first
        request. Only the offending characters are named, by code point and position:
        they cannot be part of a real key, so this never repeats any of it.
        """
        if not self.fal_key:
            return "The theme agent has no FAL_KEY configured"
        bad = [
            f"U+{ord(c):04X} at position {i}"
            for i, c in enumerate(self.fal_key)
            if not (c.isascii() and c.isprintable() and not c.isspace())
        ]
        if not bad:
            return None
        return (
            "The theme agent's FAL_KEY has characters a key cannot contain ("
            + ", ".join(bad[:4])
            + "). Write the key bare in .env, without quotes, and restart the agent."
        )

    @property
    def origins(self) -> list[str]:
        return [o.strip() for o in self.theme_agent_origins.split(",") if o.strip()]
