from abc import ABC, abstractmethod

from app.core.enums import Genre

"""프롬프트/이미지 AI어댑터 추상 클래스(claude, gpt, gemini가 상속받아서 사용)"""


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


class ImageAdapter(ABC):
    @abstractmethod
    def generate_image(self, *args, **kwargs) -> str: ...
