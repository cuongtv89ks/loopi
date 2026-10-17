import datetime as dt

import pytest

from helpers import fetch_all
from loopi_brain.mcp import tokens

T0 = dt.datetime(2026, 10, 17, 1, 0, tzinfo=dt.UTC)


async def test_a_new_token_is_shown_once_and_stored_only_as_its_hash(pool):
    token_id, token = await tokens.create_token(pool, "  Claude   Code ")
    assert token.startswith("loopi_") and len(token) >= 40
    [row] = await fetch_all(pool, "SELECT * FROM mcp_token")
    assert row["id"] == token_id and row["name"] == "Claude Code"
    assert row["token_sha256"] == tokens.digest(token) and token not in str(row)
    assert row["last_used_at"] is None and row["revoked_at"] is None
    assert await tokens.check_token(pool, token) == token_id


async def test_wrong_malformed_and_revoked_tokens_are_refused(pool):
    token_id, token = await tokens.create_token(pool, "a")
    for bad in ("", "loopi_", token + "x", token[len("loopi_"):], "loopi_" + "a" * 300, "Bearer " + token):
        assert await tokens.check_token(pool, bad) is None
    assert await tokens.revoke_token(pool, token_id) is True
    assert await tokens.check_token(pool, token) is None
    assert await tokens.revoke_token(pool, token_id) is False  # already revoked
    assert await tokens.revoke_token(pool, "01NOSUCHTOKEN0000000000000") is False


async def test_last_use_is_written_at_most_once_a_minute(pool):
    _, token = await tokens.create_token(pool, "a")
    await tokens.check_token(pool, token, now=T0)
    await tokens.check_token(pool, token, now=T0 + dt.timedelta(seconds=30))
    [row] = await fetch_all(pool, "SELECT last_used_at FROM mcp_token")
    assert row["last_used_at"] == T0
    await tokens.check_token(pool, token, now=T0 + dt.timedelta(seconds=61))
    [row] = await fetch_all(pool, "SELECT last_used_at FROM mcp_token")
    assert row["last_used_at"] == T0 + dt.timedelta(seconds=61)


@pytest.mark.parametrize("name", ["", "   ", "x" * 61])
async def test_a_name_must_be_short_and_not_empty(pool, name):
    with pytest.raises(tokens.TokenError) as exc:
        await tokens.create_token(pool, name)
    assert "x" * 61 not in str(exc.value)  # the message never repeats the name
    assert await fetch_all(pool, "SELECT * FROM mcp_token") == []


async def test_the_list_is_newest_first_and_never_shows_a_hash(pool):
    first, _ = await tokens.create_token(pool, "first")
    second, _ = await tokens.create_token(pool, "second")
    await tokens.revoke_token(pool, first)
    listed = await tokens.list_tokens(pool)
    assert [t["id"] for t in listed] == [second, first]
    assert listed[0]["revoked_at"] is None and listed[1]["revoked_at"] is not None
    assert all(set(t) == {"id", "name", "created_at", "last_used_at", "revoked_at"} for t in listed)
