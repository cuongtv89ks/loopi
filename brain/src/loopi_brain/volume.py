"""The media volume guard. When the media root is a disk of its own (deploy: LOOPI_MEDIA_DIR), the brain writes
media only while that disk is mounted, which it knows by a marker file in the disk's root, and never into the
empty mount point underneath; a forget waits too, since a missing file would count as gone and survive on the
disk. Imports nothing heavy: the api's health check calls it."""
from pathlib import Path

MARKER = ".loopi-media"


class MediaVolumeMissing(RuntimeError):
    """The media disk is not mounted: the marker file is missing from the media root."""


def ready(media_root: Path, required: bool) -> bool:
    """True unless the marker is required and ``media_root`` lacks it (a directory of that name does not count)."""
    return not required or (media_root / MARKER).is_file()


def check(media_root: Path, required: bool) -> None:
    """MediaVolumeMissing unless ``ready``."""
    if not ready(media_root, required):
        raise MediaVolumeMissing(f"media volume not mounted: {media_root / MARKER} is missing")
