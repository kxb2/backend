"""이미지 Export 생성 및 결과 조회 비즈니스 로직

ㅡ 옵션 미체크(기본): 새 파일 없이 이미 있는 Generation.grid_image_url을 그대로 재사용
ㅡ "컷 개별 포함" 옵션 체크: 그리드 1장 + 컷 9장을 zip 하나로 묶어 R2에 신규 업로드
"""

import logging
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO

import httpx
from sqlalchemy.orm import Session

from app.core import storage
from app.core.enums import ExportType, JobStatus
from app.db.session import SessionLocal
from app.exports.models import Export
from app.generations.models import Cut
from app.storyboards.models import Storyboard

logger = logging.getLogger(__name__)

EXPORT_ZIP_FOLDER = "export-images"


class StoryboardNotFound(Exception):
    """존재하지 않는 storyboard_id로 Export를 요청한 경우"""


class GenerationNotCompleted(Exception):
    """9컷 생성이 완료되지 않은 상태에서 Export를 요청한 경우"""


def create_image_export(db: Session, storyboard_id: int, *, include_individual_cuts: bool) -> Export:
    """이미지 Export job 등록 (실제 처리는 background task인 run_image_export가 수행)"""
    storyboard = db.get(Storyboard, storyboard_id)
    if storyboard is None:
        raise StoryboardNotFound()
    if storyboard.generation is None or storyboard.generation.status != JobStatus.COMPLETED:
        raise GenerationNotCompleted()

    export = Export(
        storyboard_id=storyboard.id,
        type=ExportType.IMAGE,
        include_individual_cuts=include_individual_cuts,
        status=JobStatus.PENDING,
    )
    db.add(export)
    db.commit()
    db.refresh(export)
    return export


def get_export(db: Session, export_id: int) -> Export | None:
    """Export 결과 조회"""
    return db.get(Export, export_id)


def _download_bytes(url: str) -> bytes:
    """스레드에서 실행 — URL 하나를 그대로 바이트로 다운로드"""
    return httpx.get(url, timeout=30.0).content


def _build_image_export_zip(grid_image_url: str, cuts: list[Cut]) -> bytes:
    """그리드 이미지 1장 + 컷별 개별 이미지 9장을 zip 하나로 묶음.

    ㅡ executor.map은 입력 순서를 그대로 보존해서 반환하므로 entries와 contents의 순서가 1:1로 맞음.
    """
    entries = [("grid.png", grid_image_url)] + [
        (f"cut_{cut.order_no}.png", cut.image_url) for cut in sorted(cuts, key=lambda cut: cut.order_no)
    ]

    with ThreadPoolExecutor(max_workers=len(entries)) as executor:
        contents = list(executor.map(lambda entry: _download_bytes(entry[1]), entries))

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
        for (filename, _), content in zip(entries, contents):
            zip_file.writestr(filename, content)

    return buffer.getvalue()


def run_image_export(export_id: int) -> None:
    """이미지 Export 생성 직후 BackgroundTasks로 호출되는 진입점.

    ㅡ 요청-응답 사이클과 독립적으로 실행, 넘겨받은 세션을 재사용 X, 자체 DB 세션을 열고 닫음.
    """
    db = SessionLocal()
    try:
        export = db.get(Export, export_id)
        export.status = JobStatus.PROCESSING
        db.commit()

        storyboard = db.get(Storyboard, export.storyboard_id)
        generation = storyboard.generation

        if export.include_individual_cuts:
            zip_bytes = _build_image_export_zip(generation.grid_image_url, storyboard.cuts)
            download_url = storage.upload_bytes(
                zip_bytes, key=f"{EXPORT_ZIP_FOLDER}/{uuid.uuid4().hex}.zip", content_type="application/zip"
            )
        else:
            download_url = generation.grid_image_url

        export.download_url = download_url
        export.status = JobStatus.COMPLETED
        db.commit()
    except Exception:
        # 위에서 에러(다운로드 실패 등)가 나도 PROCESSING에 영원히 멈추지 않도록,
        # 최종적으로는 반드시 FAILED로 확정
        logger.exception("run_image_export 실패 (export_id=%d)", export_id)
        try:
            db.rollback()
            export = db.get(Export, export_id)
            export.status = JobStatus.FAILED
            db.commit()
        except Exception:
            logger.exception("run_image_export 실패 후 FAILED 상태 기록도 실패 (export_id=%d)", export_id)
    finally:
        db.close()
