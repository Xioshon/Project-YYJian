from __future__ import annotations

import core_tools


class FakeResponse:
    def __init__(self, status_code: int, text: str, content_type: str = "text/html"):
        self.status_code = status_code
        self.text = text
        self.ok = 200 <= status_code < 300
        self.headers = {"content-type": content_type}

    def raise_for_status(self) -> None:
        if not self.ok:
            raise RuntimeError(f"HTTP {self.status_code}")


def test_read_webpage_falls_back_to_direct_html_when_reader_is_blocked(monkeypatch) -> None:
    responses = iter(
        [
            FakeResponse(401, '{"message":"blocked from performing anonymous queries due to bad network reputation"}', "application/json"),
            FakeResponse(
                200,
                "<html><head><title>Example Domain</title><style>hidden</style></head>"
                "<body><h1>Example Domain</h1><script>ignore()</script><p>Useful page text.</p></body></html>",
            ),
        ]
    )
    monkeypatch.setattr(core_tools.requests, "get", lambda *args, **kwargs: next(responses))

    result = core_tools.real_read_webpage("https://example.com")

    assert result.status == "ok"
    assert "Example Domain" in result.data
    assert "Useful page text" in result.data
    assert "ignore()" not in result.data
    assert "hidden" not in result.data


def test_read_webpage_direct_fallback_preserves_plain_text(monkeypatch) -> None:
    responses = iter(
        [
            FakeResponse(503, "unavailable", "text/plain"),
            FakeResponse(200, "plain response body", "text/plain"),
        ]
    )
    monkeypatch.setattr(core_tools.requests, "get", lambda *args, **kwargs: next(responses))

    result = core_tools.real_read_webpage("https://example.com/data.txt")

    assert result.status == "ok"
    assert result.data == "plain response body"
