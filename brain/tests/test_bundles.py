"""The OTA bundle catalog and GET /v1/ota/{bundle} (P4a design §7, §8)."""
import datetime as dt
import json

import httpx
import pytest

from helpers import auth_headers, paired_device, write_bundle
from loopi_brain import bundles, devices
from loopi_brain.app import create_app
from loopi_brain.config import Settings


def test_default_bundle_dir_is_in_the_data_dir():
    assert Settings.model_fields["ota_dir"].default == bundles.Path("/srv/loopi/ota")


def test_scan_lists_bundles_newest_build_first(tmp_path):
    write_bundle(tmp_path, version="2026.10.20-aaaaaaa", built_at="2026-10-20T01:00:00Z")
    write_bundle(tmp_path, version="2026.10.22-ccccccc", variant="prod", built_at="2026-10-22T01:00:00Z")
    write_bundle(tmp_path, version="2026.10.21-bbbbbbb", built_at="2026-10-21T01:00:00+00:00",
                 notes=[{"commit": "d" * 40, "subject": "feat(agent): x"}], notes_base="2026.10.20-aaaaaaa")
    found, unusable = bundles.scan(tmp_path)
    assert [b.version for b in found] == ["2026.10.22-ccccccc", "2026.10.21-bbbbbbb", "2026.10.20-aaaaaaa"]
    assert unusable == []
    middle = found[1]
    assert middle.name == "loopi-dev-2026.10.21-bbbbbbb.raucb"
    assert middle.stem == "loopi-dev-2026.10.21-bbbbbbb"
    assert middle.notes == (bundles.Note("d" * 40, "feat(agent): x"),)
    assert middle.notes_base == "2026.10.20-aaaaaaa"
    assert middle.built_at == dt.datetime(2026, 10, 21, 1, tzinfo=dt.UTC)


def test_missing_dir_is_an_empty_catalog(tmp_path):
    assert bundles.scan(tmp_path / "nope") == ([], [])


@pytest.mark.parametrize("override, reason", [
    ({"size": 999}, "kích thước"),
    ({"size": 0}, "kích thước"),
    ({"size": True}, "kích thước"),
    ({"commit": "xyz"}, "commit"),
    ({"sha256": "XY"}, "sha256"),
    ({"bundle": "loopi-dev-other.raucb"}, "tên bundle"),
    ({"built_at": "yesterday"}, "ngày build"),
    ({"built_at": "2026-10-21T01:00:00"}, "ngày build"),
    ({"notes": [{"commit": "c" * 40}]}, "ghi chú"),
    ({"notes": [{"commit": "c" * 40, "subject": "s"}] * 51}, "ghi chú"),
    ({"notes_truncated": "yes"}, "ghi chú"),
    ({"notes_base": "a b"}, "ghi chú"),
])
def test_descriptions_that_disagree_with_their_bundle_are_unusable(tmp_path, override, reason):
    write_bundle(tmp_path, **override)
    found, unusable = bundles.scan(tmp_path)
    assert found == []
    (entry,) = unusable
    assert entry.stem == "loopi-dev-2026.10.21-a1b2c3d"
    assert reason in entry.reason


def test_a_description_whose_version_disagrees_with_its_file_name_is_unusable(tmp_path):
    # `version` is a named parameter of write_bundle (it also names the files), so it cannot be passed as an override.
    description = write_bundle(tmp_path)
    (tmp_path / "loopi-dev-2026.10.21-a1b2c3d.json").write_text(json.dumps({**description, "version": "2026.10.21-other"}))
    found, unusable = bundles.scan(tmp_path)
    assert found == []
    (entry,) = unusable
    assert entry.stem == "loopi-dev-2026.10.21-a1b2c3d"
    assert "phiên bản" in entry.reason


def test_broken_json_and_orphans_are_unusable(tmp_path):
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "loopi-dev-1.json").write_text("{not json")
    (tmp_path / "loopi-dev-4.json").write_text("[]")
    (tmp_path / "loopi-prod-2.raucb").write_bytes(b"x")
    write_bundle(tmp_path, version="3")
    (tmp_path / "loopi-dev-3.raucb").unlink()
    found, unusable = bundles.scan(tmp_path)
    assert found == []
    reasons = {u.stem: u.reason for u in unusable}
    assert "JSON" in reasons["loopi-dev-1"]
    assert "JSON" in reasons["loopi-dev-4"]
    assert "mô tả" in reasons["loopi-prod-2"]
    assert ".raucb" in reasons["loopi-dev-3"]


def test_an_unreadable_description_is_unusable_not_fatal(tmp_path):
    (tmp_path / "loopi-dev-5.json").mkdir()  # reading a directory raises OSError
    found, unusable = bundles.scan(tmp_path)
    assert found == []
    assert [(u.stem, u.reason) for u in unusable] == [("loopi-dev-5", "không đọc được file")]


@pytest.mark.parametrize("name", [
    "../loopi-dev-2026.10.21-a1b2c3d.raucb", "loopi-dev-2026.10.21-a1b2c3d.json", "loopi-dev-unknown.raucb", "",
])
def test_find_only_returns_listed_bundles(tmp_path, name):
    write_bundle(tmp_path)
    assert bundles.find(tmp_path, name) is None


def test_find_returns_a_bundle(tmp_path):
    write_bundle(tmp_path)
    assert bundles.find(tmp_path, "loopi-dev-2026.10.21-a1b2c3d.raucb").size == len(b"rauc-bundle-bytes")


def _client(settings, pool) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(settings, pool=pool)), base_url="http://brain.test")


NAME = "loopi-dev-2026.10.21-a1b2c3d.raucb"
BODY = bytes(range(256)) * 4


async def test_paired_device_downloads_a_bundle(pki_settings, pool):
    write_bundle(pki_settings.ota_dir, body=BODY)
    dev = await paired_device(pool, pki_settings)
    async with _client(pki_settings, pool) as c:
        r = await c.get(f"/v1/ota/{NAME}", headers=auth_headers(dev["fingerprint"]))
    assert r.status_code == 200
    assert r.content == BODY
    assert r.headers["content-type"] == "application/octet-stream"


async def test_range_resumes_a_download(pki_settings, pool):
    write_bundle(pki_settings.ota_dir, body=BODY)
    dev = await paired_device(pool, pki_settings)
    async with _client(pki_settings, pool) as c:
        r = await c.get(f"/v1/ota/{NAME}", headers={**auth_headers(dev["fingerprint"]), "Range": "bytes=1000-"})
        past = await c.get(f"/v1/ota/{NAME}", headers={**auth_headers(dev["fingerprint"]), "Range": f"bytes={len(BODY)}-"})
    assert r.status_code == 206
    assert r.content == BODY[1000:]
    assert r.headers["content-range"] == f"bytes 1000-{len(BODY) - 1}/{len(BODY)}"
    assert past.status_code == 416


async def test_only_paired_devices_download(pki_settings, pool):
    write_bundle(pki_settings.ota_dir, body=BODY)
    dev = await paired_device(pool, pki_settings)
    await devices.revoke_device(pool, dev["device_id"])
    async with _client(pki_settings, pool) as c:
        anonymous = await c.get(f"/v1/ota/{NAME}")
        revoked = await c.get(f"/v1/ota/{NAME}", headers=auth_headers(dev["fingerprint"]))
    assert anonymous.status_code == 401
    assert revoked.status_code == 401


@pytest.mark.parametrize("name", ["loopi-dev-unknown.raucb", "loopi-dev-2026.10.21-a1b2c3d.json", "passwd", "..%2Fpasswd"])
async def test_unknown_or_odd_names_are_404(pki_settings, pool, name):
    write_bundle(pki_settings.ota_dir, body=BODY)
    dev = await paired_device(pool, pki_settings)
    async with _client(pki_settings, pool) as c:
        r = await c.get(f"/v1/ota/{name}", headers=auth_headers(dev["fingerprint"]))
    assert r.status_code == 404


async def test_unsupported_agent_major_is_426(pki_settings, pool):
    write_bundle(pki_settings.ota_dir, body=BODY)
    dev = await paired_device(pool, pki_settings)
    async with _client(pki_settings, pool) as c:
        r = await c.get(f"/v1/ota/{NAME}", headers=auth_headers(dev["fingerprint"], agent_version="9.0.0"))
    assert r.status_code == 426


async def test_a_bundle_whose_file_no_longer_matches_is_not_served(pki_settings, pool):
    write_bundle(pki_settings.ota_dir, body=BODY)
    (pki_settings.ota_dir / NAME).write_bytes(BODY + b"tampered")
    dev = await paired_device(pool, pki_settings)
    async with _client(pki_settings, pool) as c:
        r = await c.get(f"/v1/ota/{NAME}", headers=auth_headers(dev["fingerprint"]))
    assert r.status_code == 404
    assert json.loads(r.text)["detail"] == "unknown bundle"
