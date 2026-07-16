import httpx
import pytest

from app.core import storage

"""R2 관련 download_bytes의 재시도 동작 테스트"""

@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """테스트가 실제로 대기하지 않도록 sleep 제거."""
    sleeps: list[float] = []
    monkeypatch.setattr("app.core.storage.time.sleep", lambda seconds: sleeps.append(seconds))
    return sleeps


def _response(status_code: int, content: bytes = b"") -> httpx.Response:
    request = httpx.Request("GET", "https://pub-x.r2.dev/test.png")
    return httpx.Response(status_code, content=content, request=request)


class TestDownloadBytes:
    def test_succeeds_without_retry_on_first_try(self, monkeypatch):
        calls = []

        def fake_get(url, timeout):
            calls.append(url)
            return _response(200, content=b"ok-bytes")

        monkeypatch.setattr("app.core.storage.httpx.get", fake_get)

        assert storage.download_bytes("https://pub-x.r2.dev/test.png") == b"ok-bytes"
        assert len(calls) == 1

    def test_retries_on_timeout_then_succeeds(self, monkeypatch):
        """타임아웃은 재시도 대상. 성공하는지"""
        calls = []

        def fake_get(url, timeout):
            calls.append(1)
            if len(calls) < 3:
                raise httpx.TimeoutException("timeout")
            return _response(200, content=b"ok-bytes")

        monkeypatch.setattr("app.core.storage.httpx.get", fake_get)

        assert storage.download_bytes("https://pub-x.r2.dev/test.png") == b"ok-bytes"
        assert len(calls) == 3  # 최초 1회 + 재시도 2회(MAX_DOWNLOAD_RETRIES)

    def test_retries_on_retryable_status_then_succeeds(self, monkeypatch):
        """503(일시 과부하)은 재시도 대상. 성공하는지"""
        calls = []

        def fake_get(url, timeout):
            calls.append(1)
            if len(calls) < 2:
                return _response(503)
            return _response(200, content=b"ok-bytes")

        monkeypatch.setattr("app.core.storage.httpx.get", fake_get)

        assert storage.download_bytes("https://pub-x.r2.dev/test.png") == b"ok-bytes"
        assert len(calls) == 2

    def test_does_not_retry_non_retryable_status(self, monkeypatch):
        """404(요청 url 서버에 X)는 재시도해도 성공 X → 즉시 예외 전파"""
        calls = []

        def fake_get(url, timeout):
            calls.append(1)
            return _response(404)

        monkeypatch.setattr("app.core.storage.httpx.get", fake_get)

        with pytest.raises(httpx.HTTPStatusError):
            storage.download_bytes("https://pub-x.r2.dev/test.png")

        assert len(calls) == 1

    def test_gives_up_after_max_retries_and_raises(self, monkeypatch):
        """계속 503(과부하, 보수)이면 MAX_DOWNLOAD_RETRIES까지만 재시도하고 마지막 에러 던지는지"""
        calls = []

        def fake_get(url, timeout):
            calls.append(1)
            return _response(503)

        monkeypatch.setattr("app.core.storage.httpx.get", fake_get)

        with pytest.raises(httpx.HTTPStatusError):
            storage.download_bytes("https://pub-x.r2.dev/test.png")

        assert len(calls) == 1 + storage.MAX_DOWNLOAD_RETRIES
