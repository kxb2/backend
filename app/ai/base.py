from abc import ABC, abstractmethod

from app.core.enums import Genre


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