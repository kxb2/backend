"""run_regeneration 오케스트레이션 자체(앵커 전달, 그리드 재조립, 실패 처리) 테스트.

test/generations/test_run_generation.py와 동일한 SQLite 픽스처 패턴으로 작성.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.ai.base import GeneratedImage
from app.core.enums import Genre, ImageModel, JobStatus
from app.db.base import Base
from app.generations.models import Cut, Generation
from app.regenerations import service
from app.regenerations.models import Regeneration
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


def _create_storyboard_with_regeneration(
    session_factory, *, reference_image_urls: list[str] | None = None
) -> tuple[int, int, int]:
    """9컷 전부 COMPLETED인 스토리보드 + 컷1에 대한 PENDING 재생성 요청을 만들어둠.

    반환: (storyboard_id, cut_id(재생성 대상=컷1), regeneration_id)
    """
    db = session_factory()
    try:
        storyboard = Storyboard(
            scenario_text="a hero saves the day", genre=Genre.ACTION, image_model=ImageModel.GPT_IMAGE
        )
        db.add(storyboard)
        db.flush()

        db.add(Generation(storyboard_id=storyboard.id, status=JobStatus.COMPLETED))
        target_cut_id = None
        for order_no in range(1, 10):
            cut = Cut(
                storyboard_id=storyboard.id,
                order_no=order_no,
                prompt_text=f"prompt-{order_no}",
                image_url=f"https://pub-x.r2.dev/cuts/old-{order_no}.png",
                status=JobStatus.COMPLETED,
            )
            db.add(cut)
            db.flush()
            if order_no == 1:
                target_cut_id = cut.id
        for url in reference_image_urls or []:
            db.add(ReferenceImage(storyboard_id=storyboard.id, image_url=url))

        regeneration = Regeneration(cut_id=target_cut_id, status=JobStatus.PENDING)
        db.add(regeneration)
        db.commit()
        return storyboard.id, target_cut_id, regeneration.id
    finally:
        db.close()


def _load_regeneration(session_factory, regeneration_id):
    db = session_factory()
    try:
        return db.get(Regeneration, regeneration_id)
    finally:
        db.close()


def _load_cut(session_factory, cut_id):
    db = session_factory()
    try:
        return db.get(Cut, cut_id)
    finally:
        db.close()


def _load_generation(session_factory, storyboard_id):
    db = session_factory()
    try:
        storyboard = db.get(Storyboard, storyboard_id)
        return storyboard.generation
    finally:
        db.close()


class _FakeImageAdapter:
    def __init__(self, result: GeneratedImage | Exception):
        self._result = result
        self.calls: list[dict] = []

    def generate_image(self, *, prompt_text, aspect_ratio=None, reference_images=None) -> GeneratedImage:
        self.calls.append(
            {"prompt_text": prompt_text, "aspect_ratio": aspect_ratio, "reference_images": reference_images}
        )
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def _new_image() -> GeneratedImage:
    return GeneratedImage(url="https://pub-x.r2.dev/cuts/new.png", data=b"new-bytes", content_type="image/png")


class TestRunRegenerationHappyPath:
    def test_replaces_cut_image_and_completes(self, monkeypatch, session_factory):
        """성공하면 컷 이미지가 새 URL로 바뀌고, 재생성/컷 상태가 COMPLETED로 확정되는지"""
        adapter = _FakeImageAdapter(_new_image())
        monkeypatch.setattr(service, "get_image_adapter", lambda image_model: adapter)
        deleted_urls = []
        monkeypatch.setattr(service.storage, "delete_file", lambda url: deleted_urls.append(url))
        grid_calls = []
        monkeypatch.setattr(
            service,
            "build_grid_image",
            lambda urls, known_image_bytes=None: grid_calls.append((urls, known_image_bytes)) or b"grid-bytes",
        )
        monkeypatch.setattr(
            service.storage,
            "upload_image_bytes",
            lambda data, content_type, folder: "https://pub-x.r2.dev/grids/new.png",
        )

        storyboard_id, cut_id, regeneration_id = _create_storyboard_with_regeneration(session_factory)
        service.run_regeneration(regeneration_id)

        regeneration = _load_regeneration(session_factory, regeneration_id)
        cut = _load_cut(session_factory, cut_id)
        assert regeneration.status == JobStatus.COMPLETED
        assert regeneration.image_url == "https://pub-x.r2.dev/cuts/new.png"
        assert cut.status == JobStatus.COMPLETED
        assert cut.image_url == "https://pub-x.r2.dev/cuts/new.png"
        assert deleted_urls == ["https://pub-x.r2.dev/cuts/old-1.png"]

        generation = _load_generation(session_factory, storyboard_id)
        assert generation.grid_image_url == "https://pub-x.r2.dev/grids/new.png"

    def test_grid_rebuild_skips_redownload_of_regenerated_cut(self, monkeypatch, session_factory):
        """그리드 재조립 시 방금 재생성한 컷의 바이트는 known_image_bytes로 넘어가 재다운로드 대상에서 빠지는지"""
        adapter = _FakeImageAdapter(_new_image())
        monkeypatch.setattr(service, "get_image_adapter", lambda image_model: adapter)
        monkeypatch.setattr(service.storage, "delete_file", lambda url: None)
        monkeypatch.setattr(
            service.storage,
            "upload_image_bytes",
            lambda data, content_type, folder: "https://pub-x.r2.dev/grids/new.png",
        )
        grid_calls = []
        monkeypatch.setattr(
            service,
            "build_grid_image",
            lambda urls, known_image_bytes=None: grid_calls.append((urls, known_image_bytes)) or b"grid-bytes",
        )

        _storyboard_id, _cut_id, regeneration_id = _create_storyboard_with_regeneration(session_factory)
        service.run_regeneration(regeneration_id)

        assert len(grid_calls) == 1
        _urls, known_image_bytes = grid_calls[0]
        assert known_image_bytes == {"https://pub-x.r2.dev/cuts/new.png": b"new-bytes"}


class TestRunRegenerationReferenceAnchor:
    def test_downloads_and_passes_storyboard_reference_images_as_anchor(self, monkeypatch, session_factory):
        """스토리보드에 레퍼런스가 있으면 다운로드해서 image_adapter 호출에 앵커로 전달하는지"""
        adapter = _FakeImageAdapter(_new_image())
        monkeypatch.setattr(service, "get_image_adapter", lambda image_model: adapter)
        monkeypatch.setattr(service.storage, "delete_file", lambda url: None)
        monkeypatch.setattr(
            service.storage,
            "upload_image_bytes",
            lambda data, content_type, folder: "https://pub-x.r2.dev/grids/new.png",
        )
        monkeypatch.setattr(service, "build_grid_image", lambda urls, known_image_bytes=None: b"grid-bytes")
        download_calls = []

        def _fake_download_reference_images(urls):
            download_calls.append(urls)
            return [(b"ref-bytes", "image/png")]

        monkeypatch.setattr(service, "download_reference_images", _fake_download_reference_images)

        _storyboard_id, _cut_id, regeneration_id = _create_storyboard_with_regeneration(
            session_factory, reference_image_urls=["https://pub-x.r2.dev/refs/a.png"]
        )
        service.run_regeneration(regeneration_id)

        assert download_calls == [["https://pub-x.r2.dev/refs/a.png"]]
        assert adapter.calls[0]["reference_images"] == [(b"ref-bytes", "image/png")]

    def test_no_reference_storyboard_passes_empty_anchor(self, monkeypatch, session_factory):
        """레퍼런스 없는 스토리보드는 빈 리스트가 전달되는지(지금과 동작 동일)"""
        adapter = _FakeImageAdapter(_new_image())
        monkeypatch.setattr(service, "get_image_adapter", lambda image_model: adapter)
        monkeypatch.setattr(service.storage, "delete_file", lambda url: None)
        monkeypatch.setattr(
            service.storage,
            "upload_image_bytes",
            lambda data, content_type, folder: "https://pub-x.r2.dev/grids/new.png",
        )
        monkeypatch.setattr(service, "build_grid_image", lambda urls, known_image_bytes=None: b"grid-bytes")

        _storyboard_id, _cut_id, regeneration_id = _create_storyboard_with_regeneration(session_factory)
        service.run_regeneration(regeneration_id)

        assert adapter.calls[0]["reference_images"] == []


class TestRunRegenerationFailure:
    def test_adapter_error_marks_regeneration_failed_without_touching_cut(self, monkeypatch, session_factory):
        """이미지 생성 실패 시 재생성만 FAILED로 기록되고, 컷의 기존 이미지/상태는 그대로인지"""
        adapter = _FakeImageAdapter(RuntimeError("image gen failed"))
        monkeypatch.setattr(service, "get_image_adapter", lambda image_model: adapter)

        storyboard_id, cut_id, regeneration_id = _create_storyboard_with_regeneration(session_factory)
        service.run_regeneration(regeneration_id)

        regeneration = _load_regeneration(session_factory, regeneration_id)
        cut = _load_cut(session_factory, cut_id)
        assert regeneration.status == JobStatus.FAILED
        assert regeneration.error_message == "image gen failed"
        assert cut.status == JobStatus.COMPLETED
        assert cut.image_url == "https://pub-x.r2.dev/cuts/old-1.png"
