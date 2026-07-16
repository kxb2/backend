from io import BytesIO
from unittest.mock import Mock
from zipfile import ZipFile

import pytest

from app.core.enums import ExportType, JobStatus
from app.exports.models import Export
from app.exports.service import (
    GenerationNotCompleted,
    StoryboardNotFound,
    _build_image_export_zip,
    create_image_export,
    get_export,
)
from app.generations.models import Cut, Generation
from app.storyboards.models import Storyboard

"""Export 관련 순수 로직 테스트 파일 (run_image_export 오케스트레이션 본체는 test_run_image_export.py에서)"""


def _storyboard_with_completed_generation(storyboard_id: int = 1) -> Storyboard:
    storyboard = Storyboard(id=storyboard_id)
    storyboard.generation = Generation(
        storyboard_id=storyboard_id, status=JobStatus.COMPLETED, grid_image_url="https://pub-x.r2.dev/grids/1.png"
    )
    return storyboard


class TestCreateImageExport:
    def test_creates_export_row_with_pending_status(self):
        """정상 케이스: Export row가 IMAGE 타입/PENDING 상태로 생성되고 commit되는지"""
        db = Mock()
        db.get.return_value = _storyboard_with_completed_generation()

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

    def test_raises_when_generation_not_completed(self):
        """9컷 생성이 아직 완료 안 됐으면 GenerationNotCompleted"""
        storyboard = _storyboard_with_completed_generation()
        storyboard.generation.status = JobStatus.PROCESSING
        db = Mock()
        db.get.return_value = storyboard

        with pytest.raises(GenerationNotCompleted):
            create_image_export(db, 1, include_individual_cuts=False)

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
            "app.exports.service.httpx.get",
            lambda url, **kwargs: Mock(content=contents_by_url[url]),
        )

        zip_bytes = _build_image_export_zip("https://pub-x.r2.dev/grids/1.png", cuts)

        with ZipFile(BytesIO(zip_bytes)) as zip_file:
            names = sorted(zip_file.namelist())
            assert names == ["cut_1.png", "cut_2.png", "cut_3.png", "cut_4.png", "cut_5.png", "cut_6.png", "cut_7.png", "cut_8.png", "cut_9.png", "grid.png"]
            assert zip_file.read("grid.png") == b"grid-bytes"
            assert zip_file.read("cut_5.png") == b"cut-5-bytes"
