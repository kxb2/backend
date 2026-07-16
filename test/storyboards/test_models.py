"""Storyboard 삭제 시 cuts/generation/reference_images가 cascade로 같이 지워지는지 테스트.

SQLite는 기본적으로 FK 제약(과 그에 딸린 ON DELETE CASCADE)을 강제하지 않아서,
PRAGMA foreign_keys=ON을 명시적으로 켜야 실제 프로덕션(Postgres)과 동일하게 검증 가능.
"""

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.enums import Genre, ImageModel, JobStatus
from app.db.base import Base
from app.generations.models import Cut, Generation
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


def test_deleting_storyboard_cascades_to_cuts_generation_and_reference_images(session_factory):
    """db.delete(storyboard)가 NOT NULL 위반 없이 성공하고, 자식 행들도 같이 지워지는지"""
    db = session_factory()
    try:
        storyboard = Storyboard(scenario_text="test", genre=Genre.DRAMA, image_model=ImageModel.GPT_IMAGE)
        db.add(storyboard)
        db.flush()

        db.add(ReferenceImage(storyboard_id=storyboard.id, image_url="https://pub-x.r2.dev/ref.png"))
        db.add(Generation(storyboard_id=storyboard.id, status=JobStatus.COMPLETED))
        for order_no in range(1, 10):
            db.add(Cut(storyboard_id=storyboard.id, order_no=order_no, status=JobStatus.COMPLETED))
        db.commit()

        storyboard_id = storyboard.id
        db.delete(storyboard)
        db.commit()  # passive_deletes=True가 없으면 여기서 NotNullViolation로 실패했던 부분

        assert db.get(Storyboard, storyboard_id) is None
        assert db.query(Cut).filter(Cut.storyboard_id == storyboard_id).count() == 0
        assert db.query(Generation).filter(Generation.storyboard_id == storyboard_id).count() == 0
        assert db.query(ReferenceImage).filter(ReferenceImage.storyboard_id == storyboard_id).count() == 0
    finally:
        db.close()
