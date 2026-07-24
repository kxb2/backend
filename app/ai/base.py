from abc import ABC, abstractmethod
from dataclasses import dataclass

from app.core.enums import Genre

"""프롬프트/이미지 AI어댑터 추상 클래스(claude, gpt, gemini가 상속받아서 사용)"""
# 규칙만 정의하고 실제 동작은 X


class PromptAdapter(ABC):
    @abstractmethod
    def generate_prompt(
        self,
        *,
        scenario_text: str,
        genre: Genre,
        style: str | None = None,
        tone: str | None = None,
        aspect_ratio: str | None = None,
        era: str | None = None,
        reference_image_urls: list[str] | None = None,
    ) -> str: ...


@dataclass(frozen=True)
class GeneratedImage:
    """이미지 어댑터 호출 결과 — R2 URL과 함께 바이트도 들고 있어서
    그리드 합성 때 R2에서 재다운로드하지 않고 그대로 재사용 가능."""

    url: str
    data: bytes
    content_type: str


class ImageAdapter(ABC):
    @abstractmethod
    def generate_image(
        self,
        *,
        prompt_text: str,
        aspect_ratio: str | None = None,
        reference_images: list[tuple[bytes, str]] | None = None,
    ) -> GeneratedImage: ...
