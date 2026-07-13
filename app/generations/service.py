"""9컷 생성 상태 조회, AI 어댑터(app/ai) 호출 오케스트레이션·재시도"""

import re

from sqlalchemy.orm import Session

from app.core.constants import CUT_COUNT
from app.storyboards.models import Storyboard

MAX_INTEGRATED_PROMPT_LENGTH = 3000

# Claude 출력의 "Shot 1: ...", "Shot 2: ..." 라벨을 순번과 함께 찾는다.
_SHOT_PATTERN = re.compile(r"Shot\s*(\d+)\s*:\s*", re.IGNORECASE)


class PromptValidationError(Exception):
    """Claude가 생성한 통합 프롬프트가 길이/형식 요구사항을 만족하지 못한 경우."""


def validate_prompt_length(
    integrated_prompt: str, *, max_length: int = MAX_INTEGRATED_PROMPT_LENGTH
) -> None:
    """샷별 프롬프트 합계(통합 프롬프트 전체 길이)가 max_length를 넘지 않는지 검증한다."""
    if len(integrated_prompt) > max_length:
        raise PromptValidationError(
            f"통합 프롬프트가 {max_length}자를 초과했습니다 (현재 {len(integrated_prompt)}자)"
        )


def split_shots(integrated_prompt: str) -> dict[int, str]:
    """"Shot 1: ...\\nShot 2: ..." 형태의 통합 프롬프트를 {컷 순번: 프롬프트 텍스트}로 분리한다."""
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
    """Claude가 생성한 통합 프롬프트를 검증한 뒤 스토리보드와 각 컷에 반영한다."""
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
