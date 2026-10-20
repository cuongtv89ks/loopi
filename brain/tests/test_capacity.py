import datetime as dt

from helpers import insert_item, make_csr_pem, paired_device
from loopi_brain import cameras, capacity, devices, pki


async def _heartbeat(pool, device_id):
    async with pool.connection() as conn:
        await conn.execute("UPDATE device SET last_heartbeat_at = now() WHERE id = %s", (device_id,))


async def test_measured_devices_and_cameras_still_waiting_count(pool, pki_settings, monkeypatch):
    monkeypatch.setattr(capacity, "_free_bytes", lambda settings: 200 * 10**9)
    pi = await paired_device(pool, pki_settings)
    await _heartbeat(pool, pi["device_id"])
    now = dt.datetime.now(dt.UTC)
    await insert_item(pool, pi["device_id"], kind="video", started_at=now - dt.timedelta(days=2))  # older than 24 h
    item = await insert_item(pool, pi["device_id"], kind="video", started_at=now - dt.timedelta(hours=1))
    async with pool.connection() as conn:
        await conn.execute("UPDATE item SET size_bytes = %s WHERE id = %s", (5 * 10**9, item))
    await cameras.add_camera(pool, pki_settings, cameras.clean_input(
        kind="ip", location="Bếp", video_url="rtsp://192.0.2.10/cam", audio_url="", username="", password="", has_audio=True))
    est = await capacity.estimate(pool, pki_settings)
    assert est.bytes_per_day == 5 * 10**9 + capacity.NOMINAL_BYTES_PER_DAY
    assert est.days == 7 and est.total_bytes == 7 * est.bytes_per_day
    assert est.new_camera_bytes == 7 * capacity.NOMINAL_BYTES_PER_DAY
    assert est.warn_if_added == (est.total_bytes + est.new_camera_bytes > 0.85 * 200 * 10**9)


async def test_an_unknown_free_space_never_warns(pool, pki_settings, monkeypatch):
    monkeypatch.setattr(capacity, "_free_bytes", lambda settings: None)
    est = await capacity.estimate(pool, pki_settings)
    assert (est.free_bytes, est.ratio, est.warn_if_added) == (None, None, False)


def test_gigabytes_are_shown_with_one_decimal():
    assert capacity.gb(31_000_000_000) == "31,0 GB" and capacity.gb(0) == "0,0 GB"


async def _paired_camera(pool, settings, location: str) -> str:
    """A network camera that has paired (its device row is linked), as the cameras container's agent does."""
    camera_id = await cameras.add_camera(pool, settings, cameras.clean_input(
        kind="ip", location=location, video_url="rtsp://192.0.2.11/cam", audio_url="", username="", password="",
        has_audio=True))
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT token FROM pair_token WHERE camera_id = %s", (camera_id,))
        token = (await cur.fetchone())["token"]
    reg = await devices.register_device(
        pool, token, {"model": "x", "serial": "1", "agent_version": "0.1.0"},
        lambda device_id: pki.sign_csr(settings.pki_dir, make_csr_pem(), device_id))
    await _heartbeat(pool, reg["device_id"])
    return reg["device_id"]


async def test_a_young_paired_camera_counts_at_the_nominal_rate_not_its_partial_day(pool, pki_settings, monkeypatch):
    monkeypatch.setattr(capacity, "_free_bytes", lambda settings: 200 * 10**9)
    device_id = await _paired_camera(pool, pki_settings, "Sân")
    item = await insert_item(pool, device_id, kind="video", started_at=dt.datetime.now(dt.UTC) - dt.timedelta(hours=1))
    async with pool.connection() as conn:
        await conn.execute("UPDATE item SET size_bytes = %s WHERE id = %s", (10**9, item))
    est = await capacity.estimate(pool, pki_settings)
    assert est.bytes_per_day == capacity.NOMINAL_BYTES_PER_DAY


async def test_a_revoked_camera_device_is_not_taken_out_of_the_total_it_was_never_put_into(pool, pki_settings, monkeypatch):
    monkeypatch.setattr(capacity, "_free_bytes", lambda settings: 200 * 10**9)
    device_id = await _paired_camera(pool, pki_settings, "Sân")
    item = await insert_item(pool, device_id, kind="video", started_at=dt.datetime.now(dt.UTC) - dt.timedelta(hours=1))
    async with pool.connection() as conn:
        await conn.execute("UPDATE item SET size_bytes = %s WHERE id = %s", (10**9, item))
        await conn.execute("UPDATE device SET revoked = TRUE WHERE id = %s", (device_id,))  # "Thu hồi", camera row stays
    est = await capacity.estimate(pool, pki_settings)
    assert est.bytes_per_day == capacity.NOMINAL_BYTES_PER_DAY  # not 4.4 GB minus its 1 GB
