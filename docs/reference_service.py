"""유저 비즈니스 로직."""

import asyncio
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.core.security import hash_password, verify_password
from app.domain.common.models import Address
from app.domain.common.schemas import AddressCreate
from app.domain.user.models import (
    CertFlag,
    Document,
    DocumentType,
    PhoneVerification,
    User,
    UserRole,
)
from app.services.r2 import delete_image, delete_r2_key, list_r2_keys


# —— 유저 ─────────
async def get_user_by_id(user_id: int, db: AsyncSession) -> User | None:
    """ID로 유저 조회"""
    # 유저 가져올때 서류 정보 가져와야 하면 추후 selectinload 추가 예정
    statement = select(User).where(User.user_id == user_id)
    result = await db.execute(statement)
    # scalar_one_or_none: 결과가 하나면 그거, 없으면 none, 2개 이상은 에러
    return result.scalar_one_or_none()


async def get_user_with_address(user_id: int, db: AsyncSession) -> User | None:
    """ID로 유저 조회 (address relationship 포함, UserResponse 반환 시 사용)"""
    statement = (
        select(User)
        .where(User.user_id == user_id)
        .options(selectinload(User.address))
    )
    result = await db.execute(statement)
    return result.scalar_one_or_none()


async def get_user_by_email(email: str, db: AsyncSession) -> User | None:
    """이메일로 유저 조회"""
    statement = select(User).where(User.email == email.lower())
    result = await db.execute(statement)
    return result.scalar_one_or_none()


async def get_user_by_phone_number(phone_number: str, db: AsyncSession) -> User | None:
    """전화번호로 유저 조회"""
    statement = select(User).where(User.phone_number == phone_number).limit(1)
    result = await db.execute(statement)
    return result.scalar_one_or_none()


async def is_phone_verified(phone_number: str, db: AsyncSession) -> bool:
    """해당 번호의 SMS 인증 완료 여부 확인"""
    statement = (
        select(PhoneVerification)
        .where(
            PhoneVerification.phone_number == phone_number,
            PhoneVerification.is_verified == True,
            PhoneVerification.expires_at > datetime.now(timezone.utc),  # 인증 후 10분 이내만 유효
        )
        .limit(1)
    )
    result = await db.execute(statement)
    # 객체면(none이 아니면) true, none이면 false
    return result.scalar_one_or_none() is not None


async def create_user(
    email: str,
    password: str,
    name: str,
    phone_number: str,
    user_role: UserRole,
    address_data: AddressCreate,
    db: AsyncSession,
) -> User:
    """회원가입: 유저 생성"""
    address = Address(**address_data.model_dump())
    db.add(address)
    await db.flush()  # address_id 확보

    user = User(
        email=email.lower(),
        password=hash_password(password),
        name=name,
        phone_number=phone_number,
        user_role=user_role,
        address_id=address.address_id,
    )
    db.add(user)
    await db.commit()

    return await get_user_with_address(user.user_id, db)


async def authenticate_user(email: str, password: str, db: AsyncSession) -> User | None:
    """로그인: 이메일과 비밀번호로 유저 인증. 실패 시 None 반환."""
    user = await get_user_by_email(email, db)
    if user is None:
        return None
    # 카카오 전용 계정은 비밀번호 로그인 불가
    if user.password is None:
        return None
    if not verify_password(password, user.password):
        return None
    return user


async def update_user(
    user_id: int,
    db: AsyncSession,
    address_data: AddressCreate | None = None,
) -> User | None:
    """마이페이지: 회원정보 수정"""
    user = await get_user_by_id(user_id, db)
    if user is None:
        return None
    if address_data is not None:
        address = await db.get(Address, user.address_id)
        for field, value in address_data.model_dump().items():
            setattr(address, field, value)
    user.updated_at = datetime.now(timezone.utc)
    await db.commit()
    return await get_user_with_address(user_id, db)


async def change_password(
    user_id: int, current_password: str, new_password: str, db: AsyncSession
) -> bool:
    """마이페이지: 비밀번호 변경 (불일치 에러는 router.py에서)"""
    user = await get_user_by_id(user_id, db)
    if user is None:
        return False  # 404 던지는게 맞지만 service 순수 파이썬 영역 유지
    if user.password is None:
        return False
    if not verify_password(current_password, user.password):
        # 현재 비밀번호가 틀리면 변경 거부
        return False
    user.password = hash_password(new_password)  # 해싱(security.py)
    user.updated_at = datetime.now(timezone.utc)
    await db.commit()
    return True


async def reset_password(user_id: int, new_password: str, db: AsyncSession) -> None:
    """비밀번호 찾기: 현재 비밀번호 확인 없이 새 비밀번호로 변경"""
    user = await get_user_by_id(user_id, db)
    if user is None:
        return
    user.password = hash_password(new_password)
    user.updated_at = datetime.now(timezone.utc)
    await db.commit()


async def delete_user(user_id: int, db: AsyncSession) -> None:
    """탈퇴: 유저를 db에서 삭제"""
    user = await get_user_by_id(user_id, db)
    if user is None:
        return  # 여기서 함수 종료하라는 뜻(return None과 같음)
    address_id = user.address_id
    await db.delete(user)
    await db.flush()
    address = await db.get(Address, address_id)
    if address:
        await db.delete(address)
    await db.commit()


# ── 서류 ───────────
async def get_document_by_id(document_id: int, db: AsyncSession) -> Document | None:
    """document_id(PK)에 해당되는 서류 조회"""
    statement = select(Document).where(Document.document_id == document_id)
    result = await db.execute(statement)
    return result.scalar_one_or_none()


async def get_documents_by_user_id(user_id: int, db: AsyncSession) -> list[Document]:
    """마이페이지: 유저의 서류 목록 조회"""
    statement = select(Document).where(Document.user_id == user_id)
    result = await db.execute(statement)
    return list(result.scalars().all())  # 유저당 서류는 복수/ 결과 없으면 빈 리스트 반환


async def create_document(
    user_id: int, document_type: DocumentType, document_url: str, db: AsyncSession
) -> Document:
    """서류 업로드 (cert_flag를 PENDING으로 리셋해 관리자가 재검토하도록 함)"""
    document = Document(
        user_id=user_id,
        document_type=document_type,
        document_url=document_url,
    )
    db.add(document)

    user = await get_user_by_id(user_id, db)
    if user is not None:
        user.cert_flag = CertFlag.PENDING
        user.cert_reject_reason = None

    await db.commit()
    await db.refresh(document)
    return document


async def delete_document(document_id: int, db: AsyncSession) -> None:
    """서류 삭제"""
    document = await get_document_by_id(document_id, db)
    if document is None:
        return  # 이미 서류 없으면 조용히 종료
    await db.delete(document)
    await db.commit()


# ── 카카오 ─────────
async def get_user_by_kakao_id(kakao_id: str, db: AsyncSession) -> User | None:
    """카카오 콜백용: kakao_id로 기존 유저만 조회 (신규면 None 반환)"""
    statement = (
        select(User)
        .where(User.kakao_id == kakao_id)
        .options(selectinload(User.address))
    )  # selectinload: 유저+주소 한번에 조회
    result = await db.execute(statement)
    return result.scalar_one_or_none()


async def create_kakao_user(
    kakao_id: str,
    name: str,
    phone_number: str,
    user_role: UserRole,
    address_data: AddressCreate,
    db: AsyncSession,
) -> User:
    """카카오 전용 회원가입: register.html 제출 완료 후 호출"""
    address = Address(**address_data.model_dump())
    db.add(address)
    await db.flush()  # 주소 테이블에서 address_id 확보

    user = User(
        kakao_id=kakao_id,
        name=name,
        phone_number=phone_number,
        address_id=address.address_id,
        user_role=user_role,
    )
    db.add(user)
    await db.commit()
    # 일반 유저와 반환값 형태 동일(이메일, 비번은 null)
    return await get_user_with_address(user.user_id, db)


# ── SMS 인증 ────────
async def send_phone_verification(phone_number: str, db: AsyncSession) -> bool:
    """SMS 인증 코드 생성하고 발송. 발송 성공 시 True 반환."""
    # sms.py에서 service.py를 참조하고있어서 순환오류 빠지지않도록 함수안에서 import
    from app.services.sms import send_auth_sms
    # 코드 랜덤생성 6자리 / 만료시간 3분
    code = "".join(secrets.choice("0123456789") for _ in range(6))
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(minutes=3)

    # 기존 미만료·미인증 코드 만료처리 (동일한 번호로 여러번 재발송한 경우)
    await db.execute(
        update(PhoneVerification)
        .where(
            PhoneVerification.phone_number == phone_number,
            PhoneVerification.is_verified == False,
            PhoneVerification.expires_at > now,
        )
        .values(expires_at=now)
    )

    # SMS 먼저 발송 (실패하면 DB 저장 X)
    success = await send_auth_sms(phone_number, code)
    if not success:
        return False

    verification = PhoneVerification(
        phone_number=phone_number,
        code=code,
        expires_at=expires_at,
    )
    db.add(verification)
    await db.commit()

    return True


async def verify_phone_code(phone_number: str, code: str, db: AsyncSession) -> bool | None:
    """SMS 인증 코드 확인. True: 성공 / False: 코드 불일치 / None: 만료 또는 없음."""
    statement = (
        select(PhoneVerification)
        .where(PhoneVerification.phone_number == phone_number)
        .order_by(PhoneVerification.created_at.desc())
        .limit(1)
    )
    result = await db.execute(statement)
    verification = result.scalar_one_or_none()

    if verification is None:
        return None
    # 이미 인증에 사용된 코드, 시간 만료된 코드 -> None 반환
    expires_at = verification.expires_at
    if expires_at.tzinfo is None: # 타임존 없으면 utc 붙여주기
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if verification.is_verified or expires_at < datetime.now(timezone.utc):
        return None
    if verification.code != code:
        return False

    verification.is_verified = True
    verification.expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)  # 가입 완료 여유시간
    await db.commit()
    return True


async def delete_phone_verifications(phone_number: str, db: AsyncSession) -> None:
    """가입 완료 후, 해당 번호의 인증 기록 전체 삭제."""
    await db.execute(
        delete(PhoneVerification).where(PhoneVerification.phone_number == phone_number)
    )
    await db.commit()


# ── 정리 스케줄러용 함수 ────────
async def delete_expired_phone_verifications(db: AsyncSession) -> int:
    """만료된 phone_verifications(sms 인증코드) 행 삭제/ 삭제된 행 수 반환."""
    now = datetime.now(timezone.utc)
    result = await db.execute(
        delete(PhoneVerification).where(PhoneVerification.expires_at < now)
    )
    await db.commit()
    return result.rowcount


async def delete_duplicate_documents(db: AsyncSession) -> int:
    """같은 유저의 같은 유형 서류들 중 최신 1개만 남기고 나머지 R2, DB 삭제/ 삭제된 수 반환."""
    # 유저당 같은 서류유형 중복그룹과 가장 최신파일 뽑기
    subq = (
        select(
            Document.user_id,
            Document.document_type,
            func.max(Document.created_at).label("max_created_at"),
        )
        .group_by(Document.user_id, Document.document_type)
        .having(func.count(Document.document_id) > 1)
        .subquery()
    )
    # 가장 최신파일보다 오래된 것들만 stmt
    stmt = select(Document).join(  # join(대상, on조건)
        subq,
        (Document.user_id == subq.c.user_id)
        & (Document.document_type == subq.c.document_type)
        & (Document.created_at < subq.c.max_created_at),  # c: columns
    )
    result = await db.execute(stmt)
    # db에서 뽑아내서 duplicates에 list로 저장
    duplicates = list(result.scalars().all())

    if not duplicates:
        return 0

    # R2 파일 먼저 삭제(db 먼저 지우면 url 잃어버려서 안됨)
    await asyncio.gather(
        *[delete_image(doc.document_url) for doc in duplicates],
        return_exceptions=True,
    )

    # db 파일 삭제
    duplicate_ids = [doc.document_id for doc in duplicates]
    await db.execute(delete(Document).where(Document.document_id.in_(duplicate_ids)))
    await db.commit()
    return len(duplicates)


async def delete_orphan_r2_documents(db: AsyncSession) -> int:
    """R2 private 버킷 documents/ 폴더 파일 중 DB에 없는 파일 삭제, 삭제된 수 반환."""
    # 로컬 개발환경에서는 팀원마다 DB가 달라 다른 팀원 파일을 고아로 오인할 수 있음
    if settings.DEBUG:
        return 0

    # db에서 모든 document_url을 set(해시 때문에 조회 빠름)으로 가져옴
    result = await db.execute(select(Document.document_url))
    db_urls = set(result.scalars().all())

    bucket = settings.R2_PRIVATE_BUCKET
    url_prefix = f"{settings.R2_ENDPOINT}/{bucket}/"

    # R2 key로 url 조립해서 db set에 있는지 비교 -> db에 없으면 삭제
    orphan_count = 0
    for key in await asyncio.to_thread(list_r2_keys, bucket, "documents/"):
        r2_url = f"{url_prefix}{key}"
        if r2_url not in db_urls:
            if await asyncio.to_thread(delete_r2_key, bucket, key):
                orphan_count += 1

    return orphan_count
