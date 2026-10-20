from psycopg.types.json import Jsonb

from helpers import paired_device


async def _beat(pool, device_id, *, recording, seconds_ago=0):
    await _status(pool, device_id, {"recording_video": recording}, seconds_ago)


async def _status(pool, device_id, status, seconds_ago=0):
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE device SET last_heartbeat = %s, last_heartbeat_at = now() - make_interval(secs => %s) WHERE id = %s",
            (Jsonb(status), seconds_ago, device_id),
        )


async def test_every_page_asks_for_the_recording_line(client):
    page = (await client.get("/settings")).text
    assert 'hx-get="/status/recording"' in page and 'hx-trigger="load, every 15s"' in page


async def test_the_line_counts_recording_cameras_heard_from_lately(client, pool, pki_settings):
    a = await paired_device(pool, pki_settings)
    b = await paired_device(pool, pki_settings)
    c = await paired_device(pool, pki_settings)
    await _beat(pool, a["device_id"], recording=True)
    await _beat(pool, b["device_id"], recording=False)
    await _beat(pool, c["device_id"], recording=True, seconds_ago=120)  # silent for two minutes
    r = await client.get("/status/recording")
    assert r.status_code == 200 and '<a href="/devices">Đang ghi: 1 camera</a>' in r.text


async def test_a_revoked_camera_is_not_counted(client, pool, pki_settings):
    from loopi_brain import devices

    a = await paired_device(pool, pki_settings)
    await _beat(pool, a["device_id"], recording=True)
    assert await devices.revoke_device(pool, a["device_id"])
    r = await client.get("/status/recording")
    assert r.status_code == 200 and '<a href="/devices">Đang ghi: 0 camera</a>' in r.text


async def test_only_a_heartbeat_that_says_recording_video_true_counts(client, pool, pki_settings):
    never = await paired_device(pool, pki_settings)  # paired, never sent a heartbeat
    for status in ({"recording_video": None}, {"recording_video": "yes"}, {"recording_video": 1},
                   {"recording_video": "maybe"}, {}, {"paused": True}):
        d = await paired_device(pool, pki_settings)
        await _status(pool, d["device_id"], status)
    assert never["device_id"]
    # A cast to boolean takes "yes" and 1 as true and raises on "maybe": only a JSON true counts, and nothing raises.
    r = await client.get("/status/recording")
    assert r.status_code == 200 and '<a href="/devices">Đang ghi: 0 camera</a>' in r.text
    assert "<html" not in r.text  # a fragment, not a page inside the header


async def test_the_poll_never_touches_the_error_banner(client):
    text = (await client.get("/settings")).text
    assert 'const fromPoll = (e) => e.detail.elt.id === "recording-now";' in text
    listener = text.split('addEventListener("htmx:beforeRequest", (e) => {', 1)[1]
    assert listener.lstrip().startswith("if (fromPoll(e)) return;")
    # A failed poll says the count is unknown on its own line, still never on the banner.
    assert 'const pollFailed = (e) => { e.detail.elt.textContent = "Đang ghi: không rõ"; };' in text
    for event in ("htmx:responseError", "htmx:sendError"):
        listener = text.split(f'addEventListener("{event}", (e) => {{', 1)[1]
        assert listener.lstrip().startswith("if (fromPoll(e)) return pollFailed(e);"), event
