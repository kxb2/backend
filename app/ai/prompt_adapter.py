# TODO: Claude API 호출 구현 (영문 고정, Camera->...->Style 순서 구조)
from app.ai.base import PromptAdapter


class ClaudePromptAdapter(PromptAdapter):
    def generate_prompt(self, *args, **kwargs) -> str:
        raise NotImplementedError