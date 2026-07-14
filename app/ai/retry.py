"""GPT/Gemini/Claude 어댑터 호출 공통 재시도 유틸리티.

PRD 안정성 요구사항: "모델 API 타임아웃·실패 시 해당 컷만 재시도, 전체 파이프라인 중단 금지"
이 함수는 호출 1건 단위의 재시도만 책임지고,
컷 단위 격리(하나 실패해도 나머지 8컷은 계속 진행)는 호출하는 쪽의 책임.
"""

import logging
import random
import time
from typing import Callable, TypeVar

from app.ai.exceptions import AIAdapterTimeoutError, AIAdapterUnavailableError

logger = logging.getLogger(__name__)

T = TypeVar("T")

# 재시도하면 성공할 가능성이 있는 에러만 재시도 대상.
# AIAdapterRequestError(400/401 등)나 그 외 예상 못한 예외는 즉시 그대로 전파.
RETRYABLE_ERRORS = (AIAdapterTimeoutError, AIAdapterUnavailableError)


def call_with_retry(
    func: Callable[[], T],
    *,
    max_retries: int = 2,
    base_delay: float = 1.0,
    max_delay: float = 10.0,
    label: str = "ai_adapter",
) -> T:
    """func()를 실행하고, RETRYABLE_ERRORS만 지수 백오프(+jitter)로 재시도.

    max_retries를 소진하면 마지막 예외를 그대로 던짐.
    """
    attempt = 0
    while True:
        try:
            return func()
        except RETRYABLE_ERRORS as exc:
            if attempt >= max_retries:
                logger.error("%s 재시도 %d회 모두 실패: %s", label, max_retries, exc)
                raise

            delay = min(base_delay * (2**attempt), max_delay) + random.uniform(0, base_delay)
            logger.warning(
                "%s 호출 실패(%s), %.1fs 후 재시도 (%d/%d)",
                label,
                exc,
                delay,
                attempt + 1,
                max_retries,
            )
            time.sleep(delay)
            attempt += 1
