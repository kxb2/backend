from io import BytesIO
from unittest.mock import Mock

import pytest
from PIL import Image

from app.ai.exceptions import AIAdapterError
from app.generations.service import (
    PromptValidationError,
    _build_grid_image,
    _generate_and_apply_prompt,
    _generate_cut_images,
    apply_integrated_prompt,
    split_shots,
    validate_prompt_length,
)
from app.generations.models import Cut
from app.storyboards.models import Storyboard

"""9컷 생성 관련 테스트 파일"""

def _shot_text(order_no: int) -> str:
    return f"description for shot {order_no}"


def _integrated_prompt(order_nos: list[int] | None = None) -> str:
    return "\n".join(f"Shot {n}: {_shot_text(n)}" for n in (order_nos or list(range(1, 10))))


def _storyboard_with_cuts(order_nos: list[int] | None = None) -> Storyboard:
    storyboard = Storyboard(id=1)
    storyboard.cuts = [Cut(order_no=n) for n in (order_nos or list(range(1, 10)))]
    return storyboard


class TestSplitShots:
    def test_splits_nine_shots_in_order(self):
        """정상적인 9컷(샷)이 순서대로 잘 분리되는지"""
        shots = split_shots(_integrated_prompt())

        assert len(shots) == 9
        for n in range(1, 10):
            assert shots[n] == _shot_text(n)

    def test_maps_correctly_even_when_shots_appear_out_of_order(self):
        """텍스트 안 순서 뒤섞여도 순번 기준으로 정확히 매핑되는지"""
        shuffled = [3, 1, 2, 4, 5, 6, 7, 8, 9]
        shots = split_shots(_integrated_prompt(shuffled))

        assert shots[3] == _shot_text(3)
        assert shots[1] == _shot_text(1)

    def test_raises_when_fewer_than_nine_shots(self):
        """샷 8개 에러"""
        with pytest.raises(PromptValidationError):
            split_shots(_integrated_prompt(list(range(1, 9))))

    def test_raises_when_more_than_nine_shots(self):
        """샷 10개 에러"""
        with pytest.raises(PromptValidationError):
            split_shots(_integrated_prompt(list(range(1, 11))))

    def test_raises_when_shot_numbers_are_not_exactly_one_to_nine(self):
        """순번 중복/누락 이면 에러"""
        # 1이 중복되고 5가 빠짐
        with pytest.raises(PromptValidationError):
            split_shots(_integrated_prompt([1, 1, 2, 3, 4, 6, 7, 8, 9]))

    def test_raises_when_a_shot_is_empty(self):
        """특정 샷 내용 비어있으면 에러"""
        prompt = _integrated_prompt(list(range(1, 9))) + "\nShot 9:   "

        with pytest.raises(PromptValidationError):
            split_shots(prompt)


class TestValidatePromptLength:
    def test_passes_under_limit(self):
        """2999자 통과"""
        validate_prompt_length("x" * 2999)

    def test_passes_exactly_at_limit(self):
        """딱 3000자(경계값)도 통과"""
        validate_prompt_length("x" * 3000)

    def test_raises_over_limit(self):
        """3001자 에러"""
        with pytest.raises(PromptValidationError):
            validate_prompt_length("x" * 3001)


class TestApplyIntegratedPrompt:
    def test_assigns_prompt_to_storyboard_and_each_cut(self):
        """정상 케이스: storyboard/각 cut에 값 반영되고 commit 호출되는지"""
        storyboard = _storyboard_with_cuts()
        db = Mock()
        prompt = _integrated_prompt()

        apply_integrated_prompt(db, storyboard, prompt)

        assert storyboard.integrated_prompt == prompt
        for cut in storyboard.cuts:
            assert cut.prompt_text == _shot_text(cut.order_no)
        db.commit.assert_called_once()

    def test_raises_and_does_not_commit_when_cut_count_is_wrong(self):
        """컷 순번 1~9와 안 맞으면 에러 나고 commit 안됨"""
        storyboard = _storyboard_with_cuts(list(range(1, 9)))  # 8개뿐
        db = Mock()

        with pytest.raises(PromptValidationError):
            apply_integrated_prompt(db, storyboard, _integrated_prompt())

        db.commit.assert_not_called()

    def test_raises_and_does_not_commit_when_prompt_too_long(self):
        """너무 길면 에러나고 아무것도 반영 안된채 commit 안됨"""
        storyboard = _storyboard_with_cuts()
        db = Mock()
        too_long = _integrated_prompt() + "x" * 3000

        with pytest.raises(PromptValidationError):
            apply_integrated_prompt(db, storyboard, too_long)

        db.commit.assert_not_called()
        assert storyboard.integrated_prompt is None

    def test_raises_and_does_not_commit_when_shots_malformed(self):
        """샷 라벨 자체가 통째로 없는 경우(명백한 오류) 에러나고 commit 안됨"""
        storyboard = _storyboard_with_cuts()
        db = Mock()

        with pytest.raises(PromptValidationError):
            apply_integrated_prompt(db, storyboard, "not a valid integrated prompt")

        db.commit.assert_not_called()
        assert storyboard.integrated_prompt is None


class _FakePromptAdapter:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def generate_prompt(self, **kwargs):
        self.calls += 1
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class TestGenerateAndApplyPrompt:
    def test_succeeds_on_first_try(self):
        """Claude가 처음부터 정상 포맷 주면 재시도 없이 성공"""
        storyboard = _storyboard_with_cuts()
        adapter = _FakePromptAdapter([_integrated_prompt()])

        assert _generate_and_apply_prompt(Mock(), storyboard, adapter) is True
        assert adapter.calls == 1

    def test_retries_once_after_malformed_response_then_succeeds(self):
        """첫 응답이 형식 오류면 한 번 더 새로 생성해서 성공"""
        storyboard = _storyboard_with_cuts()
        adapter = _FakePromptAdapter(["malformed, no shot labels", _integrated_prompt()])

        assert _generate_and_apply_prompt(Mock(), storyboard, adapter) is True
        assert adapter.calls == 2

    def test_returns_false_after_max_attempts_all_malformed(self):
        """MAX_PROMPT_ATTEMPTS(3)번 다 형식 오류면 False 반환"""
        storyboard = _storyboard_with_cuts()
        adapter = _FakePromptAdapter(["malformed 1", "malformed 2", "malformed 3"])

        assert _generate_and_apply_prompt(Mock(), storyboard, adapter) is False
        assert adapter.calls == 3

    def test_retries_after_adapter_error_too(self):
        """Claude 호출 자체가(재시도 다 쓰고) 실패해도 service.py에서 한 번 더 시도"""
        storyboard = _storyboard_with_cuts()
        adapter = _FakePromptAdapter([AIAdapterError("claude down"), _integrated_prompt()])

        assert _generate_and_apply_prompt(Mock(), storyboard, adapter) is True
        assert adapter.calls == 2


class _FakeImageAdapter:
    def __init__(self, responses_by_prompt):
        self._responses = responses_by_prompt
        self.calls: list[tuple[str, str | None]] = []

    def generate_image(self, *, prompt_text, aspect_ratio=None):
        self.calls.append((prompt_text, aspect_ratio))
        response = self._responses[prompt_text]
        if isinstance(response, Exception):
            raise response
        return response


def _cuts_with_prompts(count: int = 9) -> list[Cut]:
    cuts = []
    for order_no in range(1, count + 1):
        cut = Cut(id=order_no, order_no=order_no, prompt_text=f"prompt-{order_no}")
        cuts.append(cut)
    return cuts


class TestGenerateCutImages:
    def test_all_succeed_maps_cut_id_to_url(self):
        """9개 컷 성공하면 각 cut.id에 맞는 이미지 URL이 정확히 매핑되고, aspect_ratio가 모든 호출에 전달되는지"""
        cuts = _cuts_with_prompts()
        responses = {f"prompt-{n}": f"https://pub-x.r2.dev/cuts/{n}.png" for n in range(1, 10)}
        adapter = _FakeImageAdapter(responses)

        results = _generate_cut_images(adapter, cuts, aspect_ratio="16:9")

        for cut in cuts:
            assert results[cut.id] == f"https://pub-x.r2.dev/cuts/{cut.order_no}.png"
        assert all(call[1] == "16:9" for call in adapter.calls)

    def test_failed_cuts_map_to_none_without_affecting_others(self):
        """일부 컷만 실패해도(AIAdapterError) 나머지 컷은 정상적으로 URL 반환"""
        cuts = _cuts_with_prompts()
        responses = {f"prompt-{n}": f"https://pub-x.r2.dev/cuts/{n}.png" for n in range(1, 10)}
        responses["prompt-5"] = AIAdapterError("image gen failed")
        adapter = _FakeImageAdapter(responses)

        results = _generate_cut_images(adapter, cuts, aspect_ratio=None)

        assert results[5] is None
        assert results[1] == "https://pub-x.r2.dev/cuts/1.png"
        assert results[9] == "https://pub-x.r2.dev/cuts/9.png"

    def test_non_ai_adapter_error_also_fails_only_that_cut(self):
        """R2 업로드 실패처럼 AIAdapterError가 아닌 예외도, 그 컷만 실패 처리되고 나머지·전체 흐름은 안 죽나"""
        cuts = _cuts_with_prompts()
        responses = {f"prompt-{n}": f"https://pub-x.r2.dev/cuts/{n}.png" for n in range(1, 10)}
        responses["prompt-5"] = RuntimeError("R2 upload failed")
        adapter = _FakeImageAdapter(responses)

        results = _generate_cut_images(adapter, cuts, aspect_ratio=None)

        assert results[5] is None
        assert results[1] == "https://pub-x.r2.dev/cuts/1.png"
        assert results[9] == "https://pub-x.r2.dev/cuts/9.png"


def _solid_png_bytes(color: tuple[int, int, int], size: tuple[int, int] = (2, 2)) -> bytes:
    image = Image.new("RGB", size, color)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


class TestBuildGridImage:
    def test_composes_nine_tiles_into_3x3_grid_in_order(self, monkeypatch):
        """9개 타일을 순서대로 3x3으로 정확히 배치하는지"""
        colors = [(i * 25, 0, 0) for i in range(9)]
        urls = [f"https://pub-x.r2.dev/cuts/{i}.png" for i in range(9)]
        tile_bytes_by_url = {url: _solid_png_bytes(colors[i]) for i, url in enumerate(urls)}

        monkeypatch.setattr(
            "app.core.storage.httpx.get",
            lambda url, **kwargs: Mock(content=tile_bytes_by_url[url]),
        )

        grid_bytes = _build_grid_image(urls)
        grid = Image.open(BytesIO(grid_bytes))

        assert grid.size == (6, 6)  # 2x2 타일 9개 -> 6x6
        for index, color in enumerate(colors):
            row, col = divmod(index, 3)
            assert grid.getpixel((col * 2, row * 2)) == color
