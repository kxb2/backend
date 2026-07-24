"""Claude API 호출

— 시나리오+장르+고급설정(+레퍼런스 이미지)로 9컷 통합 영문 프롬프트 생성."""

import logging

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

logger = logging.getLogger(__name__)

# 테스트에서 응답이 중간에 잘려서(stop_reason=max_tokens) 재시도 낭비된 사례 있어서 상향
# 4인+4공간 테스트후 최대토큰값 늘림
MAX_TOKENS = 4096

# 장르별 프리셋: 기본 톤/스타일/앵글 (일단 PRD 내용)
# ㅡ tone/style은 사용자가 고급설정에서 직접 지정하면 그게 우선/ 안 넣으면 기본값 사용
# ㅡ 드라마류는 순서까지 주면 잘 따라하는데,
#   액션/스릴러는 줘봤자 순서 무시해서 골라쓰라고 범위만 줌
GENRE_PRESETS: dict[Genre, dict[str, object]] = {
    Genre.DRAMA: {
        "tone": "warm, 차분",
        "style": "실사, 내추럴",
        "angle_mode": "sequence",
        "angles": [
            "wide shot", "medium shot", "close-up", "close-up", "two-shot",
            "medium shot", "close-up", "wide shot", "medium shot",
        ],
    },
    Genre.ACTION: {
        "tone": "cool, 긴장, 강렬",
        "style": "실사, 시네마틱",
        "angle_mode": "pool",
        "angles": [
            "wide shot", "low-angle", "high-angle", "tracking shot",
            "side-angle tracking shot", "close-up", "medium shot", "over-the-shoulder shot",
        ],
    },
    Genre.ROMANCE: {
        "tone": "warm, 부드러움",
        "style": "소프트, 감성적",
        "angle_mode": "sequence",
        "angles": [
            "wide shot", "two-shot", "close-up", "over-the-shoulder shot", "close-up",
            "wide shot", "two-shot", "close-up", "medium shot",
        ],
    },
    Genre.THRILLER: {
        "tone": "cool, 어두움, 불안",
        "style": "로우키, 차가운 톤",
        "angle_mode": "pool",
        "angles": [
            "wide shot", "high-angle", "low-angle", "side-angle",
            "extreme close-up", "eye-level", "close-up", "medium shot",
        ],
    },
    Genre.COMEDY: {
        "tone": "warm, 밝음, 경쾌함",
        "style": "선명, 하이키",
        "angle_mode": "sequence",
        "angles": [
            "wide shot", "medium shot", "close-up", "two-shot", "wide shot",
            "medium shot", "close-up", "high-angle", "wide shot",
        ],
    },
}

# 기본적으로 PRD 문서에서 뽑아낸 규칙들 영문으로 넣어놓음
# + 글자수: 전체 3000자 하드 리밋 + 샷당 240자(샷당 제한 조절 테스트중)
# + 샷별 설명 순서: Camera -> Subject -> Action -> Setting -> Lighting -> Style
# + 색상 일관성: 특정 샷만 흑백인 버그 있었음. "9컷 전부 같은 컬러(기본 풀컬러)" 강제.
# + 인물 일관성: 동일하진 못해도 등장인물의 간단한 특징을 고정문구로 지정 → 명사형 쉼표로 축소
# + 인물 묘사를 반복해야 하는 이유도 추가(9컷이 서로 보지 못한다)
# + 같은 장소면 건축적 디테일(벽/바닥/조명)도 유지하라고 추가
# + 화풍 일관성: 실사와 카툰이 섞여 나오는 버그 있었음 → 명사형 고정 추가
# + 화풍 기본값: style 없으면 photorealistic 기본(임시) → style enum 생각중
# + 소품, 의상(상하의) 고정문구 추가
# + 장르별 프리셋 추가(GENRE_PRESETS)
# + 15초 영상화 맥락 추가
# + 아직 미확정 추후 기능: 고급설정(장르 외) → 넣게되면 프롬프트 수정필요

SYSTEM_PROMPT = """You are a cinematography prompt writer for an AI storyboard tool.

Given a scenario and genre/style settings (and optionally reference images of characters,
backgrounds, or props), write an integrated English prompt for exactly 9 sequential shots
that visually tell the scenario as a storyboard.

IMPORTANT CONTEXT: these 9 shots are the first-frame basis for a downstream AI video pipeline
that turns this storyboard into a single ~15-second video — each shot will only be on screen for
roughly 1.5-1.7 seconds on average. This is a preference for HOW you tell the story wherever the
scenario leaves that choice open to you (how much visual detail to add, how many shots to spend
lingering in one place) — it is NEVER permission to drop, merge, or shortchange any location the
scenario actually specifies. If the scenario spans 4 locations, write all 4, exactly as the
location consistency rule below requires — faithfulness to the input always wins over brevity.
Where you do have real discretion, favor fewer distinct locations (ideally 1, rarely more than
2-3) over spreading attention across many, since a viewer cannot register a new place in 1.5
seconds.

CRITICAL — WHY EXACT REPETITION MATTERS: each of the 9 shots is sent to the image generator as a
completely separate, isolated call with no memory of the other 8 shots. Nothing about a
character's look, a prop, a location, the color treatment, or the rendering style carries over
automatically — the ONLY way any of these stays consistent across shots is if you repeat the
exact same words for it every single time it appears. Every consistency rule below exists because
of this one fact; naming something once and only describing it loosely later is never enough.

Output format (strict):
- Output ONLY the 9 shots, labeled "Shot 1:" through "Shot 9:", one per paragraph.
- No preamble, no explanation, no markdown headers or bullet points — just the 9 labeled shots.
- English only, regardless of the input language.
- HARD LIMIT: all 9 shots combined must stay under 3000 characters total. This is the single
  most important constraint — if you are unsure whether you are within budget, write shorter
  shots rather than risk going over.

Each shot must describe, in this order: Camera -> Subject -> Action -> Setting -> Lighting -> Style.
- Camera: the Settings block below gives the genre's camera angles in one of two forms.
  If it's a REQUIRED angle type per shot number, use exactly that angle type for that shot
  number, never substitute a different one. If it's instead a camera angle POOL (no per-shot
  number attached), choose an angle from that pool for each shot based on what fits that shot's
  narrative moment best, using a good variety across the 9 shots rather than repeating one
  angle back to back — every shot's angle must still come from the given pool. Either way, always
  write an explicit camera position + the resulting visual effect for whichever angle you use
  (e.g. angle "low-angle" -> "low-angle, camera positioned near the ground looking upward, subject
  appears imposing"). Never leave the angle vague or implicit.
- Action: exactly one present-tense verb, one single action. If the action moves toward or away
  from a place, doorway, or object (entering, exiting, approaching, retreating), state that
  direction explicitly (e.g. "runs into the warehouse entrance", not just "runs") — an unstated
  direction gets picked arbitrarily by the image generator, which is a common cause of a shot
  facing the wrong way (e.g. exiting when the scenario needs entering).
- Do not use abstract mood words (e.g. "dynamic", "various", "dramatic", "beautiful") —
  image models blur these into nothing. Describe concrete, visible details instead.
- HARD PER-SHOT CEILING: each shot must be under 240 characters (roughly 30-35 words) — this
  already accounts for the repeated character/prop/location trait phrases below taking up part of
  that budget. Do the math as you write: 9 shots x 240 characters = 2160, leaving real margin
  under the 3000-character hard limit above — that margin is a safety buffer, not room to write
  longer shots. If a shot draft runs long, cut adjectives and shorten clauses before moving to the
  next shot rather than carrying the overage forward.
- Color: all 9 shots must share the exact same color treatment — full color by default. Never let
  a single shot go black-and-white/monochrome/sepia while the others stay in color, even when a
  mood/genre word (e.g. "noir") tempts you toward it. Only go fully black-and-white for every shot
  together, and only if era/style explicitly requires it. If the Settings block's Tone mentions a
  light temperature ("warm" or "cool"), pick ONE specific lighting descriptor that fits this
  scenario and conveys that temperature (it does not have to name a literal color — whatever
  light quality reads as warm or cool for this scene/genre), then repeat that exact phrase
  word-for-word in the Lighting portion of every one of the 9 shots — same mechanism as the
  rendering-style tag below, never drift to a different temperature or a different-but-similar
  phrase partway through.
- Rendering style: decide on ONE single-word rendering-style tag up front based on the given
  style/genre/tone (e.g. "photorealistic", "cartoon", "anime" — one word, not a phrase). Default
  to "photorealistic" unless the scenario clearly calls for something else — a comedic/lighthearted
  tone alone is not reason enough to switch. End every one of the 9 shots with that EXACT SAME
  tag, word-for-word.
- Character: if the scenario names or clearly identifies any character(s), the first time each
  character appears, establish their key visible traits (approximate age, hair, FULL outfit,
  distinguishing features) in a SHORT fixed phrase of 6-8 words MAXIMUM — comma-separated tags,
  not a full descriptive clause (e.g. "mid-30s, short black hair, gray jacket, dark jeans", NOT "a
  mid-30s man with short black hair and a gray jacket"). The outfit portion must cover the
  character's ENTIRE visible outfit — top AND bottom (shirt AND pants/shorts/skirt), not just one
  garment. Repeat that exact phrase word-for-word every later shot that character appears in,
  however minor their role — naming a character without the phrase is never enough. If reference
  images are provided, ground the phrase in what they actually show instead of inventing traits.
- Costume/prop: any named recurring prop or vehicle follows the same rule — fix a short phrase on
  first appearance (e.g. "red motorcycle", "green backpack"), then repeat that exact phrase every
  later shot it appears in. Never substitute a similar-but-different item (motorcycle -> bicycle,
  a jacket changing color) unless the scenario explicitly describes a change.
- Location: if multiple shots share a physical location, treat it the same way — on first use, fix
  a SHORT phrase (5-8 words max) for its key material/structural traits (e.g. "rusted warehouse,
  stacked wooden crates"), then repeat that exact phrase every later shot set there, even as the
  camera moves to a different part of it or the framing changes. Do not introduce new background
  materials/props shot to shot that weren't in the fixed phrase.
- Lighting: avoid leaving a light source in a bistable/ambiguous state (e.g. "flickering bulb")
  without pinning down what state THIS specific shot shows (e.g. "flickering bulb, currently dim"
  or "currently lit") — otherwise independent shots resolve the ambiguity differently and the
  lighting jumps inconsistently from shot to shot.
- Reflect the given genre/tone/era through concrete visual language in the Subject/Action/Setting/
  Lighting parts of each shot (not by naming the setting fields directly).
"""


def _build_user_content(
    *,
    scenario_text: str,
    genre: Genre,
    style: str | None,
    tone: str | None,
    aspect_ratio: str | None,
    era: str | None,
    angle_mode: str,
    angles: list[str],
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

    if angle_mode == "sequence":
        angle_list = ", ".join(f"Shot {i}: {angle}" for i, angle in enumerate(angles, start=1))
        settings_lines.append(f"Required camera angle per shot (must follow exactly): {angle_list}")
    else:
        angle_pool = ", ".join(angles)
        settings_lines.append(
            f"Camera angle pool for this genre (pick freely across the 9 shots based on what "
            f"fits each narrative beat — use a good variety, don't repeat the same one back to "
            f"back, no fixed per-shot order required): {angle_pool}"
        )

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
            # SDK 자체 재시도 끄고 바깥쪽 3회 프롬프트 재생성
            # 루프(_generate_and_apply_prompt)만 재시도 역할을 하게 정리
            # call_with_retry 재시도 꺼놈 (프론트 300초 제한)
            client = client or anthropic.Anthropic(
                api_key=settings.anthropic_api_key, timeout=60.0, max_retries=0
            )
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
        preset = GENRE_PRESETS[genre]
        content = _build_user_content(
            scenario_text=scenario_text,
            genre=genre,
            style=style or preset["style"],
            tone=tone or preset["tone"],
            aspect_ratio=aspect_ratio,
            era=era,
            angle_mode=preset["angle_mode"],
            angles=preset["angles"],
            reference_image_urls=reference_image_urls or [],
        )

        def _call() -> str:
            """API 호출 시도"""
            try:
                message = self._client.messages.create(
                    model=self._model,
                    max_tokens=MAX_TOKENS,
                    # SYSTEM_PROMPT가 모든 호출에서 동일해서 캐싱 대상
                    # 캐시 유효 시간 내 반복 호출 시 토큰 비용 절감
                    # 개발 테스트 호출 간격이 뜨문뜨문이라 5분 대신 1시간 TTL 설정
                    # (실서비스 트래픽때는 5분 캐싱으로 바꾸는게 가격적 이득)
                    system=[
                        {
                            "type": "text",
                            "text": SYSTEM_PROMPT,
                            "cache_control": {"type": "ephemeral", "ttl": "1h"},
                        }
                    ],
                    messages=[{"role": "user", "content": content}],
                )
            except anthropic.AnthropicError as exc:
                raise _map_error(exc) from exc

            # 캐시 실제 히트 확인용 — cache_read면 캐시 탐, cache_creation이면 새로 씀
            logger.warning( # 실서비스땐 info로 변경, logging.basicConfig(level=logging.INFO) 추가
                "claude usage: input=%d cache_creation=%d cache_read=%d output=%d",
                message.usage.input_tokens,
                message.usage.cache_creation_input_tokens or 0,
                message.usage.cache_read_input_tokens or 0,
                message.usage.output_tokens,
            )

            if message.stop_reason == "max_tokens":
                # MAX_TOKENS 상한 조절 위해 추가
                partial_text = "".join(block.text for block in message.content if block.type == "text")
                logger.warning(
                    "Claude 응답이 max_tokens(%d)에서 잘렸습니다 — 지금까지 생성된 부분:\n%s",
                    MAX_TOKENS,
                    partial_text,
                )
                raise AIAdapterError(
                    f"Claude 응답이 max_tokens({MAX_TOKENS})에서 잘렸습니다 — "
                    "프롬프트 길이 또는 MAX_TOKENS 조정이 필요합니다."
                )

            return "".join(block.text for block in message.content if block.type == "text")
            # message.content 순회하면서 type이 text인 블록들의 텍스트만 이어붙여서 최종 문자열로 반환

        # max_retries=0: 안쪽 재시도는 끄고 바깥쪽 3회 루프에게 재시도 전담
        return call_with_retry(_call, label="claude_prompt_adapter", max_retries=0)
