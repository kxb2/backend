import anthropic
import httpx
import pytest

from app.ai.exceptions import (
    AIAdapterError,
    AIAdapterRequestError,
    AIAdapterTimeoutError,
    AIAdapterUnavailableError,
)
from app.ai.prompt_adapter import ClaudePromptAdapter
from app.core.enums import Genre

"""Claude 프롬프트 생성 어댑터 테스트용 파일"""

class _FakeTextBlock:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _FakeUsage:
    def __init__(self):
        self.input_tokens = 10
        self.output_tokens = 10
        self.cache_creation_input_tokens = 0
        self.cache_read_input_tokens = 0


class _FakeMessage:
    def __init__(self, text, stop_reason="end_turn"):
        self.content = [_FakeTextBlock(text)]
        self.stop_reason = stop_reason
        self.usage = _FakeUsage()


class _FakeMessages:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class _FakeClient:
    def __init__(self, responses):
        self.messages = _FakeMessages(responses)


def _adapter(responses):
    client = _FakeClient(responses)
    return ClaudePromptAdapter(client=client, model="claude-sonnet-5"), client


def _status_error(cls, status_code):
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(status_code=status_code, request=request)
    return cls(f"status {status_code}", response=response, body=None)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr("app.ai.retry.time.sleep", lambda seconds: None)
    monkeypatch.setattr("app.ai.retry.random.uniform", lambda a, b: 0)


def test_returns_response_text():
    """Claude 응답의 텍스트 블록들을 이어붙여 정상 반환하는지"""
    adapter, client = _adapter([_FakeMessage("Shot 1: ...\nShot 2: ...")])

    result = adapter.generate_prompt(scenario_text="한 남자가 걷는다", genre=Genre.DRAMA)

    assert result == "Shot 1: ...\nShot 2: ..."
    assert len(client.messages.calls) == 1


def test_sends_text_only_when_no_reference_images():
    """레퍼런스 이미지 없으면 텍스트 블록만 전송하는지"""
    adapter, client = _adapter([_FakeMessage("ok")])

    adapter.generate_prompt(scenario_text="scenario", genre=Genre.ACTION)

    content = client.messages.calls[0]["messages"][0]["content"]
    assert len(content) == 1
    assert content[0]["type"] == "text"


def test_attaches_reference_images_as_url_blocks():
    """레퍼런스 이미지 있으면 URL 이미지 블록으로 첨부되는지"""
    adapter, client = _adapter([_FakeMessage("ok")])

    adapter.generate_prompt(
        scenario_text="scenario",
        genre=Genre.ROMANCE,
        reference_image_urls=["https://pub-x.r2.dev/a.png", "https://pub-x.r2.dev/b.png"],
    )

    content = client.messages.calls[0]["messages"][0]["content"]
    image_blocks = [block for block in content if block["type"] == "image"]
    assert len(image_blocks) == 2
    assert image_blocks[0]["source"] == {"type": "url", "url": "https://pub-x.r2.dev/a.png"}


def test_timeout_is_not_retried_at_adapter_level():
    """어댑터 레벨(call_with_retry)은 max_retries=0이라 타임아웃 나면 재시도 없이 즉시
    실패하는지 (호출 1회) — 실제 재시도는 generations.service의 바깥쪽 3회 루프가 담당"""
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    adapter, client = _adapter([anthropic.APITimeoutError(request=request)])

    with pytest.raises(AIAdapterTimeoutError):
        adapter.generate_prompt(scenario_text="s", genre=Genre.COMEDY)

    assert len(client.messages.calls) == 1


def test_rate_limit_is_not_retried_at_adapter_level():
    """429(너무 많은 요청)도 max_retries=0이라 재시도 없이 즉시 실패하는지 (호출 1회)"""
    adapter, client = _adapter([_status_error(anthropic.RateLimitError, 429)])

    with pytest.raises(AIAdapterUnavailableError):
        adapter.generate_prompt(scenario_text="s", genre=Genre.THRILLER)

    assert len(client.messages.calls) == 1


def test_bad_request_is_not_retried():
    """400(잘못된 요청)은 재시도 없이 바로 실패하는지 (호출 1회)"""
    adapter, client = _adapter([_status_error(anthropic.BadRequestError, 400)])

    with pytest.raises(AIAdapterRequestError):
        adapter.generate_prompt(scenario_text="s", genre=Genre.DRAMA)

    assert len(client.messages.calls) == 1


def test_server_error_is_not_retried_at_adapter_level():
    """503(서버 과부하, 점검)도 max_retries=0이라 재시도 없이 즉시 에러 전파되는지 (호출 1회)"""
    adapter, client = _adapter([_status_error(anthropic.APIStatusError, 503)])

    with pytest.raises(AIAdapterUnavailableError):
        adapter.generate_prompt(scenario_text="s", genre=Genre.ACTION)

    assert len(client.messages.calls) == 1


def test_raises_when_response_is_truncated_by_max_tokens():
    """토큰 제한 걸리면 명확한 에러로 처리되는지"""
    adapter, _ = _adapter([_FakeMessage("Shot 1: incomplete...", stop_reason="max_tokens")])

    with pytest.raises(AIAdapterError, match="max_tokens"):
        adapter.generate_prompt(scenario_text="s", genre=Genre.DRAMA)
