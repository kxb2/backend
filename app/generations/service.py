"""9컷 생성 상태 조회, AI 어댑터(app/ai) 호출 전체 과정 명령·재시도

오케스트레이션 순서(run_generation): 스토리보드 로드 → Claude 통합 프롬프트 생성+컷별 분리(형식 오류 시 재시도)
→ 9컷 이미지 병렬 생성(실패한 컷만 FAILED) → 전부 성공 시 3x3 그리드 합성 → Generation 상태 확정.
"""

import logging
import re
from concurrent.futures import ThreadPoolExecutor, wait
from io import BytesIO

import httpx
from PIL import Image
from sqlalchemy.orm import Session

from app.ai.base import ImageAdapter, PromptAdapter
from app.ai.exceptions import AIAdapterError
from app.ai.image_adapter import get_image_adapter
from app.ai.prompt_adapter import ClaudePromptAdapter
from app.core import storage
from app.core.constants import CUT_COUNT
from app.core.enums import JobStatus
from app.db.session import SessionLocal
from app.generations.models import Cut
from app.storyboards.models import Storyboard

logger = logging.getLogger(__name__)

MAX_INTEGRATED_PROMPT_LENGTH = 3000
MAX_PROMPT_ATTEMPTS = 2
GRID_IMAGE_FOLDER = "grids"

# Claude 출력의 "Shot 1: ...", "Shot 2: ..." 라벨을 순번과 함께 찾기
_SHOT_PATTERN = re.compile(r"Shot\s*(\d+)\s*:\s*", re.IGNORECASE)


class PromptValidationError(Exception):
    """Claude가 생성한 통합 프롬프트가 길이/형식 요구사항을 만족 못한 경우."""


def validate_prompt_length(
    integrated_prompt: str, *, max_length: int = MAX_INTEGRATED_PROMPT_LENGTH
) -> None:
    """샷별 프롬프트 합계(통합 프롬프트 전체 길이)가 max_length를 넘지 않는지 검증."""
    if len(integrated_prompt) > max_length:
        raise PromptValidationError(
            f"통합 프롬프트가 {max_length}자를 초과했습니다 (현재 {len(integrated_prompt)}자)"
        )


def split_shots(integrated_prompt: str) -> dict[int, str]:
    """"Shot 1: ...\\nShot 2: ..." 형태의 통합 프롬프트를 {컷 순번: 프롬프트 텍스트}로 분리."""
    matches = list(_SHOT_PATTERN.finditer(integrated_prompt))
    if len(matches) != CUT_COUNT:
        raise PromptValidationError(
            f"통합 프롬프트에서 Shot {CUT_COUNT}개를 찾지 못했습니다 (찾은 개수: {len(matches)})"
        )

    shots: dict[int, str] = {}
    for index, match in enumerate(matches):
        order_no = int(match.group(1))
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(integrated_prompt)
        text = integrated_prompt[start:end].strip()
        if not text:
            raise PromptValidationError(f"Shot {order_no}의 내용이 비어 있습니다.")
        shots[order_no] = text

    if set(shots) != set(range(1, CUT_COUNT + 1)):
        raise PromptValidationError(
            f"컷 순번이 1~{CUT_COUNT}로 정확히 매겨지지 않았습니다 (찾은 순번: {sorted(shots)})"
        )

    return shots


def apply_integrated_prompt(db: Session, storyboard: Storyboard, integrated_prompt: str) -> None:
    """Claude가 생성한 통합 프롬프트를 → 길이 검증, 샷 분리, 순번 일치 확인후 → 스토리보드와 컷에 반영."""
    validate_prompt_length(integrated_prompt)
    shots = split_shots(integrated_prompt)

    order_nos = {cut.order_no for cut in storyboard.cuts}
    if order_nos != set(range(1, CUT_COUNT + 1)):
        raise PromptValidationError(
            f"스토리보드의 컷 순번이 1~{CUT_COUNT}와 일치하지 않습니다 (실제: {sorted(order_nos)})"
        )

    storyboard.integrated_prompt = integrated_prompt
    for cut in storyboard.cuts:
        cut.prompt_text = shots[cut.order_no]

    db.commit()


def _generate_and_apply_prompt(db: Session, storyboard: Storyboard, prompt_adapter: PromptAdapter) -> bool:
    """Claude 호출 + 분리/검증. 형식이 이상하면 MAX_PROMPT_ATTEMPTS까지 새로 생성해서 재시도, 최종 실패 시 False."""
    for attempt in range(1, MAX_PROMPT_ATTEMPTS + 1):
        try:
            integrated_prompt = prompt_adapter.generate_prompt(
                scenario_text=storyboard.scenario_text,
                genre=storyboard.genre,
                style=storyboard.style,
                tone=storyboard.tone,
                aspect_ratio=storyboard.aspect_ratio,
                era=storyboard.era,
                reference_image_urls=[ref.image_url for ref in storyboard.reference_images],
            )
            apply_integrated_prompt(db, storyboard, integrated_prompt)
            return True
        except (AIAdapterError, PromptValidationError) as exc:
            logger.warning("통합 프롬프트 생성/검증 실패(시도 %d/%d): %s", attempt, MAX_PROMPT_ATTEMPTS, exc)

    return False


def _generate_one_cut_image(
    image_adapter: ImageAdapter, cut: Cut, aspect_ratio: str | None
) -> tuple[int, str | None]:
    """스레드에서 실행 — DB 접근 없이 (cut.id, 이미지 URL) 또는 실패 시 (cut.id, None)만 반환."""
    try:
        url = image_adapter.generate_image(prompt_text=cut.prompt_text, aspect_ratio=aspect_ratio)
        return cut.id, url
    except AIAdapterError as exc:
        logger.error("컷 %d(id=%d) 이미지 생성 실패: %s", cut.order_no, cut.id, exc)
        return cut.id, None


def _generate_cut_images(
    image_adapter: ImageAdapter, cuts: list[Cut], aspect_ratio: str | None
) -> dict[int, str | None]:
    """9컷 이미지를 스레드풀로 병렬 생성. {cut.id: 이미지 URL(성공) 또는 None(실패)} 반환. DB 쓰기는 안 함."""
    with ThreadPoolExecutor(max_workers=len(cuts)) as executor:
        futures = [executor.submit(_generate_one_cut_image, image_adapter, cut, aspect_ratio) for cut in cuts]
        wait(futures)

    return dict(future.result() for future in futures)


def _build_grid_image(cut_image_urls: list[str]) -> bytes:
    """order_no 순서로 정렬된 9개 이미지 URL을 내려받아 3x3 그리드 1장(PNG)으로 합성."""
    images = [Image.open(BytesIO(httpx.get(url).content)) for url in cut_image_urls]
    tile_size = images[0].size
    images = [image if image.size == tile_size else image.resize(tile_size) for image in images]

    grid = Image.new("RGB", (tile_size[0] * 3, tile_size[1] * 3))
    for index, image in enumerate(images):
        row, col = divmod(index, 3)
        grid.paste(image, (col * tile_size[0], row * tile_size[1]))

    buffer = BytesIO()
    grid.save(buffer, format="PNG")
    return buffer.getvalue()


def run_generation(storyboard_id: int) -> None:
    """스토리보드 생성 직후 BackgroundTasks로 호출되는 9컷 생성 오케스트레이션 진입점.

    요청-응답 사이클과 독립적으로 실행되므로, 넘겨받은 세션을 재사용하지 않고 자체 DB 세션을 열고 닫는다.
    """
    db = SessionLocal()
    try:
        storyboard = db.get(Storyboard, storyboard_id)
        generation = storyboard.generation
        generation.status = JobStatus.PROCESSING
        db.commit()

        if not _generate_and_apply_prompt(db, storyboard, ClaudePromptAdapter()):
            for cut in storyboard.cuts:
                cut.status = JobStatus.FAILED
            generation.status = JobStatus.FAILED
            db.commit()
            return

        for cut in storyboard.cuts:
            cut.status = JobStatus.PROCESSING
        db.commit()

        image_adapter = get_image_adapter(storyboard.image_model)
        results = _generate_cut_images(image_adapter, storyboard.cuts, storyboard.aspect_ratio)

        for cut in storyboard.cuts:
            image_url = results.get(cut.id)
            cut.image_url = image_url
            cut.status = JobStatus.COMPLETED if image_url else JobStatus.FAILED
        db.commit()

        if all(cut.status == JobStatus.COMPLETED for cut in storyboard.cuts):
            grid_bytes = _build_grid_image([cut.image_url for cut in storyboard.cuts])
            generation.grid_image_url = storage.upload_image_bytes(
                grid_bytes, content_type="image/png", folder=GRID_IMAGE_FOLDER
            )
            generation.status = JobStatus.COMPLETED
        else:
            generation.status = JobStatus.FAILED
        db.commit()
    finally:
        db.close()
