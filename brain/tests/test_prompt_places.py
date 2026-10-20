import datetime as dt

import pytest

from helpers import insert_chapter, insert_moment, paired_device
from loopi_brain import places
from loopi_brain.pipeline import chapters, recap

NINE = dt.datetime(2026, 10, 14, 0, 0, tzinfo=dt.UTC)
DAY = dt.date(2026, 10, 14)


def at(minutes: int) -> dt.datetime:
    return NINE + dt.timedelta(minutes=minutes)


@pytest.fixture
async def two(pool, pki_settings):
    living = await paired_device(pool, pki_settings)
    kitchen = await paired_device(pool, pki_settings)
    await places.set_location(pool, living["device_id"], "Phòng khách")
    await places.set_location(pool, kitchen["device_id"], "Bếp")
    return living["device_id"], kitchen["device_id"]


async def test_chapter_lines_name_the_place_when_two_cameras_recorded(pool, pki_settings, two):
    living, kitchen = two
    await insert_moment(pool, living, kind="visual", ts=at(15), text="Two people cook")
    await insert_moment(pool, kitchen, kind="visual", ts=at(16), text="Two people cook")  # same words, other room
    await insert_moment(pool, living, kind="visual", ts=at(17), text="Two people cook")  # a repeat for that camera
    await insert_moment(pool, kitchen, kind="speech", ts=at(20), text="Mai mình đi chợ nhé", lang="vi")
    await insert_moment(pool, kitchen, kind="marker", ts=at(40), text="Marked moment")
    lines = await chapters.chapter_lines(pool, NINE, at(59), pki_settings.zone)
    assert lines[:3] == ["09:15 at Phòng khách, picture: Two people cook", "09:16 at Bếp, picture: Two people cook",
                         "09:20 at Bếp, speech (vi): Mai mình đi chợ nhé"]
    assert lines[3].startswith("09:40 at Bếp, the user pressed the mark button")
    assert len(lines) == 4 and not any(line.startswith("09:17") for line in lines)  # de-duplicated per camera


async def test_chapter_lines_of_one_camera_are_unchanged(pool, pki_settings, two):
    living, _ = two
    await insert_moment(pool, living, kind="visual", ts=at(15), text="Two people cook")
    await insert_moment(pool, living, kind="visual", ts=at(16), text="Two people cook")  # a repeat: dropped
    await insert_moment(pool, living, kind="speech", ts=at(20), text="Mai mình đi chợ nhé", lang="vi")
    assert await chapters.chapter_lines(pool, NINE, at(59), pki_settings.zone) == [
        "09:15 picture: Two people cook", "09:20 speech (vi): Mai mình đi chợ nhé"]


async def test_recap_lines_name_the_place_when_two_cameras_recorded(pool, pki_settings, two):
    living, kitchen = two
    await insert_chapter(pool, DAY, NINE, at(30), title="Sáng", summary="Một buổi sáng.")
    await insert_moment(pool, living, kind="speech", ts=at(10), text="Thank you.", lang="en")
    await insert_moment(pool, kitchen, kind="marker", ts=at(40), text="Marked moment")
    material = await recap.recap_material(pool, DAY, pki_settings.zone)
    assert material.speech == ["[S1] 09:10 at Phòng khách (en) Thank you."]
    assert material.markers[0].startswith("[M1] 09:40 at Bếp, the user pressed the mark button")


async def test_recap_lines_of_one_camera_are_unchanged(pool, pki_settings, two):
    living, _ = two
    await insert_moment(pool, living, kind="speech", ts=at(10), text="Thank you.", lang="en")
    material = await recap.recap_material(pool, DAY, pki_settings.zone)
    assert material.speech == ["[S1] 09:10 (en) Thank you."]
