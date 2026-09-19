"""Avatar and language catalog, loaded from ``avatars.yaml``.

A session pins one ``AvatarProfile`` and one ``LanguageProfile`` at creation
and never changes them: swapping voice or language mid-session forces a SLNG
reconnect (heard as a gap) and an Anam persona restart (seen as a cut).
"""
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pipecat.transcriptions.language import Language
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

#: The languages the product supports. Adding one here is deliberate: the
#: system prompt, STT model and TTS voice all have to be checked against it.
LanguageCode = Literal["en", "es", "fr", "ca"]

DEFAULT_CATALOG_PATH = Path(__file__).resolve().parents[2] / "avatars.yaml"
CATALOG_PATH_ENV = "AVATARS_FILE"


class LanguageProfile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    code: LanguageCode
    #: English name, used inside the (English-written) system prompt.
    name: str = Field(min_length=1)
    #: Name in the language itself, shown to the viewer in pickers.
    native_name: str = Field(min_length=1)
    #: Language code handed to TTS when the synthesiser lacks this language.
    #: Cartesia Sonic has no Catalan, so ``ca`` is voiced as ``es``: the
    #: text is still Catalan, the pronunciation model is Spanish.
    tts_language: str | None = None

    @field_validator("tts_language")
    @classmethod
    def _known_to_pipecat(cls, v: str | None) -> str | None:
        if v is not None:
            try:
                Language(v)
            except ValueError as err:
                raise ValueError(f"tts_language {v!r} is not a Pipecat language code") from err
        return v

    @property
    def pipecat(self) -> Language:
        """The Pipecat enum SLNG forwards verbatim to Reson8 (STT)."""
        return Language(self.code)

    @property
    def pipecat_tts(self) -> Language:
        """What Cartesia is told to speak; differs from ``pipecat`` only via ``tts_language``."""
        return Language(self.tts_language or self.code)


class AvatarProfile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    name: str = Field(min_length=1)
    description: str = ""
    anam_avatar_id: str = Field(min_length=1)
    anam_avatar_model: str = "cara-4"
    #: Cartesia voice id. One per avatar; sonic-3 voices are multilingual.
    voice: str = Field(min_length=1)
    #: Session languages this avatar may be paired with. ``None`` = all.
    languages: tuple[LanguageCode, ...] | None = Field(default=None, min_length=1)

    def speaks(self, code: str) -> bool:
        return self.languages is None or code in self.languages

    def public(self, all_languages: tuple[LanguageCode, ...]) -> dict:
        """Non-provider view for ``/config``: no Anam or Cartesia ids."""
        return {"id": self.id, "name": self.name, "description": self.description,
                "avatar_model": self.anam_avatar_model,
                "languages": list(self.languages or all_languages)}


@dataclass(frozen=True)
class SessionPersona:
    """What a session speaks with, resolved once from the catalog."""
    avatar: AvatarProfile
    language: LanguageProfile


class AvatarCatalog(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    default_avatar: str
    default_language: LanguageCode = "en"
    languages: tuple[LanguageProfile, ...] = Field(min_length=1)
    avatars: tuple[AvatarProfile, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _consistent(self) -> "AvatarCatalog":
        ids = [a.id for a in self.avatars]
        if len(set(ids)) != len(ids):
            raise ValueError(f"duplicate avatar ids: {sorted(set(i for i in ids if ids.count(i) > 1))}")
        codes = [l.code for l in self.languages]
        if len(set(codes)) != len(codes):
            raise ValueError(f"duplicate language codes: {sorted(set(c for c in codes if codes.count(c) > 1))}")
        if self.default_avatar not in ids:
            raise ValueError(f"default_avatar {self.default_avatar!r} is not one of {ids}")
        if self.default_language not in codes:
            raise ValueError(f"default_language {self.default_language!r} is not one of {codes}")
        for a in self.avatars:
            unknown = [c for c in a.languages or () if c not in codes]
            if unknown:
                raise ValueError(f"avatar {a.id!r} lists languages not in the catalog: {unknown}")
        if not self.avatar(self.default_avatar).speaks(self.default_language):
            raise ValueError(f"default_avatar {self.default_avatar!r} does not speak "
                             f"default_language {self.default_language!r}")
        return self

    def avatar(self, avatar_id: str) -> AvatarProfile:
        for a in self.avatars:
            if a.id == avatar_id:
                return a
        raise KeyError(f"unknown avatar {avatar_id!r}; known: {[a.id for a in self.avatars]}")

    def language(self, code: str) -> LanguageProfile:
        for l in self.languages:
            if l.code == code:
                return l
        raise KeyError(f"unknown language {code!r}; known: {[l.code for l in self.languages]}")

    def resolve(self, avatar_id: str | None = None, language: str | None = None) -> SessionPersona:
        """Pick a persona, falling back to the catalog defaults.

        When ``language`` is omitted and the avatar cannot speak the catalog
        default, its first listed language is used instead, so a Spanish-only
        avatar can be requested without also naming ``es``.

        Raises ``KeyError`` for an unknown id or code, and for a pairing the
        avatar does not allow (an English-only avatar asked to speak Catalan).
        """
        avatar = self.avatar(avatar_id or self.default_avatar)
        if language is None:
            language = self.default_language if avatar.speaks(self.default_language) else avatar.languages[0]
        lang = self.language(language)
        if not avatar.speaks(lang.code):
            raise KeyError(f"avatar {avatar.id!r} does not speak {lang.code!r}; "
                           f"it speaks: {list(avatar.languages or ())}")
        return SessionPersona(avatar=avatar, language=lang)

    def public(self) -> dict:
        codes = tuple(l.code for l in self.languages)
        return {
            "default_avatar": self.default_avatar,
            "default_language": self.default_language,
            "avatars": [a.public(codes) for a in self.avatars],
            "languages": [l.model_dump(exclude={"tts_language"}) for l in self.languages],
        }


def load_catalog(path: Path) -> AvatarCatalog:
    try:
        with path.open(encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
    except FileNotFoundError as err:
        raise FileNotFoundError(f"avatar catalog not found at {path}") from err
    except yaml.YAMLError as err:
        raise ValueError(f"avatar catalog {path} is not valid YAML: {err}") from err
    if not isinstance(raw, dict):
        raise ValueError(f"avatar catalog {path} must be a mapping at the top level")
    return AvatarCatalog.model_validate(raw)


def catalog_path() -> Path:
    # Read directly rather than through Settings: the catalog must load even
    # when the provider keys are missing, so /config can still list options.
    return Path(os.environ.get(CATALOG_PATH_ENV) or DEFAULT_CATALOG_PATH)


@lru_cache
def get_catalog() -> AvatarCatalog:
    return load_catalog(catalog_path())
