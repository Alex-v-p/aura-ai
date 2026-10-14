"""Pillow-backed image inspection provider for private attachments.

The attachment domain owns authorization and lifecycle.  This concrete
provider implements its inward ``ImageInspector`` port and returns only
bounded metadata plus a normalized derivative.
"""

# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false

from __future__ import annotations

import io
import warnings
from typing import Any, cast

from aura_core.domains.artifacts.attachments.ports import ImageInspector, InspectedImage

MAX_IMAGE_BYTES = 25 * 1_000_000
MAX_IMAGE_PIXELS = 40_000_000
MAX_DERIVATIVE_LONGEST_SIDE = 2048
SUPPORTED_MEDIA_TYPES = frozenset({"image/png", "image/jpeg", "image/webp"})
_MEDIA_TO_FORMAT = {"image/png": "PNG", "image/jpeg": "JPEG", "image/webp": "WEBP"}
_FORMAT_TO_MEDIA = {value: key for key, value in _MEDIA_TO_FORMAT.items()}


class ImageInspectionError(ValueError):
    """Content-free validation failure for an image upload."""

    def __init__(self, reason: str) -> None:
        safe_reason = reason if reason in {
            "image_required",
            "image_too_large",
            "unsupported_image_type",
            "malformed_image",
            "animated_image",
            "image_pixel_limit",
            "image_inspection_unavailable",
        } else "malformed_image"
        super().__init__(safe_reason)
        self.reason = safe_reason


class ImageInspectionResult(InspectedImage):
    """Verified dimensions and a metadata-free model/preview derivative."""

    __slots__ = ("derivative_media_type", "derivative_bytes")

    def __init__(
        self,
        media_type: str,
        width: int,
        height: int,
        derivative_media_type: str,
        derivative_bytes: bytes,
        animated: bool = False,
    ) -> None:
        super().__init__(media_type, width, height, derivative_bytes, animated)
        object.__setattr__(self, "derivative_media_type", derivative_media_type)
        object.__setattr__(self, "derivative_bytes", derivative_bytes)


class PillowImageInspector(ImageInspector):
    """Decode and normalize PNG, JPEG, and static WebP with Pillow."""

    def inspect(self, data: bytes, *, declared_media_type: str) -> ImageInspectionResult:
        if not data:
            raise ImageInspectionError("image_required")
        if len(data) > MAX_IMAGE_BYTES:
            raise ImageInspectionError("image_too_large")
        if declared_media_type not in SUPPORTED_MEDIA_TYPES:
            raise ImageInspectionError("unsupported_image_type")
        try:
            from PIL import Image, ImageOps
        except ImportError as exc:  # pragma: no cover - deployment guard
            raise ImageInspectionError("image_inspection_unavailable") from exc

        try:
            with Image.open(io.BytesIO(data)) as opened:
                actual_format = opened.format
                actual_media_type = _FORMAT_TO_MEDIA.get(actual_format or "")
                if actual_media_type != declared_media_type:
                    raise ImageInspectionError("unsupported_image_type")
                verified_media_type = cast(str, actual_media_type)
                width, height = opened.size
                if width < 1 or height < 1 or width * height > MAX_IMAGE_PIXELS:
                    raise ImageInspectionError("image_pixel_limit")
                frame_count = getattr(opened, "n_frames", 1)
                if not isinstance(frame_count, int) or frame_count != 1:
                    raise ImageInspectionError("animated_image")
                with warnings.catch_warnings():
                    warnings.simplefilter("error")
                    opened.verify()

            with Image.open(io.BytesIO(data)) as decoded:
                decoded.load()
                frame_count = getattr(decoded, "n_frames", 1)
                if not isinstance(frame_count, int) or frame_count != 1:
                    raise ImageInspectionError("animated_image")
                normalized = ImageOps.exif_transpose(decoded)
                normalized.thumbnail(
                    (MAX_DERIVATIVE_LONGEST_SIDE, MAX_DERIVATIVE_LONGEST_SIDE),
                    Image.Resampling.LANCZOS,
                )
                if actual_format == "PNG" and "A" in normalized.getbands():
                    normalized = normalized.convert("RGBA")
                else:
                    normalized = normalized.convert("RGB")
                output = io.BytesIO()
                save_kwargs: dict[str, Any] = {"format": actual_format}
                if actual_format == "JPEG":
                    save_kwargs.update({"quality": 90, "optimize": True, "progressive": False})
                elif actual_format == "WEBP":
                    save_kwargs.update({"quality": 90, "method": 4})
                normalized.save(output, **save_kwargs)
                derivative = output.getvalue()
        except ImageInspectionError:
            raise
        except Exception as exc:
            raise ImageInspectionError("malformed_image") from exc

        return ImageInspectionResult(
            media_type=verified_media_type,
            width=width,
            height=height,
            derivative_media_type=verified_media_type,
            derivative_bytes=derivative,
        )


__all__ = [
    "ImageInspectionError",
    "ImageInspectionResult",
    "PillowImageInspector",
    "MAX_IMAGE_BYTES",
    "MAX_IMAGE_PIXELS",
    "MAX_DERIVATIVE_LONGEST_SIDE",
    "SUPPORTED_MEDIA_TYPES",
]
