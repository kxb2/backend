import anthropic
import httpx
import pytest

from app.ai.exceptions import AIAdapterRequestError, AIAdapterUnavailableError
from app.ai.prompt_adapter import ClaudePromptAdapter
from app.core.enums import Genre


class _FakeTextBlock:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _FakeMessage:
    def __init__(self, text):
        self.content = [_FakeTextBlock(text)]


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
    adapter, client = _adapter([_FakeMessage("Shot 1: ...\nShot 2: ...")])

    result = adapter.generate_prompt(scenario_text="한 남자가 걷는다", genre=Genre.DRAMA)

    assert result == "Shot 1: ...\nShot 2: ..."
    assert len(client.messages.calls) == 1


def test_sends_text_only_when_no_reference_images():
    adapter, client = _adapter([_FakeMessage("ok")])

    adapter.generate_prompt(scenario_text="scenario", genre=Genre.ACTION)

    content = client.messages.calls[0]["messages"][0]["content"]
    assert len(content) == 1
    assert content[0]["type"] == "text"


def test_attaches_reference_images_as_url_blocks():
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


def test_timeout_is_retried_then_succeeds():
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    adapter, client = _adapter([anthropic.APITimeoutError(request=request), _FakeMessage("recovered")])

    assert adapter.generate_prompt(scenario_text="s", genre=Genre.COMEDY) == "recovered"
    assert len(client.messages.calls) == 2


def test_rate_limit_is_retryable():
    adapter, client = _adapter([_status_error(anthropic.RateLimitError, 429), _FakeMessage("recovered")])

    assert adapter.generate_prompt(scenario_text="s", genre=Genre.THRILLER) == "recovered"
    assert len(client.messages.calls) == 2


def test_bad_request_is_not_retried():
    adapter, client = _adapter([_status_error(anthropic.BadRequestError, 400)])

    with pytest.raises(AIAdapterRequestError):
        adapter.generate_prompt(scenario_text="s", genre=Genre.DRAMA)

    assert len(client.messages.calls) == 1


def test_exhausting_retries_raises_unavailable_error():
    responses = [_status_error(anthropic.APIStatusError, 503) for _ in range(3)]
    adapter, client = _adapter(responses)

    with pytest.raises(AIAdapterUnavailableError):
        adapter.generate_prompt(scenario_text="s", genre=Genre.ACTION)

    assert len(client.messages.calls) == 3
