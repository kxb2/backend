# 백엔드 작업 메모 (개인용)


## 확정 사항
- 배포: AWS EC2 (t3.small, 서울 ap-northeast-2, Ubuntu 24.04 LTS, 30GB) — 전 개발자 Dockerfile/docker-compose/GitHub Actions(`deploy.yml`) 재사용, Dockerfile만 Python(FastAPI)용으로 교체
- 프롬프트 생성: Claude API (PRD 기술스택 확정 사항)
- 이미지 생성: GPT image(기본값) / Gemini 3.5 Flash Image, 어댑터 패턴으로 호출
- 유저/ 로그인 최종 확정 정책: 사내 이메일(도메인) 제한 없음, 일반 이메일 회원가입/로그인 및 구글 소셜 로그인(OAuth) 동시 지원.
최초 검토했던 폐쇄형 화이트리스트 방식에서 '일반 유저 가입이 가능한 오픈형 방식'으로 기획 방향이 변경.


## 검토 중 (미확정)
- DB: Supabase(Postgres) — 관계형 구조(스토리보드-컷-캔버스-Export) 적합
- 이미지 스토리지: Cloudflare R2 — 대역폭 무료라 이미지 위주 프로젝트에 유리
- 마이그레이션: Alembic 예정 (models.py db 스키마 안정화된후 세팅)
- ORM — SQLAlchemy
- 드라이버 — psycopg2-binary (한번 더 검토 필요)


## DB 전체 ER 스케치 (큰 그림, 실제 구현은 이슈별로 순차 진행)

| 테이블 | 핵심 컬럼 | 비고 | 구현 시점 |
|---|---|---|---|
| `storyboards` | scenario_text, genre, style/tone/aspect_ratio/era, image_model | 고급설정 포함 | **스토리보드 생성 이슈** |
| `reference_images` | storyboard_id(FK), image_url | 0~10장 | **스토리보드 생성 이슈** |
| `cuts` | storyboard_id(FK), order_no(1~9), prompt_text, angle_type, image_url, status | 9컷 순서 고정 | **스토리보드 생성 이슈** |
| `generations` | storyboard_id(FK), status | 9컷 생성 job 추적 (API_SPEC의 generationId) | **스토리보드 생성 이슈** |
| `regenerations` | cut_id(FK), status | 컷 재생성 job 추적 | 이슈 #3 재생성 때 |
| `canvases` | storyboard_id(FK, nullable) | 독립 보드라 nullable | 캔버스 이슈 때 |
| `canvas_elements` | canvas_id(FK), type, content_url, x, y | 자유배치 | 캔버스 이슈 때 |
| `canvas_connections` | canvas_id(FK), from_element_id, to_element_id | 노드 연결 | 캔버스 이슈 때 |
| `exports` | storyboard_id(FK), type(pdf/image), status, download_url | | Export 이슈 때 |
| `users` | email, password_hash, name, role, created_at/updated_at | A2팀 컬럼 구조 참고 (user_관련공지.txt) | 인증 이슈 (3주차) |
+ user 만들어지면 `storyboards`에 `user_id`(FK) 컬럼 추가해서 누가 만들었는지 기록


## Git 브랜치 워크플로우
- `main`: 배포용. **여기 push되면 GitHub Actions(`deploy.yml`)가 자동으로 EC2에 배포**함
- `develop`: 중간 점검. 기능들이 여기 쌓이는 곳, 여기 머지돼도 실제 서버는 안 바뀜
- `feature/xxx`: 기능 단위 브랜치. 작업은 항상 여기서 시작

**작업 순서**
1. `git checkout develop` → `git checkout -b feature/작업명` (develop 기준으로 새 브랜치 생성)
2. 작업하고 커밋 (`git commit -m "타입: 내용 #이슈번호"`)
3. `git push -u origin feature/작업명`
4. GitHub에서 PR 생성: **`feature/작업명 → develop`** (CONVENTION.md PR 템플릿 사용)
5. PR 머지 → develop에 반영됨 (기능 하나 끝날 때마다 이 4~5번을 반복, 자주 함)
6. develop에 기능이 어느 정도 쌓이고 **진짜 배포하고 싶을 때만** 별도로 **`develop → main` PR** 생성 + 머지 (이때만 자동 배포 트리거됨, 가끔 함)

**헷갈렸던 부분**: `develop`에 로컬에서 직접 pull/merge 하는 게 아니라, **GitHub에서 PR을 머지하는 행위 자체가 그 역할**을 한다. 로컬 `develop`은 머지 끝난 뒤에 `git pull`로 받아오기만 하면 됨.


## ⚠️ Supabase 무료 플랜 주의사항
- 무료 프로젝트는 **7일간 API/DB 요청이 없으면 자동 일시정지**됨
- 개발 중엔 실제 API 테스트하면서 자연스럽게 활동이 잡히니 신경 안 써도 되지만, **며칠 이상 작업을 쉬었다 다시 시작할 때**는 대시보드 들어가서 상태 확인
- 멈춰있으면 대시보드에 "Restore project" 버튼 뜸 → 클릭 후 1~2분 대기하면 복구 (데이터는 유지됨, 연결 문자열도 안 바뀜)
- 대시보드 → Table Editor 열어서 살아있는지 확인할 것 (요청이 실제로 DB까지 도달해야 활동으로 잡힘 — 단순 로그인만으론 부족할 수 있음)


## 나중에 필요하면 고려 (지금은 안 함)
- **`POST /storyboards` idempotency key**: storyboard 저장은 성공했는데 9컷 생성 job 등록(enqueue)만 실패하는 경우, 재시도 시 storyboard row가 중복 생성될 수 있음. 문제 되면 같은 입력(or idempotency key)이면 기존 storyboard를 재사용하도록 처리하는 것 고려. 60시간 MVP라 지금 우선순위 아님 — 중복 row 생겨도 당장은 무해함.

---

## 백엔드 담당 작업 목록 (PRD / 백로그.md 추출)

> 진행 순서 요약 (오늘 7/8 ~ 마감 7/29): **스토리보드 → 컷 생성 → (여유되면 캔버스 병행) → Export → 인증 → NFR은 진행 내내 챙김.** 근거: PRD §4 우선순위표엔 인증이 없음(P0는 입력→9컷생성→Export까지) + `user_관련공지.txt`상 통일되지도 않았고 올인원 되면 어차피 엎고 다시 만듦.

### 스토리보드
- [ ] 스토리보드 생성+저장 — 시나리오 텍스트, 장르, 이미지를 받아 저장 **(1주차)** — 9컷 생성과 API 통합 여부
- [ ] 스토리보드 조회 — 저장된 스토리보드 정보(시나리오/장르/이미지) 조회 **(1주차)**
- [ ] 레퍼런스 이미지 업로드 — 캐릭터/배경/소품 이미지 업로드·저장, AI에 반환 **(1주차)**
- [ ] 첨부 이미지 수량(0~10장) — 서버 측 검증 — 스토리보드 생성과 함께 **(1주차)**
- [ ] 장르 프리셋 기본값 자동 적용 — 고급 설정 없을 시 장르 기본값(톤·스타일) 채우기 **(2주차, 3순위·여유 시)**

### 컷 생성
- [ ] API 연동 — GPT, Gemini, Claude API 연동 **(1주차, 어댑터 패턴 골격)**
- [ ] 9컷 생성 요청 — 생성 버튼 클릭 시 AI에 스토리보드 값 전달, 가능하면 병렬 처리 **(2주차)**
- [ ] 컷별 프롬프트 적용 — 컷마다 프롬프트 연결 **(2주차)**
- [ ] 샷별 프롬프트 3000자 제한 — 서버 검증 — 컷별 프롬프트 적용과 함께 **(2주차)**
- [ ] 컷 생성 상태 확인 — 대기/생성/완료/실패 상태 주기적 확인 **(2주차)**
- [ ] 컷 재생성 — 특정 컷만 재생성, 컷ID 부여 필요 **(2주차)**

### 캔버스
- [ ] 캔버스 조회 — 저장된 컷 배치 위치 불러오기 **(2주차, 여유되면)**
- [ ] 캔버스 배치 저장 — 각 컷 위치(X,Y) 저장 **(2주차, 여유되면)**

### Export
- [ ] 이미지 내보내기 — 3×3 그리드 이미지 1장 기본 내보내기, 추후 개별 이미지 옵션 **(3주차)**
- [ ] PDF 내보내기 — 9컷 이미지+프롬프트가 담긴 PDF 생성, 다운로드 링크 반환 **(3주차)**
- [ ] 내보내기 결과 확인 — 완료 여부·다운로드 링크 조회 **(3주차)**

### 인증 (회원가입/로그인)
- [ ] users 테이블 설계 (이메일, 비밀번호 해시 등) **(3주차)**
- [ ] 회원가입 API (이메일 도메인 화이트리스트 적용 여부 — 회의 결과 확정 전, A1팀 방식 참고 예정) **(3주차)**
- [ ] 로그인 API (비밀번호 검증, JWT 발급) **(3주차)**
- [ ] JWT 검증 dependency (다른 라우터에서 재사용) **(3주차)**
- [ ] 계정 로그인 + 접근 권한 제한 (보안 Lv2) — 인증 구현과 함께 **(3주차)**
- [ ] 기존 엔드포인트들 인증, 로그인 필요(Authorization 헤더) 필요/불필요 표시 달고, user 관련 엔드포인트 추가 **(3주차)**

### NFR (PRD §6, 백엔드 관련 항목만)
- [ ] 업로드·생성 이미지 보존 기간 후 삭제 로직 (보안 Lv2) — **진행 중 계속**
- [ ] 모델 API 타임아웃·실패 시 해당 컷만 재시도, 전체 파이프라인 중단 금지 — **진행 중 계속**
- [ ] 피크 시간 503 과부하 대비 재시도 로직 — **진행 중 계속**
- [ ] API 키(이미지 모델·Claude) 클라이언트 노출 금지 — 서버 측 보관 — **진행 중 계속**
