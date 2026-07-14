"""GPT image / Gemini image API 호출 — 컷별 프롬프트 텍스트로 이미지 1장 생성해서 R2에 업로드."""

import base64
import logging

import openai
from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
import httpx

from app.ai.base import ImageAdapter
from app.ai.exceptions import (
    RETRYABLE_STATUS_CODES,
    AIAdapterError,
    AIAdapterRequestError,
    AIAdapterTimeoutError,
    AIAdapterUnavailableError,
)
from app.ai.retry import call_with_retry
from app.core import storage
from app.core.config import get_settings
from app.core.enums import ImageModel

logger = logging.getLogger(__name__)

IMAGE_FOLDER = "cuts"

# gpt-image-1이 실제로 지원하는 크기(정사각형/가로/세로)만 사용.
# 즉 "16:9", "4:3"처럼 임의의 화면비를 요청해도 실제로는 이 3개 프리셋 중 하나로 근사됨
# (예: "16:9" -> 1536x1024 = 실제로는 3:2). 다운스트림 영상 AI와 정확한 화면비를 맞춰야 한다면
# gpt-image-1을 선택했을 때 프론트 화면비 옵션 자체를 이 3개로 제한하는 것도 고려.
# gemini가 지원하는 비율은 아직 확인 필요
_GPT_LANDSCAPE_SIZE = "1536x1024"
_GPT_PORTRAIT_SIZE = "1024x1536"
_GPT_SQUARE_SIZE = "1024x1024"


def _gpt_size_for_aspect_ratio(aspect_ratio: str | None) -> str:
    """"16:9" 같은 화면비 문자열을 gpt-image-1이 받는 size 문자열로 변환. 못 읽으면 "auto"."""
    if not aspect_ratio or ":" not in aspect_ratio:
        return "auto"
    try:
        width, height = (float(part) for part in aspect_ratio.split(":", 1))
    except ValueError:
        return "auto"

    if width > height:
        size = _GPT_LANDSCAPE_SIZE
    elif width < height:
        size = _GPT_PORTRAIT_SIZE
    else:
        size = _GPT_SQUARE_SIZE

    logger.info("requested %s, approximated to %s", aspect_ratio, size)
    return size


def _map_openai_error(exc: openai.OpenAIError) -> AIAdapterError:
    if isinstance(exc, openai.APITimeoutError):
        return AIAdapterTimeoutError(str(exc))
    if isinstance(exc, openai.APIConnectionError):
        return AIAdapterUnavailableError(str(exc))
    if isinstance(exc, openai.APIStatusError):
        if exc.status_code in RETRYABLE_STATUS_CODES:
            return AIAdapterUnavailableError(str(exc))
        return AIAdapterRequestError(str(exc))
    return AIAdapterError(str(exc))


def _map_gemini_error(exc: Exception) -> AIAdapterError:
    if isinstance(exc, httpx.TimeoutException):
        return AIAdapterTimeoutError(str(exc))
    if isinstance(exc, httpx.TransportError):
        return AIAdapterUnavailableError(str(exc))
    if isinstance(exc, genai_errors.APIError):
        if exc.code in RETRYABLE_STATUS_CODES:
            return AIAdapterUnavailableError(str(exc))
        return AIAdapterRequestError(str(exc))
    return AIAdapterError(str(exc))


class GptImageAdapter(ImageAdapter):
    def __init__(self, client: openai.OpenAI | None = None, model: str | None = None) -> None:
        if client is None or model is None:
            settings = get_settings()
            client = client or openai.OpenAI(api_key=settings.openai_api_key)
            model = model or settings.openai_image_model
        self._client = client
        self._model = model

    def generate_image(self, *, prompt_text: str, aspect_ratio: str | None = None) -> str:
        def _call() -> str:
            try:
                response = self._client.images.generate(
                    model=self._model,
                    prompt=prompt_text,
                    size=_gpt_size_for_aspect_ratio(aspect_ratio),
                    n=1,
                    output_format="png",
                )
            except openai.OpenAIError as exc:
                raise _map_openai_error(exc) from exc

            b64_data = response.data[0].b64_json
            if not b64_data:
                raise AIAdapterError("GPT image 응답에 이미지 데이터(b64_json)가 없습니다.")

            image_bytes = base64.b64decode(b64_data)
            return storage.upload_image_bytes(image_bytes, content_type="image/png", folder=IMAGE_FOLDER)

        return call_with_retry(_call, label="gpt_image_adapter")


class GeminiImageAdapter(ImageAdapter):
    def __init__(self, client: genai.Client | None = None, model: str | None = None) -> None:
        if client is None or model is None:
            settings = get_settings()
            client = client or genai.Client(api_key=settings.gemini_api_key)
            model = model or settings.gemini_image_model
        self._client = client
        self._model = model

    def generate_image(self, *, prompt_text: str, aspect_ratio: str | None = None) -> str:
        image_config = genai_types.ImageConfig(aspect_ratio=aspect_ratio) if aspect_ratio else None
        config = genai_types.GenerateContentConfig(
            response_modalities=["IMAGE"],
            image_config=image_config,
        )

        def _call() -> str:
            try:
                response = self._client.models.generate_content(
                    model=self._model,
                    contents=prompt_text,
                    config=config,
                )
            except (genai_errors.APIError, httpx.TimeoutException, httpx.TransportError) as exc:
                raise _map_gemini_error(exc) from exc

            candidates = response.candidates or []
            content = candidates[0].content if candidates else None
            parts = content.parts if content and content.parts else []
            for part in parts:
                if part.inline_data and part.inline_data.data:
                    return storage.upload_image_bytes(
                        part.inline_data.data,
                        content_type=part.inline_data.mime_type or "image/png",
                        folder=IMAGE_FOLDER,
                    )

            raise AIAdapterError("Gemini image 응답에 이미지 데이터가 없습니다.")

        return call_with_retry(_call, label="gemini_image_adapter")


def get_image_adapter(image_model: ImageModel) -> ImageAdapter:
    """storyboards.image_model 값에 따라 GPT/Gemini 이미지 어댑터로 분기."""
    if image_model == ImageModel.GPT_IMAGE:
        return GptImageAdapter()
    if image_model == ImageModel.GEMINI_3_5_FLASH_IMAGE:
        return GeminiImageAdapter()
    raise ValueError(f"지원하지 않는 이미지 모델입니다: {image_model}")
