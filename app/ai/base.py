from abc import ABC, abstractmethod


class PromptAdapter(ABC):
    @abstractmethod
    def generate_prompt(self, *args, **kwargs) -> str: ...


class ImageAdapter(ABC):
    @abstractmethod
    def generate_image(self, *args, **kwargs) -> str: ...