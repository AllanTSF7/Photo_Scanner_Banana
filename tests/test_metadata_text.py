"""What may go into a photo's metadata (banana/export/metadata.py::text_problems): ordinary text passes, and a
stray character is named together with what to type instead - it is never written silently and never left as
an ExifTool error at export time."""

from banana.export.metadata import text_problems


def test_ordinary_text_including_commas_and_accents_passes():
    assert text_problems({
        "description": "Christmas at Grandma’s, 1984 – \"the bike\"\nsecond line",
        "people": ["O'Neil", "José Ünal", "Jim & Jane"], "places": ["Bellville, Texas"], "events": [],
        "tags": ["Family/Reunion", "#1 Dad"],
    }) == []


def test_cjk_punctuation_read_by_ocr_is_named_with_a_replacement():
    assert text_problems({"description": "Live Oaks、Bellville，Tx"}) == [
        'Description: "、" (ideographic comma): use "," instead; "，" (fullwidth comma): use "," instead',
    ]


def test_symbols_and_control_characters_are_reported_per_field():
    assert text_problems({"people": ["Tom·Jane"], "places": ["Houston§"], "tags": ["a\tb"]}) == [
        'People: "·" (middle dot): use "-" instead',
        'Places: "§" (section sign): remove it',
        "Tags: a tab character: use a normal space",
    ]


def test_a_line_break_is_fine_in_the_description_but_not_in_a_name():
    assert text_problems({"description": "two\nlines"}) == []
    assert text_problems({"people": ["two\nlines"]}) == ["People: a line break character: remove it"]


def test_tag_level_separators_in_names_are_refused_instead_of_being_changed_silently():
    assert text_problems({"people": ["Tom/Jerry"], "tags": ["Family/Reunion", "a|b"]}) == [
        'People: "/" separates tag levels: use "-" instead',
        'Tags: "|" separates tag levels: use "-" instead',
    ]


def test_empty_and_missing_values_are_fine():
    assert text_problems({"description": None, "people": [], "tags": [""]}) == []
