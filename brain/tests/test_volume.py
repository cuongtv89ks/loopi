"""The media volume guard: with LOOPI_MEDIA_REQUIRE_MARKER the brain writes media only while the disk holding them is
mounted (its root carries the marker file), never into the empty mount point underneath."""
import httpx
import pytest

from helpers import fetch_all, item_headers, paired_device
from loopi_brain import forget, volume
from loopi_brain.app import create_app
from loopi_brain.pipeline import worker

ITEM_ID = "01J9Z8Y7X6W5V4T3S2R1Q0P9N8"


@pytest.fixture
def unmounted(pki_settings):
    """Settings that require the marker, over a media root that lacks it (the disk did not mount)."""
    pki_settings.media_root.mkdir(parents=True, exist_ok=True)
    return pki_settings.model_copy(update={"media_require_marker": True})


@pytest.fixture
def mounted(unmounted):
    (unmounted.media_root / volume.MARKER).touch()
    return unmounted


def test_without_the_requirement_any_media_root_is_ready(tmp_path):
    assert volume.ready(tmp_path / "missing", required=False)
    volume.check(tmp_path / "missing", required=False)


def test_a_required_marker_must_be_a_file_in_the_media_root(tmp_path):
    assert not volume.ready(tmp_path, required=True)
    with pytest.raises(volume.MediaVolumeMissing, match=r"\.loopi-media"):
        volume.check(tmp_path, required=True)
    (tmp_path / volume.MARKER).mkdir()  # a directory of that name is not the marker
    assert not volume.ready(tmp_path, required=True)
    (tmp_path / volume.MARKER).rmdir()
    (tmp_path / volume.MARKER).touch()
    assert volume.ready(tmp_path, required=True)
    volume.check(tmp_path, required=True)


def _client(settings, pool) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(settings, pool=pool)), base_url="http://brain.test")


async def test_healthz_fails_while_the_media_volume_is_missing(unmounted, pool):
    async with _client(unmounted, pool) as c:
        r = await c.get("/healthz")
    assert r.status_code == 503
    assert r.json() == {"status": "media volume missing"}


async def test_healthz_passes_once_the_marker_is_there(mounted, pool):
    async with _client(mounted, pool) as c:
        assert (await c.get("/healthz")).status_code == 200


async def test_an_upload_is_refused_as_507_and_nothing_is_written(unmounted, pool):
    device = await paired_device(pool, unmounted)
    body = b"\x00\x00\x00\x01" + b"x" * 64
    async with _client(unmounted, pool) as c:
        r = await c.put(f"/v1/items/{ITEM_ID}", content=body, headers=item_headers(device["fingerprint"], body))
    assert r.status_code == 507  # the agent keeps the item queued and retries (BrainFull)
    assert "not mounted" in r.json()["detail"]
    assert await fetch_all(pool, "SELECT id FROM item") == []
    assert [p for p in unmounted.media_root.rglob("*") if p.is_file()] == []


async def test_an_upload_is_stored_once_the_marker_is_there(mounted, pool):
    device = await paired_device(pool, mounted)
    body = b"\x00\x00\x00\x01" + b"x" * 64
    async with _client(mounted, pool) as c:
        r = await c.put(f"/v1/items/{ITEM_ID}", content=body, headers=item_headers(device["fingerprint"], body))
    assert r.status_code == 201


async def test_the_worker_refuses_to_start_without_the_volume(unmounted):
    with pytest.raises(volume.MediaVolumeMissing):
        await worker.run_worker(unmounted)  # before migrating or opening anything


async def test_forget_refuses_without_the_volume_and_keeps_every_row(unmounted, pool):
    """A missing file counts as gone: forgetting with the disk away would drop the rows and leave the files behind."""
    import datetime as dt

    zone = unmounted.zone
    start = dt.datetime(2026, 10, 14, 9, tzinfo=zone)
    with pytest.raises(volume.MediaVolumeMissing):
        await forget.forget_range(pool, unmounted, start, start + dt.timedelta(hours=1))
    assert await fetch_all(pool, "SELECT id FROM forget_log") == []


async def test_the_forget_page_says_the_disk_is_missing(unmounted, pool, jobs_open):
    form = {"start": "2026-10-14T09:00", "end": "2026-10-14T10:00", "confirm": "yes"}
    async with _client(unmounted, pool) as c:
        r = await c.post("/settings/forget", data=form, headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert "Ổ media chưa được gắn" in r.text
    assert await fetch_all(pool, "SELECT id FROM forget_log") == []
