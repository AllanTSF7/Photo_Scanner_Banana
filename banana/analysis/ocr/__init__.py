"""Offline text reading on photo backs.

Engines implement `Reader`. v1: RapidOCR (PP-OCR detection + recognition, ONNX Runtime, CPU) for printed
labels and stamps. Planned: TrOCR for handwritten lines, Ollama VLM as a low-confidence fallback.
"""

from __future__ import annotations

import re
import threading
from dataclasses import asdict, dataclass
from typing import Protocol

import numpy as np

MIN_DESCRIPTION_SCORE = 0.8


@dataclass
class TextLine:
    text: str
    score: float
    box: list[list[int]]  # 4 corner points in the edited (cropped + rotated) preview image


class Reader(Protocol):
    name: str

    def read(self, image: np.ndarray, use_cls: bool = True) -> list[TextLine]: ...


class RapidOcrReader:
    name = "rapidocr-ppocr"

    def __init__(self) -> None:
        from rapidocr_onnxruntime import RapidOCR  # optional dependency ("ocr" extra)

        self._engine = RapidOCR()
        self._lock = threading.Lock()  # the ONNX sessions aren't shared across concurrent calls

    def read(self, image: np.ndarray, use_cls: bool = True) -> list[TextLine]:
        """use_cls=False skips the built-in upside-down classifier - used by orientation search, which needs
        recognition to fail on upside-down text rather than have it silently corrected."""
        with self._lock:
            result, _ = self._engine(image, use_cls=use_cls)
        lines = []
        for box, text, score in result or []:
            text = str(text).strip()
            if text:
                lines.append(TextLine(text, round(float(score), 3), [[int(x), int(y)] for x, y in box]))
        return lines


_reader: Reader | None = None
_reader_error: str | None = None
_reader_lock = threading.Lock()


def get_reader() -> Reader | None:
    """Lazily created shared reader; None (with `reader_error()`) when no engine is installed."""
    global _reader, _reader_error
    with _reader_lock:
        if _reader is None and _reader_error is None:
            try:
                _reader = RapidOcrReader()
            except Exception as exc:  # noqa: BLE001 - reported by the health check
                _reader_error = f"{type(exc).__name__}: {exc}"
        return _reader


def reader_error() -> str | None:
    return _reader_error


def is_meaningful(line: TextLine) -> bool:
    """Words rather than lab codes/negative numbers: mostly letters and confident."""
    letters = sum(ch.isalpha() for ch in line.text)
    return line.score >= MIN_DESCRIPTION_SCORE and letters >= 3 and letters * 2 >= len(re.sub(r"\s", "", line.text))


def description_from(lines: list[TextLine]) -> str:
    from banana.analysis.entities import split_joined_words

    return "\n".join(split_joined_words(line.text) for line in lines if is_meaningful(line))


def lines_to_json(lines: list[TextLine]) -> list[dict]:
    return [asdict(line) for line in lines]
