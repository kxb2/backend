import logging

from fastapi import UploadFile
from sqlalchemy.orm import Session

from app.core import storage
from app.core.constants import CUT_COUNT
from app.core.enums import Genre, ImageModel, JobStatus
from app.exports.models import Export
from app.generations.models import Cut, Generation
from app.storyboards.models import ReferenceImage, Storyboard

logger = logging.getLogger(__name__)

MAX_REFERENCE_IMAGES = 10


class ReferenceImageLimitExceeded(Exception):
    def __init__(self, limit: int):
        self.limit = limit


class StoryboardNotFound(Exception):
    """존재하지 않는 storyboard_id로 요청한 경우"""


def create_storyboard(
    db: Session,
    *,
    scenario_text: str,
    genre: Genre,
    style: str | None,
    tone: str | None,
    aspect_ratio: str | None,
    era: str | None,
    image_model: ImageModel,
    reference_images: list[UploadFile],
) -> tuple[Storyboard, Generation]:
    """스토리보드 생성"""
    if len(reference_images) > MAX_REFERENCE_IMAGES:
        raise ReferenceImageLimitExceeded(MAX_REFERENCE_IMAGES)

    # R2 업로드 전에 전체 파일을 먼저 검증 (하나라도 형식/용량 문제면 업로드 자체를 하지 않음)
    reference_data = [(storage.validate_image(image), image.content_type) for image in reference_images]

    storyboard = Storyboard(
        scenario_text=scenario_text,
        genre=genre,
        style=style,
        tone=tone,
        aspect_ratio=aspect_ratio,
        era=era,
        image_model=image_model,
    )
    db.add(storyboard)
    db.flush()

    uploaded_urls = storage.upload_images_parallel(reference_data, folder="reference-images")

    try:
        for image_url in uploaded_urls:
            db.add(ReferenceImage(storyboard_id=storyboard.id, image_url=image_url))

        generation = Generation(storyboard_id=storyboard.id, status=JobStatus.PENDING)
        db.add(generation)
        db.flush()

        for order_no in range(1, CUT_COUNT + 1):
            db.add(Cut(storyboard_id=storyboard.id, order_no=order_no, status=JobStatus.PENDING))

        db.commit()
    except Exception:
        db.rollback()
        # DB 저장 실패해도 R2 업로드는 이미 끝난 상태라 R2 고아 안남도록 정리하는것
        # ㅡ 파일 하나 삭제가 실패해도 나머지는 계속 정리 시도 + 원래 DB 에러가 묻히지 않게 raise로 유지
        for url in uploaded_urls:
            try:
                storage.delete_file(url)  # DB 저장 실패했으니까 성공한 R2 업로드도 버리기
            except Exception:
                logger.exception("스토리보드 생성 실패 롤백 중 참조이미지 삭제 실패 (url=%s)", url)
        raise

    return storyboard, generation


def get_storyboard(db: Session, storyboard_id: int) -> Storyboard | None:
    """스토리보드 조회"""
    return db.get(Storyboard, storyboard_id)


def delete_storyboard(db: Session, storyboard_id: int) -> None:
    """스토리보드 삭제(개발용으로 일단 만들)

    ㅡ 순서 중요: DB 삭제 → R2 삭제 (DB 깨지는것보다 R2 고아 남는게 나음)
    ㅡ URL은 storyboard.cuts 등 관계속성 대신 컬럼 직접 쿼리 (null로 바꾸려고해서)
    """
    storyboard = db.get(Storyboard, storyboard_id)
    if storyboard is None:
        raise StoryboardNotFound()

    urls = [url for (url,) in db.query(ReferenceImage.image_url).filter(
        ReferenceImage.storyboard_id == storyboard_id
    )]
    urls += [url for (url,) in db.query(Cut.image_url).filter(
        Cut.storyboard_id == storyboard_id, Cut.image_url.isnot(None)
    )]
    grid_image_url = db.query(Generation.grid_image_url).filter(
        Generation.storyboard_id == storyboard_id
    ).scalar()
    if grid_image_url:
        urls.append(grid_image_url)
    urls += [url for (url,) in db.query(Export.download_url).filter(
        Export.storyboard_id == storyboard_id, Export.download_url.isnot(None)
    )]

    db.delete(storyboard)
    db.commit()

    for url in urls:
        try:
            storage.delete_file(url)
        except Exception:
            # 스토리보드 삭제 자체는 성공 + 첨부파일 정리만 실패: 로그만 남김.
            logger.exception("스토리보드 삭제는 성공했지만 첨부파일 삭제 실패 (storyboard_id=%d, url=%s)", storyboard_id, url)
