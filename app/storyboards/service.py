from fastapi import UploadFile
from sqlalchemy.orm import Session

from app.core import storage
from app.core.enums import ImageModel, JobStatus
from app.generations.models import Cut, Generation
from app.storyboards.models import ReferenceImage, Storyboard

MAX_REFERENCE_IMAGES = 10
CUT_COUNT = 9


class ReferenceImageLimitExceeded(Exception):
    def __init__(self, limit: int):
        self.limit = limit


def create_storyboard(
    db: Session,
    *,
    scenario_text: str,
    genre: str,
    style: str | None,
    tone: str | None,
    aspect_ratio: str | None,
    era: str | None,
    image_model: ImageModel,
    reference_images: list[UploadFile],
) -> tuple[Storyboard, Generation]:
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

    for image_url in storage.upload_images_parallel(reference_data, folder="reference-images"):
        db.add(ReferenceImage(storyboard_id=storyboard.id, image_url=image_url))

    generation = Generation(storyboard_id=storyboard.id, status=JobStatus.PENDING)
    db.add(generation)
    db.flush()

    for order_no in range(1, CUT_COUNT + 1):
        db.add(Cut(storyboard_id=storyboard.id, order_no=order_no, status=JobStatus.PENDING))

    db.commit()
    return storyboard, generation


def get_storyboard(db: Session, storyboard_id: int) -> Storyboard | None:
    return db.get(Storyboard, storyboard_id)
