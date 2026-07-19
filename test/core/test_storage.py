import httpx
import pytest
from botocore.exceptions import ClientError
from fastapi import HTTPException

from app.core import storage

"""R2 관련 download_bytes의 재시도 동작, upload_bytes 에러 메시지 테스트"""

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


class TestUploadBytes:
    def test_includes_underlying_error_detail_on_failure(self, monkeypatch):
        """R2 업로드 실패 시 HTTPException detail에 고정 문구뿐 아니라 원본 에러 내용까지 포함되는지
        (error_message로 저장됐을 때 실제 원인을 알 수 있어야 함)"""
        error = ClientError({"Error": {"Code": "AccessDenied", "Message": "denied"}}, "PutObject")

        class _FakeClient:
            def put_object(self, **kwargs):
                raise error

        monkeypatch.setattr(storage, "_get_client", lambda: _FakeClient())

        with pytest.raises(HTTPException) as exc_info:
            storage.upload_bytes(b"data", key="test.png", content_type="image/png")

        assert "AccessDenied" in exc_info.value.detail

    def test_sets_content_disposition_when_filename_given(self, monkeypatch):
        """filename을 주면(PDF/zip 등 다운로드 전용 파일) Content-Disposition: attachment로 업로드되는지"""
        calls = []

        class _FakeClient:
            def put_object(self, **kwargs):
                calls.append(kwargs)

        monkeypatch.setattr(storage, "_get_client", lambda: _FakeClient())

        storage.upload_bytes(b"data", key="export.pdf", content_type="application/pdf", filename="storyboard_1.pdf")

        assert calls[0]["ContentDisposition"] == 'attachment; filename="storyboard_1.pdf"'

    def test_no_content_disposition_when_filename_omitted(self, monkeypatch):
        """filename 없으면(그리드/컷 이미지처럼 화면에 바로 렌더링해야 하는 파일) Content-Disposition을 안 붙이는지"""
        calls = []

        class _FakeClient:
            def put_object(self, **kwargs):
                calls.append(kwargs)

        monkeypatch.setattr(storage, "_get_client", lambda: _FakeClient())

        storage.upload_bytes(b"data", key="cuts/1.png", content_type="image/png")

        assert "ContentDisposition" not in calls[0]
