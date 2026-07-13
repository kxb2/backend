"""GPT/Gemini/Claude 어댑터 공통 예외 타입.

각 어댑터 구현체는 SDK/HTTP 원본 예외를 여기 정의된 타입으로 변환해서 던지고,
app.ai.retry.call_with_retry는 SDK 종류에 상관없이 이 타입만 보고 재시도 여부 판단.
"""


class AIAdapterError(Exception):
    """AI 어댑터 호출 중 발생한 에러의 공통 베이스."""


class AIAdapterTimeoutError(AIAdapterError):
    """호출이 타임아웃된 경우. 재시도 대상 O."""


class AIAdapterUnavailableError(AIAdapterError):
    """429/503 등 일시적 과부하로 재시도하면 성공할 수 있는 경우. 재시도 대상 O."""


class AIAdapterRequestError(AIAdapterError):
    """400/401 등 요청 자체가 잘못되어 재시도해도 성공할 수 없는 경우. 재시도 대상 X."""


# GPT/Gemini/Claude 세 어댑터가 공통으로 쓰는, 재시도하면 성공할 가능성이 있는 HTTP 상태 코드.
# 429=rate limit, 5xx=서버 과부하/일시 장애, 529=Anthropic 전용 overloaded.
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504, 529}
