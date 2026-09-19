from pathlib import Path

import pytest
from pipecat.transcriptions.language import Language
from pydantic import ValidationError

from tv_avatar.agent.prompt import initial_messages, system_prompt
from tv_avatar.catalog import (
    DEFAULT_CATALOG_PATH,
    AvatarCatalog,
    catalog_path,
    get_catalog,
    load_catalog,
)

LANGS = [
    {"code": "en", "name": "English", "native_name": "English"},
    {"code": "es", "name": "Spanish", "native_name": "Español"},
    {"code": "fr", "name": "French", "native_name": "Français"},
    {"code": "ca", "name": "Catalan", "native_name": "Català"},
]


def _raw(**over):
    base = {
        "default_avatar": "a",
        "default_language": "en",
        "languages": LANGS,
        "avatars": [
            {"id": "a", "name": "A", "anam_avatar_id": "anam-a", "voice": "voice-a"},
            {"id": "b", "name": "B", "anam_avatar_id": "anam-b", "voice": "voice-b",
             "anam_avatar_model": "cara-5"},
        ],
    }
    base.update(over)
    return base


# --- the shipped file ---------------------------------------------------------


def test_shipped_catalog_loads_and_covers_the_four_languages():
    cat = load_catalog(DEFAULT_CATALOG_PATH)
    assert [l.code for l in cat.languages] == ["en", "es", "fr", "ca"]
    assert [l.pipecat for l in cat.languages] == [Language.EN, Language.ES, Language.FR, Language.CA]
    assert cat.avatar(cat.default_avatar).voice


def test_shipped_igor_is_english_only_with_his_own_voice():
    cat = load_catalog(DEFAULT_CATALOG_PATH)
    igor = cat.avatar("igor")
    assert igor.voice == "228fca29-3a0a-435c-8728-5cb483251068"
    assert igor.languages == ("en",)
    assert igor.voice != cat.avatar("cara").voice
    assert cat.resolve("igor", "en").language.code == "en"
    with pytest.raises(KeyError, match="does not speak 'ca'"):
        cat.resolve("igor", "ca")


def test_shipped_lucia_is_spanish_only_with_a_distinct_anam_avatar():
    cat = load_catalog(DEFAULT_CATALOG_PATH)
    lucia = cat.avatar("lucia")
    assert lucia.languages == ("es",)
    assert lucia.anam_avatar_id != cat.avatar("cara").anam_avatar_id
    assert len({a.voice for a in cat.avatars}) == len(cat.avatars)
    assert cat.resolve("lucia", "es").language.pipecat == Language.ES
    # Omitting the language falls back to hers, not to the catalog default (en).
    assert cat.resolve("lucia").language.code == "es"
    with pytest.raises(KeyError, match="does not speak 'en'"):
        cat.resolve("lucia", "en")


def test_shipped_chloe_is_french_only():
    cat = load_catalog(DEFAULT_CATALOG_PATH)
    chloe = cat.avatar("chloe")
    assert chloe.languages == ("fr",)
    assert cat.resolve("chloe").language.pipecat == Language.FR
    # Every shipped avatar has its own Anam id and voice, except Igor's
    # placeholder video (tracked by the TODO in avatars.yaml).
    assert len({a.voice for a in cat.avatars}) == len(cat.avatars)
    assert len({a.anam_avatar_id for a in cat.avatars}) == len(cat.avatars) - 1


def test_get_catalog_is_cached_and_honours_env_override(monkeypatch, tmp_path):
    assert get_catalog() is get_catalog()
    custom = tmp_path / "c.yaml"
    custom.write_text("default_avatar: x\nlanguages: [{code: en, name: English, native_name: English}]\n"
                      "avatars: [{id: x, name: X, anam_avatar_id: i, voice: v}]\n", encoding="utf-8")
    monkeypatch.setenv("AVATARS_FILE", str(custom))
    assert catalog_path() == custom
    assert load_catalog(catalog_path()).default_avatar == "x"


# --- validation -----------------------------------------------------------------


def test_resolve_falls_back_to_defaults_and_pins_the_avatar_voice():
    cat = AvatarCatalog.model_validate(_raw())
    p = cat.resolve()
    assert (p.avatar.id, p.language.code) == ("a", "en")
    p = cat.resolve("b", "ca")
    assert (p.avatar.anam_avatar_model, p.avatar.voice, p.language.pipecat) == ("cara-5", "voice-b", Language.CA)


def test_unknown_avatar_or_language_raises_key_error_with_the_options():
    cat = AvatarCatalog.model_validate(_raw())
    with pytest.raises(KeyError, match="'a', 'b'"):
        cat.resolve("zzz")
    with pytest.raises(KeyError, match="unknown language"):
        cat.resolve(language="de")


@pytest.mark.parametrize("over, msg", [
    ({"default_avatar": "nope"}, "default_avatar"),
    ({"default_language": "fr", "languages": LANGS[:1]}, "default_language"),
    ({"avatars": [{"id": "a", "name": "A", "anam_avatar_id": "x", "voice": "v"}] * 2}, "duplicate avatar"),
    ({"languages": LANGS + LANGS[:1]}, "duplicate language"),
    ({"languages": LANGS + [{"code": "de", "name": "German", "native_name": "Deutsch"}]}, "code"),
    ({"avatars": [{"id": "Bad Id", "name": "A", "anam_avatar_id": "x", "voice": "v"}]}, "pattern"),
    ({"avatars": [{"id": "a", "name": "A", "anam_avatar_id": "", "voice": "v"}]}, "anam_avatar_id"),
    ({"avatars": []}, "avatars"),
    ({"avatars": [{"id": "a", "name": "A", "anam_avatar_id": "x", "voice": "v", "languages": ["de"]}]}, "literal_error"),
    ({"avatars": [{"id": "a", "name": "A", "anam_avatar_id": "x", "voice": "v", "languages": []}]}, "languages"),
    ({"avatars": [{"id": "a", "name": "A", "anam_avatar_id": "x", "voice": "v", "languages": ["es"]}],
      "languages": LANGS}, "does not speak default_language"),
    ({"languages": LANGS[:2],
      "avatars": [{"id": "a", "name": "A", "anam_avatar_id": "x", "voice": "v", "languages": ["en", "ca"]}]},
     "not in the catalog"),
    ({"avatars": [{"id": "a", "name": "A", "anam_avatar_id": "x", "voice": "v", "extra": 1}]}, "extra"),
])
def test_invalid_catalogs_are_rejected(over, msg):
    with pytest.raises(ValidationError, match=msg):
        AvatarCatalog.model_validate(_raw(**over))


def test_public_view_hides_provider_ids():
    pub = AvatarCatalog.model_validate(_raw()).public()
    # An avatar without an explicit list is reported as speaking everything.
    assert pub["avatars"][0] == {"id": "a", "name": "A", "description": "", "avatar_model": "cara-4",
                                 "languages": ["en", "es", "fr", "ca"]}
    assert pub["languages"][3]["native_name"] == "Català"


def test_load_errors_name_the_file(tmp_path):
    with pytest.raises(FileNotFoundError, match="missing.yaml"):
        load_catalog(tmp_path / "missing.yaml")
    bad = tmp_path / "bad.yaml"
    bad.write_text("- just\n- a list\n", encoding="utf-8")
    with pytest.raises(ValueError, match="mapping"):
        load_catalog(bad)
    broken = tmp_path / "broken.yaml"
    broken.write_text("a: [unclosed\n", encoding="utf-8")
    with pytest.raises(ValueError, match="not valid YAML"):
        load_catalog(broken)


# --- prompt -----------------------------------------------------------------------


def test_prompt_is_written_in_english_but_pins_the_reply_language():
    cat = AvatarCatalog.model_validate(_raw())
    ca = cat.language("ca")
    prompt = system_prompt(ca)
    assert "Use plain spoken Catalan." in prompt
    assert "Always answer in Catalan" in prompt
    assert "{language}" not in prompt
    msgs = initial_messages(ca)
    assert [m["role"] for m in msgs] == ["system", "user"]
    assert "Greet them in Catalan" in msgs[1]["content"]
