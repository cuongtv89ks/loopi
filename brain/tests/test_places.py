from importlib import resources

import psycopg
import pytest

from helpers import fetch_one, fresh_database, paired_device
from loopi_brain import devices, places
from loopi_brain.db import migrate

HX = {"HX-Request": "true"}


def test_a_location_is_trimmed_and_its_inner_spaces_collapsed():
    assert places.clean_location("  Phòng   khách \n") == "Phòng khách"


@pytest.mark.parametrize(("text", "message"), [
    ("   ", "Tên vị trí không được để trống."),
    ("x" * 61, "Tên vị trí dài quá 60 ký tự."),
    ("Bếp\x00", "Tên vị trí có ký tự không hợp lệ."),
])
def test_a_bad_location_is_refused_with_a_vietnamese_reason(text, message):
    with pytest.raises(places.LocationError, match=message):
        places.clean_location(text)


def test_places_and_the_phrase_of_model_text():
    assert places.several(["A", "A", None]) is False
    assert places.several(["A", "B"]) is True
    assert places.at("Bếp", True) == " at Bếp" and places.at("Bếp", True, ",") == " at Bếp,"
    assert places.at("Bếp", False) == "" and places.at(None, True) == ""


def test_the_migration_starts_every_location_as_the_name(database_url):
    before = ["001_init.sql", "002_pipeline.sql", "003_understanding.sql", "004_retention_files.sql",
              "005_evaluation.sql", "006_mcp.sql", "007_vlog.sql", "008_ota.sql", "009_one_copy.sql",
              "010_retention_on.sql"]
    with fresh_database(database_url, "loopi_location") as url:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute("CREATE TABLE schema_migration (name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())")
            for name in before:
                conn.execute(resources.files("loopi_brain.migrations").joinpath(name).read_text())
                conn.execute("INSERT INTO schema_migration (name) VALUES (%s)", (name,))
            conn.execute("INSERT INTO device (id, name, cert_fingerprint) VALUES ('D1', 'loopi-ab12', 'fp')")
        assert "011_device_location.sql" in migrate(url)
        with psycopg.connect(url) as conn:
            assert conn.execute("SELECT location FROM device WHERE id = 'D1'").fetchone() == ("loopi-ab12",)
            with pytest.raises(psycopg.errors.NotNullViolation):
                conn.execute("INSERT INTO device (id, name, cert_fingerprint, location) VALUES ('D2', 'n', 'fp2', NULL)")


async def test_a_new_device_starts_with_its_name_as_its_location(pool, pki_settings):
    dev = await paired_device(pool, pki_settings)
    row = await fetch_one(pool, "SELECT name, location FROM device WHERE id = %s", (dev["device_id"],))
    assert row["location"] == row["name"]
    assert {d["id"]: d["location"] for d in await devices.list_devices(pool)}[dev["device_id"]] == row["name"]


async def test_the_owner_renames_a_location_from_the_devices_page(client, pool, pki_settings):
    dev = await paired_device(pool, pki_settings)
    url = f"/devices/{dev['device_id']}/location"
    assert (await client.post(url, data={"location": "Bếp"})).status_code == 403  # no HX-Request
    r = await client.post(url, data={"location": "  Phòng   khách "}, headers=HX)
    assert r.status_code == 200
    assert 'value="Phòng khách"' in r.text and "Đã lưu." in r.text
    assert (await fetch_one(pool, "SELECT location FROM device WHERE id = %s", (dev["device_id"],)))[
        "location"] == "Phòng khách"
    page = (await client.get("/devices")).text
    assert f'<strong id="device-title-{dev["device_id"]}">Phòng khách</strong>' in page and f'hx-post="/devices/{dev["device_id"]}/location"' in page


async def test_a_refused_location_keeps_the_old_one_and_says_why(client, pool, pki_settings):
    dev = await paired_device(pool, pki_settings)
    r = await client.post(f"/devices/{dev['device_id']}/location", data={"location": " "}, headers=HX)
    assert r.status_code == 200 and "Tên vị trí không được để trống." in r.text
    row = await fetch_one(pool, "SELECT name, location FROM device WHERE id = %s", (dev["device_id"],))
    assert row["location"] == row["name"]


async def test_a_location_is_escaped_everywhere(client, pool, pki_settings):
    dev = await paired_device(pool, pki_settings)
    await client.post(f"/devices/{dev['device_id']}/location", data={"location": "<b>x</b>"}, headers=HX)
    page = (await client.get("/devices")).text
    assert "<b>x</b>" not in page and "&lt;b&gt;x&lt;/b&gt;" in page


async def test_unknown_and_revoked_devices_cannot_be_renamed(client, pool, pki_settings):
    assert (await client.post("/devices/NOPE/location", data={"location": "Bếp"}, headers=HX)).status_code == 404
    dev = await paired_device(pool, pki_settings)
    await devices.revoke_device(pool, dev["device_id"])
    r = await client.post(f"/devices/{dev['device_id']}/location", data={"location": "Bếp"}, headers=HX)
    assert r.status_code == 404
    page = (await client.get("/devices")).text
    assert f'hx-post="/devices/{dev["device_id"]}/location"' not in page  # no form on a revoked card
