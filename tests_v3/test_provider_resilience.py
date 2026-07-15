from __future__ import annotations

from types import SimpleNamespace

import pytest

from yueyue_v3 import providers


def _client_that_raises(error: Exception, calls: list[int]):
    def create(**_kwargs):
        calls.append(1)
        raise error

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def test_balance_error_is_classified_non_retryable() -> None:
    failure = providers.classify_provider_error(
        RuntimeError("Error code: 403 - {'code': 30001, 'message': 'Sorry, your account balance is insufficient'}")
    )

    assert failure.category == "insufficient_balance"
    assert failure.retryable is False


def test_balance_error_is_attempted_only_once() -> None:
    calls: list[int] = []
    provider = providers.SiliconFlowProvider("test-key", max_retries=3)
    provider._client = _client_that_raises(
        RuntimeError("Error code: 403 - {'code': 30001, 'message': 'account balance is insufficient'}"),
        calls,
    )
    try:
        with pytest.raises(providers.ProviderFailure) as raised:
            provider.chat([{"role": "user", "content": "hello"}])
    finally:
        provider.close()

    assert raised.value.category == "insufficient_balance"
    assert len(calls) == 1


def test_success_after_old_balance_failure_reports_current_success() -> None:
    events: list[dict] = []
    provider = providers.SiliconFlowProvider("test-key", max_retries=2)
    provider.set_event_sink(lambda event: events.append(event))
    calls: list[int] = []
    provider._client = _client_that_raises(
        RuntimeError("Error code: 403 - {'code': 30001, 'message': 'account balance is insufficient'}"),
        calls,
    )
    with pytest.raises(providers.ProviderFailure):
        provider.chat([{"role": "user", "content": "first"}])

    message = SimpleNamespace(content="ok", reasoning_content="", tool_calls=[])
    provider._client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(
                create=lambda **_kwargs: SimpleNamespace(choices=[SimpleNamespace(message=message)])
            )
        )
    )
    try:
        response = provider.chat([{"role": "user", "content": "second"}])
    finally:
        provider.close()

    assert response.content == "ok"
    assert [event["status"] for event in events] == ["error", "ok"]
    assert events[-1]["model"] == provider.model
