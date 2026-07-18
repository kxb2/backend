"""이미지/PDF Export 생성 및 결과 조회 비즈니스 로직

ㅡ 이미지 Export, 옵션 미체크(기본): 이미 있는 Generation.grid_image_url을 그대로 재사용
ㅡ 이미지 Export, "컷 개별 포함" 옵션 체크: 그리드 1장 + 컷 9장을 zip 하나로 묶어 R2에 신규 업로드
ㅡ PDF Export: 컷마다 이미지 1장 + Shot 번호/프롬프트를 한 페이지씩 구성(F-05)해서 PDF 1개로 R2에 신규 업로드
"""

import logging
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from xml.sax.saxutils import escape

from PIL import Image as PILImage
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import Image as RLImage
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer
from sqlalchemy.orm import Session

from app.core import storage
from app.core.constants import CUT_COUNT
from app.core.enums import ExportType, JobStatus
from app.db.session import SessionLocal
from app.exports.models import Export
from app.generations.models import Cut
from app.storyboards.models import Storyboard

logger = logging.getLogger(__name__)

# BytesIO: 가상메모리 / zipfile: 압축 관련 파이썬 기본 라이브러리
EXPORT_ZIP_FOLDER = "export-images"
EXPORT_PDF_FOLDER = "export-pdfs"

_PDF_MARGIN = 2 * cm
_PDF_MAX_IMAGE_WIDTH = A4[0] - 2 * _PDF_MARGIN
_PDF_MAX_IMAGE_HEIGHT = A4[1] * 0.55
_PDF_MAX_IMAGE_PIXELS = 1600  # 해상도 캡: 원본이 이보다 크면 축소
_PDF_JPEG_QUALITY = 85        # pdf 용량 적당하게 줄이려고 jpeg로 재인코딩

class StoryboardNotFound(Exception):
    """존재하지 않는 storyboard_id로 Export를 요청한 경우"""


class GenerationNotCompleted(Exception):
    """9컷 생성이 완료되지 않은 상태에서 Export를 요청한 경우"""

    def __init__(self, message: str = "9컷 생성이 아직 완료되지 않아 Export할 수 없습니다."):
        super().__init__(message)


def _get_exportable_storyboard(db: Session, storyboard_id: int) -> Storyboard:
    """Export 가능한(9컷 생성이 완료된) storyboard를 반환. 없거나 미완료면 예외."""
    storyboard = db.get(Storyboard, storyboard_id)
    if storyboard is None:
        raise StoryboardNotFound()

    generation = storyboard.generation
    cuts_all_completed = (
        len(storyboard.cuts) == CUT_COUNT
        and all(cut.status == JobStatus.COMPLETED for cut in storyboard.cuts)
    )

    if generation is None or generation.status != JobStatus.COMPLETED:
        # '9컷들 성공 + 그리드 합성만 실패' 경우 구분하려고 메시지 분기
        if generation is not None and cuts_all_completed:
            raise GenerationNotCompleted("9컷 이미지는 모두 생성됐지만 그리드 합성에 실패했습니다. 다시 시도해 주세요.")
        raise GenerationNotCompleted()

    if not cuts_all_completed:
        # generation.status는 COMPLETED인데 실제 컷 개수/상태가 안 맞는 이례적 상태 — 방어적으로 차단
        raise GenerationNotCompleted()

    return storyboard


def create_image_export(db: Session, storyboard_id: int, *, include_individual_cuts: bool) -> Export:
    """이미지 Export job 등록 (실제 처리는 background task: run_image_export가 수행)"""
    storyboard = _get_exportable_storyboard(db, storyboard_id)

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


def create_pdf_export(db: Session, storyboard_id: int) -> Export:
    """PDF Export job 등록 (실제 처리는 background task: run_pdf_export가 수행)"""
    storyboard = _get_exportable_storyboard(db, storyboard_id)

    export = Export(storyboard_id=storyboard.id, type=ExportType.PDF, status=JobStatus.PENDING)
    db.add(export)
    db.commit()
    db.refresh(export)
    return export


def get_export(db: Session, export_id: int) -> Export | None:
    """Export 결과 조회"""
    return db.get(Export, export_id)


def _extension_for_url(url: str) -> str:
    """URL의 실제 확장자를 그대로 사용 — 컷 이미지가 항상 png라는 보장이 없음(Gemini는 jpeg/webp도 가능)"""
    filename = url.rsplit("/", 1)[-1]
    return filename.rsplit(".", 1)[-1] if "." in filename else "png"


def _build_image_export_zip(grid_image_url: str, cuts: list[Cut]) -> bytes:
    """그리드 이미지 1장 + 컷별 개별 이미지 9장을 zip 하나로 묶음.

    ㅡ executor.map은 입력 순서를 그대로 보존해서 '입력 순서 = 결과 반환 순서'
    """
    entries = [("grid.png", grid_image_url)] + [
        (f"cut_{cut.order_no}.{_extension_for_url(cut.image_url)}", cut.image_url)
        for cut in sorted(cuts, key=lambda cut: cut.order_no)
    ]

    with ThreadPoolExecutor(max_workers=len(entries)) as executor:
        contents = list(executor.map(lambda entry: storage.download_bytes(entry[1]), entries))

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
                zip_bytes,
                key=f"{EXPORT_ZIP_FOLDER}/{uuid.uuid4().hex}.zip",
                content_type="application/zip",
                filename=f"storyboard_{storyboard.id}_images.zip",
            )
        else:
            download_url = generation.grid_image_url

        export.download_url = download_url
        export.status = JobStatus.COMPLETED
        db.commit()
    except Exception as exc:
        # 위에서 에러(다운로드 실패 등)가 나도 PROCESSING에 영원히 멈추지 않도록,
        # 최종적으로는 반드시 FAILED로 확정
        logger.exception("run_image_export 실패 (export_id=%d)", export_id)
        try:
            db.rollback()
            export = db.get(Export, export_id)
            export.status = JobStatus.FAILED
            export.error_message = str(exc)
            db.commit()
        except Exception:
            logger.exception("run_image_export 실패 후 FAILED 상태 기록도 실패 (export_id=%d)", export_id)
    finally:
        db.close()


def _downscale_to_jpeg(image_bytes: bytes) -> bytes:
    """PDF에 넣기 전에 원본 이미지 → JPEG로 재인코딩.

    ㅡ 원본 PNG 해상도가 크면 PDF가 수십 MB까지 부풀어 공유하기 부적합.
    ㅡ jpeg는 화질 손실 없이 크기만 축소 가능(pdf는 정보공유용이고 실제 컷만 원본이면 적당하다고 판단).
    ㅡ 이미 JPEG고 해상도 작으면(Gemini 컷의 경우) 재인코딩 X
    """
    image = PILImage.open(BytesIO(image_bytes))
    if image.format == "JPEG" and max(image.size) <= _PDF_MAX_IMAGE_PIXELS:
        return image_bytes

    image = image.convert("RGB")
    if max(image.size) > _PDF_MAX_IMAGE_PIXELS:
        image.thumbnail((_PDF_MAX_IMAGE_PIXELS, _PDF_MAX_IMAGE_PIXELS), PILImage.LANCZOS)

    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=_PDF_JPEG_QUALITY)
    return buffer.getvalue()


def _build_pdf_export(cuts: list[Cut]) -> bytes:
    """컷마다 이미지 1장 + "Shot N" + 프롬프트를 한 페이지씩 구성해서 PDF 1개로 합성.

    ㅡ 일단은 pdf 1페이지에 '컷 이미지 + 해당 컷 프롬프트' 1개씩 넣어놓음 (A4 세로방향)
    ㅡ 1페이지당 3개씩 넣는게 가독성이 나을지? 고민중
    """
    sorted_cuts = sorted(cuts, key=lambda cut: cut.order_no)

    with ThreadPoolExecutor(max_workers=len(sorted_cuts)) as executor:
        image_bytes_list = list(
            executor.map(lambda cut: _downscale_to_jpeg(storage.download_bytes(cut.image_url)), sorted_cuts)
        )

    styles = getSampleStyleSheet()
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4, topMargin=_PDF_MARGIN, bottomMargin=_PDF_MARGIN,
        leftMargin=_PDF_MARGIN, rightMargin=_PDF_MARGIN,
    ) # A4 기본값은 세로방향(portrait) / 가로방향: landscape(A4)

    elements = []
    for index, (cut, image_bytes) in enumerate(zip(sorted_cuts, image_bytes_list)):
        width, height = PILImage.open(BytesIO(image_bytes)).size
        scale = min(_PDF_MAX_IMAGE_WIDTH / width, _PDF_MAX_IMAGE_HEIGHT / height, 1.0)

        image = RLImage(BytesIO(image_bytes), width=width * scale, height=height * scale)
        image.hAlign = "CENTER"  # 첨부되는 컷별 이미지 가운데 정렬
        elements.append(image)
        elements.append(Spacer(1, 0.5 * cm))
        elements.append(Paragraph(f"Shot {cut.order_no}", styles["Heading2"]))
        # prompt_text는 Claude(LLM)가 생성한 텍스트라 &, <, > 포함 여부를 통제 못 함 —
        # reportlab Paragraph는 이 문자들을 자체 마크업으로 파싱해서 이스케이프 없이 넣으면
        # 내용이 조용히 손상되거나(예: "AT&T" -> "AT&T;") 특정 패턴에서 파싱 에러로 export가 실패함
        elements.append(Paragraph(escape(cut.prompt_text or ""), styles["BodyText"]))
        if index < len(sorted_cuts) - 1:
            elements.append(PageBreak())

    doc.build(elements)
    return buffer.getvalue()


def run_pdf_export(export_id: int) -> None:
    """PDF Export 생성 직후 BackgroundTasks로 호출되는 진입점.

    ㅡ 요청-응답 사이클과 독립적으로 실행, 넘겨받은 세션을 재사용 X, 자체 DB 세션을 열고 닫음.
    """
    db = SessionLocal()
    try:
        export = db.get(Export, export_id)
        export.status = JobStatus.PROCESSING
        db.commit()

        storyboard = db.get(Storyboard, export.storyboard_id)
        pdf_bytes = _build_pdf_export(storyboard.cuts)
        download_url = storage.upload_bytes(
            pdf_bytes,
            key=f"{EXPORT_PDF_FOLDER}/{uuid.uuid4().hex}.pdf",
            content_type="application/pdf",
            filename=f"storyboard_{storyboard.id}.pdf",
        )

        export.download_url = download_url
        export.status = JobStatus.COMPLETED
        db.commit()
    except Exception as exc:
        # 위에서 에러(다운로드/PDF 합성 실패 등)가 나도 PROCESSING에 영원히 멈추지 않도록,
        # 최종적으로는 반드시 FAILED로 확정
        logger.exception("run_pdf_export 실패 (export_id=%d)", export_id)
        try:
            db.rollback()
            export = db.get(Export, export_id)
            export.status = JobStatus.FAILED
            export.error_message = str(exc)
            db.commit()
        except Exception:
            logger.exception("run_pdf_export 실패 후 FAILED 상태 기록도 실패 (export_id=%d)", export_id)
    finally:
        db.close()
