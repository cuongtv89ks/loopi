"""The OTA bundle catalog (P4a design §8, §8.1, spec D29): signed RAUC bundles in LOOPI_OTA_DIR, each beside the JSON
description the build and os/scripts/publish-bundle.sh wrote. The brain never opens a bundle (the device's RAUC checks
its signature); it offers one only while the file on disk agrees with its description (name, size)."""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from .devices import BUNDLE_RE, MAX_BUNDLE_BYTES, SHA256_RE, VERSION_RE

log = logging.getLogger(__name__)
STEM_RE = re.compile(r"^loopi-(dev|prod)-[0-9A-Za-z.+-]{1,64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
MAX_NOTES = 50
MAX_SUBJECT = 200  # characters of a commit subject kept for the page


@dataclass(frozen=True)
class Note:
    commit: str
    subject: str


@dataclass(frozen=True)
class Bundle:
    name: str
    version: str
    variant: str
    sha256: str
    size: int
    built_at: dt.datetime
    commit: str
    notes_base: str | None
    notes: tuple[Note, ...]
    notes_truncated: bool

    @property
    def stem(self) -> str:
        return self.name.removesuffix(".raucb")


@dataclass(frozen=True)
class Unusable:
    stem: str
    file: str
    reason: str  # Vietnamese: shown on the Phiên bản page


class BadBundle(ValueError):
    """A bundle whose description or file cannot be trusted; the message is for the owner (Vietnamese)."""


def description_path(ota_dir: Path, name: str) -> Path:
    return ota_dir / (name.removesuffix(".raucb") + ".json")


def _notes(raw) -> tuple[Note, ...]:
    if not isinstance(raw, list) or len(raw) > MAX_NOTES:
        raise BadBundle("ghi chú phát hành không hợp lệ")
    notes = []
    for entry in raw:
        if not (isinstance(entry, dict) and isinstance(entry.get("commit"), str) and isinstance(entry.get("subject"), str)):
            raise BadBundle("ghi chú phát hành không hợp lệ")
        notes.append(Note(entry["commit"][:40], entry["subject"][:MAX_SUBJECT]))
    return tuple(notes)


def _built_at(value) -> dt.datetime:
    parsed = None
    if isinstance(value, str):
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            parsed = None
    if parsed is None or parsed.tzinfo is None:
        raise BadBundle("ngày build không hợp lệ")
    return parsed


def load(ota_dir: Path, description: Path) -> Bundle:
    """Parse one description and check the bundle file beside it. Raises BadBundle or OSError."""
    try:
        data = json.loads(description.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise BadBundle("file mô tả không phải JSON hợp lệ") from exc
    if not isinstance(data, dict):
        raise BadBundle("file mô tả không phải JSON hợp lệ")
    name, version, variant = data.get("bundle"), data.get("version"), data.get("variant")
    if not (isinstance(name, str) and BUNDLE_RE.fullmatch(name)) or description_path(ota_dir, name) != description:
        raise BadBundle("tên bundle không khớp tên file mô tả")
    if not (isinstance(version, str) and VERSION_RE.fullmatch(version)) or variant not in ("dev", "prod") \
            or name != f"loopi-{variant}-{version}.raucb":
        raise BadBundle("phiên bản hoặc biến thể không khớp tên bundle")
    sha256, size, commit = data.get("sha256"), data.get("size"), data.get("commit")
    if not (isinstance(sha256, str) and SHA256_RE.fullmatch(sha256)):
        raise BadBundle("sha256 không hợp lệ")
    if type(size) is not int or not 1 <= size <= MAX_BUNDLE_BYTES:
        raise BadBundle("kích thước trong file mô tả không hợp lệ")
    if not (isinstance(commit, str) and COMMIT_RE.fullmatch(commit)):
        raise BadBundle("mã commit không hợp lệ")
    base = data.get("notes_base")
    if base is not None and not (isinstance(base, str) and VERSION_RE.fullmatch(base)):
        raise BadBundle("ghi chú phát hành không hợp lệ")
    truncated = data.get("notes_truncated", False)
    if not isinstance(truncated, bool):
        raise BadBundle("ghi chú phát hành không hợp lệ")
    notes = _notes(data.get("notes", []))
    built_at = _built_at(data.get("built_at"))
    path = ota_dir / name
    if not path.is_file():
        raise BadBundle("thiếu file .raucb")
    if path.stat().st_size != size:
        raise BadBundle("file .raucb khác kích thước ghi trong file mô tả")
    return Bundle(name, version, variant, sha256, size, built_at, commit, base, notes, truncated)


def scan(ota_dir: Path) -> tuple[list[Bundle], list[Unusable]]:
    """Every bundle the brain can offer (newest build first) and every file it cannot, with the reason."""
    if not ota_dir.is_dir():
        return [], []
    found: list[Bundle] = []
    unusable: list[Unusable] = []
    described: set[str] = set()
    for description in sorted(ota_dir.glob("loopi-*.json")):
        stem = description.name.removesuffix(".json")
        described.add(stem)
        try:
            found.append(load(ota_dir, description))
        except BadBundle as exc:
            log.warning("unusable OTA bundle %s: %s", description.name, exc)
            unusable.append(Unusable(stem, description.name, str(exc)))
        except OSError as exc:
            log.warning("unreadable OTA bundle %s: %s", description.name, exc)
            unusable.append(Unusable(stem, description.name, "không đọc được file"))
    for path in sorted(ota_dir.glob("loopi-*.raucb")):
        stem = path.name.removesuffix(".raucb")
        if stem not in described:
            unusable.append(Unusable(stem, path.name, "thiếu file mô tả .json"))
    found.sort(key=lambda bundle: bundle.built_at, reverse=True)
    return found, unusable


def find(ota_dir: Path, name: str) -> Bundle | None:
    """The bundle called ``name`` if the catalog offers it (exact file name; nothing else is ever resolved)."""
    if not BUNDLE_RE.fullmatch(name):
        return None
    try:
        return load(ota_dir, description_path(ota_dir, name))
    except (BadBundle, OSError):
        return None
