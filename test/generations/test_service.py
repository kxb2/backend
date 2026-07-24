from io import BytesIO
from unittest.mock import Mock

import pytest
from PIL import Image

from app.ai.base import GeneratedImage
from app.ai.exceptions import AIAdapterError
from app.generations.service import (
    MAX_MODEL_REFERENCE_IMAGES,
    PromptValidationError,
    _build_reference_images_by_cut,
    _extract_refmap,
    _generate_and_apply_prompt,
    _generate_cut_images,
    apply_integrated_prompt,
    build_grid_image,
    download_reference_images,
    download_reference_images_by_index,
    split_shots,
    validate_prompt_length,
)
from app.generations.models import Cut
from app.storyboards.models import ReferenceImage, Storyboard

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
        """Claude가 처음부터 정상 포맷 주면 재시도 없이 성공(에러 None, 시도횟수 1, refmap 없음)"""
        storyboard = _storyboard_with_cuts()
        adapter = _FakePromptAdapter([_integrated_prompt()])

        error, attempts, refmap = _generate_and_apply_prompt(Mock(), storyboard, adapter)
        assert error is None
        assert attempts == 1
        assert refmap is None
        assert adapter.calls == 1

    def test_retries_once_after_malformed_response_then_succeeds(self):
        """첫 응답이 형식 오류면 한 번 더 새로 생성해서 성공(시도횟수 2로 기록)"""
        storyboard = _storyboard_with_cuts()
        adapter = _FakePromptAdapter(["malformed, no shot labels", _integrated_prompt()])

        error, attempts, refmap = _generate_and_apply_prompt(Mock(), storyboard, adapter)
        assert error is None
        assert attempts == 2
        assert refmap is None
        assert adapter.calls == 2

    def test_returns_last_error_after_max_attempts_all_malformed(self):
        """MAX_PROMPT_ATTEMPTS(3)번 다 형식 오류면 마지막 시도의 에러 메시지와 시도횟수(3)를 반환"""
        storyboard = _storyboard_with_cuts()
        adapter = _FakePromptAdapter(["malformed 1", "malformed 2", "malformed 3"])

        error, attempts, refmap = _generate_and_apply_prompt(Mock(), storyboard, adapter)
        assert error is not None
        assert attempts == 3
        assert refmap is None
        assert adapter.calls == 3

    def test_retries_after_adapter_error_too(self):
        """Claude 호출 자체가(재시도 다 쓰고) 실패해도 service.py에서 한 번 더 시도"""
        storyboard = _storyboard_with_cuts()
        adapter = _FakePromptAdapter([AIAdapterError("claude down"), _integrated_prompt()])

        error, attempts, refmap = _generate_and_apply_prompt(Mock(), storyboard, adapter)
        assert error is None
        assert attempts == 2
        assert refmap is None
        assert adapter.calls == 2

    def test_extracts_refmap_and_strips_it_before_storing(self):
        """REFMAP 줄이 있으면 파싱해서 반환하고, storyboard.integrated_prompt에는 REFMAP이 안 남는지"""
        storyboard = _storyboard_with_cuts()
        prompt_with_refmap = (
            _integrated_prompt() + "\nREFMAP: 1=[1]; 2=[]; 3=[]; 4=[]; 5=[]; 6=[]; 7=[]; 8=[]; 9=[]"
        )
        adapter = _FakePromptAdapter([prompt_with_refmap])

        error, attempts, refmap = _generate_and_apply_prompt(Mock(), storyboard, adapter)

        assert error is None
        assert refmap == {1: [1], 2: [], 3: [], 4: [], 5: [], 6: [], 7: [], 8: [], 9: []}
        assert "REFMAP" not in storyboard.integrated_prompt
        # Shot 9 본문에도 REFMAP이 안 흡수됐는지(마지막 샷이라 가장 위험한 케이스)
        assert storyboard.cuts[-1].prompt_text == _shot_text(9)


class TestExtractRefmap:
    def test_no_refmap_line_returns_none(self):
        """REFMAP 줄 자체가 없으면 원본 그대로 반환하고 파싱 결과는 None(전체 폴백 트리거)"""
        prompt = _integrated_prompt()
        text, refmap = _extract_refmap(prompt)
        assert text == prompt
        assert refmap is None

    def test_full_refmap_parses_all_nine(self):
        """1~9 전부 있는 정상 REFMAP을 다 파싱하는지, 원본에서 REFMAP 줄이 제거되는지"""
        prompt = _integrated_prompt() + "\nREFMAP: 1=[]; 2=[3]; 3=[3,5]; 4=[1]; 5=[1]; 6=[]; 7=[]; 8=[1,2]; 9=[2]"
        text, refmap = _extract_refmap(prompt)

        assert refmap == {1: [], 2: [3], 3: [3, 5], 4: [1], 5: [1], 6: [], 7: [], 8: [1, 2], 9: [2]}
        assert "REFMAP" not in text
        assert text == _integrated_prompt()

    def test_partial_refmap_is_still_a_success(self):
        """일부 컷 번호가 빠져도(부분) 성공 취급 — 컷 단위 폴백은 호출부 몫이지 여기서 실패 처리 안 함"""
        prompt = _integrated_prompt() + "\nREFMAP: 1=[1]; 3=[2]"
        text, refmap = _extract_refmap(prompt)

        assert refmap == {1: [1], 3: [2]}
        assert "REFMAP" not in text

    def test_malformed_refmap_with_no_entries_returns_none(self):
        """REFMAP: 줄은 있는데 N=[..] 형식을 하나도 못 뽑으면 파싱 결과 None(전체 폴백)"""
        prompt = _integrated_prompt() + "\nREFMAP: 완전히 이상한 형식"
        text, refmap = _extract_refmap(prompt)

        assert refmap is None
        assert "REFMAP" not in text

    def test_explicit_empty_assignment_is_preserved_not_none(self):
        """명시적 빈 배정 6=[]가 빈 리스트로 정확히 파싱되는지(누락과 구분)"""
        prompt = _integrated_prompt() + "\nREFMAP: 6=[]"
        _text, refmap = _extract_refmap(prompt)

        assert refmap == {6: []}
        assert 6 in refmap  # 키 자체는 존재(누락 아님)


def _generated(url: str) -> GeneratedImage:
    return GeneratedImage(url=url, data=url.encode(), content_type="image/png")


class _FakeImageAdapter:
    def __init__(self, responses_by_prompt):
        self._responses = responses_by_prompt
        self.calls: list[tuple[str, str | None, list]] = []

    def generate_image(self, *, prompt_text, aspect_ratio=None, reference_images=None):
        self.calls.append((prompt_text, aspect_ratio, reference_images))
        response = self._responses[prompt_text]
        if isinstance(response, Exception):
            raise response
        return _generated(response)


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
            result, error = results[cut.id]
            assert result.url == f"https://pub-x.r2.dev/cuts/{cut.order_no}.png"
            assert error is None
        assert all(call[1] == "16:9" for call in adapter.calls)

    def test_each_cut_gets_its_own_reference_set(self):
        """reference_images_by_cut_id로 넘긴 컷별 레퍼런스 세트가 각 컷 호출에 그대로 전달되는지"""
        cuts = _cuts_with_prompts()
        responses = {f"prompt-{n}": f"https://pub-x.r2.dev/cuts/{n}.png" for n in range(1, 10)}
        adapter = _FakeImageAdapter(responses)
        ref_a = [(b"ref-a", "image/png")]
        ref_b = [(b"ref-b", "image/png")]
        reference_images_by_cut_id = {1: ref_a, 2: ref_b}

        _generate_cut_images(adapter, cuts, aspect_ratio=None, reference_images_by_cut_id=reference_images_by_cut_id)

        calls_by_prompt = {call[0]: call[2] for call in adapter.calls}
        assert calls_by_prompt["prompt-1"] is ref_a
        assert calls_by_prompt["prompt-2"] is ref_b
        # 딕셔너리에 없는 cut_id(3~9)는 빈 리스트로 처리
        assert calls_by_prompt["prompt-3"] == []

    def test_failed_cuts_map_to_none_without_affecting_others(self):
        """일부 컷만 실패해도(AIAdapterError) 나머지 컷은 정상적으로 URL 반환, 실패한 컷은 에러 메시지도 같이 반환"""
        cuts = _cuts_with_prompts()
        responses = {f"prompt-{n}": f"https://pub-x.r2.dev/cuts/{n}.png" for n in range(1, 10)}
        responses["prompt-5"] = AIAdapterError("image gen failed")
        adapter = _FakeImageAdapter(responses)

        results = _generate_cut_images(adapter, cuts, aspect_ratio=None)

        assert results[5] == (None, "image gen failed")
        assert results[1] == (_generated("https://pub-x.r2.dev/cuts/1.png"), None)
        assert results[9] == (_generated("https://pub-x.r2.dev/cuts/9.png"), None)

    def test_non_ai_adapter_error_also_fails_only_that_cut(self):
        """R2 업로드 실패처럼 AIAdapterError가 아닌 예외도, 그 컷만 실패 처리되고 나머지·전체 흐름은 안 죽나"""
        cuts = _cuts_with_prompts()
        responses = {f"prompt-{n}": f"https://pub-x.r2.dev/cuts/{n}.png" for n in range(1, 10)}
        responses["prompt-5"] = RuntimeError("R2 upload failed")
        adapter = _FakeImageAdapter(responses)

        results = _generate_cut_images(adapter, cuts, aspect_ratio=None)

        assert results[5] == (None, "R2 upload failed")
        assert results[1] == (_generated("https://pub-x.r2.dev/cuts/1.png"), None)
        assert results[9] == (_generated("https://pub-x.r2.dev/cuts/9.png"), None)


def _solid_png_bytes(color: tuple[int, int, int], size: tuple[int, int] = (2, 2)) -> bytes:
    image = Image.new("RGB", size, color)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


class TestBuildGridImage:
    def test_composes_nine_tiles_into_3x3_grid_in_order(self, monkeypatch):
        """9개 타일을 순서대로 3x3으로 정확히 배치하는지 (JPEG로 저장되니 픽셀은 완전 동일 X, 근사값만 확인)"""
        colors = [(i * 25, 0, 0) for i in range(9)]
        tile_size = 16  # JPEG 블록 크기(8x8)보다 충분히 커야 압축 오차가 안정적으로 작음
        urls = [f"https://pub-x.r2.dev/cuts/{i}.png" for i in range(9)]
        tile_bytes_by_url = {url: _solid_png_bytes(colors[i], size=(tile_size, tile_size)) for i, url in enumerate(urls)}

        monkeypatch.setattr(
            "app.core.storage.httpx.get",
            lambda url, **kwargs: Mock(content=tile_bytes_by_url[url]),
        )

        grid_bytes = build_grid_image(urls)
        grid = Image.open(BytesIO(grid_bytes))

        assert grid.format == "JPEG"
        assert grid.size == (tile_size * 3, tile_size * 3)
        for index, color in enumerate(colors):
            row, col = divmod(index, 3)
            # 타일 경계가 아니라 중앙을 샘플링(JPEG 블록 경계 번짐 영향 최소화)
            center = (col * tile_size + tile_size // 2, row * tile_size + tile_size // 2)
            got = grid.getpixel(center)
            assert max(abs(a - b) for a, b in zip(got, color)) <= 10

    def test_known_image_bytes_skips_redownload(self, monkeypatch):
        """known_image_bytes에 있는 URL은 R2로 재다운로드하지 않고 그대로 사용하는지"""
        tile_size = 16
        urls = [f"https://pub-x.r2.dev/cuts/{i}.png" for i in range(9)]
        tile_bytes_by_url = {
            url: _solid_png_bytes((i * 25, 0, 0), size=(tile_size, tile_size)) for i, url in enumerate(urls)
        }
        # known_image_bytes로 전부 미리 채워두고, httpx.get은 호출되면 바로 에러나게 해서
        # 재다운로드가 실제로 일어나지 않는지 검증
        monkeypatch.setattr(
            "app.core.storage.httpx.get",
            lambda url, **kwargs: (_ for _ in ()).throw(AssertionError(f"재다운로드 발생: {url}")),
        )

        grid_bytes = build_grid_image(urls, known_image_bytes=tile_bytes_by_url)
        grid = Image.open(BytesIO(grid_bytes))

        assert grid.size == (tile_size * 3, tile_size * 3)

    def test_partial_known_image_bytes_only_downloads_missing(self, monkeypatch):
        """known_image_bytes에 없는 URL만 실제로 다운로드되는지"""
        tile_size = 16
        urls = [f"https://pub-x.r2.dev/cuts/{i}.png" for i in range(9)]
        tile_bytes_by_url = {
            url: _solid_png_bytes((i * 25, 0, 0), size=(tile_size, tile_size)) for i, url in enumerate(urls)
        }
        known = {urls[0]: tile_bytes_by_url[urls[0]]}
        downloaded_urls = []

        def _fake_get(url, **kwargs):
            downloaded_urls.append(url)
            return Mock(content=tile_bytes_by_url[url])

        monkeypatch.setattr("app.core.storage.httpx.get", _fake_get)

        build_grid_image(urls, known_image_bytes=known)

        assert sorted(downloaded_urls) == sorted(urls[1:])


class TestDownloadReferenceImages:
    def test_downloads_each_url_with_content_type(self, monkeypatch):
        """각 URL을 다운로드해서 (바이트, content_type) 튜플로 반환하는지"""
        urls = ["https://pub-x.r2.dev/refs/a.png", "https://pub-x.r2.dev/refs/b.jpg"]
        bytes_by_url = {urls[0]: b"a-bytes", urls[1]: b"b-bytes"}
        monkeypatch.setattr(
            "app.core.storage.httpx.get",
            lambda url, **kwargs: Mock(content=bytes_by_url[url]),
        )

        results = download_reference_images(urls)

        assert results == [(b"a-bytes", "image/png"), (b"b-bytes", "image/jpeg")]

    def test_empty_list_returns_empty_without_downloading(self, monkeypatch):
        """빈 리스트면 다운로드 시도조차 안 하는지"""
        monkeypatch.setattr(
            "app.core.storage.httpx.get",
            lambda url, **kwargs: (_ for _ in ()).throw(AssertionError("호출되면 안 됨")),
        )

        assert download_reference_images([]) == []

    def test_caps_to_max_model_reference_images(self, monkeypatch):
        """MAX_MODEL_REFERENCE_IMAGES(3)장을 넘으면 앞 3장만 다운로드하는지(품질 희석 방지)"""
        urls = [f"https://pub-x.r2.dev/refs/{i}.png" for i in range(8)]
        downloaded_urls = []

        def _fake_get(url, **kwargs):
            downloaded_urls.append(url)
            return Mock(content=b"bytes")

        monkeypatch.setattr("app.core.storage.httpx.get", _fake_get)

        results = download_reference_images(urls)

        assert len(results) == MAX_MODEL_REFERENCE_IMAGES
        assert downloaded_urls == urls[:MAX_MODEL_REFERENCE_IMAGES]


class TestDownloadReferenceImagesByIndex:
    def test_downloads_only_requested_indices(self, monkeypatch):
        """요청한 인덱스에 해당하는 URL만 다운로드해서 {인덱스: (바이트, content_type)}로 반환하는지"""
        urls = [f"https://pub-x.r2.dev/refs/{i}.png" for i in range(1, 6)]  # 1~5번(인덱스 1~5)
        downloaded_urls = []

        def _fake_get(url, **kwargs):
            downloaded_urls.append(url)
            return Mock(content=f"{url}-bytes".encode())

        monkeypatch.setattr("app.core.storage.httpx.get", _fake_get)

        results = download_reference_images_by_index(urls, {2, 4})

        assert set(results) == {2, 4}
        assert results[2] == (b"https://pub-x.r2.dev/refs/2.png-bytes", "image/png")
        assert sorted(downloaded_urls) == [urls[1], urls[3]]

    def test_out_of_range_indices_are_ignored(self, monkeypatch):
        """범위 밖 인덱스(0, 존재하지 않는 큰 수)는 조용히 무시하는지"""
        urls = ["https://pub-x.r2.dev/refs/1.png"]
        monkeypatch.setattr(
            "app.core.storage.httpx.get", lambda url, **kwargs: Mock(content=b"bytes")
        )

        results = download_reference_images_by_index(urls, {0, 1, 99})

        assert set(results) == {1}

    def test_empty_indices_returns_empty_without_downloading(self, monkeypatch):
        """인덱스가 하나도 없으면 다운로드 시도 자체를 안 하는지"""
        monkeypatch.setattr(
            "app.core.storage.httpx.get",
            lambda url, **kwargs: (_ for _ in ()).throw(AssertionError("호출되면 안 됨")),
        )

        assert download_reference_images_by_index(["https://pub-x.r2.dev/refs/1.png"], set()) == {}

    def test_one_index_failure_does_not_abort_the_others(self, monkeypatch):
        """인덱스 하나가 다운로드 실패해도 나머지 인덱스는 정상 반환되는지(배치 전체 실패 X)"""
        urls = [f"https://pub-x.r2.dev/refs/{i}.png" for i in range(1, 4)]  # 1,2,3번

        def _fake_get(url, **kwargs):
            if url == urls[1]:  # 2번만 실패
                raise RuntimeError("R2 blip")
            return Mock(content=b"bytes")

        monkeypatch.setattr("app.core.storage.httpx.get", _fake_get)

        results = download_reference_images_by_index(urls, {1, 2, 3})

        assert set(results) == {1, 3}  # 2번만 빠짐


def _storyboard_with_references(reference_urls: list[str], cut_count: int = 9) -> Storyboard:
    storyboard = Storyboard(id=1)
    storyboard.cuts = _cuts_with_prompts(cut_count)  # cut.id를 order_no로 명시 부여(구분 가능하게)
    storyboard.reference_images = [ReferenceImage(image_url=url) for url in reference_urls]
    return storyboard


class TestBuildReferenceImagesByCut:
    def test_no_references_returns_empty_dict(self):
        """레퍼런스 자체가 없으면 빈 딕셔너리(모든 컷이 빈 리스트로 처리됨)"""
        storyboard = _storyboard_with_references([])
        assert _build_reference_images_by_cut(storyboard, refmap={1: [1]}) == {}

    def test_refmap_none_falls_back_to_shared_capped_set_for_all_cuts(self, monkeypatch):
        """refmap이 None이면 기존 동작(캡 3, 전 컷 공유)으로 폴백하는지"""
        urls = [f"https://pub-x.r2.dev/refs/{i}.png" for i in range(1, 5)]
        storyboard = _storyboard_with_references(urls)
        monkeypatch.setattr(
            "app.core.storage.httpx.get", lambda url, **kwargs: Mock(content=b"bytes")
        )

        result = _build_reference_images_by_cut(storyboard, refmap=None)

        assert len(result) == 9
        shared = next(iter(result.values()))
        assert len(shared) == MAX_MODEL_REFERENCE_IMAGES  # 캡 적용됨
        assert all(v is shared for v in result.values())  # 전 컷 동일 객체 공유

    def test_routes_different_references_per_cut(self, monkeypatch):
        """REFMAP대로 컷마다 다른 레퍼런스 세트가 배정되는지"""
        urls = [f"https://pub-x.r2.dev/refs/{i}.png" for i in range(1, 4)]  # 1,2,3번
        storyboard = _storyboard_with_references(urls)
        bytes_by_index = {1: b"person", 2: b"place", 3: b"prop"}

        def _fake_get(url, **kwargs):
            index = int(url.rsplit("/", 1)[-1].split(".")[0])
            return Mock(content=bytes_by_index[index])

        monkeypatch.setattr("app.core.storage.httpx.get", _fake_get)
        refmap = {1: [1], 2: [2, 3], 3: [], 4: [], 5: [], 6: [], 7: [], 8: [], 9: []}

        result = _build_reference_images_by_cut(storyboard, refmap)

        cuts_by_order = {cut.order_no: cut for cut in storyboard.cuts}
        assert result[cuts_by_order[1].id] == [(b"person", "image/png")]
        assert result[cuts_by_order[2].id] == [(b"place", "image/png"), (b"prop", "image/png")]
        assert result[cuts_by_order[3].id] == []  # 명시적 빈 배정 존중

    def test_missing_cut_in_refmap_falls_back_to_first_n(self, monkeypatch):
        """REFMAP에 컷 번호 자체가 없으면(누락) 앞 N장으로 폴백하는지"""
        urls = [f"https://pub-x.r2.dev/refs/{i}.png" for i in range(1, 3)]  # 1,2번
        storyboard = _storyboard_with_references(urls)
        monkeypatch.setattr(
            "app.core.storage.httpx.get", lambda url, **kwargs: Mock(content=b"bytes")
        )
        # 컷 5번이 refmap에서 통째로 빠짐(누락) — 나머지는 명시적 빈 배정
        refmap = {i: [] for i in range(1, 10) if i != 5}

        result = _build_reference_images_by_cut(storyboard, refmap)

        cuts_by_order = {cut.order_no: cut for cut in storyboard.cuts}
        assert result[cuts_by_order[5].id] == [(b"bytes", "image/png"), (b"bytes", "image/png")]  # 앞 2장 폴백
        assert result[cuts_by_order[1].id] == []  # 명시적 빈 배정은 그대로 빈 리스트

    def test_out_of_range_indices_in_refmap_are_filtered(self, monkeypatch):
        """컷의 REFMAP 인덱스 중 범위 밖(존재하지 않는 번호)은 걸러지고 나머지는 사용되는지"""
        urls = ["https://pub-x.r2.dev/refs/1.png"]
        storyboard = _storyboard_with_references(urls)
        monkeypatch.setattr(
            "app.core.storage.httpx.get", lambda url, **kwargs: Mock(content=b"bytes")
        )
        refmap = {1: [1, 99], 2: [], 3: [], 4: [], 5: [], 6: [], 7: [], 8: [], 9: []}

        result = _build_reference_images_by_cut(storyboard, refmap)

        cuts_by_order = {cut.order_no: cut for cut in storyboard.cuts}
        assert result[cuts_by_order[1].id] == [(b"bytes", "image/png")]  # 99는 걸러지고 1만 남음

    def test_one_broken_reference_only_affects_cuts_that_needed_it(self, monkeypatch):
        """레퍼런스 하나만 다운로드 실패해도, 그 이미지를 안 쓰는 다른 컷들은 여전히 정밀 라우팅되는지
        (배치 전체가 전체 폴백으로 강등되지 않음)"""
        urls = [f"https://pub-x.r2.dev/refs/{i}.png" for i in range(1, 3)]  # 1,2번
        storyboard = _storyboard_with_references(urls)

        def _fake_get(url, **kwargs):
            if url == urls[1]:  # 2번만 실패
                raise RuntimeError("R2 blip")
            return Mock(content=b"person-bytes")

        monkeypatch.setattr("app.core.storage.httpx.get", _fake_get)
        # 컷 1은 정상 1번만 참조, 컷 2는 깨진 2번을 참조
        refmap = {1: [1], 2: [2], 3: [], 4: [], 5: [], 6: [], 7: [], 8: [], 9: []}

        result = _build_reference_images_by_cut(storyboard, refmap)

        cuts_by_order = {cut.order_no: cut for cut in storyboard.cuts}
        assert result[cuts_by_order[1].id] == [(b"person-bytes", "image/png")]  # 깨진 이미지랑 무관, 정상 유지
        assert result[cuts_by_order[2].id] == []  # 깨진 이미지만 조용히 빠짐(전체 폴백 X)

    def test_over_cap_assignment_logs_warning(self, monkeypatch, caplog):
        """컷 하나에 상한(3장)보다 많이 배정되면 뒤쪽부터 잘리면서 경고 로그를 남기는지"""
        urls = [f"https://pub-x.r2.dev/refs/{i}.png" for i in range(1, 5)]  # 1~4번
        storyboard = _storyboard_with_references(urls)
        monkeypatch.setattr("app.core.storage.httpx.get", lambda url, **kwargs: Mock(content=b"bytes"))
        refmap = {1: [1, 2, 3, 4], 2: [], 3: [], 4: [], 5: [], 6: [], 7: [], 8: [], 9: []}

        with caplog.at_level("WARNING"):
            result = _build_reference_images_by_cut(storyboard, refmap)

        cuts_by_order = {cut.order_no: cut for cut in storyboard.cuts}
        assert len(result[cuts_by_order[1].id]) == MAX_MODEL_REFERENCE_IMAGES  # 4장 -> 3장으로 잘림
        assert any("컷당 상한" in record.message for record in caplog.records)

    def test_download_failure_falls_back_to_shared(self, monkeypatch):
        """REFMAP 기반 다운로드 자체가 실패해도 전체 폴백으로 전환해서 9컷이 안 죽는지"""
        urls = ["https://pub-x.r2.dev/refs/1.png"]
        storyboard = _storyboard_with_references(urls)
        monkeypatch.setattr(
            "app.generations.service.download_reference_images_by_index",
            lambda urls, indices: (_ for _ in ()).throw(RuntimeError("R2 down")),
        )
        monkeypatch.setattr(
            "app.generations.service.download_reference_images", lambda urls: [(b"fallback", "image/png")]
        )

        result = _build_reference_images_by_cut(storyboard, refmap={1: [1]})

        assert len(result) == 9
        assert all(v == [(b"fallback", "image/png")] for v in result.values())
