"""storyboard 삭제(delete_storyboard) 테스트.

test_models.py와 동일하게 SQLite FK pragma를 켜서 실제 cascade 동작까지 검증한다.
"""

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.enums import ExportType, Genre, ImageModel, JobStatus
from app.db.base import Base
from app.exports.models import Export
from app.generations.models import Cut, Generation
from app.regenerations.models import Regeneration
from app.storyboards import service
from app.storyboards.models import ReferenceImage, Storyboard


@pytest.fixture
def session_factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)

    @event.listens_for(engine, "connect")
    def _enable_sqlite_fk(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autocommit=False, autoflush=False)


class TestDeleteStoryboard:
    def test_deletes_storyboard_and_cleans_up_all_r2_files(self, monkeypatch, session_factory):
        """DB row(+cascade)가 지워지고, 연결된 R2 URL delete_file 호출되는지"""
        db = session_factory()
        try:
            storyboard = Storyboard(scenario_text="test", genre=Genre.DRAMA, image_model=ImageModel.GPT_IMAGE)
            db.add(storyboard)
            db.flush()

            db.add(ReferenceImage(storyboard_id=storyboard.id, image_url="https://pub-x.r2.dev/ref.png"))
            db.add(
                Generation(
                    storyboard_id=storyboard.id,
                    status=JobStatus.COMPLETED,
                    grid_image_url="https://pub-x.r2.dev/grids/1.jpg",
                )
            )
            for order_no in range(1, 3):
                db.add(
                    Cut(
                        storyboard_id=storyboard.id,
                        order_no=order_no,
                        status=JobStatus.COMPLETED,
                        image_url=f"https://pub-x.r2.dev/cuts/{order_no}.png",
                    )
                )
            db.add(
                Export(
                    storyboard_id=storyboard.id,
                    type=ExportType.PDF,
                    status=JobStatus.COMPLETED,
                    download_url="https://pub-x.r2.dev/export-pdfs/1.pdf",
                )
            )
            db.commit()
            storyboard_id = storyboard.id

            deleted_urls = []
            monkeypatch.setattr(service.storage, "delete_file", lambda url: deleted_urls.append(url))

            service.delete_storyboard(db, storyboard_id)

            assert db.get(Storyboard, storyboard_id) is None
            assert set(deleted_urls) == {
                "https://pub-x.r2.dev/ref.png",
                "https://pub-x.r2.dev/grids/1.jpg",
                "https://pub-x.r2.dev/cuts/1.png",
                "https://pub-x.r2.dev/cuts/2.png",
                "https://pub-x.r2.dev/export-pdfs/1.pdf",
            }
        finally:
            db.close()

    def test_raises_when_storyboard_not_found(self, monkeypatch, session_factory):
        """존재하지 않는 storyboard_id면 StoryboardNotFound, R2 삭제 시도도 X"""
        db = session_factory()
        try:
            deleted_urls = []
            monkeypatch.setattr(service.storage, "delete_file", lambda url: deleted_urls.append(url))

            with pytest.raises(service.StoryboardNotFound):
                service.delete_storyboard(db, 999)

            assert deleted_urls == []
        finally:
            db.close()

    def test_deletes_cleanly_when_generation_or_exports_missing(self, monkeypatch, session_factory):
        """아직 generation/export가 없는(생성 직후) storyboard도 에러 없이 삭제되는지"""
        db = session_factory()
        try:
            storyboard = Storyboard(scenario_text="test", genre=Genre.DRAMA, image_model=ImageModel.GPT_IMAGE)
            db.add(storyboard)
            db.commit()
            storyboard_id = storyboard.id

            deleted_urls = []
            monkeypatch.setattr(service.storage, "delete_file", lambda url: deleted_urls.append(url))

            service.delete_storyboard(db, storyboard_id)

            assert db.get(Storyboard, storyboard_id) is None
            assert deleted_urls == []
        finally:
            db.close()


class TestDeleteStoryboardGuards:
    """진행 중인 생성/재생성/Export가 있으면 삭제 자체가 막히는지."""

    @pytest.mark.parametrize("status", [JobStatus.PENDING, JobStatus.PROCESSING])
    def test_raises_when_generation_in_progress(self, monkeypatch, session_factory, status):
        db = session_factory()
        try:
            storyboard = Storyboard(scenario_text="test", genre=Genre.DRAMA, image_model=ImageModel.GPT_IMAGE)
            db.add(storyboard)
            db.flush()
            db.add(Generation(storyboard_id=storyboard.id, status=status))
            db.commit()
            storyboard_id = storyboard.id

            deleted_urls = []
            monkeypatch.setattr(service.storage, "delete_file", lambda url: deleted_urls.append(url))

            with pytest.raises(service.GenerationInProgress):
                service.delete_storyboard(db, storyboard_id)

            assert db.get(Storyboard, storyboard_id) is not None
            assert deleted_urls == []
        finally:
            db.close()

    @pytest.mark.parametrize("status", [JobStatus.PENDING, JobStatus.PROCESSING])
    def test_raises_when_regeneration_in_progress(self, monkeypatch, session_factory, status):
        db = session_factory()
        try:
            storyboard = Storyboard(scenario_text="test", genre=Genre.DRAMA, image_model=ImageModel.GPT_IMAGE)
            db.add(storyboard)
            db.flush()
            db.add(Generation(storyboard_id=storyboard.id, status=JobStatus.COMPLETED))
            cut = Cut(
                storyboard_id=storyboard.id,
                order_no=1,
                status=JobStatus.COMPLETED,
                image_url="https://pub-x.r2.dev/cuts/1.png",
            )
            db.add(cut)
            db.flush()
            db.add(Regeneration(cut_id=cut.id, status=status))
            db.commit()
            storyboard_id = storyboard.id

            deleted_urls = []
            monkeypatch.setattr(service.storage, "delete_file", lambda url: deleted_urls.append(url))

            with pytest.raises(service.RegenerationInProgress):
                service.delete_storyboard(db, storyboard_id)

            assert db.get(Storyboard, storyboard_id) is not None
            assert deleted_urls == []
        finally:
            db.close()

    @pytest.mark.parametrize("status", [JobStatus.PENDING, JobStatus.PROCESSING])
    def test_raises_when_export_in_progress(self, monkeypatch, session_factory, status):
        db = session_factory()
        try:
            storyboard = Storyboard(scenario_text="test", genre=Genre.DRAMA, image_model=ImageModel.GPT_IMAGE)
            db.add(storyboard)
            db.flush()
            db.add(Generation(storyboard_id=storyboard.id, status=JobStatus.COMPLETED))
            db.add(Export(storyboard_id=storyboard.id, type=ExportType.PDF, status=status))
            db.commit()
            storyboard_id = storyboard.id

            deleted_urls = []
            monkeypatch.setattr(service.storage, "delete_file", lambda url: deleted_urls.append(url))

            with pytest.raises(service.ExportInProgress):
                service.delete_storyboard(db, storyboard_id)

            assert db.get(Storyboard, storyboard_id) is not None
            assert deleted_urls == []
        finally:
            db.close()
