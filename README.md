# kx-backend
## 1. 로컬 실행 방법

### 사전 준비
1. `.env.example`을 복사해서 `.env` 생성
2. `.env`에 실제 값 채우기 — Supabase DB 연결 문자열, Cloudflare R2 키, Claude/OpenAI/Gemini API 키

### 실행 — Docker 없이 직접
```
python -m venv .venv
.venv\Scripts\activate       # Windows
source .venv/bin/activate    # Mac/Linux

pip install -r requirements.txt
uvicorn app.main:app --reload --port 8080
```

### 실행 — Docker
```
docker-compose up --build
```
→ `http://localhost:8080` 에서 서버 실행됨

---

## 2. API 문서 확인 (Swagger)

서버 실행 후 브라우저에서:
- `http://localhost:8080/docs` — Swagger UI (요청 형식 확인 + 직접 호출 테스트 가능)

⚠️ **`POST /storyboards`는 JSON이 아니라 `multipart/form-data`로 보내야 합니다** (레퍼런스 이미지 첨부 때문)

⚠️ **9컷 생성은 비동기(백그라운드)로 처리됩니다.** `POST /storyboards` 응답은 이미지가 만들어지기 전에 먼저 오고(`status: "pending"`), 실제 완료 여부/결과는 `GET /generations/{id}`로 확인해야 합니다.

---

## 확정사항
1) 프론트 배포는 Vercel, 백엔드 배포는 AWS EC2
2) 프롬프트 생성: Claude API
3) 이미지 생성: GPT image(기본값) / Gemini 3.1 Flash Image (3.5가 공식모델에 없어요...)
4) 로그인: 일반 이메일 회원가입/로그인 및 구글 소셜 로그인(OAuth) 동시 지원
5) DB: Supabase(Postgres)
6) ORM: SQLAlchemy, 드라이버: psycopg2-binary
7) 이미지, 파일, 동영상 스토리지: Cloudflare R2 — 대역폭 무료라 이미지 위주 프로젝트에 유리

---

## 검토중
1) 마이그레이션: Alembic 예정

---

## 진행순서
[머지된 PR 목록](https://github.com/kxb2/backend/pulls?q=is%3Apr+is%3Aclosed)

- [x] 스토리보드 생성/조회
- [x] 레퍼런스 이미지 업로드
- [x] 9컷 생성 + AI 연동 (Claude 통합 프롬프트 → 컷별 분리 → GPT/Gemini 9컷 이미지 병렬 생성 → 3×3 그리드 합성)
- [x] 9컷 생성 상태/결과 조회 (`GET /generations/{id}`)
- [x] 내보내기 (PDF/이미지 Export)
- [x] 컷 재생성 (기존 컷별 프롬프트 사용해서/ 재명령은 아직 X)
- [x] 캔버스 조회/저장
- [ ] 유저/인증 로그인
- [ ] 스토리보드 관련 고도화 ?
- [ ] 캔버스 관련 고도화 ?
