"""What "what do you remember about me?" says: facts, never the memory
file's own headings and notes."""

import brain_gemini as bg

PROFILE = """# Cygnus Profile

> Tier-2 durable profile. Injected into Cygnus's router prompt and every
> delegation to Hermes. This is DATA, not code.

## User
- Name: Roger A. Abay Jr.
- GitHub: samakatuwid00
- Role: PH full-stack dev
- Lives in Pasacao
"""


def test_the_profile_file_is_read_as_facts_not_markup():
    # 2026-09-22 smoke run: "From what I remember, # Cygnus Profile; > Tier-2
    # durable profile. Injected into Cygnus's router prompt and every; ..."
    out = bg._format_recall_for_speech(PROFILE)
    assert out == ("From what I remember, Name: Roger A. Abay Jr.; GitHub: samakatuwid00; "
                   "Role: PH full-stack dev.")
    assert "#" not in out and ">" not in out and "Tier-2" not in out


def test_honcho_labels_are_still_dropped():
    out = bg._format_recall_for_speech("SUMMARY: likes tea\nRECENT:\n  - drinks coffee\n  - lives in Davao")
    assert out == "From what I remember, likes tea; drinks coffee; lives in Davao."


def test_only_markup_means_nothing_remembered():
    assert bg._format_recall_for_speech("# Cygnus Profile\n\n> a note\n## User\n").startswith(
        "I don't have anything specific remembered")
