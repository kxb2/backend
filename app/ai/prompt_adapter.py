"""Claude API 호출

— 시나리오+장르+고급설정(+레퍼런스 이미지)로 9컷 통합 영문 프롬프트 생성."""

import anthropic

from app.ai.base import PromptAdapter
from app.ai.exceptions import (
    RETRYABLE_STATUS_CODES,
    AIAdapterError,
    AIAdapterRequestError,
    AIAdapterTimeoutError,
    AIAdapterUnavailableError,
)
from app.ai.retry import call_with_retry
from app.core.config import get_settings
from app.core.enums import Genre

MAX_TOKENS = 1500

# PRD 문서에서 뽑아낸 규칙들 영문으로 넣어놓음
SYSTEM_PROMPT = """You are a cinematography prompt writer for an AI storyboard tool.

Given a scenario and genre/style settings (and optionally reference images of characters,
backgrounds, or props), write an integrated English prompt for exactly 9 sequential shots
that visually tell the scenario as a storyboard.

Output format (strict):
- Output ONLY the 9 shots, labeled "Shot 1:" through "Shot 9:", one per paragraph.
- No preamble, no explanation, no markdown headers or bullet points — just the 9 labeled shots.
- English only, regardless of the input language.

Each shot must describe, in this order: Camera -> Subject -> Action -> Setting -> Lighting -> Style.
- Camera: an explicit angle name + camera position + the resulting visual effect
  (e.g. "low-angle, camera positioned near the ground looking upward, subject appears imposing").
  Never leave the angle vague or implicit.
- Action: exactly one present-tense verb, one single action.
- Do not use abstract mood words (e.g. "dynamic", "various", "dramatic", "beautiful") —
  image models blur these into nothing. Describe concrete, visible details instead.
- Target 50-70 words per shot; all 9 shots combined must stay under 3000 characters.
- If reference images are provided, keep character appearance and background details
  consistent across all 9 shots based on what is shown in those images.
- Reflect the given genre/style/tone/era through concrete visual language (not by naming
  the setting fields directly), consistently across all 9 shots.
"""


def _build_user_content(
    *,
    scenario_text: str,
    genre: Genre,
    style: str | None,
    tone: str | None,
    aspect_ratio: str | None,
    era: str | None,
    reference_image_urls: list[str],
) -> list[anthropic.types.TextBlockParam | anthropic.types.ImageBlockParam]:
    """Claude한테 보낼 사용자 메시지 내용"""
    settings_lines = [f"Genre: {genre}"]
    if style:
        settings_lines.append(f"Style: {style}")
    if tone:
        settings_lines.append(f"Tone: {tone}")
    if era:
        settings_lines.append(f"Era: {era}")
    if aspect_ratio:
        settings_lines.append(f"Aspect ratio: {aspect_ratio}")

    text = f"Scenario:\n{scenario_text}\n\nSettings:\n" + "\n".join(settings_lines)

    content: list[anthropic.types.TextBlockParam | anthropic.types.ImageBlockParam] = [
        {"type": "text", "text": text}
    ]
    for url in reference_image_urls:
        content.append({"type": "image", "source": {"type": "url", "url": url}})
    return content


def _map_error(exc: anthropic.AnthropicError) -> AIAdapterError:
    """anthropic SDK가 던지는 에러를 exceptions.py 형식으로 바꾸기"""
    if isinstance(exc, anthropic.APITimeoutError):
        return AIAdapterTimeoutError(str(exc))
    if isinstance(exc, anthropic.APIConnectionError):
        return AIAdapterUnavailableError(str(exc))
    if isinstance(exc, anthropic.APIStatusError):
        if exc.status_code in RETRYABLE_STATUS_CODES:
            return AIAdapterUnavailableError(str(exc))
        return AIAdapterRequestError(str(exc))
    return AIAdapterError(str(exc))


class ClaudePromptAdapter(PromptAdapter):
    def __init__(self, client: anthropic.Anthropic | None = None, model: str | None = None) -> None:
        """테스트 때문에 client나 model이 없는 경우를 넣어놓음"""
        if client is None or model is None:
            settings = get_settings()
            client = client or anthropic.Anthropic(api_key=settings.anthropic_api_key)
            model = model or settings.anthropic_model
        self._client = client
        self._model = model

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
    ) -> str:
        content = _build_user_content(
            scenario_text=scenario_text,
            genre=genre,
            style=style,
            tone=tone,
            aspect_ratio=aspect_ratio,
            era=era,
            reference_image_urls=reference_image_urls or [],
        )

        def _call() -> str:
            """API 호출 시도"""
            try:
                message = self._client.messages.create(
                    model=self._model,
                    max_tokens=MAX_TOKENS,
                    system=SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": content}],
                )
            except anthropic.AnthropicError as exc:
                raise _map_error(exc) from exc

            if message.stop_reason == "max_tokens":
                raise AIAdapterError(
                    f"Claude 응답이 max_tokens({MAX_TOKENS})에서 잘렸습니다 — "
                    "프롬프트 길이 또는 MAX_TOKENS 조정이 필요합니다."
                )

            return "".join(block.text for block in message.content if block.type == "text")
            # message.content 순회하면서 type이 text인 블록들의 텍스트만 이어붙여서 최종 문자열로 반환

        return call_with_retry(_call, label="claude_prompt_adapter")
        # call_with_retry 함수는 API 부를때마다 쓰이지만 반복(재시도) 동작은 문제 생겼을때만
