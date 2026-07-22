import logging

from fastapi import UploadFile
from sqlalchemy import text
from sqlalchemy.orm import Session, joinedload

from app.core import storage
from app.core.constants import CUT_COUNT
from app.core.enums import Genre, ImageModel, JobStatus
from app.exports.models import Export
from app.generations.models import Cut, Generation
from app.regenerations.models import Regeneration
from app.storyboards.models import ReferenceImage, Storyboard
from app.storyboards.schemas import StoryboardListItem

logger = logging.getLogger(__name__)

MAX_REFERENCE_IMAGES = 10

# 유저/인증 붙기 전까지 GET /storyboards가 전체를 무제한 노출하지 않도록 하드캡.
# user_id가 생기면 이 캡보다 먼저 `WHERE user_id == current_user.id` 필터 추가하렴
DEFAULT_LIST_LIMIT = 100


class ReferenceImageLimitExceeded(Exception):
    def __init__(self, limit: int):
        self.limit = limit


class StoryboardNotFound(Exception):
    """존재하지 않는 storyboard_id로 요청한 경우"""


class GenerationInProgress(Exception):
    """9컷 생성이 진행 중(PENDING/PROCESSING)인 스토리보드를 삭제하려는 경우"""

    def __init__(self, message: str = "9컷 생성이 진행 중이라 스토리보드를 삭제할 수 없습니다. 완료 후 다시 시도해 주세요."):
        super().__init__(message)


class RegenerationInProgress(Exception):
    """컷 재생성이 진행 중(PENDING/PROCESSING)인 스토리보드를 삭제하려는 경우"""

    def __init__(
        self,
        message: str = "진행 중인 컷 재생성 작업이 있어 스토리보드를 삭제할 수 없습니다. 완료 후 다시 시도해 주세요.",
    ):
        super().__init__(message)


class ExportInProgress(Exception):
    """내보내기가 진행 중(PENDING/PROCESSING)인 스토리보드를 삭제하려는 경우"""

    def __init__(self, message: str = "진행 중인 Export 작업이 있어 스토리보드를 삭제할 수 없습니다. 완료 후 다시 시도해 주세요."):
        super().__init__(message)


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
    # 레퍼런스 이미지 업로드 전에 리사이즈: 저장 용량/토큰 비용 ↓
    reference_data = [
        (storage.resize_reference_image(data, content_type), content_type)
        for data, content_type in reference_data
    ]

    storyboard = Storyboard(
        title=_next_default_title(db),
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


def _next_default_title(db: Session) -> str:
    """처음 제목 (임시: 전역 sequence) — 유저별 번호 붙기 전까지 "storyboard N" 형태로 부여.
    DB SEQUENCE(storyboard_default_title_seq)에서 번호 발급받아서
    동시 요청 와도 중복 안 되고, title 텍스트를 다시 파싱하지 않아
    title을 자유롭게 수정해도 번호 로직에 영향 X (캔버스랑 독스트링내용 똑같음)"""
    next_no = db.execute(text("SELECT nextval('storyboard_default_title_seq')")).scalar()
    return f"storyboard {next_no}"


def list_storyboards(db: Session, limit: int = DEFAULT_LIST_LIMIT) -> list[StoryboardListItem]:
    """스토리보드 전체 목록(요약) 조회 — 최신 수정순, 최대 limit개"""
    storyboards = (
        db.query(Storyboard)
        .options(joinedload(Storyboard.generation))
        .order_by(Storyboard.updated_at.desc())
        .limit(limit)
        .all()
    )
    return [ # 응답: id, 제목, 장르(아이콘 같은거 추가할수도 잇지않을까해서), status, 생성/수정 날짜
        StoryboardListItem(
            id=sb.id,
            title=sb.title,
            genre=sb.genre,
            status=sb.generation.status if sb.generation else None,
            created_at=sb.created_at,
            updated_at=sb.updated_at,
        )
        for sb in storyboards
    ]


def get_storyboard(db: Session, storyboard_id: int) -> Storyboard | None:
    """스토리보드 조회"""
    return db.get(Storyboard, storyboard_id)


def _raise_if_generation_in_progress(db: Session, storyboard_id: int) -> None:
    """스토리보드의 9컷 생성이 PENDING/PROCESSING이면 삭제 차단."""
    generation_status = (
        db.query(Generation.status).filter(Generation.storyboard_id == storyboard_id).scalar()
    )
    if generation_status in (JobStatus.PENDING, JobStatus.PROCESSING):
        raise GenerationInProgress()


def _raise_if_regeneration_in_progress(db: Session, storyboard_id: int) -> None:
    """스토리보드 컷 중 하나라도 재생성(PENDING/PROCESSING)이 있으면 삭제 차단."""
    exists = (
        db.query(Regeneration.id)
        .join(Cut, Cut.id == Regeneration.cut_id)
        .filter(
            Cut.storyboard_id == storyboard_id,
            Regeneration.status.in_([JobStatus.PENDING, JobStatus.PROCESSING]),
        )
        .first()
    )
    if exists is not None:
        raise RegenerationInProgress()


def _raise_if_export_in_progress(db: Session, storyboard_id: int) -> None:
    """스토리보드에 PENDING/PROCESSING 내보내기가 있으면 삭제 차단."""
    exists = (
        db.query(Export.id)
        .filter(
            Export.storyboard_id == storyboard_id,
            Export.status.in_([JobStatus.PENDING, JobStatus.PROCESSING]),
        )
        .first()
    )
    if exists is not None:
        raise ExportInProgress()


def delete_storyboard(db: Session, storyboard_id: int) -> None:
    """스토리보드 삭제

    ㅡ 진행 중인 생성/재생성/Export가 하나라도 있으면 삭제 차단
    ㅡ 순서 중요: DB 삭제 → R2 삭제 (DB 깨지는것보다 R2 고아 남는게 나음)
    ㅡ URL은 storyboard.cuts 등 관계속성 대신 컬럼 직접 쿼리 (null로 바꾸려고해서)
    """
    storyboard = db.get(Storyboard, storyboard_id)
    if storyboard is None:
        raise StoryboardNotFound()

    _raise_if_generation_in_progress(db, storyboard_id)
    _raise_if_regeneration_in_progress(db, storyboard_id)
    _raise_if_export_in_progress(db, storyboard_id)

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
