"""People / Places / Events from descriptions."""

import pytest

from banana.analysis import entities
from banana.analysis.entities import Entities, extract, split_joined_words
from banana.config import Settings
from banana.models import Scan

HAS_NLP = entities.NLP.get() is not None
needs_nlp = pytest.mark.skipif(not HAS_NLP, reason="spaCy en_core_web_sm not installed")


def test_split_joined_words():
    assert split_joined_words("JimmyDawley") == "Jimmy Dawley"
    assert split_joined_words("McDonald and MacArthur") == "McDonald and MacArthur"  # surname prefixes kept
    assert split_joined_words("VanHorn FitzGerald SaraJones") == "VanHorn FitzGerald Sara Jones"


def test_rules_without_nlp(monkeypatch):
    monkeypatch.setattr(entities.NLP, "get", lambda: None)
    found = extract("Mary's 5th birthday with Uncle Bob and Grandma. Field Trip. ORIGINAL", Entities(places=["Lake Erie"]))
    assert found.people == ["Uncle Bob", "Grandma"]
    assert found.events == ["5th Birthday", "Field Trip"]
    assert found.places == []
    assert extract("Xmas '84").events == ["Christmas"]
    assert extract("") == Entities()


def test_known_vocabulary_wins(monkeypatch):
    monkeypatch.setattr(entities.NLP, "get", lambda: None)
    found = extract("Tommy fishing at the old mill", Entities(people=["Tommy"], places=["Old Mill"]))
    assert found.people == ["Tommy"] and found.places == ["Old Mill"]


@needs_nlp
@pytest.mark.parametrize(
    ("text", "people", "places", "events"),
    [
        ("ORIGINAL\nJimmyDawley\nChocolat Design - Field Trip\nJanuaru 12, 2006",
         ["Jimmy Dawley"], ["Chocolat Design"], ["Field Trip"]),  # real label from the first test scan
        ("Christmas morning at Grandma's house in Toledo, Ohio. John and Mary opening presents.",
         ["Grandma", "John", "Mary"], ["Toledo", "Ohio"], ["Christmas"]),
        ("Mary's 5th birthday at Lake Erie with Uncle Bob", ["Uncle Bob", "Mary"], ["Lake Erie"], ["5th Birthday"]),
        ("Wedding of Susan Miller and David Chen, St. Mary's Church, Chicago",
         ["Susan Miller", "David Chen"], ["St. Mary's Church", "Chicago"], ["Wedding"]),
        ("Xmas '84", [], [], ["Christmas"]),
    ],
)
def test_nlp_extraction(text, people, places, events):
    found = extract(text)
    assert found.people == people
    assert found.places == places
    assert found.events == events


def test_derive_fills_only_empty_fields(monkeypatch):
    from banana.ingest import service

    monkeypatch.setattr(entities.NLP, "get", lambda: None)
    scan = Scan(batch_id=1, source_key="k", front_path="f", description="Grandma at the Field Trip", people=["Typed"])
    service.derive_entities(scan, Settings())
    assert scan.people == ["Typed"]  # never overwritten
    assert scan.events == ["Field Trip"]

    off = Settings.model_validate({"analysis": {"derive_entities": False}})
    other = Scan(batch_id=1, source_key="k2", front_path="f", description="Grandma")
    service.derive_entities(other, off)
    assert other.people == []
