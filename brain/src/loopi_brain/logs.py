"""Logging setup for the api process."""
from __future__ import annotations

import copy
import logging
from typing import Any


class StripQuery(logging.Filter):
    """Cut the query string off the request target of uvicorn's access records.

    uvicorn logs ``'%s - "%s %s HTTP/%s" %d'`` with ``args = (client, method, target, http_version, status)`` and the
    target still carries ``?q=...``: the Search page would put the owner's question into the api's log. The target is
    percent-encoded, so the first ``?`` always starts the query. A record of any other shape passes untouched, and no
    record is ever dropped.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) == 5 and isinstance(args[2], str):
            client, method, target, http_version, status = args
            record.args = (client, method, target.partition("?")[0], http_version, status)
        return True


def uvicorn_log_config() -> dict[str, Any]:
    """uvicorn's own logging config with ``StripQuery`` declared on its access handler.

    ``uvicorn.run`` / ``uvicorn.Config`` call ``logging.config.dictConfig`` on this, which rebuilds the handlers: a
    filter added to the logger or the handler beforehand would not be the one that ends up in place, one declared in
    the config is created with the handler. Each call returns a fresh copy because uvicorn edits the dict it is given.
    """
    from uvicorn.config import LOGGING_CONFIG

    config = copy.deepcopy(LOGGING_CONFIG)
    config["filters"] = {"strip_query": {"()": StripQuery}}
    config["handlers"]["access"]["filters"] = ["strip_query"]
    return config
