import httpx
import openai
import pytest
from google.genai import errors as genai_errors
from google.genai import types as genai_types

from app.ai.exceptions import AIAdapterError, AIAdapterRequestError, AIAdapterUnavailableError
from app.ai.image_adapter import (
    GeminiImageAdapter,
    GptImageAdapter,
    _gpt_size_for_aspect_ratio,
    get_image_adapter,
)
from app.core.enums import ImageModel


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr("app.ai.retry.time.sleep", lambda seconds: None)
    monkeypatch.setattr("app.ai.retry.random.uniform", lambda a, b: 0)


@pytest.fixture(autouse=True)
def _fake_r2_upload(monkeypatch):
    uploads = []

    def _fake_upload(data, content_type, folder):
        uploads.append((data, content_type, folder))
        return f"https://pub-x.r2.dev/{folder}/fake.png"

    monkeypatch.setattr("app.ai.image_adapter.storage.upload_image_bytes", _fake_upload)
    return uploads


class TestGptSizeForAspectRatio:
    def test_landscape(self):
        """가로가 더 크면 "1536x1024" """
        assert _gpt_size_for_aspect_ratio("16:9") == "1536x1024"

    def test_portrait(self):
        """세로가 더 크면 "1024x1536" """
        assert _gpt_size_for_aspect_ratio("9:16") == "1024x1536"

    def test_square(self):
        """1:1이면 "1024x1024" """
        assert _gpt_size_for_aspect_ratio("1:1") == "1024x1024"

    def test_none_defaults_to_landscape(self):
        """화면비가 없으면 "auto" 대신 고정 가로형 — 그리드 타일 크기 일관성 위해"""
        assert _gpt_size_for_aspect_ratio(None) == "1536x1024"

    def test_unparseable_defaults_to_landscape(self):
        """이상한 형식 문자열이면 고정 가로형"""
        assert _gpt_size_for_aspect_ratio("bogus") == "1536x1024"


class _FakeOpenAIImage:
    def __init__(self, b64_json):
        self.b64_json = b64_json


class _FakeOpenAIResponse:
    def __init__(self, b64_json):
        self.data = [_FakeOpenAIImage(b64_json)]


class _FakeOpenAIImages:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def generate(self, **kwargs):
        self.calls.append(kwargs)
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class _FakeOpenAIClient:
    def __init__(self, responses):
        self.images = _FakeOpenAIImages(responses)


def _gpt_adapter(responses):
    client = _FakeOpenAIClient(responses)
    return GptImageAdapter(client=client, model="gpt-image-1"), client


def _openai_status_error(cls, status_code):
    request = httpx.Request("POST", "https://api.openai.com/v1/images/generations")
    response = httpx.Response(status_code=status_code, request=request)
    return cls(f"status {status_code}", response=response, body=None)


# b64: 원본 바이너리(예: 이미지 바이트) → 텍스트로 바꾸는 인코딩 방식
class TestGptImageAdapter:
    def test_uploads_decoded_image_and_returns_url(self, _fake_r2_upload):
        """b64 응답을 디코딩해서 R2에 업로드하고 URL을 반환하는지, size 파라미터도 맞게 넘어가는지"""
        b64 = "aGVsbG8="  # "hello" 를 base64 인코딩
        adapter, client = _gpt_adapter([_FakeOpenAIResponse(b64)])

        url = adapter.generate_image(prompt_text="a cat", aspect_ratio="1:1")

        assert url == "https://pub-x.r2.dev/cuts/fake.png"
        assert _fake_r2_upload[0] == (b"hello", "image/png", "cuts")
        assert client.images.calls[0]["size"] == "1024x1024"

    def test_timeout_is_retried_then_succeeds(self):
        """타임아웃 나면 재시도해서 성공하는지 (호출 2회)"""
        request = httpx.Request("POST", "https://api.openai.com/v1/images/generations")
        adapter, client = _gpt_adapter([openai.APITimeoutError(request=request), _FakeOpenAIResponse("aGk=")])

        adapter.generate_image(prompt_text="a cat")

        assert len(client.images.calls) == 2

    def test_bad_request_is_not_retried(self):
        """400(잘못된 요청)은 재시도 안 하는지 (호출 1회)"""
        adapter, client = _gpt_adapter([_openai_status_error(openai.BadRequestError, 400)])

        with pytest.raises(AIAdapterRequestError):
            adapter.generate_image(prompt_text="a cat")

        assert len(client.images.calls) == 1

    def test_rate_limit_is_retryable(self):
        """429(너무 많은 요청)는 재시도 대상인지 (호출 2회)"""
        adapter, client = _gpt_adapter([_openai_status_error(openai.RateLimitError, 429), _FakeOpenAIResponse("aGk=")])

        adapter.generate_image(prompt_text="a cat")

        assert len(client.images.calls) == 2

    def test_missing_image_data_raises(self):
        """응답에 이미지 데이터(b64_json)가 없으면 에러 나는지"""
        adapter, _ = _gpt_adapter([_FakeOpenAIResponse(None)])

        with pytest.raises(AIAdapterError):
            adapter.generate_image(prompt_text="a cat")


class _FakeBlob:
    def __init__(self, data, mime_type="image/png"):
        self.data = data
        self.mime_type = mime_type


class _FakePart:
    def __init__(self, inline_data=None):
        self.inline_data = inline_data


class _FakeContent:
    def __init__(self, parts):
        self.parts = parts


class _FakeCandidate:
    def __init__(self, parts):
        self.content = _FakeContent(parts)


class _FakeGeminiResponse:
    def __init__(self, parts):
        self.candidates = [_FakeCandidate(parts)]


class _FakeGeminiModels:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class _FakeGeminiClient:
    def __init__(self, responses):
        self.models = _FakeGeminiModels(responses)


def _gemini_adapter(responses):
    client = _FakeGeminiClient(responses)
    return GeminiImageAdapter(client=client, model="gemini-3.1-flash-image"), client


def _gemini_status_error(status_code):
    return genai_errors.APIError(status_code, {"message": f"status {status_code}"})


class TestGeminiImageAdapter:
    def test_uploads_first_image_part_and_returns_url(self, _fake_r2_upload):
        """응답의 첫 이미지 part를 업로드하고 URL을 반환하는지, aspect_ratio가 ImageConfig에 반영되는지"""
        response = _FakeGeminiResponse([_FakePart(_FakeBlob(b"image-bytes"))])
        adapter, client = _gemini_adapter([response])

        url = adapter.generate_image(prompt_text="a cat", aspect_ratio="16:9")

        assert url == "https://pub-x.r2.dev/cuts/fake.png"
        assert _fake_r2_upload[0] == (b"image-bytes", "image/png", "cuts")
        assert client.models.calls[0]["config"].image_config == genai_types.ImageConfig(aspect_ratio="16:9")

    def test_none_aspect_ratio_defaults_to_3_2(self, _fake_r2_upload):
        """aspect_ratio 없으면 ImageConfig 자체를 생략(None)하지 않고 3:2 기본값을 명시하는지 —
        9컷이 독립 호출이라 명시 안 하면 컷마다 제멋대로 비율이 나올 수 있음"""
        response = _FakeGeminiResponse([_FakePart(_FakeBlob(b"image-bytes"))])
        adapter, client = _gemini_adapter([response])

        adapter.generate_image(prompt_text="a cat")

        assert client.models.calls[0]["config"].image_config == genai_types.ImageConfig(aspect_ratio="3:2")

    def test_skips_non_image_parts(self, _fake_r2_upload):
        """이미지가 없는 part는 건너뛰고 실제 이미지가 있는 part를 쓰는지"""
        response = _FakeGeminiResponse([_FakePart(None), _FakePart(_FakeBlob(b"real-bytes"))])
        adapter, _ = _gemini_adapter([response])

        adapter.generate_image(prompt_text="a cat")

        assert _fake_r2_upload[0][0] == b"real-bytes"

    def test_no_image_in_response_raises(self):
        """응답에 이미지가 하나도 없으면 에러 나는지"""
        response = _FakeGeminiResponse([_FakePart(None)])
        adapter, _ = _gemini_adapter([response])

        with pytest.raises(AIAdapterError):
            adapter.generate_image(prompt_text="a cat")

    def test_client_error_status_is_not_retried(self):
        """400(잘못된 요청)은 재시도 안 하는지"""
        adapter, client = _gemini_adapter([_gemini_status_error(400)])

        with pytest.raises(AIAdapterRequestError):
            adapter.generate_image(prompt_text="a cat")

        assert len(client.models.calls) == 1

    def test_server_error_status_is_retryable(self, _fake_r2_upload):
        """503(서버 에러, 일시적 오류)은 재시도해서 성공하는지"""
        response = _FakeGeminiResponse([_FakePart(_FakeBlob(b"bytes"))])
        adapter, client = _gemini_adapter([_gemini_status_error(503), response])

        adapter.generate_image(prompt_text="a cat")

        assert len(client.models.calls) == 2

    def test_timeout_is_retryable(self, _fake_r2_upload):
        """타임아웃도 재시도해서 성공하는지"""
        response = _FakeGeminiResponse([_FakePart(_FakeBlob(b"bytes"))])
        adapter, client = _gemini_adapter([httpx.TimeoutException("timed out"), response])

        adapter.generate_image(prompt_text="a cat")

        assert len(client.models.calls) == 2


class _FakeSettings:
    openai_api_key = "test-openai-key"
    openai_image_model = "gpt-image-1"
    gemini_api_key = "test-gemini-key"
    gemini_image_model = "gemini-3.1-flash-image"


class TestGetImageAdapter:
    @pytest.fixture(autouse=True)
    def _fake_settings(self, monkeypatch):
        monkeypatch.setattr("app.ai.image_adapter.get_settings", lambda: _FakeSettings())

    def test_returns_gpt_adapter_for_gpt_image(self):
        """image_model: GPT_IMAGE → GptImageAdapter 반환하는지"""
        assert isinstance(get_image_adapter(ImageModel.GPT_IMAGE), GptImageAdapter)

    def test_returns_gemini_adapter_for_gemini(self):
        """image_model: GEMINI_3_1_FLASH_IMAGE → GeminiImageAdapter 반환하는지"""
        assert isinstance(get_image_adapter(ImageModel.GEMINI_3_1_FLASH_IMAGE), GeminiImageAdapter)
