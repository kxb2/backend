# TODO: GPT image / Gemini 3.5 Flash Image 호출 구현
from app.ai.base import ImageAdapter


class GptImageAdapter(ImageAdapter):
    def generate_image(self, *args, **kwargs) -> str:
        raise NotImplementedError


class GeminiImageAdapter(ImageAdapter):
    def generate_image(self, *args, **kwargs) -> str:
        raise NotImplementedError


def get_image_adapter(image_model: str) -> ImageAdapter:
    # TODO: storyboards.image_model 값("gpt_image" / "gemini_3_5_flash_image")에 따라 분기
    raise NotImplementedError