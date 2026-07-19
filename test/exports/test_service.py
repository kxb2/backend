from io import BytesIO
from unittest.mock import Mock
from zipfile import ZipFile

import pytest
from PIL import Image as PILImage
from pypdf import PdfReader

from app.core.enums import ExportType, JobStatus
from app.exports.models import Export
from app.exports.service import (
    ExportInProgress,
    GenerationNotCompleted,
    StoryboardNotFound,
    _build_image_export_zip,
    _build_pdf_export,
    _downscale_to_jpeg,
    create_image_export,
    create_pdf_export,
    get_export,
)
from app.generations.models import Cut, Generation
from app.storyboards.models import Storyboard

"""Export 관련 순수 로직 테스트 파일 (run_image_export 오케스트레이션 본체는 test_run_image_export.py에서)"""


def _storyboard_with_completed_generation(storyboard_id: int = 1) -> Storyboard:
    """9컷 생성까지 전부 끝나고 그리드도 완성된, Export 가능한 정상 상태"""
    storyboard = Storyboard(id=storyboard_id)
    storyboard.generation = Generation(
        storyboard_id=storyboard_id, status=JobStatus.COMPLETED, grid_image_url="https://pub-x.r2.dev/grids/1.png"
    )
    storyboard.cuts = [
        Cut(order_no=n, status=JobStatus.COMPLETED, image_url=f"https://pub-x.r2.dev/cuts/{n}.png")
        for n in range(1, 10)
    ]
    return storyboard


class TestCreateImageExport:
    def test_creates_export_row_with_pending_status(self):
        """정상 케이스: Export row가 IMAGE 타입/PENDING 상태로 생성되고 commit되는지"""
        db = Mock()
        db.get.return_value = _storyboard_with_completed_generation()
        db.query.return_value.filter.return_value.first.return_value = None  # 처리 중인 Export 없음

        export = create_image_export(db, 1, include_individual_cuts=True)

        assert export.storyboard_id == 1
        assert export.type == ExportType.IMAGE
        assert export.include_individual_cuts is True
        assert export.status == JobStatus.PENDING
        db.add.assert_called_once_with(export)
        db.commit.assert_called_once()

    def test_raises_when_storyboard_not_found(self):
        """존재하지 않는 storyboard_id면 StoryboardNotFound"""
        db = Mock()
        db.get.return_value = None

        with pytest.raises(StoryboardNotFound):
            create_image_export(db, 999, include_individual_cuts=False)

        db.add.assert_not_called()

    def test_raises_generic_message_when_cuts_not_done_yet(self):
        """컷 자체가 아직 다 안 끝났으면 일반적인 메시지("9컷 생성이 아직...")"""
        storyboard = _storyboard_with_completed_generation()
        storyboard.generation.status = JobStatus.PROCESSING
        storyboard.cuts[0].status = JobStatus.PROCESSING
        db = Mock()
        db.get.return_value = storyboard

        with pytest.raises(GenerationNotCompleted) as exc_info:
            create_image_export(db, 1, include_individual_cuts=False)

        assert "그리드" not in str(exc_info.value)
        db.add.assert_not_called()

    def test_raises_specific_message_when_only_grid_composition_failed(self):
        """9컷은 전부 완료됐는데 그리드 합성만 실패한 경우엔 별도 메시지로 구분"""
        storyboard = _storyboard_with_completed_generation()
        storyboard.generation.status = JobStatus.FAILED
        storyboard.generation.grid_image_url = None
        db = Mock()
        db.get.return_value = storyboard

        with pytest.raises(GenerationNotCompleted) as exc_info:
            create_image_export(db, 1, include_individual_cuts=False)

        assert "그리드" in str(exc_info.value)
        db.add.assert_not_called()

    def test_raises_when_generation_completed_but_cut_count_mismatches(self):
        """status는 COMPLETED인데 실제 컷 개수/상태가 안 맞는 이례적 상태도 방어적으로 차단하는지"""
        storyboard = _storyboard_with_completed_generation()
        storyboard.cuts = storyboard.cuts[:8]  # 9개여야 하는데 8개뿐인 이례적 상태
        db = Mock()
        db.get.return_value = storyboard

        with pytest.raises(GenerationNotCompleted):
            create_image_export(db, 1, include_individual_cuts=False)

        db.add.assert_not_called()


class TestCreatePdfExport:
    def test_creates_export_row_with_pending_status(self):
        """정상 케이스: Export row가 PDF 타입/PENDING 상태로 생성되고 commit되는지"""
        db = Mock()
        db.get.return_value = _storyboard_with_completed_generation()
        db.query.return_value.filter.return_value.first.return_value = None  # 처리 중인 Export 없음

        export = create_pdf_export(db, 1)

        assert export.storyboard_id == 1
        assert export.type == ExportType.PDF
        assert export.status == JobStatus.PENDING
        db.add.assert_called_once_with(export)
        db.commit.assert_called_once()

    def test_raises_when_storyboard_not_found(self):
        """존재하지 않는 storyboard_id면 StoryboardNotFound (이미지 Export와 검증 로직 공유)"""
        db = Mock()
        db.get.return_value = None

        with pytest.raises(StoryboardNotFound):
            create_pdf_export(db, 999)

        db.add.assert_not_called()

    def test_raises_when_generation_not_completed(self):
        """9컷 생성이 아직 안 끝났으면 PDF도 마찬가지로 GenerationNotCompleted"""
        storyboard = _storyboard_with_completed_generation()
        storyboard.generation.status = JobStatus.PROCESSING
        db = Mock()
        db.get.return_value = storyboard

        with pytest.raises(GenerationNotCompleted):
            create_pdf_export(db, 1)

        db.add.assert_not_called()

    def test_raises_when_generation_completed_but_cut_count_mismatches(self):
        """status는 COMPLETED인데 실제 컷 개수/상태가 안 맞는 이례적 상태도 방어적으로 차단하는지"""
        storyboard = _storyboard_with_completed_generation()
        storyboard.cuts = storyboard.cuts[:8]  # 9개여야 하는데 8개뿐인 이례적 상태
        db = Mock()
        db.get.return_value = storyboard

        with pytest.raises(GenerationNotCompleted):
            create_pdf_export(db, 1)

        db.add.assert_not_called()


class TestExportInProgressGuard:
    def test_image_export_blocked_when_export_already_in_progress(self):
        """같은 storyboard에 PENDING/PROCESSING Export가 이미 있으면 새 이미지 Export 요청을 막는지"""
        db = Mock()
        db.get.return_value = _storyboard_with_completed_generation()
        db.query.return_value.filter.return_value.first.return_value = Export(id=99)  # 이미 처리 중인 export

        with pytest.raises(ExportInProgress):
            create_image_export(db, 1, include_individual_cuts=False)

        db.add.assert_not_called()

    def test_pdf_export_blocked_when_export_already_in_progress(self):
        """같은 storyboard에 PENDING/PROCESSING Export가 이미 있으면 새 PDF Export 요청도 막는지"""
        db = Mock()
        db.get.return_value = _storyboard_with_completed_generation()
        db.query.return_value.filter.return_value.first.return_value = Export(id=99)  # 이미 처리 중인 export

        with pytest.raises(ExportInProgress):
            create_pdf_export(db, 1)

        db.add.assert_not_called()


class TestGetExport:
    def test_returns_export_by_id(self):
        db = Mock()
        db.get.return_value = Export(id=1)

        result = get_export(db, 1)

        assert result.id == 1
        db.get.assert_called_once_with(Export, 1)


class TestBuildImageExportZip:
    def test_zip_contains_grid_and_all_cuts_named_by_order_no(self, monkeypatch):
        """그리드+9컷이 각각 grid.png / cut_{order_no}.png로 zip에 들어가는지, 내용도 URL에 맞게 매핑되는지"""
        cuts = [Cut(order_no=n, image_url=f"https://pub-x.r2.dev/cuts/{n}.png") for n in range(9, 0, -1)]
        # 순서 뒤섞어서 넣어도 order_no 기준으로 정렬되어 담기는지 확인하려고 역순으로 구성

        contents_by_url = {f"https://pub-x.r2.dev/cuts/{n}.png": f"cut-{n}-bytes".encode() for n in range(1, 10)}
        contents_by_url["https://pub-x.r2.dev/grids/1.png"] = b"grid-bytes"

        monkeypatch.setattr(
            "app.core.storage.httpx.get",
            lambda url, **kwargs: Mock(content=contents_by_url[url]),
        )

        zip_bytes = _build_image_export_zip("https://pub-x.r2.dev/grids/1.png", cuts)

        with ZipFile(BytesIO(zip_bytes)) as zip_file:
            names = sorted(zip_file.namelist())
            assert names == ["cut_1.png", "cut_2.png", "cut_3.png", "cut_4.png", "cut_5.png", "cut_6.png", "cut_7.png", "cut_8.png", "cut_9.png", "grid.png"]
            assert zip_file.read("grid.png") == b"grid-bytes"
            assert zip_file.read("cut_5.png") == b"cut-5-bytes"

    def test_grid_entry_name_matches_actual_grid_extension(self, monkeypatch):
        """그리드가 이제 JPEG로 저장 → 실제 URL 확장자 따라 grid.jpg로 들어가는지"""
        cuts = [Cut(order_no=n, image_url=f"https://pub-x.r2.dev/cuts/{n}.png") for n in range(1, 10)]
        contents_by_url = {f"https://pub-x.r2.dev/cuts/{n}.png": b"cut-bytes" for n in range(1, 10)}
        contents_by_url["https://pub-x.r2.dev/grids/1.jpg"] = b"grid-jpeg-bytes"

        monkeypatch.setattr(
            "app.core.storage.httpx.get",
            lambda url, **kwargs: Mock(content=contents_by_url[url]),
        )

        zip_bytes = _build_image_export_zip("https://pub-x.r2.dev/grids/1.jpg", cuts)

        with ZipFile(BytesIO(zip_bytes)) as zip_file:
            assert "grid.jpg" in zip_file.namelist()
            assert zip_file.read("grid.jpg") == b"grid-jpeg-bytes"


def _solid_png_bytes(color: tuple[int, int, int] = (200, 50, 50), size: tuple[int, int] = (40, 40)) -> bytes:
    from PIL import Image

    image = Image.new("RGB", size, color)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _solid_jpeg_bytes(color: tuple[int, int, int] = (200, 50, 50), size: tuple[int, int] = (40, 40)) -> bytes:
    from PIL import Image

    image = Image.new("RGB", size, color)
    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


class TestDownscaleToJpeg:
    def test_already_small_jpeg_passes_through_unchanged(self):
        """이미 JPEG고 크기도 기준 이하(Gemini 컷 등)면 재인코딩 없이 원본 그대로 반환(이중 압축 방지)"""
        jpeg_bytes = _solid_jpeg_bytes(size=(1264, 848))

        result = _downscale_to_jpeg(jpeg_bytes)

        assert result == jpeg_bytes

    def test_png_gets_reencoded_to_jpeg(self):
        """PNG는(포맷이 다르니) 크기가 작아도 JPEG로 재인코딩되는지"""
        png_bytes = _solid_png_bytes(size=(100, 100))

        result = _downscale_to_jpeg(png_bytes)

        assert result != png_bytes
        assert PILImage.open(BytesIO(result)).format == "JPEG"

    def test_oversized_jpeg_still_gets_downscaled(self):
        """이미 JPEG여도 기준 해상도보다 크면 축소해서 재인코딩되는지"""
        huge_jpeg_bytes = _solid_jpeg_bytes(size=(4000, 3000))

        result = _downscale_to_jpeg(huge_jpeg_bytes)

        resized = PILImage.open(BytesIO(result))
        assert resized.format == "JPEG"
        assert max(resized.size) <= 1600


class TestBuildPdfExport:
    def test_one_page_per_cut_with_shot_number_and_prompt(self, monkeypatch):
        """9컷이면 9페이지, 각 페이지에 "Shot N"과 그 컷의 prompt_text가 들어가는지(F-05: 샷번호+프롬프트 매핑)"""
        cuts = [
            Cut(order_no=n, image_url=f"https://pub-x.r2.dev/cuts/{n}.png", prompt_text=f"description for shot {n}")
            for n in range(9, 0, -1)
        ]  # order_no 역순으로 넣어도 페이지 순서가 1~9로 정렬되는지 같이 확인

        png_bytes = _solid_png_bytes()
        monkeypatch.setattr(
            "app.core.storage.httpx.get", lambda url, **kwargs: Mock(content=png_bytes)
        )

        pdf_bytes = _build_pdf_export(cuts)

        reader = PdfReader(BytesIO(pdf_bytes))
        assert len(reader.pages) == 9
        for index, page in enumerate(reader.pages, start=1):
            text = page.extract_text()
            assert f"Shot {index}" in text
            assert f"description for shot {index}" in text

    def test_downscales_image_to_fit_page(self, monkeypatch):
        """실제 컷 이미지가 페이지보다 훨씬 크면 페이지 안에 들어가도록 줄어드는지(원본 그대로 못 넣음)"""
        cuts = [Cut(order_no=1, image_url="https://pub-x.r2.dev/cuts/1.png", prompt_text="a huge image")]

        huge_png_bytes = _solid_png_bytes(size=(4000, 3000))
        monkeypatch.setattr(
            "app.core.storage.httpx.get", lambda url, **kwargs: Mock(content=huge_png_bytes)
        )

        pdf_bytes = _build_pdf_export(cuts)

        reader = PdfReader(BytesIO(pdf_bytes))
        assert len(reader.pages) == 1
        assert "Shot 1" in reader.pages[0].extract_text()

    def test_narrow_image_is_center_aligned(self, monkeypatch):
        """세로로 좁은 이미지가 페이지 폭을 다 못 채울 때 왼쪽으로 치우치지 않고 가운데 정렬되는지"""
        import app.exports.service as exports_service

        cuts = [Cut(order_no=1, image_url="https://pub-x.r2.dev/cuts/1.png", prompt_text="a tall image")]
        tall_png_bytes = _solid_png_bytes(size=(100, 200))
        monkeypatch.setattr(
            "app.core.storage.httpx.get", lambda url, **kwargs: Mock(content=tall_png_bytes)
        )

        created_images = []
        original_rlimage = exports_service.RLImage
        monkeypatch.setattr(
            exports_service,
            "RLImage",
            lambda *args, **kwargs: created_images.append(original_rlimage(*args, **kwargs)) or created_images[-1],
        )

        _build_pdf_export(cuts)

        assert len(created_images) == 1
        assert created_images[0].hAlign == "CENTER"

    def test_prompt_text_with_markup_special_characters_is_escaped(self, monkeypatch):
        """prompt_text(Claude 생성 텍스트)에 &, <, > 가 들어있어도 reportlab 마크업으로 오인되지 않고
        원문 그대로 보존되는지 — 이스케이프 안 하면 내용이 조용히 손상되거나 파싱 에러로 export 자체가 실패함"""
        tricky_text = "Camera at <50mm lens>, subject wears an AT&T cap, height < 6ft & > 5ft"
        cuts = [Cut(order_no=1, image_url="https://pub-x.r2.dev/cuts/1.png", prompt_text=tricky_text)]

        png_bytes = _solid_png_bytes()
        monkeypatch.setattr(
            "app.core.storage.httpx.get", lambda url, **kwargs: Mock(content=png_bytes)
        )

        pdf_bytes = _build_pdf_export(cuts)

        reader = PdfReader(BytesIO(pdf_bytes))
        assert tricky_text in reader.pages[0].extract_text()
