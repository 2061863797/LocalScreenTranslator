# -*- coding: utf-8 -*-
"""ROI-localized OCR service with automatic full OCR fallback.

PR 5: ROI OCR & Fallback.
Wraps OcrEngine to perform localized OCR on changed regions with safe margin padding.
Automatically falls back to full-frame OCR when changed area ratio > 0.6 or on
periodic health check (default every 25 frames). Remaps line bounding boxes to global coordinates.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Callable
import numpy as np

try:
    from .ocr_engine import OcrEngine, OcrLine
except ImportError:
    OcrEngine = None  # type: ignore
    OcrLine = None  # type: ignore


class OcrService:
    """Localized ROI OCR service with fallback and coordinate remapping."""

    def __init__(
        self,
        ocr_engine: Any = None,
        padding: int = 24,
        max_roi_ratio: float = 0.6,
        health_check_interval: int = 25,
        mock_ocr_fn: Callable[[np.ndarray], list[Any]] | None = None,
    ) -> None:
        self.ocr_engine = ocr_engine
        self.mock_ocr_fn = mock_ocr_fn
        self.padding = int(padding)
        self.max_roi_ratio = float(max_roi_ratio)
        self.health_check_interval = int(health_check_interval)

        self.frames_since_full_ocr: int = 0
        self.last_was_fallback: bool = False
        self.last_padded_roi: tuple[int, int, int, int] | None = None

    def compute_padded_roi(
        self, roi_box: tuple[int, int, int, int], width: int, height: int
    ) -> tuple[int, int, int, int]:
        """Expand ROI bounding box with margin padding clamped to frame boundaries."""
        x1, y1, x2, y2 = roi_box
        p = self.padding
        return (
            max(0, int(x1 - p)),
            max(0, int(y1 - p)),
            min(int(width), int(x2 + p)),
            min(int(height), int(y2 + p)),
        )

    def _call_ocr(self, image: np.ndarray) -> list[Any]:
        """Invoke OCR engine or mock function on image/crop."""
        if self.mock_ocr_fn is not None:
            return self.mock_ocr_fn(image)

        if self.ocr_engine is not None:
            if hasattr(self.ocr_engine, "recognize"):
                return self.ocr_engine.recognize(image)
            elif callable(self.ocr_engine):
                return self.ocr_engine(image)

        return []

    def recognize_frame(
        self,
        frame: np.ndarray,
        roi_box: tuple[int, int, int, int] | None = None,
        force_full: bool = False,
    ) -> list[Any]:
        """Recognize text in frame, utilizing ROI crop when beneficial.

        Args:
            frame: Full captured frame.
            roi_box: Visual difference bounding box (x1, y1, x2, y2) or None.
            force_full: Force full-frame recognition regardless of ROI.

        Returns:
            List of OCR lines with bounding boxes in global frame coordinates.
        """
        if frame is None or frame.size == 0:
            return []

        h, w = frame.shape[:2]
        self.frames_since_full_ocr += 1

        # Fallback check 1: Periodic health check or missing roi_box or forced full
        if force_full or roi_box is None or self.frames_since_full_ocr >= self.health_check_interval:
            self.frames_since_full_ocr = 0
            self.last_was_fallback = True
            return self._call_ocr(frame)

        # Compute padded ROI and evaluate area ratio
        padded_roi = self.compute_padded_roi(roi_box, w, h)
        self.last_padded_roi = padded_roi
        px1, py1, px2, py2 = padded_roi
        roi_area = (px2 - px1) * (py2 - py1)
        full_area = w * h
        area_ratio = roi_area / max(1, full_area)

        # Fallback check 2: Changed area exceeds maximum localized ratio
        if area_ratio > self.max_roi_ratio or px1 >= px2 or py1 >= py2:
            self.frames_since_full_ocr = 0
            self.last_was_fallback = True
            return self._call_ocr(frame)

        # Localized ROI recognition
        self.last_was_fallback = False
        crop = frame[py1:py2, px1:px2]
        crop_lines = self._call_ocr(crop)

        # Coordinate remapping: add crop offset (px1, py1) to each bounding box
        remapped_lines: list[Any] = []
        for line in crop_lines:
            bx1, by1, bx2, by2 = line.box
            remapped_box = (
                max(0, min(w, int(bx1 + px1))),
                max(0, min(h, int(by1 + py1))),
                max(0, min(w, int(bx2 + px1))),
                max(0, min(h, int(by2 + py1))),
            )
            if dataclasses.is_dataclass(line) and not isinstance(line, type):
                remapped_lines.append(dataclasses.replace(line, box=remapped_box))
            elif hasattr(line, "__dict__"):
                # Handle generic object with box attribute
                try:
                    cls = type(line)
                    if hasattr(line, "confidence"):
                        remapped_lines.append(cls(line.text, remapped_box, line.confidence))
                    elif hasattr(line, "score"):
                        remapped_lines.append(cls(line.text, line.score, remapped_box))
                    else:
                        remapped_lines.append(cls(line.text, remapped_box))
                except Exception:
                    line.box = remapped_box
                    remapped_lines.append(line)
            else:
                remapped_lines.append(line)

        return remapped_lines
