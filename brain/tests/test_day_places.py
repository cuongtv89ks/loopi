import datetime as dt

import pytest

from helpers import insert_chapter, insert_item, insert_keyframe, insert_moment, paired_device
from loopi_brain import devices, places

NINE = dt.datetime(2026, 10, 14, 0, 0, tzinfo=dt.UTC)  # 09:00 in Seoul
DAY = dt.date(2026, 10, 14)


@pytest.fixture
async def two(pool, pki_settings):
    """Two cameras: the living room (Pi) and the kitchen."""
    living = await paired_device(pool, pki_settings)
    kitchen = await paired_device(pool, pki_settings)
    await places.set_location(pool, living["device_id"], "Phòng khách")
    await places.set_location(pool, kitchen["device_id"], "Bếp")
    return living["device_id"], kitchen["device_id"]


async def test_a_day_with_one_camera_has_no_labels_and_no_filter(client, pool, pki_settings):
    dev = await paired_device(pool, pki_settings)
    await insert_moment(pool, dev["device_id"], kind="speech", ts=NINE, text="Chào", lang="vi")
    text = (await client.get("/", params={"day": "2026-10-14"})).text
    assert 'class="place"' not in text and 'class="places"' not in text


async def test_every_entry_of_a_merged_day_names_its_place(client, pool, two):
    living, kitchen = two
    a = await insert_moment(pool, living, kind="speech", ts=NINE, text="Chào", lang="vi")
    b = await insert_moment(pool, kitchen, kind="marker", ts=NINE + dt.timedelta(minutes=5), text="x")
    text = (await client.get("/", params={"day": "2026-10-14"})).text
    assert f'<p class="speech" id="m-{a}"><time>09:00:00</time> <span class="place">Phòng khách</span> ' in text
    assert f'<p class="marker" id="m-{b}"><time>09:05:00</time> <span class="place">Bếp</span> ★' in text
    assert text.index("Tất cả") < text.index(f'href="/?day=2026-10-14&amp;device={kitchen}">Bếp</a>') < text.index(
        f'href="/?day=2026-10-14&amp;device={living}">Phòng khách</a>')  # ordered by location: Bếp before Phòng khách


async def test_the_filter_keeps_one_camera_and_every_link_keeps_the_filter(client, pool, two):
    living, kitchen = two
    keep = await insert_moment(pool, living, kind="speech", ts=NINE, text="Ở phòng khách", lang="vi")
    drop = await insert_moment(pool, kitchen, kind="speech", ts=NINE + dt.timedelta(hours=2), text="Ở bếp", lang="vi")
    only_kitchen = await insert_chapter(pool, DAY, NINE + dt.timedelta(hours=2), NINE + dt.timedelta(hours=2),
                                        title="Nấu ăn")
    text = (await client.get("/", params={"day": "2026-10-14", "device": living})).text
    assert f'id="m-{keep}"' in text and f'id="m-{drop}"' not in text
    assert f'id="ch-{only_kitchen}"' not in text  # no moment of this camera in that chapter
    assert 'class="place"' not in text  # one camera shown: no tags, the bar says which
    assert "<strong>Phòng khách</strong>" in text and 'href="/?day=2026-10-14">Tất cả</a>' in text
    assert f'href="/?day=2026-10-13&amp;device={living}"' in text and f'href="/?day=2026-10-15&amp;device={living}"' in text
    assert f'href="/hours?day=2026-10-14&amp;device={living}"' in text
    assert f'<input type="hidden" name="device" value="{living}">' in text


async def test_an_unknown_device_filter_is_refused(client, pool, two):
    assert (await client.get("/", params={"day": "2026-10-14", "device": "NOPE"})).status_code == 422
    assert (await client.get("/hours", params={"day": "2026-10-14", "device": "NOPE"})).status_code == 422


async def test_a_revoked_devices_moments_keep_their_label(client, pool, two):
    living, kitchen = two
    await insert_moment(pool, living, kind="speech", ts=NINE, text="Một", lang="vi")
    await insert_moment(pool, kitchen, kind="speech", ts=NINE + dt.timedelta(minutes=1), text="Hai", lang="vi")
    await devices.revoke_device(pool, kitchen)
    text = (await client.get("/", params={"day": "2026-10-14"})).text
    assert '<span class="place">Bếp</span>' in text


async def test_the_hours_page_filters_and_labels_too(client, pool, two):
    living, kitchen = two
    await insert_item(pool, living, kind="audio", started_at=NINE)
    other = await insert_item(pool, kitchen, kind="audio", started_at=NINE + dt.timedelta(minutes=1))
    await insert_moment(pool, kitchen, kind="speech", ts=NINE + dt.timedelta(minutes=1), text="Ở bếp", lang="vi")
    merged = (await client.get("/hours", params={"day": "2026-10-14"})).text
    assert '<span class="place">Bếp</span>' in merged and '<span class="place">Phòng khách</span>' in merged
    filtered = (await client.get("/hours", params={"day": "2026-10-14", "device": living})).text
    assert f'id="item-{other}"' not in filtered and "Ở bếp" not in filtered
    assert f'href="/hours?day=2026-10-13&amp;device={living}"' in filtered
    assert f'href="/?day=2026-10-14&amp;device={living}"' in filtered


async def test_a_location_is_escaped_in_the_filter_bar(client, pool, two):
    living, kitchen = two
    await places.set_location(pool, kitchen, "<i>Bếp</i>")
    await insert_moment(pool, living, kind="speech", ts=NINE, text="Một", lang="vi")
    await insert_moment(pool, kitchen, kind="speech", ts=NINE, text="Hai", lang="vi")
    text = (await client.get("/", params={"day": "2026-10-14"})).text
    assert "<i>Bếp</i>" not in text and "&lt;i&gt;Bếp&lt;/i&gt;" in text


async def test_the_hours_page_labels_keyframes_and_markers_and_a_filter_drops_them(client, pool, pki_settings, two):
    living, kitchen = two
    item = await insert_item(pool, kitchen, kind="video", started_at=NINE)
    kf = await insert_keyframe(pool, pki_settings, item, kitchen, NINE)
    await insert_moment(pool, kitchen, kind="marker", ts=NINE, text="x")
    await insert_moment(pool, living, kind="speech", ts=NINE, text="Một", lang="vi")  # two cameras: labels shown
    merged = (await client.get("/hours", params={"day": "2026-10-14"})).text
    assert f'<figure id="kf-{kf}">' in merged and '<time>09:00:00</time>\n        <span class="place">Bếp</span> ' in merged
    assert '<time>09:00:00</time> <span class="place">Bếp</span> ★' in merged
    only_living = (await client.get("/hours", params={"day": "2026-10-14", "device": living})).text
    assert f'id="kf-{kf}"' not in only_living and "★" not in only_living


async def test_a_blank_device_filter_is_no_filter(client, pool, two):
    living, kitchen = two
    await insert_moment(pool, living, kind="speech", ts=NINE, text="Một", lang="vi")
    await insert_moment(pool, kitchen, kind="speech", ts=NINE, text="Hai", lang="vi")
    for path in ("/", "/hours"):
        text = (await client.get(path, params={"day": "2026-10-14", "device": ""})).text
        assert "Một" in text and "Hai" in text and '<span class="place">Bếp</span>' in text


async def test_a_known_camera_with_no_recording_that_day_still_filters(client, pool, two):
    living, kitchen = two
    await insert_moment(pool, living, kind="speech", ts=NINE, text="Một", lang="vi")
    text = (await client.get("/", params={"day": "2026-10-14", "device": kitchen})).text
    assert "Một" not in text and "<strong>Bếp</strong>" not in text  # the bar lists the day's cameras only
    assert 'href="/?day=2026-10-14">Tất cả</a>' in text
    assert "Không có dữ liệu cho ngày này." in text


async def test_day_places_lists_cameras_with_a_recording_in_range_by_location_then_id(pool, pki_settings, two):
    living, kitchen = two
    idle = (await paired_device(pool, pki_settings))["device_id"]
    await insert_item(pool, living, kind="audio", started_at=NINE)
    await insert_moment(pool, kitchen, kind="marker", ts=NINE + dt.timedelta(hours=1))
    await insert_moment(pool, idle, kind="marker", ts=NINE + dt.timedelta(days=1))  # outside the range
    got = await places.day_places(pool, NINE, NINE + dt.timedelta(hours=2))
    assert got == [{"id": kitchen, "location": "Bếp"}, {"id": living, "location": "Phòng khách"}]
    assert await places.known_device(pool, kitchen) and not await places.known_device(pool, "NOPE")
