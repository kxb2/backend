import pytest

from app.ai.exceptions import (
    AIAdapterRequestError,
    AIAdapterTimeoutError,
    AIAdapterUnavailableError,
)
from app.ai.retry import call_with_retry


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """테스트가 실제로 대기하지 않도록 sleep/jitter를 제거."""
    sleeps: list[float] = []
    monkeypatch.setattr("app.ai.retry.time.sleep", lambda seconds: sleeps.append(seconds))
    monkeypatch.setattr("app.ai.retry.random.uniform", lambda a, b: 0)
    return sleeps


def test_succeeds_without_retry_on_first_try():
    """첫 시도 성공 시 재시도 안 함"""
    calls = []

    def func():
        calls.append(1)
        return "ok"

    assert call_with_retry(func) == "ok"
    assert len(calls) == 1


def test_retries_on_timeout_then_succeeds():
    """타임아웃 나면 재시도해서 결국 성공하는지"""
    calls = []

    def func():
        calls.append(1)
        if len(calls) < 3:
            raise AIAdapterTimeoutError("timeout")
        return "ok"

    assert call_with_retry(func, max_retries=2) == "ok"
    assert len(calls) == 3


def test_retries_on_unavailable_then_succeeds():
    """일시적 에러(Unavailable)도 재시도해서 성공하는지"""
    calls = []

    def func():
        calls.append(1)
        if len(calls) < 2:
            raise AIAdapterUnavailableError("503")
        return "ok"

    assert call_with_retry(func, max_retries=2) == "ok"
    assert len(calls) == 2


def test_gives_up_after_max_retries_and_raises_last_error():
    """max_retries 다 쓰면 마지막 에러를 그대로 던지는지 (호출 횟수 = 최초 1 + 재시도 2 = 3회)"""
    calls = []

    def func():
        calls.append(1)
        raise AIAdapterUnavailableError(f"fail-{len(calls)}")

    with pytest.raises(AIAdapterUnavailableError, match="fail-3"):
        call_with_retry(func, max_retries=2)

    # 최초 시도 1회 + 재시도 2회 = 총 3회
    assert len(calls) == 3


def test_does_not_retry_non_retryable_request_error():
    """AIAdapterRequestError는 재시도 없이 즉시 전파되는지"""
    calls = []

    def func():
        calls.append(1)
        raise AIAdapterRequestError("bad request")

    with pytest.raises(AIAdapterRequestError):
        call_with_retry(func, max_retries=2)

    assert len(calls) == 1


def test_does_not_retry_unexpected_exception():
    """우리 타입이 아닌 일반 예외(ValueError)도 재시도 없이 즉시 전파되는지"""
    calls = []

    def func():
        calls.append(1)
        raise ValueError("bug")

    with pytest.raises(ValueError):
        call_with_retry(func, max_retries=2)

    assert len(calls) == 1


def test_delay_grows_exponentially(_no_sleep):
    """백오프 간격이 1.0 → 2.0 → 4.0처럼 지수적으로 늘어나는지"""
    calls = []

    def func():
        calls.append(1)
        raise AIAdapterTimeoutError("timeout")

    with pytest.raises(AIAdapterTimeoutError):
        call_with_retry(func, max_retries=3, base_delay=1.0, max_delay=100.0)

    assert _no_sleep == [1.0, 2.0, 4.0]
