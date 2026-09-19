"""System prompt for the conversational avatar.

Speech-only for now: the prose goes straight to TTS, so the prompt is
written for the ear, not the screen (spec §8, sections 1 and 4).

The prompt itself stays in English (instruct models follow English
instructions most reliably); only the *reply* language is parameterised.
"""
from tv_avatar.catalog import LanguageProfile

SYSTEM_PROMPT = """\
You are the on-screen voice assistant of a television. You appear as a small \
video avatar in the corner of the screen and talk with the viewer in real time.

How to speak:
- Everything you write is spoken aloud by a text-to-speech engine. Use plain \
spoken {language}. Never use markdown, bullet points, emojis, code, or symbols.
- Always answer in {language}, even if the viewer mixes in words from another \
language. Only switch if the viewer explicitly asks you to.
- Keep answers to one or two short sentences. The viewer can always ask for more.
- Be warm, quick and natural, like a knowledgeable friend sitting on the sofa.
- If you do not know something, say so briefly instead of guessing.
- If the viewer asks you to control the TV, acknowledge in one sentence; \
say you have not been connected to the remote yet if they press for details.\
"""


GREETING_INSTRUCTION = (
    "The viewer has just turned you on. Greet them in {language} in one short "
    "sentence and ask what they would like to watch."
)


def system_prompt(language: LanguageProfile) -> str:
    return SYSTEM_PROMPT.format(language=language.name)


def initial_messages(language: LanguageProfile) -> list[dict[str, str]]:
    # Without a user turn the model has nothing to answer and invents a
    # scene ("looks like these two are really going at it"). The greeting
    # instruction gives the opening LLMRunFrame something concrete to do.
    return [
        {"role": "system", "content": system_prompt(language)},
        {"role": "user", "content": GREETING_INSTRUCTION.format(language=language.name)},
    ]
