import re

import httpx
import pytest

from helpers import FakeOllama, fetch_all
from loopi_brain import store
from loopi_brain.app import create_app
from loopi_brain.mcp import tokens

HX = {"HX-Request": "true"}


@pytest.fixture
async def web(pool, pki_settings, jobs_open):
    app = create_app(pki_settings, pool=pool, http_client=FakeOllama().client())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://brain.test") as c:
        yield c


async def test_the_settings_page_has_the_mcp_card_with_pictures_off(web):
    text = (await web.get("/settings")).text
    assert 'id="mcp"' in text and "MCP (Claude Code)" in text
    assert "Chữ trả về qua MCP sẽ được client gửi lên model trên cloud" in text
    assert "Ảnh qua MCP: <strong>đang tắt</strong>" in text and "Chưa có token nào." in text


async def test_a_new_token_is_shown_once_with_the_command_to_connect(web, pool):
    r = await web.post("/settings/mcp/tokens", data={"name": "Claude Code"}, headers=HX)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    [row] = await fetch_all(pool, "SELECT id, token_sha256 FROM mcp_token")
    shown = set(re.findall(r"loopi_[A-Za-z0-9_-]{40,}", r.text))
    assert len(shown) == 1 and tokens.digest(next(iter(shown))) == row["token_sha256"]
    assert "claude mcp add --transport http loopi https://localhost:8443/mcp" in r.text
    page = (await web.get("/settings")).text
    assert next(iter(shown)) not in page and "Claude Code" in page and "chưa dùng" in page  # never shown again


@pytest.mark.parametrize("name", ["", "   ", "x" * 61])
async def test_an_unusable_name_gets_its_reason_next_to_the_form(web, pool, name):
    r = await web.post("/settings/mcp/tokens", data={"name": name}, headers=HX)
    assert r.status_code == 200 and "Tên token phải có từ 1 tới 60 ký tự." in r.text
    assert "x" * 61 not in r.text and await fetch_all(pool, "SELECT * FROM mcp_token") == []


async def test_a_token_can_be_revoked_once(web, pool):
    token_id, token = await tokens.create_token(pool, "old")
    r = await web.post(f"/settings/mcp/tokens/{token_id}/revoke", headers=HX)
    assert r.status_code == 200 and "đã thu hồi" in r.text
    assert await tokens.check_token(pool, token) is None
    assert (await web.post(f"/settings/mcp/tokens/{token_id}/revoke", headers=HX)).status_code == 404
    assert (await web.post("/settings/mcp/tokens/01NOSUCH/revoke", headers=HX)).status_code == 404


async def test_the_picture_switch_saves(web, pool):
    r = await web.post("/settings/mcp/images", data={"enabled": "on"}, headers=HX)
    assert r.status_code == 200 and "được gửi" in r.text and (await store.get_mcp(pool)).images is True
    r = await web.post("/settings/mcp/images", data={}, headers=HX)
    assert "không gửi ảnh" in r.text and (await store.get_mcp(pool)).images is False


@pytest.mark.parametrize("path", ["/settings/mcp/tokens", "/settings/mcp/tokens/x/revoke", "/settings/mcp/images"])
async def test_every_mcp_post_needs_htmx_and_every_form_is_a_post(web, pool, path):
    assert (await web.post(path, data={"name": "a", "enabled": "on"})).status_code == 403
    assert await fetch_all(pool, "SELECT * FROM mcp_token") == [] and (await store.get_mcp(pool)).images is False
    page = (await web.get("/settings")).text
    card = page.split('id="mcp"', 1)[1].split("</section>", 1)[0]
    assert card.count("<form") == card.count('method="post"') == 2
