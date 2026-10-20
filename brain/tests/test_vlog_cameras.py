import datetime as dt
from zoneinfo import ZoneInfo

from helpers import insert_item, insert_keyframe, insert_moment, paired_device
from loopi_brain import places
from loopi_brain.vlog import material
from loopi_brain.vlog.material import Segment

DAY = dt.date(2026, 10, 14)
ZONE = ZoneInfo("Asia/Seoul")
START = dt.datetime(2026, 10, 13, 15, 0, tzinfo=dt.UTC)  # the local day's start
NINE = dt.datetime(2026, 10, 14, 0, 0, tzinfo=dt.UTC)


def seg(item, device, minutes):
    return Segment(item, f"view/{item}.mp4", START + dt.timedelta(minutes=minutes), device)


def test_each_block_keeps_the_camera_with_the_most_moments():
    segments = [seg("p1", "pi", 541), seg("c1", "cam", 541), seg("p2", "pi", 552), seg("c2", "cam", 553)]
    counts = {(54, "pi"): 2, (54, "cam"): 5, (55, "pi"): 7, (55, "cam"): 1}
    assert [s.item_id for s in material.choose_cameras(segments, counts, START)] == ["c1", "p2"]


def test_ties_go_to_more_segments_then_the_smaller_id():
    segments = [seg("a1", "a", 1), seg("b1", "b", 2), seg("b2", "b", 3)]
    assert [s.item_id for s in material.choose_cameras(segments, {}, START)] == ["b1", "b2"]
    assert [s.item_id for s in material.choose_cameras([seg("x", "b", 1), seg("y", "a", 2)], {}, START)] == ["y"]


def test_one_camera_keeps_every_active_segment():
    segments = [seg("p1", "pi", 1), seg("p2", "pi", 12), seg("p3", "pi", 25)]
    assert material.choose_cameras(segments, {}, START) == segments


async def _video(pool, device_id, start):
    item_id = await insert_item(pool, device_id, kind="video", started_at=start)
    async with pool.connection() as conn:
        await conn.execute("UPDATE item SET analysis_state = 'done', view_path = %s WHERE id = %s",
                           (f"view/{item_id}.mp4", item_id))
    return item_id


async def test_the_material_takes_one_camera_per_block_and_names_the_places(pool, pki_settings):
    pi, cam = await paired_device(pool, pki_settings), await paired_device(pool, pki_settings)
    await places.set_location(pool, pi["device_id"], "Bếp")
    await places.set_location(pool, cam["device_id"], "Phòng khách")
    pi_seg = await _video(pool, pi["device_id"], NINE)
    cam_seg = await _video(pool, cam["device_id"], NINE)
    for k in range(3):
        await insert_moment(pool, cam["device_id"], kind="visual", ts=NINE + dt.timedelta(seconds=10 * k), text="x")
    await insert_moment(pool, pi["device_id"], kind="visual", ts=NINE, text="y")
    got = await material.vlog_material(pool, DAY, ZONE)
    assert [s.item_id for s in got.segments] == [cam_seg] and got.segments[0].device_id == cam["device_id"]
    assert got.places == {cam["device_id"]: "Phòng khách"}  # only the devices the material shows
    assert pi_seg not in [s.item_id for s in got.segments]


async def test_stills_from_any_camera_keep_their_camera_and_name_their_place(pool, pki_settings):
    pi, cam = await paired_device(pool, pki_settings), await paired_device(pool, pki_settings)
    await places.set_location(pool, cam["device_id"], "Phòng khách")
    pi_seg = await _video(pool, pi["device_id"], NINE)
    cam_item = await insert_item(pool, cam["device_id"], kind="video", started_at=NINE)
    await insert_moment(pool, pi["device_id"], kind="visual", ts=NINE, text="x")
    pi_frame = await insert_keyframe(pool, pki_settings, pi_seg, pi["device_id"], NINE)
    cam_frame = await insert_keyframe(pool, pki_settings, cam_item, cam["device_id"], NINE + dt.timedelta(minutes=1))
    got = await material.vlog_material(pool, DAY, ZONE)
    assert [s.device_id for s in got.segments] == [pi["device_id"]]  # the camera of the block
    assert {f.keyframe_id: f.device_id for f in got.frames} == {pi_frame: pi["device_id"], cam_frame: cam["device_id"]}
    assert set(got.places) == {pi["device_id"], cam["device_id"]}  # the still of the other camera is in the material


async def test_a_day_without_material_names_no_place(pool):
    got = await material.vlog_material(pool, DAY, ZONE)
    assert got.empty and got.places == {}
