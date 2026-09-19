"""Pins for prompt-level fixes to the SGR agent's rules.

Each test here corresponds to a behaviour seen in live runs with a small,
literal-minded model (Nemotron Lightning). They assert on the rule *text* —
the only lever we have — so a later rewording that drops the fix fails loudly.
"""
import re

from tv_avatar.agent.prompt import build_system_prompt
from tv_avatar.catalog import LanguageProfile

_LANG = LanguageProfile(code="en", name="English", native_name="English")


def _prompt() -> str:
    return build_system_prompt(_LANG)


def test_reject_example_say_is_a_placeholder_not_speakable_text():
    # Live: the model spoke the example's `say` verbatim three times, once as
    # its whole reply to a selection. The example must not contain a sentence
    # that reads naturally aloud, and the prompt must say examples are not
    # to be copied.
    prompt = _prompt()
    assert "Sure, let me find something else." not in prompt
    assert '"say": "[' in prompt, "example `say` should be a bracketed placeholder"
    assert "never repeat example text verbatim" in prompt


def test_change_of_request_is_not_a_rejection():
    # Live: "actually give me a horror" emitted reject_title for titles the
    # viewer never declined, permanently excluding them.
    prompt = _prompt()
    assert "A change of request is not a rejection" in prompt
    sentence = next(s for s in re.split(r"(?<=\.)\s", prompt)
                    if "A change of request is not a rejection" in s)
    assert "reject_title" in sentence
    # The trigger phrases that caused the spurious rejections are gone.
    assert '"something else")' not in prompt
    assert '"forget about X"' not in prompt


def test_facts_outside_the_sections_are_not_stated():
    # Live: "who directed this one?" produced invented directors; the catalog
    # has no director field.
    prompt = _prompt()
    assert "Never state a fact that is not written in the Screen, Memory, " \
           "Recent activity or Recommendations sections" in prompt
    assert "say briefly that you do not have that information" in prompt


def test_ordinals_refer_to_the_most_recently_offered_recommendations():
    # Live: after three recommendations, "play the first one" correctly meant
    # the first recommendation, contradicting the old "Screen tile [0]" rule.
    prompt = _prompt()
    assert "refer to the recommendations you most recently offered" in prompt
    assert "otherwise to Screen tiles" in prompt
