"""The notes line-break contract, pinned as a system-prompt property.

This pins the contract, not the model. Asserting that a probabilistic generation
comes back with a newline in it would be an eval, would need a credential, and
would be flaky; those live behind the `eval` marker. What is deterministic is
what the system prompt actually instructs, and that is what regresses silently:
a later edit tidying rule 9 into "join the requested items" would change agent
behavior in production with every existing test still green.

The negative assertions matter more than the positive ones. It is easy to add a
line saying newlines are preserved and leave a contradicting line elsewhere.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.prompts import SYSTEM_PROMPT


NEWLINE_TRIGGERS = (
    "on the next line",
    "on a new line",
    "directly below",
    "under it",
    "each on its own line",
)


def test_every_newline_phrasing_is_named():
    """A phrasing the prompt does not name is a phrasing the model may flatten."""
    missing = [phrase for phrase in NEWLINE_TRIGGERS if phrase not in SYSTEM_PROMPT]
    assert not missing, f"unnamed newline phrasings: {missing}"


def test_the_separator_is_an_actual_newline_character():
    assert "actual newline characters" in SYSTEM_PROMPT


def test_spaces_and_punctuation_are_refused_as_separators():
    assert "not\n   with spaces, commas, or any other joining punctuation" in (
        SYSTEM_PROMPT
    )


def test_the_worked_example_shows_three_separate_lines():
    """The example is the part a model copies, so it must not be one line."""
    assert "       buy bananas\n       buy apples\n       buy pears" in SYSTEM_PROMPT


def test_the_flattened_forms_are_named_as_wrong():
    assert 'reading "buy bananas buy apples buy pears"' in SYSTEM_PROMPT
    assert 'reading "buy bananas, buy apples, buy pears"' in SYSTEM_PROMPT


def test_nothing_is_added_that_the_user_did_not_request():
    """Bullets and numbering are the model's favorite unrequested embellishment."""
    assert "No bullets, no hyphens, no numbering" in SYSTEM_PROMPT


def test_empty_notes_do_not_gain_a_leading_newline():
    assert "do not\n   begin the value with a newline" in SYSTEM_PROMPT


def test_a_requested_blank_line_survives():
    assert "two newline characters" in SYSTEM_PROMPT


def test_the_prompt_never_instructs_the_model_to_flatten_notes():
    """Negative regression against a future edit that undoes this rule.

    Each phrase here is one a plausible "tidy up the notes rule" edit would
    introduce, and each would silently reverse the contract above.
    """
    forbidden = (
        "join the requested",
        "comma-separated list",
        "separate them with commas",
        "on a single line",
        "collapse newlines",
        "collapse whitespace",
        "strip newlines",
        "remove line breaks",
        "as a bulleted list",
    )
    present = [phrase for phrase in forbidden if phrase in SYSTEM_PROMPT.lower()]
    assert not present, f"the prompt instructs flattening: {present}"


def test_set_and_append_semantics_are_still_distinguished():
    """The line-break rule extends rule 9; it must not have replaced it."""
    assert '"set" or "replace"' in SYSTEM_PROMPT
    assert '"add", "append", "add to", or "add in"' in SYSTEM_PROMPT
    assert "preserve the existing" in SYSTEM_PROMPT
