from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from .protocol import ABCError


MEDIA_EXTENSION_KEY = "agentbc.media"
SUPPORTED_IMAGE_SUFFIXES = {".gif", ".jpeg", ".jpg", ".png", ".webp"}


def normalize_image_inputs(
    images: Iterable[str | Path] | None,
    *,
    allowed_roots: Iterable[str | Path],
) -> list[str]:
    roots = [Path(root).expanduser().resolve() for root in allowed_roots if str(root).strip()]
    normalized: list[str] = []
    seen: set[str] = set()
    for raw_image in images or []:
        image = Path(raw_image).expanduser().resolve()
        if not image.is_file():
            raise ABCError("invalid_image_input", f"image input does not exist or is not a file: {image}")
        if image.suffix.lower() not in SUPPORTED_IMAGE_SUFFIXES:
            supported = ", ".join(sorted(SUPPORTED_IMAGE_SUFFIXES))
            raise ABCError(
                "invalid_image_input",
                f"unsupported image input type: {image.suffix or '<none>'}; supported: {supported}",
            )
        if not any(_is_within(image, root) for root in roots):
            raise ABCError("invalid_image_input", f"image input is outside task roots: {image}")
        value = str(image)
        if value not in seen:
            normalized.append(value)
            seen.add(value)
    return normalized


def media_extension(images: Iterable[str | Path] | None) -> dict[str, Any]:
    values = [str(image) for image in images or []]
    if not values:
        return {}
    return {MEDIA_EXTENSION_KEY: {"images": values}}


def task_image_paths(task_packet: dict[str, Any]) -> list[Path]:
    extensions = task_packet.get("extensions")
    if not isinstance(extensions, dict):
        return []
    media = extensions.get(MEDIA_EXTENSION_KEY)
    if not isinstance(media, dict):
        return []
    images = media.get("images")
    if not isinstance(images, list):
        return []
    return [Path(str(image)).expanduser().resolve() for image in images if str(image).strip()]


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
