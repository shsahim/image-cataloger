"""API engine behaviour with a mocked Anthropic client."""
from types import SimpleNamespace

import anthropic
import httpx2 as httpx
import pytest

from image_cataloger.engines.api_engine import ApiEngine, FatalEngineError
from image_cataloger.engines.base import RunControl


def _response(text, stop_reason="end_turn", in_tok=1000, out_tok=50):
    return SimpleNamespace(
        content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)],
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=in_tok, output_tokens=out_tok),
    )


class FakeMessages:
    def __init__(self, script):
        self.script = list(script)  # callables or responses, consumed in order
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        if callable(item):
            return item(kwargs)
        return item


def make_engine(script, **kw):
    fake = SimpleNamespace(messages=FakeMessages(script))
    eng = ApiEngine(api_key="x", model="claude-haiku-5-5", client=fake, **kw)
    return eng, fake.messages


def _status_error(status, cls=anthropic.APIStatusError, headers=None):
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    resp = httpx.Response(status, request=req, headers=headers or {})
    return cls(message=f"status {status}", response=resp, body=None)


@pytest.fixture
def three(make_image):
    return [make_image(f"{i}.jpg", size=(100, 80)) for i in range(3)]


def test_batch_happy_path(three):
    eng, msgs = make_engine([_response('["one", "two", "three"]', in_tok=3000, out_tok=30)])
    res = eng.describe(three, RunControl())
    assert [r.summary for r in res] == ["one", "two", "three"]
    assert all(r.status == "ok" for r in res)
    assert sum(r.input_tokens for r in res) == 3000 and sum(r.output_tokens for r in res) == 30
    assert len(msgs.calls) == 1
    call = msgs.calls[0]
    assert call["model"] == "claude-haiku-5-5"
    assert call["output_config"] == {"effort": "low"}
    assert "temperature" not in call
    images = [b for b in call["messages"][0]["content"] if b["type"] == "image"]
    assert len(images) == 3 and images[0]["source"]["media_type"] == "image/jpeg"


def test_count_mismatch_reasks_individually(three):
    eng, msgs = make_engine([
        _response('["one", "two"]'),   # wrong count for 3 images
        _response('["A"]'), _response("B"), _response('["C"]'),
    ])
    res = eng.describe(three, RunControl())
    assert [r.summary for r in res] == ["A", "B", "C"]
    assert len(msgs.calls) == 4
    assert all(len([b for b in c["messages"][0]["content"] if b["type"] == "image"]) == 1
               for c in msgs.calls[1:])


def test_unparseable_single_is_error(three):
    eng, _ = make_engine([_response("nonsense\nmore nonsense")])
    res = eng.describe(three[:1], RunControl())
    assert res[0].status == "error" and "unparseable" in res[0].error


def test_refusal_is_error_not_crash(three):
    eng, _ = make_engine([_response("", stop_reason="refusal")])
    res = eng.describe(three[:1], RunControl())
    assert res[0].status == "error" and "refused" in res[0].error


def test_unreadable_file_skipped_but_others_described(three, tmp_path):
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"nope")
    eng, msgs = make_engine([_response('["x", "y", "z"]')])
    res = eng.describe([three[0], str(bad), three[1], three[2]], RunControl())
    assert res[1].status == "unreadable"
    assert [r.summary for r in res if r.status == "ok"] == ["x", "y", "z"]
    assert len([b for b in msgs.calls[0]["messages"][0]["content"] if b["type"] == "image"]) == 3


def test_backoff_on_429_then_success(three, monkeypatch):
    sleeps = []
    monkeypatch.setattr(RunControl, "sleep", lambda self, s: sleeps.append(s) or True)
    eng, msgs = make_engine([
        _status_error(429, anthropic.RateLimitError, headers={"retry-after": "3"}),
        _status_error(529),
        _response('["a", "b", "c"]'),
    ])
    res = eng.describe(three, RunControl())
    assert [r.summary for r in res] == ["a", "b", "c"]
    assert len(msgs.calls) == 3
    assert len(sleeps) == 2 and 3 <= sleeps[0] <= 4.5


def test_retries_exhausted_marks_error(three, monkeypatch):
    monkeypatch.setattr(RunControl, "sleep", lambda self, s: True)
    eng, msgs = make_engine([_status_error(529)] * 3, max_attempts=3)
    res = eng.describe(three, RunControl())
    assert all(r.status == "error" for r in res)
    assert len(msgs.calls) == 3


def test_400_is_not_retried(three, monkeypatch):
    monkeypatch.setattr(RunControl, "sleep", lambda self, s: True)
    eng, msgs = make_engine([_status_error(400, anthropic.BadRequestError)])
    res = eng.describe(three, RunControl())
    assert all(r.status == "error" for r in res) and len(msgs.calls) == 1


def test_auth_error_is_fatal(three):
    eng, _ = make_engine([_status_error(401, anthropic.AuthenticationError)])
    with pytest.raises(FatalEngineError):
        eng.describe(three, RunControl())


def test_request_too_large_splits_batch(three):
    eng, msgs = make_engine([
        _status_error(413, anthropic.RequestTooLargeError),
        _response('["a"]'), _response('["b"]'), _response('["c"]'),
    ])
    res = eng.describe(three, RunControl())
    assert [r.summary for r in res] == ["a", "b", "c"] and len(msgs.calls) == 4


def test_stop_leaves_files_untouched(three):
    ctl = RunControl()
    ctl.stop()
    eng, msgs = make_engine([])
    res = eng.describe(three, ctl)
    assert all(r.status == "error" and r.error == "stopped" for r in res)
    assert msgs.calls == []
