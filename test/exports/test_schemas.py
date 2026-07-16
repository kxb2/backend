from app.core.enums import ExportType, JobStatus
from app.exports.schemas import ExportDetailResponse, ImageExportRequest


def test_export_detail_response_serializes_to_camel_case():
    """snake_case 필드(storyboard_id, download_url 등)가 응답 JSON에서는 camelCase로 나가는지"""
    response = ExportDetailResponse(
        id=1,
        storyboard_id=2,
        type=ExportType.IMAGE,
        status=JobStatus.COMPLETED,
        download_url="https://pub-x.r2.dev/grids/1.png",
    )

    data = response.model_dump(by_alias=True)

    assert data["storyboardId"] == 2
    assert data["downloadUrl"] == "https://pub-x.r2.dev/grids/1.png"


def test_export_detail_response_allows_null_download_url_while_processing():
    """아직 처리 중이라 download_url이 비어있는 상태도 정상 직렬화되는지"""
    response = ExportDetailResponse(
        id=1, storyboard_id=2, type=ExportType.IMAGE, status=JobStatus.PROCESSING, download_url=None
    )

    data = response.model_dump(by_alias=True)

    assert data["downloadUrl"] is None
    assert data["status"] == "processing"


def test_image_export_request_accepts_camel_case_field():
    """요청 body의 camelCase(includeIndividualCuts)를 populate_by_name으로 읽어들이는지"""
    request = ImageExportRequest.model_validate({"includeIndividualCuts": True})

    assert request.include_individual_cuts is True


def test_image_export_request_defaults_to_false():
    """옵션 안 보내면 기본값 False (그리드 1장만)"""
    request = ImageExportRequest.model_validate({})

    assert request.include_individual_cuts is False
