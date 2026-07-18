"""run_pdf_export 오케스트레이션 자체(커밋 순서, 실패 처리, catch-all) 테스트.

test_service.py는 순수 헬퍼 함수(_build_pdf_export)만 다루고 run_pdf_export 본체는 다루지 않아서,
실제 SQLite 세션으로 커밋/롤백까지 포함한 전체 흐름을 검증하기 위해 파일을 분리함
(test/exports/test_run_image_export.py와 동일한 패턴).
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.enums import ExportType, Genre, ImageModel, JobStatus
from app.db.base import Base
from app.exports import service
from app.exports.models import Export
from app.generations.models import Cut, Generation
from app.storyboards.models import Storyboard


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


def _create_completed_storyboard(session_factory) -> int:
    """9컷 생성까지 이미 완료된 스토리보드를 흉내냄"""
    db = session_factory()
    try:
        storyboard = Storyboard(
            scenario_text="a hero saves the day", genre=Genre.ACTION, image_model=ImageModel.GPT_IMAGE
        )
        db.add(storyboard)
        db.flush()

        db.add(
            Generation(
                storyboard_id=storyboard.id,
                status=JobStatus.COMPLETED,
                grid_image_url="https://pub-x.r2.dev/grids/1.png",
            )
        )
        for order_no in range(1, 10):
            db.add(
                Cut(
                    storyboard_id=storyboard.id,
                    order_no=order_no,
                    status=JobStatus.COMPLETED,
                    image_url=f"https://pub-x.r2.dev/cuts/{order_no}.png",
                    prompt_text=f"description for shot {order_no}",
                )
            )

        db.commit()
        return storyboard.id
    finally:
        db.close()


def _create_export(session_factory, storyboard_id: int) -> int:
    db = session_factory()
    try:
        export = Export(storyboard_id=storyboard_id, type=ExportType.PDF, status=JobStatus.PENDING)
        db.add(export)
        db.commit()
        return export.id
    finally:
        db.close()


def _load_export(session_factory, export_id: int) -> Export:
    db = session_factory()
    try:
        return db.get(Export, export_id)
    finally:
        db.close()


class TestRunPdfExport:
    def test_builds_pdf_and_uploads_it(self, monkeypatch, session_factory):
        """PDF를 새로 만들어 업로드하고, 그 URL이 download_url로 반영되는지"""
        monkeypatch.setattr(service, "_build_pdf_export", lambda cuts: b"fake-pdf-bytes")
        upload_calls = []
        monkeypatch.setattr(
            service.storage,
            "upload_bytes",
            lambda data, key, content_type, filename=None: upload_calls.append(filename)
            or "https://pub-x.r2.dev/export-pdfs/fake.pdf",
        )

        storyboard_id = _create_completed_storyboard(session_factory)
        export_id = _create_export(session_factory, storyboard_id)

        service.run_pdf_export(export_id)

        export = _load_export(session_factory, export_id)
        assert export.status == JobStatus.COMPLETED
        assert export.download_url == "https://pub-x.r2.dev/export-pdfs/fake.pdf"
        assert upload_calls == [f"storyboard_{storyboard_id}.pdf"]


class TestRunPdfExportFailure:
    def test_catch_all_marks_export_failed(self, monkeypatch, session_factory):
        """예상 못한 예외가 나도 PROCESSING에 멈추지 않고 최종적으로 FAILED로 확정되는지"""

        def _boom(cuts):
            raise RuntimeError("pdf build failed")

        monkeypatch.setattr(service, "_build_pdf_export", _boom)

        storyboard_id = _create_completed_storyboard(session_factory)
        export_id = _create_export(session_factory, storyboard_id)

        service.run_pdf_export(export_id)

        export = _load_export(session_factory, export_id)
        assert export.status == JobStatus.FAILED
        assert export.download_url is None
        assert export.error_message == "pdf build failed"
