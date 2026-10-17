import datetime as dt
import json

import pytest

from loopi_brain.providers.base import ProviderUnavailable
from loopi_brain.vlog import script
from loopi_brain.vlog.material import Candidate, Frame, Material

NINE = dt.datetime(2026, 10, 14, 0, 0, tzinfo=dt.UTC)
DAY = dt.date(2026, 10, 14)


def _material(n=12, audio_every=3) -> Material:
    candidates = []
    for k in range(n):
        at = NINE + dt.timedelta(minutes=10 * k)
        kind = "talk" if k % audio_every == 0 else "notable"
        candidates.append(Candidate(f"H{k + 1}", kind, at, Frame(f"K{k}", f"keyframes/K{k}.jpg", at), f"việc {k}",
                                    1.0 + k / 10, f"A{k}" if kind == "talk" else None, 5.0))
    return Material(DAY, [], [], candidates)


class FakeRouter:
    def __init__(self, answer=None, error=None):
        self.answer, self.error, self.calls = answer, error, []

    async def chat(self, task, messages, **kwargs):
        self.calls.append((task, messages, kwargs))
        if self.error:
            raise self.error
        return type("R", (), {"text": self.answer, "model_version": "ollama:qwen3:8b"})()


async def test_the_model_chooses_by_ref_and_the_brain_keeps_order_and_limits(pki_settings):
    answer = json.dumps({"title": "Một ngày ở nhà", "highlights": [
        {"ref": "H7", "line": "Buổi chiều", "audio": True},
        {"ref": "H1", "line": "Buổi sáng", "audio": True},
        {"ref": "H1", "line": "trùng", "audio": False},
        {"ref": "H99", "line": "không có", "audio": False},
        {"ref": "H4", "line": "Trưa", "audio": True},
        {"ref": "H10", "line": "Tối", "audio": True},
        {"ref": "H2", "line": "x" * 300, "audio": True},
    ]})
    router = FakeRouter(answer)
    written = await script.write_script(router, _material(), pki_settings.zone)
    assert written.title == "Một ngày ở nhà" and written.model_version == "ollama:qwen3:8b"
    assert [h.candidate.ref for h in written.highlights] == ["H1", "H2", "H4", "H7", "H10"]
    assert [h.audio for h in written.highlights] == [True, False, True, True, False]  # H2 has no sound; 3 at most
    assert len(written.highlights[1].line) == script.LINE_MAX
    task, messages, kwargs = router.calls[0]
    assert task == "recap" and kwargs["json_schema"] == script.SCRIPT_SCHEMA
    assert "H1 · 09:00 · hội thoại (có tiếng) · việc 0" in messages[1]["content"]


@pytest.mark.parametrize("answer, error", [
    (None, ProviderUnavailable("down")),
    ("not json", None),
    (json.dumps({"title": " ", "highlights": [{"ref": "H99", "line": "x", "audio": True}]}), None),
])
async def test_a_useless_answer_gives_the_rule_script(pki_settings, answer, error):
    written = await script.write_script(FakeRouter(answer, error), _material(), pki_settings.zone)
    assert written.model_version == script.RULE and written.title == "Ngày 14/10"
    assert len(written.highlights) == script.HIGHLIGHTS_RULE
    assert sum(h.audio for h in written.highlights) == script.AUDIO_RULE
    assert [h.candidate.at for h in written.highlights] == sorted(h.candidate.at for h in written.highlights)


async def test_a_blank_title_from_the_model_becomes_the_default(pki_settings):
    answer = json.dumps({"title": "  ", "highlights": [{"ref": "H2", "line": "a", "audio": False}]})
    written = await script.write_script(FakeRouter(answer), _material(), pki_settings.zone)
    assert written.title == "Ngày 14/10" and written.model_version == "ollama:qwen3:8b"


def test_the_rule_script_takes_the_best_candidates():
    written = script.rule_script(_material())
    assert {h.candidate.ref for h in written.highlights} == {f"H{k}" for k in range(5, 13)}  # scores 1.4 … 2.1


def test_the_stored_script_is_json_with_utc_times():
    written = script.rule_script(_material(n=2))
    stored = script.to_json(written)
    json.dumps(stored)  # serialisable
    first = stored["highlights"][0]
    assert first["at"] == "2026-10-14T00:00:00+00:00" and first["jpeg"] == "keyframes/K0.jpg"
    assert first["speech"] == "việc 0" and first["audio_item"] == "A0" and first["audio_offset"] == 5.0
    assert stored["highlights"][1]["speech"] is None  # only conversations carry their sentence


def test_no_candidate_means_a_title_and_no_highlight():
    empty = Material(DAY, [], [], [])
    written = script.rule_script(empty)
    assert (written.title, written.highlights) == ("Ngày 14/10", [])
