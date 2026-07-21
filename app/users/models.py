# TODO: User 모델 정의 (email, password_hash, role, created_at/updated_at 등)
# 일반회원계정, 관리자계정 있을것같다고 일단 role은 넣어두기로
from app.db.base import Base

# 회원가입할때 프론트 필드: 일반이메일, 비밀번호, 비밀번호확인
# 회원가입 방식 2가지: 일반이메일 / 구글연동
# 일반이메일은 jwt 토큰 방식 - access, refresh 토큰
# 내가 다른프로젝트에서 일반이메일/카카오연동했던 레퍼런스파일 참고: reference_router, schemas, service.py
# (이때 유저/인증 만들었을때 보안은 괜찮았는데 초반에 refresh token 구현못했었음)
#
# 구글 연동(claude가 예전에 대답한 원문): "Google Cloud Console에서 OAuth Client ID 발급(웹 애플리케이션 타입) + 리디렉션 URI 등록.
#   프론트가 구글 로그인 붙여서 authorization code 또는 id_token을 받아 백엔드로 넘김.
#   id_token 방식이면 google-auth 라이브러리로 서명만 로컬 검증하면 끝/ access_token 방식이면 구글 userinfo 엔드포인트 호출.
#   검증해서 얻은 이메일로 기존 유저면 로그인, 없으면 자동 가입 → 이후는 일반 이메일이랑 똑같이 자체 access/refresh JWT 발급.
#   카카오에서 만들었던 "소셜 로그인 → 유저 조회/생성 → 자체 JWT 발급" 뼈대를 그대로 재사용하고, 구글 검증 부분만 갈아끼우면 돼.
#   오히려 id_token 방식 쓰면 카카오보다 더 간단할 수도(외부 API 왕복 없이 로컬 검증)."
