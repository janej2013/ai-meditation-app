"""The meditation script prompt.

Kept in its own module so it can be reviewed and diffed as a piece of product
copy rather than buried in request-plumbing code, and so tests can assert on it
without invoking Bedrock.
"""

from __future__ import annotations

# Slow, guided-meditation delivery. Used to convert the requested duration into
# a word target for the model.
WORDS_PER_MINUTE = 95

SYSTEM_PROMPT = """\
You write guided meditation scripts that will be read aloud by a \
text-to-speech voice.

Rules:
- Write in English, in a calm, warm second person ("you").
- Plain prose only. No headings, no titles, no stage directions, and no \
bracketed cues such as [pause] or [breathe] -- a pause is a paragraph break \
and nothing else.
- Separate paragraphs with a blank line. Put a break wherever the listener \
should rest in silence.
- Never repeat the listener's personal details back to them. Do not restate \
names, places, people, jobs, relationships, or events they mention, and do \
not paraphrase them closely enough to identify anyone. Speak to the \
underlying feeling, not the circumstance that produced it.
- Keep sentences short and easy to follow when spoken aloud.
- Open by settling attention on the breath or the body. Close by gently \
returning attention outward.
- Output the script only. No preamble, no commentary, no word count.\
"""


# Average English word plus its trailing space, for turning a word target into
# a character floor.
_CHARS_PER_WORD = 6

# A script this far below target is truncated or refused, not merely terse.
# The threshold is a trade: rejecting costs a rollback and no meditation,
# letting it through costs the user a credit for a few seconds of audio.
_MIN_SCRIPT_RATIO = 1 / 3


def target_word_count(duration_minutes: int) -> int:
    """Words needed to fill ``duration_minutes`` of slow, spoken delivery."""
    return duration_minutes * WORDS_PER_MINUTE


def min_script_chars(duration_minutes: int) -> int:
    """Shortest script worth sending to TTS for ``duration_minutes``.

    Scales with the request: a flat floor that passes a 3-minute script would
    wave through a 30-minute one truncated to a twentieth of its length.
    """
    return int(target_word_count(duration_minutes) * _CHARS_PER_WORD * _MIN_SCRIPT_RATIO)


def build_user_message(mood_text: str, duration_minutes: int) -> str:
    """The per-request half of the prompt.

    The mood is labelled rather than embedded in an instruction so the model
    treats it as material to respond to, not as directions to follow -- which
    also blunts prompt injection from the free-text field.
    """
    words = target_word_count(duration_minutes)
    return (
        f"The listener described how they feel:\n\n{mood_text}\n\n"
        f"Write a meditation of roughly {words} words "
        f"(about {duration_minutes} minutes read slowly)."
    )
