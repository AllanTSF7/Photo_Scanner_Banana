"""Static enforcement of the UI DESIGN SYSTEM rules in CLAUDE.md (live behavior is in tests/ui)."""

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "banana" / "web" / "static"
VENDOR = STATIC / "vendor"
# Vendored third-party files (driver.js) still have to pass the offline and no-emoji rules; they're exempt from
# tokens-only (their colors are overridden in app.css, per SOURCE.txt) and from the reduced-motion rule (their
# animation classes are only ever added when tour.js's own `animate: !reduceMotion` check allows it).
OWN_SOURCES = sorted(p for p in STATIC.iterdir() if p.suffix in (".html", ".css", ".js"))
VENDOR_SOURCES = sorted(p for p in VENDOR.rglob("*") if p.suffix in (".html", ".css", ".js")) if VENDOR.exists() else []
SOURCES = OWN_SOURCES + VENDOR_SOURCES
NON_THEME = [p for p in OWN_SOURCES if p.name != "theme.css"]


def _ids(paths):
    return [p.name for p in paths]


def _strip_css_comments(text: str) -> str:
    return re.sub(r"/\*.*?\*/", "", text, flags=re.S)


@pytest.mark.parametrize("path", SOURCES, ids=_ids(SOURCES))
def test_ui_has_no_emoji(path):
    text = path.read_text(encoding="utf-8")
    allowed = set("×·—–…“”‘’≥≤")
    offenders = sorted({ch for ch in text if ord(ch) >= 0x2190 and ch not in allowed})
    assert not offenders, f"{path.name} contains {offenders}"


@pytest.mark.parametrize("path", SOURCES, ids=_ids(SOURCES))
def test_ui_no_external_refs(path):
    text = path.read_text(encoding="utf-8")
    patterns = [
        r"""\b(?:src|href)\s*=\s*["']?\s*(?:https?:)?//""",
        r"""@import\s+(?:url\()?\s*["']?(?:https?:)?//""",
        r"""url\(\s*["']?(?:https?:)?//""",
        r"""\bfetch\(\s*["'`](?:https?:)?//""",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        assert match is None, f"{path.name}: external reference {match.group(0)!r}"


HEX = re.compile(r"(?<![\w&-])#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})(?![\w-])")
FUNC_COLOR = re.compile(r"\b(?:rgba?|hsla?|hwb|lab|lch|oklch|oklab|color-mix)\(", re.I)
NAMED = re.compile(r":\s*[^;{}]*\b(?:white|black|red|green|blue|gray|grey|orange|yellow)\b", re.I)


@pytest.mark.parametrize("path", NON_THEME, ids=_ids(NON_THEME))
def test_ui_tokens_only(path):
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".css":
        text = _strip_css_comments(text)
    hexes = [m.group(0) for m in HEX.finditer(text)]
    assert not hexes, f"{path.name}: hard-coded hex colors {hexes[:5]}"
    assert not FUNC_COLOR.search(text), f"{path.name}: color function outside theme.css"
    for m in re.finditer(r"font-family\s*:\s*([^;}]+)", text):
        assert "var(--font-" in m.group(1), f"{path.name}: font-family {m.group(1)!r} not from a token"
    if path.suffix == ".css":
        for m in re.finditer(r"(?<![-\w])font\s*:\s*([^;}]+)", text):
            assert "var(--font-" in m.group(1), f"{path.name}: font shorthand {m.group(1)!r} not from a token"
        assert not NAMED.search(text), f"{path.name}: named color {NAMED.search(text).group(0)!r}"
        assert "gradient(" not in text, f"{path.name}: gradients are not part of the Darkroom system"
        assert "box-shadow" not in re.sub(r"box-shadow\s*:\s*none", "", text), f"{path.name}: depth comes from surfaces and borders"


def test_motion_only_when_allowed():
    css = _strip_css_comments((STATIC / "app.css").read_text(encoding="utf-8"))
    # Every animation declaration must sit inside @media (prefers-reduced-motion: no-preference).
    outside = re.sub(r"@media\s*\(prefers-reduced-motion:\s*no-preference\)\s*\{(?:[^{}]*\{[^{}]*\})*[^{}]*\}", "", css)
    assert not re.search(r"(?<![-\w])animation(?:-name)?\s*:", outside), "animation outside prefers-reduced-motion: no-preference"


def test_fonts_are_vendored():
    theme = (STATIC / "theme.css").read_text(encoding="utf-8")
    for url in re.findall(r"url\(\"?([^\")]+)\"?\)", theme):
        assert (STATIC / url).exists(), f"theme.css references missing font {url}"
