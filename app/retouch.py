from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol

from PIL import Image


class RetouchProvider(Protocol):
    def process(self, image: Image.Image, source_path: str) -> Image.Image:
        ...


class NoopRetouchProvider:
    def process(self, image: Image.Image, source_path: str) -> Image.Image:
        return image


def get_retouch_provider() -> RetouchProvider:
    provider = os.getenv("RETOUCH_PROVIDER", "none").strip().lower()
    if provider in {"", "none"}:
        return NoopRetouchProvider()
    raise RuntimeError(
        f"Unsupported RETOUCH_PROVIDER={provider!r}. "
        "Add an external provider implementation before enabling it."
    )


def apply_retouch(image: Image.Image, source_path: str) -> Image.Image:
    if not Path(source_path).is_file():
        raise FileNotFoundError(f"Retouch source file not found: {source_path}")
    return get_retouch_provider().process(image, source_path)
