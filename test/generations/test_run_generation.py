"""run_generation 오케스트레이션 자체(커밋 순서, 실패 처리, catch-all) 테스트.

test_service.py는 순수 헬퍼 함수만 다루고 run_generation 본체는 다루지 않아서,
실제 SQLite 세션으로 커밋/롤백까지 포함한 전체 흐름을 검증하기 위해 파일을 분리함.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.ai.base import GeneratedImage
from app.core.enums import Genre, ImageModel, JobStatus
from app.db.base import Base
from app.generations import service
from app.generations.models import Cut, Generation
from app.storyboards.models import ReferenceImage, Storyboard


@pytest.fixture
def session_factory():
    """테스트 하는동안 쓰이는 임시 DB"""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autocommit=False, autoflush=False)


@pytest.fixture(autouse=True)
def _patch_session_local(monkeypatch, session_factory):
    """테스트 하는동안만 위에서 만든 팩토리로 잠깐 바꿔치기, 끝나면 자동 복구"""
    monkeypatch.setattr(service, "SessionLocal", session_factory)


def _create_storyboard(session_factory, reference_image_urls: list[str] | None = None) -> int:
    """스토리보드 생성 함수 대충 흉내"""
    db = session_factory()
    try:
        storyboard = Storyboard(
            scenario_text="a hero saves the day", genre=Genre.ACTION, image_model=ImageModel.GPT_IMAGE
        )
        db.add(storyboard)
        db.flush()

        db.add(Generation(storyboard_id=storyboard.id, status=JobStatus.PENDING))
        for order_no in range(1, 10):
            db.add(Cut(storyboard_id=storyboard.id, order_no=order_no, status=JobStatus.PENDING))
        for url in reference_image_urls or []:
            db.add(ReferenceImage(storyboard_id=storyboard.id, image_url=url))

        db.commit()
        return storyboard.id
    finally:
        db.close()


def _load(session_factory, storyboard_id):
    """테스트 끝나고 지금 DB에 뭐 들었는지 다시 읽음"""
    db = session_factory()
    try:
        storyboard = db.get(Storyboard, storyboard_id)
        return storyboard.generation, list(storyboard.cuts)
    finally:
        db.close()


def _integrated_prompt() -> str:
    return "\n".join(f"Shot {n}: description for shot {n} with enough detail" for n in range(1, 10))


# Claude가 이렇게 응답했다고 치자고 흉내낸것(fake)
class _FakePromptAdapter:
    def __init__(self, *_args, **_kwargs):
        pass

    def generate_prompt(self, **_kwargs) -> str:
        return _integrated_prompt()


class _FakeMalformedPromptAdapter:
    def __init__(self, *_args, **_kwargs):
        pass

    def generate_prompt(self, **_kwargs) -> str:
        return "not a valid integrated prompt"


class _FakeImageAdapter:
    def __init__(self):
        self.calls = 0
        self.reference_images_by_call: list[list | None] = []
        # (prompt_text, reference_images) 쌍 — 스레드풀 실행 순서와 무관하게 어느 컷 호출인지 상관관계 확인용
        self.calls_detail: list[tuple[str, list]] = []

    def generate_image(self, *, prompt_text, aspect_ratio=None, reference_images=None) -> GeneratedImage:
        self.calls += 1
        self.reference_images_by_call.append(reference_images)
        self.calls_detail.append((prompt_text, reference_images))
        url = f"https://pub-x.r2.dev/cuts/{self.calls}.png"
        return GeneratedImage(url=url, data=url.encode(), content_type="image/png")


class _FakeRefmapPromptAdapter:
    """Claude가 9컷 + REFMAP 줄까지 같이 응답했다고 치는 fake"""

    def __init__(self, refmap_line: str):
        self._refmap_line = refmap_line

    def generate_prompt(self, **_kwargs) -> str:
        return _integrated_prompt() + "\n" + self._refmap_line


class TestRunGenerationHappyPath:
    """Claude, 이미지 9장, 그리드 전부 성공"""
    def test_completes_generation_and_all_cuts_with_grid_image(self, monkeypatch, session_factory):
        monkeypatch.setattr(service, "ClaudePromptAdapter", _FakePromptAdapter)
        monkeypatch.setattr(service, "get_image_adapter", lambda image_model: _FakeImageAdapter())
        monkeypatch.setattr(service, "build_grid_image", lambda urls, known_image_bytes=None: b"fake-grid-bytes")
        monkeypatch.setattr(
            service.storage,
            "upload_image_bytes",
            lambda data, content_type, folder: "https://pub-x.r2.dev/grids/fake.png",
        )

        storyboard_id = _create_storyboard(session_factory)
        service.run_generation(storyboard_id)

        generation, cuts = _load(session_factory, storyboard_id)
        assert generation.status == JobStatus.COMPLETED
        assert generation.grid_image_url == "https://pub-x.r2.dev/grids/fake.png"
        assert len(cuts) == 9
        assert all(cut.status == JobStatus.COMPLETED for cut in cuts)
        assert all(cut.image_url for cut in cuts)


class TestRunGenerationWithReferenceImages:
    def test_downloads_references_once_and_shares_across_all_cuts(self, monkeypatch, session_factory):
        """레퍼런스가 있으면 한 번만 다운로드해서 9개 컷 호출 전부에 같은 객체를 공유하는지"""
        monkeypatch.setattr(service, "ClaudePromptAdapter", _FakePromptAdapter)
        image_adapter = _FakeImageAdapter()
        monkeypatch.setattr(service, "get_image_adapter", lambda image_model: image_adapter)
        monkeypatch.setattr(service, "build_grid_image", lambda urls, known_image_bytes=None: b"fake-grid-bytes")
        monkeypatch.setattr(
            service.storage,
            "upload_image_bytes",
            lambda data, content_type, folder: "https://pub-x.r2.dev/grids/fake.png",
        )
        download_calls = []

        def _fake_download_reference_images(urls):
            download_calls.append(urls)
            return [(b"ref-bytes", "image/png")]

        monkeypatch.setattr(service, "download_reference_images", _fake_download_reference_images)

        storyboard_id = _create_storyboard(session_factory, reference_image_urls=["https://pub-x.r2.dev/refs/a.png"])
        service.run_generation(storyboard_id)

        assert download_calls == [["https://pub-x.r2.dev/refs/a.png"]]
        assert len(image_adapter.reference_images_by_call) == 9
        assert all(refs == [(b"ref-bytes", "image/png")] for refs in image_adapter.reference_images_by_call)

        generation, _cuts = _load(session_factory, storyboard_id)
        assert generation.status == JobStatus.COMPLETED

    def test_download_failure_falls_back_to_no_reference_instead_of_failing_all_cuts(
        self, monkeypatch, session_factory
    ):
        """레퍼런스 다운로드 자체가 실패해도(R2 순단 등) 9컷 전체를 죽이지 않고 텍스트 기반으로 진행하는지"""
        monkeypatch.setattr(service, "ClaudePromptAdapter", _FakePromptAdapter)
        image_adapter = _FakeImageAdapter()
        monkeypatch.setattr(service, "get_image_adapter", lambda image_model: image_adapter)
        monkeypatch.setattr(service, "build_grid_image", lambda urls, known_image_bytes=None: b"fake-grid-bytes")
        monkeypatch.setattr(
            service.storage,
            "upload_image_bytes",
            lambda data, content_type, folder: "https://pub-x.r2.dev/grids/fake.png",
        )

        def _boom(urls):
            raise RuntimeError("R2 network blip")

        monkeypatch.setattr(service, "download_reference_images", _boom)

        storyboard_id = _create_storyboard(session_factory, reference_image_urls=["https://pub-x.r2.dev/refs/a.png"])
        service.run_generation(storyboard_id)

        assert all(refs == [] for refs in image_adapter.reference_images_by_call)
        generation, cuts = _load(session_factory, storyboard_id)
        assert generation.status == JobStatus.COMPLETED
        assert all(cut.status == JobStatus.COMPLETED for cut in cuts)


class TestRunGenerationWithRefmap:
    def _setup(self, monkeypatch, refmap_line, bytes_by_url):
        monkeypatch.setattr(service, "ClaudePromptAdapter", lambda: _FakeRefmapPromptAdapter(refmap_line))
        image_adapter = _FakeImageAdapter()
        monkeypatch.setattr(service, "get_image_adapter", lambda image_model: image_adapter)
        monkeypatch.setattr(service, "build_grid_image", lambda urls, known_image_bytes=None: b"fake-grid-bytes")
        monkeypatch.setattr(
            service.storage,
            "upload_image_bytes",
            lambda data, content_type, folder: "https://pub-x.r2.dev/grids/fake.png",
        )
        monkeypatch.setattr(service.storage, "download_bytes", lambda url: bytes_by_url[url])
        return image_adapter

    def test_routes_different_references_per_cut(self, monkeypatch, session_factory):
        """REFMAP대로 컷마다 다른 레퍼런스가 실제 이미지 생성 호출에 전달되는지"""
        bytes_by_url = {
            "https://pub-x.r2.dev/refs/1.png": b"person",
            "https://pub-x.r2.dev/refs/2.png": b"place",
        }
        refmap_line = "REFMAP: 1=[1]; 2=[2]; 3=[]; 4=[]; 5=[]; 6=[]; 7=[]; 8=[]; 9=[]"
        image_adapter = self._setup(monkeypatch, refmap_line, bytes_by_url)

        storyboard_id = _create_storyboard(session_factory, reference_image_urls=list(bytes_by_url))
        service.run_generation(storyboard_id)

        generation, _cuts = _load(session_factory, storyboard_id)
        assert generation.status == JobStatus.COMPLETED

        calls_by_prompt = dict(image_adapter.calls_detail)
        assert calls_by_prompt["description for shot 1 with enough detail"] == [(b"person", "image/png")]
        assert calls_by_prompt["description for shot 2 with enough detail"] == [(b"place", "image/png")]
        # REFMAP에서 빈 배정(명시적)된 컷은 레퍼런스 없이 텍스트만
        assert calls_by_prompt["description for shot 3 with enough detail"] == []

    def test_missing_cut_falls_back_to_first_n_others_stay_routed(self, monkeypatch, session_factory):
        """REFMAP에서 특정 컷 번호가 누락되면 그 컷만 앞 N장 폴백, 나머지는 정상 라우팅 유지"""
        bytes_by_url = {
            "https://pub-x.r2.dev/refs/1.png": b"person",
            "https://pub-x.r2.dev/refs/2.png": b"place",
        }
        # 컷 3이 REFMAP에서 통째로 빠짐(나머지 8개는 명시)
        refmap_line = "REFMAP: 1=[1]; 2=[2]; 4=[]; 5=[]; 6=[]; 7=[]; 8=[]; 9=[]"
        image_adapter = self._setup(monkeypatch, refmap_line, bytes_by_url)

        storyboard_id = _create_storyboard(session_factory, reference_image_urls=list(bytes_by_url))
        service.run_generation(storyboard_id)

        generation, _cuts = _load(session_factory, storyboard_id)
        assert generation.status == JobStatus.COMPLETED

        calls_by_prompt = dict(image_adapter.calls_detail)
        # 컷 3은 누락됐으니 앞 2장(1,2번 다) 폴백
        assert calls_by_prompt["description for shot 3 with enough detail"] == [
            (b"person", "image/png"), (b"place", "image/png"),
        ]
        # 나머지는 REFMAP 그대로
        assert calls_by_prompt["description for shot 1 with enough detail"] == [(b"person", "image/png")]
        assert calls_by_prompt["description for shot 4 with enough detail"] == []


class TestRunGenerationPromptFailure:
    def test_marks_generation_and_all_cuts_failed_without_calling_image_adapter(self, monkeypatch, session_factory):
        """Claude가 MAX_PROMPT_ATTEMPTS 내내 형식 오류를 내면, 이미지 생성은 시도조차 안 하고
        전체 실패로 확정되는지."""
        monkeypatch.setattr(service, "ClaudePromptAdapter", _FakeMalformedPromptAdapter)
        image_adapter = _FakeImageAdapter()
        monkeypatch.setattr(service, "get_image_adapter", lambda image_model: image_adapter)

        storyboard_id = _create_storyboard(session_factory)
        service.run_generation(storyboard_id)

        generation, cuts = _load(session_factory, storyboard_id)
        assert generation.status == JobStatus.FAILED
        assert generation.error_message is not None
        assert all(cut.status == JobStatus.FAILED for cut in cuts)
        assert image_adapter.calls == 0


class TestRunGenerationUnexpectedException:
    def test_catch_all_marks_generation_failed_without_reverting_completed_cuts(
        self, monkeypatch, session_factory
    ):
        """그리드 합성 단계에서 예상 못한 예외가 나도(9컷 이미지 생성 자체는 이미 성공한 상태)
        catch-all이 걸려서 generation은 FAILED로 확정되고, 이미 완료된 컷 상태는 안 건드리는지."""
        monkeypatch.setattr(service, "ClaudePromptAdapter", _FakePromptAdapter)
        monkeypatch.setattr(service, "get_image_adapter", lambda image_model: _FakeImageAdapter())

        def _boom(urls, known_image_bytes=None):
            raise RuntimeError("grid build failed")

        monkeypatch.setattr(service, "build_grid_image", _boom)

        storyboard_id = _create_storyboard(session_factory)
        service.run_generation(storyboard_id)

        generation, cuts = _load(session_factory, storyboard_id)
        assert generation.status == JobStatus.FAILED
        assert generation.error_message == "grid build failed"
        assert all(cut.status == JobStatus.COMPLETED for cut in cuts)
        assert all(cut.image_url for cut in cuts)


class TestRecoverStuckGenerations:
    def test_marks_pending_and_processing_as_failed(self, session_factory):
        """서버 재시작 후 pending/processing으로 남아있던 job이 failed로 정리되는지,
        이미 completed인 건 안 건드리는지."""
        db = session_factory()
        try:
            stuck = Storyboard(scenario_text="stuck", genre=Genre.DRAMA, image_model=ImageModel.GPT_IMAGE)
            done = Storyboard(scenario_text="done", genre=Genre.DRAMA, image_model=ImageModel.GPT_IMAGE)
            db.add_all([stuck, done])
            db.flush()

            db.add(Generation(storyboard_id=stuck.id, status=JobStatus.PROCESSING))
            db.add(Cut(storyboard_id=stuck.id, order_no=1, status=JobStatus.PROCESSING))

            db.add(Generation(storyboard_id=done.id, status=JobStatus.COMPLETED))
            db.add(Cut(storyboard_id=done.id, order_no=1, status=JobStatus.COMPLETED))
            db.commit()

            recovered = service.recover_stuck_generations(db)
            assert recovered == 1

            stuck_generation, stuck_cuts = _load(session_factory, stuck.id)
            assert stuck_generation.status == JobStatus.FAILED
            assert stuck_generation.error_message is not None
            assert all(cut.status == JobStatus.FAILED for cut in stuck_cuts)

            done_generation, done_cuts = _load(session_factory, done.id)
            assert done_generation.status == JobStatus.COMPLETED
            assert all(cut.status == JobStatus.COMPLETED for cut in done_cuts)
        finally:
            db.close()
