from fastapi import UploadFile
from sqlalchemy.orm import Session

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
    image_model: str,
    reference_images: list[UploadFile],
) -> tuple[Storyboard, Generation]:
    if len(reference_images) > MAX_REFERENCE_IMAGES:
        raise ReferenceImageLimitExceeded(MAX_REFERENCE_IMAGES)

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

    # 이미지 스토리지(Cloudflare R2 등) 미확정 상태라 실제 업로드 대신 파일명만 임시 저장
    for image in reference_images:
        db.add(ReferenceImage(storyboard_id=storyboard.id, image_url=image.filename))

    generation = Generation(storyboard_id=storyboard.id, status="pending")
    db.add(generation)
    db.flush()

    for order_no in range(1, CUT_COUNT + 1):
        db.add(Cut(storyboard_id=storyboard.id, order_no=order_no, status="pending"))

    db.commit()
    return storyboard, generation


def get_storyboard(db: Session, storyboard_id: int) -> Storyboard | None:
    return db.get(Storyboard, storyboard_id)