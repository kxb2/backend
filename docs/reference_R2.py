"""Cloudflare R2 이미지 업로드 유틸리티."""

import enum
import logging
import uuid
from io import BytesIO

import boto3
from botocore.client import BaseClient
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import HTTPException, UploadFile
from PIL import Image

from app.config import settings

logger = logging.getLogger(__name__)

# 후기 이미지용(이미지만)
IMAGE_CONTENT_TYPES = {"image/jpeg", "image/png"}

# 신원 서류용(이미지 + 문서 형식)
DOCUMENT_CONTENT_TYPES = {
    "image/jpeg",                # 브라우저가 서버에 보내는값(MIME 타입)
    "image/png",
    "application/pdf",
    "application/x-hwp",         # hwp: 브라우저마다 다른 유형 2개
    "application/haansoft-hwp",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/haansoftdocx",  # docx: 공식 유형, 한컴회사꺼 2개
}

MAX_FILE_SIZE = 5 * 1024 * 1024  # 5MB

# Content-Type별 매직 바이트 (파일 앞부분 시그니처)
# Content-Type 헤더는 클라이언트가 위조 가능해서 실제 파일 바이트로 2차 검증
_MAGIC_BY_MIME: dict[str, bytes] = {
    "image/jpeg": b"\xff\xd8\xff",
    "image/png":  b"\x89PNG\r\n\x1a\n",
    "application/pdf": b"%PDF",
    "application/x-hwp":        b"\xd0\xcf\x11\xe0",  # hwp: OLE2 컨테이너 포장
    "application/haansoft-hwp": b"\xd0\xcf\x11\xe0",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": b"PK\x03\x04",
    "application/haansoftdocx": b"PK\x03\x04",        # docx: ZIP 컨테이너 포장
}


def _verify_magic(content_type: str, data: bytes) -> bool:
    """선언된 Content-Type과 실제 파일 바이트 시그니처가 일치하는지 확인"""
    expected = _MAGIC_BY_MIME.get(content_type)
    if expected is None:
        return False
    return data.startswith(expected)


# 파일 형식이 늘어나서 딕셔너리로 변환
CONTENT_TYPE_EXT = {
    "image/jpeg": "jpg",         # R2에 저장할때 쓰는 확장자
    "image/png": "png",
    "application/pdf": "pdf",
    "application/x-hwp": "hwp",
    "application/haansoft-hwp": "hwp",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/haansoftdocx": "docx",
}


class BucketType(enum.Enum):
    PUBLIC = "public"    # 공개 버킷 (후기 이미지)
    PRIVATE = "private"  # 비공개 버킷 (신원 서류)


def _get_client() -> BaseClient:
    """R2 클라이언트를 반환합니다."""
    return boto3.client(
        "s3",
        endpoint_url=settings.R2_ENDPOINT,
        aws_access_key_id=settings.R2_ACCESS_KEY_ID,
        aws_secret_access_key=settings.R2_SECRET_ACCESS_KEY,
        region_name="auto",
    )


async def upload_image(
    file: UploadFile,
    folder: str,
    bucket: BucketType = BucketType.PUBLIC,
    allowed_types: set[str] | None = None,  # None이면 기본값 적용
) -> str:
    """이미지를 R2에 업로드하고 URL을 반환합니다.

    Args:
        file: 업로드할 이미지 파일
        folder: 저장할 폴더명 (예: "reviews", "documents")
        bucket: 버킷 유형 (BucketType.PUBLIC 또는 BucketType.PRIVATE)

    Returns:
        업로드된 이미지의 URL
    """
    # allowed_types 지정하면 지정한걸로 / 따로 지정안하면 기본: IMAGE_CONTENT_TYPES
    types = allowed_types if allowed_types is not None else IMAGE_CONTENT_TYPES
    if file.content_type not in types:
        raise HTTPException(status_code=400, detail="허용되지 않는 파일 형식입니다.")

    contents = await file.read() # 파일의 바이트 데이터

    if len(contents) > MAX_FILE_SIZE:
        raise HTTPException(status_code=400, detail="파일 크기는 최대 5MB까지 허용됩니다.")

    if not _verify_magic(file.content_type, contents):
        raise HTTPException(status_code=400, detail="허용되지 않는 파일 형식입니다.")

    # 이미지 파일은 Pillow로 실제 구조 파싱 (polyglot 방어)
    if file.content_type in {"image/jpeg", "image/png"}:
        try:
            img = Image.open(BytesIO(contents))
            img.verify() # 진짜 이미지 데이터인지 해석
        except Exception:
            raise HTTPException(status_code=400, detail="허용되지 않는 파일 형식입니다.")

    ext = CONTENT_TYPE_EXT.get(file.content_type, "bin")
    key = f"{folder}/{uuid.uuid4().hex}.{ext}"
    bucket_name = (
        settings.R2_PUBLIC_BUCKET if bucket == BucketType.PUBLIC else settings.R2_PRIVATE_BUCKET
    )

    try:
        client = _get_client()
        client.put_object(
            Bucket=bucket_name,
            Key=key,
            Body=contents,
            ContentType=file.content_type,
        )
        logger.info("R2 업로드 완료: bucket=%s key=%s", bucket_name, key)
    except (BotoCoreError, ClientError) as e:
        logger.error("R2 업로드 실패: %s", e)
        raise HTTPException(status_code=503, detail="이미지 업로드에 실패했습니다.")

    # 공개 버킷은 CDN URL, 비공개 버킷은 엔드포인트 URL 반환
    if bucket == BucketType.PUBLIC:
        return f"{settings.R2_PUBLIC_URL}/{key}"
    return f"{settings.R2_ENDPOINT}/{bucket_name}/{key}"


def get_presigned_url(document_url: str, expires_in: int = 300) -> str:
    """비공개 R2 파일의 presigned URL을 반환합니다 (기본 5분 유효)."""
    bucket = settings.R2_PRIVATE_BUCKET
    prefix = f"{settings.R2_ENDPOINT}/{bucket}/"
    if not document_url.startswith(prefix):
        raise HTTPException(status_code=500, detail="잘못된 서류 URL")
    key = document_url[len(prefix):]
    client = _get_client()
    return client.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=expires_in,
    )


async def delete_image(image_url: str) -> None:
    """R2에서 이미지를 삭제합니다.

    Args:
        image_url: 삭제할 이미지의 URL
    """
    # 공개/비공개 버킷 판별 후 key 추출
    if image_url.startswith(settings.R2_PUBLIC_URL):
        bucket = settings.R2_PUBLIC_BUCKET
        key = image_url[len(settings.R2_PUBLIC_URL) + 1:]
    else:
        bucket = settings.R2_PRIVATE_BUCKET
        prefix = f"{settings.R2_ENDPOINT}/{bucket}/"
        if not image_url.startswith(prefix):
            return
        key = image_url[len(prefix):]

    try:
        client = _get_client()
        client.delete_object(Bucket=bucket, Key=key)
        logger.info("R2 삭제 완료: bucket=%s key=%s", bucket, key)
    except (BotoCoreError, ClientError) as e:
        logger.error("R2 삭제 실패: %s", e)  # R2 삭제 실패해도 DB 삭제는 진행


def list_r2_keys(bucket: str, prefix: str) -> list[str]:
    """R2 버킷의 특정 prefix 아래 모든 Key 목록을 반환합니다."""
    client = _get_client()
    # prefix: 리뷰사진은 reviews/, 회원서류는 documents/ (각자 라우터파일에서 확인가능)
    # R2는 한번에 1000개까지만 반환. 1000개 넘으면 자동으로 다음 페이지 넘어감.
    paginator = client.get_paginator("list_objects_v2")
    keys = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            keys.append(obj["Key"])
    return keys


def delete_r2_key(bucket: str, key: str) -> bool:
    """R2 버킷에서 Key로 직접 삭제합니다. 성공 시 True, 실패 시 False 반환."""
    client = _get_client()
    try:
        client.delete_object(Bucket=bucket, Key=key)
        logger.info("R2 삭제 완료: bucket=%s key=%s", bucket, key)
        return True
    except (BotoCoreError, ClientError) as e:
        logger.error("R2 삭제 실패: bucket=%s key=%s err=%s", bucket, key, e)
        return False
