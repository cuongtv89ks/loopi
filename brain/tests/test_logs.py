"""uvicorn's access log keeps method, path and status, and never the query string (the Search page sends the owner's
question as `GET /search?q=...`)."""
import asyncio
import logging
import logging.config

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from uvicorn.config import LOGGING_CONFIG
from uvicorn.logging import AccessFormatter

from loopi_brain import cli, logs

SECRET = "SECRET-7f3a"
UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access", "uvicorn.asgi")
ACCESS_FORMAT = '%s - "%s %s HTTP/%s" %d'  # what uvicorn's h11, httptools and h2 implementations log


@pytest.fixture(autouse=True)
def _restore_uvicorn_loggers():
    """dictConfig(uvicorn's config) rewires the process-wide uvicorn loggers: put them back after each test."""
    saved = {
        name: (lg.handlers[:], lg.filters[:], lg.level, lg.propagate)
        for name in UVICORN_LOGGERS
        for lg in [logging.getLogger(name)]
    }
    yield
    for name, (handlers, filters, level, propagate) in saved.items():
        lg = logging.getLogger(name)
        lg.handlers[:], lg.filters[:], lg.level, lg.propagate = handlers, filters, level, propagate


def access_record(target, **overrides):
    """A record shaped like the one uvicorn's request cycle emits."""
    fields = dict(
        name="uvicorn.access",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg=ACCESS_FORMAT,
        args=("127.0.0.1:50000", "GET", target, "1.1", 200),
        exc_info=None,
    )
    return logging.LogRecord(**{**fields, **overrides})


@pytest.mark.parametrize(
    ("target", "logged"),
    [
        (f"/search?q={SECRET}", "/search"),
        (f"/search?q={SECRET}%20xin%20ch%C3%A0o", "/search"),  # percent-encoded Vietnamese, as uvicorn writes it
        (f"/?q={SECRET}", "/"),
        (f"/healthz?q={SECRET}?again={SECRET}", "/healthz"),  # only the first ? counts
        ("/search?", "/search"),
        (f"/search%3Fq={SECRET}", f"/search%3Fq={SECRET}"),  # a literal ? in the path is %3F: it is path, not query
    ],
)
def test_the_query_string_never_reaches_the_rendered_line(target, logged):
    record = access_record(target)
    assert logs.StripQuery().filter(record) is True  # a record is never dropped
    expected = f'127.0.0.1:50000 - "GET {logged} HTTP/1.1" 200'
    assert record.getMessage() == expected
    formatter = AccessFormatter(fmt=LOGGING_CONFIG["formatters"]["access"]["fmt"], use_colors=False)
    assert formatter.format(record) == f'INFO:     127.0.0.1:50000 - "GET {logged} HTTP/1.1" 200 OK'


@pytest.mark.parametrize("target", ["/search", "/", "/media/frames/a.jpg", "/day/2026-10-14", "/static/app.css"])
def test_a_path_without_a_query_is_unchanged(target):
    record = access_record(target)
    args = record.args
    assert logs.StripQuery().filter(record) is True
    assert record.args == args
    assert record.getMessage() == f'127.0.0.1:50000 - "GET {target} HTTP/1.1" 200'


@pytest.mark.parametrize(
    "record",
    [
        access_record("", msg='%s - "%s %s"', args=("127.0.0.1", "GET", f"/search?q={SECRET}")),  # too few
        access_record("", msg=ACCESS_FORMAT + " %s", args=("a", "GET", f"/search?q={SECRET}", "1.1", 200, "x")),  # six
        access_record("/x", args=("a", "GET", 7, "1.1", 200)),  # the request target is not a string
        access_record("/x", msg="no arguments here ?q=text", args=()),
        access_record("/x", msg="%(target)s", args=({"target": f"/search?q={SECRET}"},)),  # a mapping, not a tuple
    ],
    ids=["too-few", "too-many", "not-a-string", "no-args", "mapping"],
)
def test_a_record_of_any_other_shape_is_left_alone(record):
    args, text = record.args, record.getMessage()
    assert logs.StripQuery().filter(record) is True
    assert record.args == args and record.getMessage() == text


def test_the_config_is_a_fresh_copy_so_uvicorn_never_edits_its_own_default():
    first, second = logs.uvicorn_log_config(), logs.uvicorn_log_config()
    first["formatters"]["access"]["use_colors"] = False  # what uvicorn.Config does when it is given use_colors=
    assert "use_colors" not in second["formatters"]["access"] and first is not second
    assert "filters" not in LOGGING_CONFIG and "filters" not in LOGGING_CONFIG["handlers"]["access"]
    assert set(first["handlers"]) == set(LOGGING_CONFIG["handlers"])  # everything else is uvicorn's own


def test_the_filter_survives_uvicorns_own_logging_setup(capsys):
    """uvicorn.Config(...) runs dictConfig(log_config) in its constructor, which rebuilds the access handler: a filter
    added to the old handler by hand would be gone, one declared in the config is rebuilt with it."""
    access = logging.getLogger("uvicorn.access")
    for _ in range(2):  # twice: a reload or a worker applies the config again, the filter must not pile up
        uvicorn.Config(FastAPI(), log_config=logs.uvicorn_log_config(), use_colors=False)
        (handler,) = access.handlers
        assert [type(f) for f in handler.filters] == [logs.StripQuery]
    access.info(ACCESS_FORMAT, "127.0.0.1:50000", "GET", f"/search?q={SECRET}", "1.1", 200)
    assert capsys.readouterr().out == 'INFO:     127.0.0.1:50000 - "GET /search HTTP/1.1" 200 OK\n'


async def test_a_real_uvicorn_request_is_logged_without_its_query(capsys):
    app = FastAPI()

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_config=logs.uvicorn_log_config(), use_colors=False)
    server = uvicorn.Server(config)
    serving = asyncio.create_task(server.serve())
    try:
        async with asyncio.timeout(10):
            while not server.started:
                assert not serving.done(), "uvicorn stopped before it started"
                await asyncio.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        # trust_env=False: a loopback request must not go through HTTP_PROXY / ALL_PROXY of the developer's shell
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", trust_env=False) as client:
            hit = await client.get("/healthz", params={"q": f"{SECRET} xin chào"})
            plain = await client.get("/healthz")
    finally:
        server.should_exit = True
        await serving
    assert hit.status_code == plain.status_code == 200
    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if "HTTP/1.1" in line]
    assert [line.split(' - ')[1] for line in lines] == ['"GET /healthz HTTP/1.1" 200 OK'] * 2
    assert SECRET not in out and "%20" not in out and "?" not in "".join(lines)


def test_serve_hands_uvicorn_the_stripping_log_config(monkeypatch, database_url):
    calls = {}
    monkeypatch.setenv("LOOPI_DATABASE_URL", database_url)
    monkeypatch.setattr("loopi_brain.db.migrate", lambda url: [])
    monkeypatch.setattr("loopi_brain.app.create_app", lambda settings: "the-app")
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: calls.update(app=app, **kwargs))
    assert cli.main(["serve"]) == 0
    assert calls["app"] == "the-app" and calls["host"] == "0.0.0.0" and calls["port"] == 8000
    config = calls["log_config"]
    assert config["handlers"]["access"]["filters"] == ["strip_query"]
    assert config["filters"]["strip_query"]["()"] is logs.StripQuery
