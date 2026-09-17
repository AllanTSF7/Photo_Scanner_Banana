import pytest

from banana.analysis.autocorrect import Fix, correct, one_edit_apart
from banana.core.dictionary import CorrectionDictionary


@pytest.mark.parametrize(
    ("text", "fixed_words"),
    [
        ("Januaru '84", ["January"]),
        ("Januray '84", ["January"]),          # transposed letters
        ("Febuary 3", ["February"]),
        ("Thanksgivng dinner", ["Thanksgiving"]),
        ("Summre of 1979", ["Summer"]),
        ("CHRISTMSS 1984", ["CHRISTMAS"]),      # case preserved
        ("christmas card", []),                 # already correct, never touched
    ],
)
def test_vocabulary_typos_fixed(text, fixed_words):
    fixed, fixes = correct(text)
    assert [f.after for f in fixes] == fixed_words
    if fixed_words:
        assert fixed != text
    else:
        assert fixed == text and fixes == []


def test_short_names_and_unrelated_words_untouched():
    # "Mary" and "Jon" are shorter than MIN_LENGTH, so they're never candidates - regardless of nearby vocabulary words.
    fixed, fixes = correct("Mary and Jon at the mill")
    assert fixed == "Mary and Jon at the mill" and fixes == []


def test_protected_names_never_touched():
    # "Grandma" alone is vocabulary and would normally correct a typo, but a *confirmed* name is left exactly as typed.
    fixed, fixes = correct("Grandmaa's house", protected=["Grandmaa"])
    assert fixed == "Grandmaa's house" and fixes == []


def test_plural_and_possessive_recognized_as_already_correct():
    fixed, fixes = correct("Grandma's kitchen, Christmases past")
    assert fixed == "Grandma's kitchen, Christmases past" and fixes == []


def test_learned_dictionary_takes_priority_and_reports_source():
    learned = CorrectionDictionary(words={"JimmyDawley": "Jimmy Dawley"})
    fixed, fixes = correct("JimmyDawley went home", learned=learned)
    assert fixed == "Jimmy Dawley went home"
    assert fixes == [Fix("JimmyDawley", "Jimmy Dawley", "dictionary")]


def test_whole_line_dictionary_match_wins_over_word_fixes():
    learned = CorrectionDictionary(lines={"xmas84": "Christmas 1984"})
    fixed, fixes = correct("Xmas'84", learned=learned)
    assert fixed == "Christmas 1984"
    assert fixes[0].source == "dictionary"


def test_empty_and_whitespace_text_untouched():
    assert correct("") == ("", [])
    assert correct("   ") == ("   ", [])


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [("january", "january", True), ("januaru", "january", True), ("jan", "january", False),
     ("septembr", "september", True), ("septemper", "september", True)],
)
def test_one_edit_apart(a, b, expected):
    assert one_edit_apart(a, b) is expected
