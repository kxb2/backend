from app.core.enums import JobStatus
from app.generations.schemas import CutOut, GenerationDetailResponse


def test_generation_detail_response_serializes_to_camel_case():
    """snake_case 필드(storyboard_id, grid_image_url 등)가 응답 JSON에서는 camelCase로 나가는지"""
    response = GenerationDetailResponse(
        id=1,
        storyboard_id=2,
        status=JobStatus.COMPLETED,
        grid_image_url="https://pub-x.r2.dev/grids/1.png",
        cuts=[
            CutOut(
                id=10,
                order_no=1,
                prompt_text="a cat sits",
                angle_type="wide",
                image_url="https://pub-x.r2.dev/cuts/10.png",
                status=JobStatus.COMPLETED,
            )
        ],
    )

    data = response.model_dump(by_alias=True)

    assert data["storyboardId"] == 2
    assert data["gridImageUrl"] == "https://pub-x.r2.dev/grids/1.png"
    assert data["cuts"][0]["orderNo"] == 1
    assert data["cuts"][0]["imageUrl"] == "https://pub-x.r2.dev/cuts/10.png"


def test_generation_detail_response_allows_null_grid_and_cut_fields_while_processing():
    """아직 처리 중이라 grid_image_url/prompt_text/image_url이 비어있는 상태도 정상 직렬화되는지"""
    response = GenerationDetailResponse(
        id=1,
        storyboard_id=2,
        status=JobStatus.PROCESSING,
        grid_image_url=None,
        cuts=[
            CutOut(
                id=10,
                order_no=1,
                prompt_text=None,
                angle_type=None,
                image_url=None,
                status=JobStatus.PENDING,
            )
        ],
    )

    data = response.model_dump(by_alias=True)

    assert data["gridImageUrl"] is None
    assert data["cuts"][0]["imageUrl"] is None
    assert data["cuts"][0]["status"] == "pending"
