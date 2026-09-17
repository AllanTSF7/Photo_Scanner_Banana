"""Group scanner output files into front/back pairs.

FastFoto/ScanSmart style: name_0001.jpg (original front), name_0001_a.jpg (enhanced front),
name_0001_b.jpg (back). The regex must expose a `base` group and an optional `suffix` group.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal


@dataclass
class ScanGroup:
    base: str
    original: Path | None = None
    enhanced: Path | None = None
    back: Path | None = None

    def front(self, variant: Literal["original", "enhanced"]) -> Path | None:
        if variant == "enhanced":
            return self.enhanced or self.original
        return self.original or self.enhanced


@dataclass
class PairingResult:
    groups: list[ScanGroup]
    unmatched: list[Path]
    backs_without_front: list[ScanGroup]


def pair_files(paths: Iterable[Path], pattern: str) -> PairingResult:
    regex = re.compile(pattern, re.IGNORECASE)
    groups: dict[str, ScanGroup] = {}
    unmatched: list[Path] = []
    for path in sorted(paths, key=lambda p: p.name.lower()):
        m = regex.match(path.name)
        if not m:
            unmatched.append(path)
            continue
        base = m.group("base")
        suffix = (m.groupdict().get("suffix") or "").lower()
        group = groups.setdefault(base.lower(), ScanGroup(base))
        slot = {"": "original", "_a": "enhanced", "_b": "back"}.get(suffix)
        if slot is None or getattr(group, slot) is not None:
            unmatched.append(path)
            continue
        setattr(group, slot, path)

    complete, orphans = [], []
    for group in groups.values():
        (complete if group.original or group.enhanced else orphans).append(group)
    return PairingResult(complete, unmatched, orphans)
