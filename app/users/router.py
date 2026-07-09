# TODO: 회원가입, 로그인, JWT 검증 dependency
from fastapi import APIRouter

router = APIRouter(prefix="/auth", tags=["auth"])