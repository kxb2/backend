from enum import StrEnum


class JobStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class ImageModel(StrEnum):
    GPT_IMAGE = "gpt_image"
    GEMINI_3_5_FLASH_IMAGE = "gemini_3_5_flash_image"