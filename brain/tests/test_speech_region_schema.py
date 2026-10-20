import datetime as dt

import psycopg
import pytest

from helpers import T0, fetch_one, insert_item, insert_moment, new_ulid, paired_device


async def _region(pool, item_id, device_id, *, state="waiting", covered_by=None, offset=1.0):
    region_id = new_ulid()
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO speech_region (id, item_id, device_id, start_ts, end_ts, offset_s, length_s, snr_db, state,"
            " covered_by) VALUES (%s, %s, %s, %s, %s, %s, 2.0, 20.0, %s, %s)",
            (region_id, item_id, device_id, T0 + dt.timedelta(seconds=offset),
             T0 + dt.timedelta(seconds=offset + 2), offset, state, covered_by),
        )
    return region_id


async def test_regions_go_with_their_item_and_a_cover_is_cleared(pool, pki_settings):
    a, b = await paired_device(pool, pki_settings), await paired_device(pool, pki_settings)
    item_a = await insert_item(pool, a["device_id"], kind="audio")
    item_b = await insert_item(pool, b["device_id"], kind="audio")
    chosen = await _region(pool, item_a, a["device_id"], state="chosen")
    covered = await _region(pool, item_b, b["device_id"], state="covered", covered_by=chosen)
    async with pool.connection() as conn:
        await conn.execute("DELETE FROM item WHERE id = %s", (item_a,))
    assert await fetch_one(pool, "SELECT count(*) AS n FROM speech_region WHERE item_id = %s", (item_a,)) == {"n": 0}
    assert await fetch_one(pool, "SELECT covered_by FROM speech_region WHERE id = %s", (covered,)) == {"covered_by": None}


async def test_a_region_state_is_one_of_three(pool, pki_settings):
    a = await paired_device(pool, pki_settings)
    item = await insert_item(pool, a["device_id"], kind="audio")
    with pytest.raises(psycopg.errors.CheckViolation):
        await _region(pool, item, a["device_id"], state="maybe")


async def test_items_start_without_speech_detection(pool, pki_settings):
    a = await paired_device(pool, pki_settings)
    item = await insert_item(pool, a["device_id"], kind="audio")
    assert await fetch_one(pool, "SELECT speech_detected_at FROM item WHERE id = %s", (item,)) == {
        "speech_detected_at": None}


async def test_a_region_waits_by_default_and_unlinks_a_deleted_moment(pool, pki_settings):
    a = await paired_device(pool, pki_settings)
    item = await insert_item(pool, a["device_id"], kind="audio")
    region = new_ulid()
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO speech_region (id, item_id, device_id, start_ts, end_ts, offset_s, length_s, snr_db)"
            " VALUES (%s, %s, %s, %s, %s, 1.0, 2.0, 20.0)",
            (region, item, a["device_id"], T0, T0 + dt.timedelta(seconds=2)),
        )
    assert await fetch_one(pool, "SELECT state FROM speech_region WHERE id = %s", (region,)) == {"state": "waiting"}
    moment = await insert_moment(pool, a["device_id"], kind="speech", item_id=item)
    async with pool.connection() as conn:
        await conn.execute("UPDATE speech_region SET state = 'chosen', moment_id = %s WHERE id = %s", (moment, region))
        await conn.execute("DELETE FROM moment WHERE id = %s", (moment,))
    assert await fetch_one(pool, "SELECT state, moment_id FROM speech_region WHERE id = %s", (region,)) == {
        "state": "chosen", "moment_id": None}
