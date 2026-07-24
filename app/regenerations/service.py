"""컷 재생성 요청/결과 조회 비즈니스 로직

ㅡ run_regeneration 순서: 컷 로드 → 기존 프롬프트 그대로 이미지 어댑터 재호출
→ 성공 시 컷 이미지 교체(옛 이미지 R2 삭제 + 이전 regeneration 기록도 정리)
→ 9컷 전부 완료 상태면 그리드 이미지도 재합성
→ 컷 재생성 실패 경우: 기존 컷 이미지/완료 상태 그대로 둠(재생성 시도 전 상태 유지).
"""

import logging

from sqlalchemy.orm import Session

from app.ai.image_adapter import get_image_adapter
from app.core import storage
from app.core.enums import JobStatus
from app.db.session import SessionLocal
from app.generations.models import Cut
from app.generations.service import GRID_IMAGE_FOLDER, build_grid_image, download_reference_images
from app.regenerations.models import Regeneration
from app.storyboards.models import Storyboard

logger = logging.getLogger(__name__)


class StoryboardNotFound(Exception):
    """존재하지 않는 storyboard_id로 요청한 경우"""


class CutNotFound(Exception):
    """존재하지 않거나 해당 storyboard 소속이 아닌 cut_id로 요청한 경우"""


class CutNotReady(Exception):
    """9컷 생성이 아직 진행 중(PENDING/PROCESSING)이라 재생성할 이미지가 없는 경우"""

    def __init__(self, message: str = "아직 컷 생성이 진행 중이라 재생성할 수 없습니다."):
        super().__init__(message)


class RegenerationInProgress(Exception):
    """같은 컷에 이미 PENDING/PROCESSING 재생성 작업이 있는 상태에서 새로 요청한 경우"""

    def __init__(self, message: str = "이미 진행 중인 재생성 작업이 있습니다."):
        super().__init__(message)


def create_regeneration(db: Session, storyboard_id: int, cut_id: int) -> Regeneration:
    """특정 컷 재생성 요청 등록(POST 사용)

    ㅡ cut row를 SELECT ... FOR UPDATE로 잠그고
    '진행 중인 재생성 확인 → 생성'을 한 트랜잭션 안에서 처리.
    """
    storyboard = db.get(Storyboard, storyboard_id)
    if storyboard is None:
        raise StoryboardNotFound()

    cut = db.query(Cut).filter(Cut.id == cut_id).with_for_update().first()
    if cut is None or cut.storyboard_id != storyboard_id:
        raise CutNotFound()

    if cut.status not in (JobStatus.COMPLETED, JobStatus.FAILED):
        raise CutNotReady()

    existing = (
        db.query(Regeneration)
        .filter(
            Regeneration.cut_id == cut_id,
            Regeneration.status.in_([JobStatus.PENDING, JobStatus.PROCESSING]),
        )
        .first()
    )
    if existing is not None:
        raise RegenerationInProgress()

    regeneration = Regeneration(cut_id=cut_id, status=JobStatus.PENDING)
    db.add(regeneration)
    db.commit()
    return regeneration


def get_regeneration(db: Session, regeneration_id: int) -> Regeneration | None:
    """컷 재생성 상태/결과 조회(GET 사용)"""
    return db.get(Regeneration, regeneration_id)


def recover_stuck_regenerations(db: Session) -> int:
    """서버 시작 시(배포로 인한 컨테이너 재시작 등) 호출

    ㅡ generation과 동일한 방식/ main.py에서 사용
    ㅡ pending/processing으로 멈춰있던 재생성을 failed로 정리.
    ㅡ 컷 자체는 재생성 시도 전 상태(이미 완료됐던 이미지)를 그대로 두므로 건드리지 않음.
    """
    stuck = (
        db.query(Regeneration)
        .filter(Regeneration.status.in_([JobStatus.PENDING, JobStatus.PROCESSING]))
        .all()
    )
    for regeneration in stuck:
        regeneration.status = JobStatus.FAILED
        regeneration.error_message = "서버 재시작으로 중단된 작업"
    db.commit()
    return len(stuck)


def _clear_stale_regeneration_urls(db: Session, cut_id: int, superseded_image_url: str) -> None:
    """2차 재생성, N차 재생성을 하게 될 경우 옛 regenerations row의 image_url → None으로
       
       ㅡ R2에서는 옛날 재생성들 삭제되지만, db는 row가 삭제되는건 아니니까
    """
    db.query(Regeneration).filter(
        Regeneration.cut_id == cut_id, Regeneration.image_url == superseded_image_url
    ).update({"image_url": None})


def _rebuild_grid_if_all_completed(
    db: Session, storyboard: Storyboard, known_image_bytes: dict[str, bytes] | None = None
) -> None:
    """9컷이 전부 완료 상태면 그리드 이미지를 재합성.

    ㅡ 재생성 성공한후 그리드 합성에서만 실패하는 경우도 있을 수 있으므로 함수 분리
    ㅡ known_image_bytes: 방금 재생성한 1컷은 이미 바이트를 들고 있으므로 그 컷만 R2 재다운로드 생략
      (나머지 8컷은 방금 재생성한 게 아니라 바이트가 없으므로 기존처럼 다운로드)
    """
    generation = storyboard.generation
    if generation is None or not all(c.status == JobStatus.COMPLETED for c in storyboard.cuts):
        return

    old_grid_url = generation.grid_image_url
    grid_bytes = build_grid_image([c.image_url for c in storyboard.cuts], known_image_bytes=known_image_bytes)
    generation.grid_image_url = storage.upload_image_bytes(
        grid_bytes, content_type="image/jpeg", folder=GRID_IMAGE_FOLDER
    )
    generation.status = JobStatus.COMPLETED
    generation.error_message = None
    db.commit()

    if old_grid_url:
        storage.delete_file(old_grid_url)


def run_regeneration(regeneration_id: int) -> None:
    """POST 요청 직후 BackgroundTasks로 호출되는 컷 재생성 작업 진입점.

    ㅡ 요청-응답 사이클과 독립적으로 실행, 자체 DB 세션을 열고 닫음.
    """
    db = SessionLocal()
    try:
        regeneration = db.get(Regeneration, regeneration_id)
        cut = regeneration.cut
        storyboard = cut.storyboard

        regeneration.status = JobStatus.PROCESSING
        db.commit()

        old_image_url = cut.image_url
        image_adapter = get_image_adapter(storyboard.image_model)
        # 원래 생성 때 썼던 레퍼런스를 앵커로 그대로 재전달 — 레퍼런스 없는 스토리보드는 빈 리스트라 동작 동일
        reference_images = download_reference_images([ref.image_url for ref in storyboard.reference_images])
        try:
            result = image_adapter.generate_image(
                prompt_text=cut.prompt_text, aspect_ratio=storyboard.aspect_ratio, reference_images=reference_images
            )
        except Exception as exc:  # noqa: BLE001 — 실패해도 기존 컷 이미지/상태는 건드리지 않음
            logger.error(         # 뭐가 터지든 한 컷만 실패처리하고 나머지 흐름 안끊으려고 넓게 잡음
                "컷 재생성 실패(cut_id=%d, regeneration_id=%d): %s", cut.id, regeneration_id, exc
            )
            regeneration.status = JobStatus.FAILED
            regeneration.error_message = str(exc)
            db.commit()
            return

        new_image_url = result.url
        cut.image_url = new_image_url
        cut.status = JobStatus.COMPLETED
        cut.error_message = None
        regeneration.image_url = new_image_url
        regeneration.status = JobStatus.COMPLETED
        if old_image_url:
            _clear_stale_regeneration_urls(db, cut.id, old_image_url)
        db.commit()

        if old_image_url:
            try:
                storage.delete_file(old_image_url)
            except Exception:
                # 컷 재생성 자체는 성공 + 옛 이미지 삭제 실패 케이스는
                # → 로그만 남기고 regeneration.status는 COMPLETED로 유지.
                logger.exception(
                    "컷 재생성은 성공했지만 옛 이미지 삭제 실패 (regeneration_id=%d)", regeneration_id
                )

        try:
            _rebuild_grid_if_all_completed(db, storyboard, known_image_bytes={new_image_url: result.data})
        except Exception:
            # 컷 재생성 자체는 이미 성공했으므로, 그리드 재조립 실패는 로그만 남기고
            # regeneration.status는 COMPLETED로 유지.
            logger.exception(
                "컷 재생성은 성공했지만 그리드 재조립 실패 (regeneration_id=%d)", regeneration_id
            )
    except Exception as exc:
        logger.exception("run_regeneration 실패 (regeneration_id=%d)", regeneration_id)
        try:
            db.rollback()
            regeneration = db.get(Regeneration, regeneration_id)
            regeneration.status = JobStatus.FAILED
            regeneration.error_message = str(exc)
            db.commit()
        except Exception:
            logger.exception(
                "run_regeneration 실패 후 FAILED 상태 기록도 실패 (regeneration_id=%d)", regeneration_id
            )
    finally:
        db.close()
