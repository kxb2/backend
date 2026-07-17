"""Cloudflare R2(S3 호환) 스토리지 업로드/다운로드"""

import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, wait
from functools import lru_cache

import boto3
import httpx
from botocore.client import BaseClient
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import HTTPException, UploadFile

from app.core.config import get_settings

logger = logging.getLogger(__name__)

settings = get_settings()

ALLOWED_IMAGE_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}
MAX_IMAGE_SIZE = 10 * 1024 * 1024  # 10MB

# R2 다운로드(그리드 합성, export시) 오류 재시도 로직 추가
MAX_DOWNLOAD_RETRIES = 2
_RETRYABLE_DOWNLOAD_STATUS_CODES = {429, 500, 502, 503, 504}

CONTENT_TYPE_EXT = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}


def _verify_image_magic(content_type: str, data: bytes) -> bool:
    """선언된 Content-Type과 실제 파일 바이트 시그니처가 일치하는지 확인
    (Content-Type 헤더는 클라이언트가 위조 가능)."""
    if content_type == "image/jpeg":
        return data.startswith(b"\xff\xd8\xff")
    if content_type == "image/png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if content_type == "image/webp":
        return data.startswith(b"RIFF") and data[8:12] == b"WEBP"
    return False


@lru_cache
def _get_client() -> BaseClient:
    return boto3.client(
        "s3",
        endpoint_url=settings.r2_endpoint_url,
        aws_access_key_id=settings.r2_access_key_id,
        aws_secret_access_key=settings.r2_secret_access_key,
        region_name="auto",
    )


def upload_bytes(data: bytes, key: str, content_type: str) -> str:
    """바이트 데이터를 R2 버킷에 업로드하고 공개 URL을 반환."""
    try:
        client = _get_client()
        client.put_object(
            Bucket=settings.r2_bucket_name,
            Key=key,
            Body=data,
            ContentType=content_type,
        )
        logger.info("R2 업로드 완료: key=%s", key)
    except (BotoCoreError, ClientError) as e:
        logger.error("R2 업로드 실패: key=%s err=%s", key, e)
        raise HTTPException(status_code=503, detail=f"파일 업로드에 실패했습니다: {e}") from e

    return f"{settings.r2_public_url}/{key}"


def download_bytes(url: str, *, timeout: float = 30.0) -> bytes:
    """R2에 올라간 파일을 URL로 다시 읽어옴(그리드 합성, Export zip 등에서 공용 사용).

    ㅡ 타임아웃/429/5xx처럼 일시적인 실패만 지수 백오프로 재시도.
    ㅡ 403/404 등 재시도해도 성공 못하는 응답은 즉시 예외 전파(raise_for_status).
    """
    attempt = 0
    while True:
        try:
            response = httpx.get(url, timeout=timeout)
            response.raise_for_status()
            return response.content
        except httpx.TimeoutException as exc:
            retryable, wait_exc = True, exc
        except httpx.HTTPStatusError as exc:
            retryable = exc.response.status_code in _RETRYABLE_DOWNLOAD_STATUS_CODES
            wait_exc = exc

        if not retryable or attempt >= MAX_DOWNLOAD_RETRIES:
            raise wait_exc

        delay = 2**attempt
        logger.warning(
            "R2 다운로드 실패(%s), %ds 후 재시도 (%d/%d): %s",
            url,
            delay,
            attempt + 1,
            MAX_DOWNLOAD_RETRIES,
            wait_exc,
        )
        time.sleep(delay)
        attempt += 1


def delete_file(url: str) -> None:
    """공개 URL로 R2 파일을 삭제 (실패해도 예외를 던지지 않고 로그만 남김)."""
    prefix = f"{settings.r2_public_url}/"
    if not url.startswith(prefix):
        return
    key = url[len(prefix):]

    try:
        client = _get_client()
        client.delete_object(Bucket=settings.r2_bucket_name, Key=key)
        logger.info("R2 삭제 완료: key=%s", key)
    except (BotoCoreError, ClientError) as e:
        logger.error("R2 삭제 실패: key=%s err=%s", key, e)


def validate_image(file: UploadFile) -> bytes:
    """이미지 파일을 검증하고 바이트 데이터를 반환 (R2 업로드는 X)."""
    if file.content_type not in ALLOWED_IMAGE_CONTENT_TYPES:
        raise HTTPException(status_code=400, detail="허용되지 않는 이미지 형식입니다.")

    data = file.file.read(MAX_IMAGE_SIZE + 1)
    # 초과 용량을 업로드하면 전체 메모리 읽지 않고, 한도 +1바이트만 더 읽고 거부

    if len(data) > MAX_IMAGE_SIZE:
        raise HTTPException(status_code=400, detail="이미지 크기는 최대 10MB까지 허용됩니다.")

    if not _verify_image_magic(file.content_type, data):
        raise HTTPException(status_code=400, detail="허용되지 않는 이미지 형식입니다.")

    return data


def upload_image_bytes(data: bytes, content_type: str, folder: str) -> str:
    """검증된 이미지 바이트를 R2에 업로드하고 공개 URL을 반환."""
    ext = CONTENT_TYPE_EXT[content_type]
    key = f"{folder}/{uuid.uuid4().hex}.{ext}"
    return upload_bytes(data, key, content_type)


def upload_images_parallel(items: list[tuple[bytes, str]], folder: str) -> list[str]:
    """(바이트, content_type) 목록을 병렬 업로드.

    하나라도 업로드에 실패하면, 이미 업로드에 성공한 파일들을 R2에서 롤백(삭제)하고
    원래 예외를 다시 던짐.
    """
    if not items:
        return []

    with ThreadPoolExecutor(max_workers=len(items)) as executor:
        futures = [
            executor.submit(upload_image_bytes, data, content_type, folder)
            for data, content_type in items
        ]
        wait(futures)

    uploaded_urls: list[str] = []
    error: BaseException | None = None
    for future in futures:
        exc = future.exception()
        if exc is not None:
            error = error or exc
        else:
            uploaded_urls.append(future.result())

    if error is not None:
        for url in uploaded_urls:
            delete_file(url)
        raise error

    return uploaded_urls
