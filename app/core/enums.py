from enum import StrEnum


def enum_values(enum_cls: type[StrEnum]) -> list[str]:
    """SQLAlchemy Enum(values_callable=...)용 — DB에 .name 대신 .value를 저장하도록 강제."""
    return [member.value for member in enum_cls]


class JobStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class ImageModel(StrEnum):
    GPT_IMAGE = "gpt_image"
    GEMINI_3_5_FLASH_IMAGE = "gemini_3_5_flash_image"


class Genre(StrEnum):
    DRAMA = "드라마"
    ACTION = "액션"
    ROMANCE = "로맨스"
    THRILLER = "스릴러"
    COMEDY = "코미디"


class ExportType(StrEnum):
    PDF = "pdf"
    IMAGE = "image"
