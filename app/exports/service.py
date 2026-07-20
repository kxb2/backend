"""이미지/PDF Export 생성 및 결과 조회 비즈니스 로직

ㅡ 이미지 Export, 옵션 미체크(기본): 이미 있는 Generation.grid_image_url을 그대로 재사용
ㅡ 이미지 Export, "컷 개별 포함" 옵션 체크: 그리드 1장 + 컷 9장을 zip 하나로 묶어 R2에 신규 업로드
ㅡ PDF Export: 1페이지에 9컷을 그리드로(컷 비율에 맞춰 세로/가로 방향 결정) PDF 1개 R2에 신규 업로드
"""

import logging
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from xml.sax.saxutils import escape

from PIL import Image as PILImage
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import Image as RLImage
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
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

_PDF_MAX_IMAGE_PIXELS = 1000  # 해상도 캡: 원본이 이보다 크면 축소
_PDF_JPEG_QUALITY = 85        # pdf 용량 적당하게 줄이려고 jpeg로 재인코딩

_PDF_GRID_MARGIN = 1 * cm
_PDF_GRID_SIZE = 3  # 3x3 그리드
_PDF_GRID_IMAGE_HEIGHT_RATIO = 0.62  # 칸 안에서 이미지 최대 높이 비율(나머지는 텍스트 몫)
_PDF_GRID_SHOT_STYLE = ParagraphStyle("pdfGridShot", fontName="Helvetica-Bold", fontSize=8, leading=10, spaceAfter=2)
_PDF_GRID_PROMPT_STYLE = ParagraphStyle("pdfGridPrompt", fontName="Helvetica", fontSize=6, leading=7.3)

class StoryboardNotFound(Exception):
    """존재하지 않는 storyboard_id로 Export를 요청한 경우"""


class GenerationNotCompleted(Exception):
    """9컷 생성이 완료되지 않은 상태에서 Export를 요청한 경우"""

    def __init__(self, message: str = "9컷 생성이 아직 완료되지 않아 Export할 수 없습니다."):
        super().__init__(message)


class ExportInProgress(Exception):
    """같은 storyboard에 이미 PENDING/PROCESSING Export가 있는 상태에서 새로 요청한 경우"""

    def __init__(self, message: str = "이미 처리 중인 Export가 있습니다. 완료 후 다시 시도해 주세요."):
        super().__init__(message)


def _raise_if_export_in_progress(db: Session, storyboard_id: int) -> None:
    """같은 storyboard에 PENDING/PROCESSING Export가 이미 있으면 중복 생성 차단
    (연타/중복 클릭으로 같은 storyboard의 cuts/generation을 동시에 읽는 백그라운드 태스크가 여러 개 뜨는 것 방지)."""
    exists = (
        db.query(Export.id)
        .filter(Export.storyboard_id == storyboard_id, Export.status.in_([JobStatus.PENDING, JobStatus.PROCESSING]))
        .first()
    )
    if exists is not None:
        raise ExportInProgress()


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
    _raise_if_export_in_progress(db, storyboard_id)

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
    _raise_if_export_in_progress(db, storyboard_id)

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
    entries = [(f"grid.{_extension_for_url(grid_image_url)}", grid_image_url)] + [
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


def _resolve_pdf_page_size(sample_image_bytes: bytes) -> tuple[float, float]:
    """컷 이미지의 실제 가로/세로 비율로 페이지 방향을 정함

    ㅡ 가로형, 정사각형: 가로 방향 / 세로형: 세로 방향
    ㅡ 9컷 다 똑같이 생겨서 1컷만 실제 비율 확인하면됨
    """
    width, height = PILImage.open(BytesIO(sample_image_bytes)).size
    return landscape(A4) if width >= height else A4


def _build_pdf_export(cuts: list[Cut]) -> bytes:
    """9컷을 3x3 그리드로 한 페이지에 합성."""
    sorted_cuts = sorted(cuts, key=lambda cut: cut.order_no)

    with ThreadPoolExecutor(max_workers=len(sorted_cuts)) as executor:
        image_bytes_list = list(
            executor.map(lambda cut: _downscale_to_jpeg(storage.download_bytes(cut.image_url)), sorted_cuts)
        )

    page_size = _resolve_pdf_page_size(image_bytes_list[0])
    margin = _PDF_GRID_MARGIN
    grid_width = (page_size[0] - 2 * margin) / _PDF_GRID_SIZE
    grid_height = (page_size[1] - 2 * margin) / _PDF_GRID_SIZE
    image_max_height = grid_height * _PDF_GRID_IMAGE_HEIGHT_RATIO

    cells = []
    for cut, image_bytes in zip(sorted_cuts, image_bytes_list):
        width, height = PILImage.open(BytesIO(image_bytes)).size
        scale = min((grid_width * 0.85) / width, image_max_height / height, 1.0)

        image = RLImage(BytesIO(image_bytes), width=width * scale, height=height * scale)
        image.hAlign = "CENTER"  # 컷 이미지 가운데 정렬
        cells.append([
            image,
            Spacer(1, 0.6 * cm),  # 이미지와 프롬프트 사이 간격 — PM님 피드백 반영
            Paragraph(f"Shot {cut.order_no}", _PDF_GRID_SHOT_STYLE),
            # &, <, > 이스케이프 문자 추가 (export오류나 문자깨지지않도록)
            Paragraph(escape(cut.prompt_text or ""), _PDF_GRID_PROMPT_STYLE),
        ])

    rows = [cells[row * _PDF_GRID_SIZE:(row + 1) * _PDF_GRID_SIZE] for row in range(_PDF_GRID_SIZE)]
    table = Table(rows, colWidths=[grid_width] * _PDF_GRID_SIZE)
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=page_size, topMargin=margin, bottomMargin=margin,
        leftMargin=margin, rightMargin=margin,
    )
    doc.build([table])
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
