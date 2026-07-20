# 백엔드 작업 메모 (개인용)
## 확정 사항
- 프론트 배포는 Vercel, 백엔드 배포는 AWS EC2
- 배포: AWS EC2 (t3.small, 서울 ap-northeast-2, Ubuntu 24.04 LTS, 30GB) — 전 개발자 Dockerfile/docker-compose/GitHub Actions(`deploy.yml`) 재사용, Dockerfile만 Python(FastAPI)용으로 교체
- 배포 추가 설정: nginx, HTTPS, Let's Encrypt, duckdns 도메인 완료
- 프롬프트 생성: Claude API (PRD 기술스택 확정 사항)
- 이미지 생성: GPT image(기본값) / Gemini 3.1 Flash Image, 어댑터 패턴으로 호출
- 유저/ 로그인 최종 확정 정책: 사내 이메일(도메인) 제한 없음, 일반 이메일 회원가입/로그인 및 구글 소셜 로그인(OAuth) 동시 지원. 일반 이메일 가입은 가입 링크만 있으면 누구나 가입 가능한 구조로 진행. 최초 검토했던 폐쇄형 화이트리스트 방식에서 '일반 유저 가입이 가능한 오픈형 방식'으로 기획 방향이 변경.
- DB: Supabase(Postgres) — 관계형 구조(스토리보드-컷-캔버스-Export) 적합
- ORM: SQLAlchemy
- 드라이버: psycopg2-binary
- 이미지 스토리지: Cloudflare R2 — 대역폭 무료라 이미지 위주 프로젝트에 유리


## 검토 중 (미확정)
- 마이그레이션: Alembic 예정 (models.py DB 스키마 안정화된후 세팅) -> 연결후엔 db 밀때 alembic 관련도 포함시켜야함, 프론트랑 연결되기 전에 alembic 설정과 db 설계 끝내야함(엥 아직 alembic 안붙임)
+ alembic 처음 도입할때ㅡ 실제 db랑 models.py 정확히 일치해야 깨끗하게 시작 가능. 체크하고 넣기
- 로컬 개발 DB와 배포 DB는 **분리 안 하고 하나만 사용하기로 결정**  `PROD_DATABASE_URL` secret은 로컬 `.env`의 `DATABASE_URL`과 **동일한 값**으로 등록하면 됨(`docker-compose.prod.yml`이 이미 그 이름을 참조하고 있어서 코드 수정 불필요). 대신 **실제 배포(`develop→main`) 직전에 테스트용 row 정리**하는 걸 체크리스트에 넣기 (storyboards 삭제 시 reference_images/generations/cuts는 cascade로 같이 지워짐) → 의견 물어보거나 더 나은 방향 있으면 배포 DB 따로 할수도 있고, 환경에 따라 자동화나... 다른 방향 생각해보는중.
- PRD 7번 장르별 기본 앵글 설정 아직은 필요없는데 추후 넣어야되면: Claude 프롬프트 및 로직 다시 짜야함


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

---

## R2에 실제로 파일이 올라가는 것 (보존기간 후 삭제 대상도 동일)

1. 사용자의 레퍼런스 업로드 이미지 — ReferenceImage.image_url
2. 컷별 개별 생성 이미지 — Cut.image_url (9장)
3. 9컷 그리드 이미지 — Generation.grid_image_url (개별 컷 미포함 이미지 Export 기본값과 동일)
4. PDF Export 문서 — Export.download_url (type=pdf, 항상 신규)
5. 이미지 Export 묶음 파일 — Export.download_url (type=image, "개별 컷 포함" 옵션 켰을 때만 존재하는 신규 zip)
6. 캔버스에 첨부되는 이미지/ 동영상/ 영상썸네일 — 확정: 영상도 presigned URL 아니고 이미지와 동일하게 백엔드 프록시 업로드(일단 50MB 캡) `/canvases/{canvasId}/attachments`


## 백엔드 담당 작업 목록 (PRD / 백로그.md 추출)
> 진행 순서 요약 (오늘 7/8 ~ 마감 7/29): **스토리보드 → 9컷 생성 + AI 연동 → 내보내기 → 캔버스 → 컷 재생성 → 유저/인증**

### 스토리보드
- [ ] 스토리보드 생성+저장 — 시나리오 텍스트, 장르, 이미지를 받아 저장 -> API_SPEC 참고: 9컷 생성과 같은 엔드포인트
- [ ] 스토리보드 조회 — 저장된 스토리보드 정보(시나리오/장르/이미지) 조회
- [ ] 레퍼런스 이미지 업로드 — 캐릭터/배경/소품 이미지 업로드·저장, AI에 반환
- [ ] 첨부 이미지 수량(0~10장) — 서버 측 검증 — 스토리보드 생성과 함께
- [ ] 장르 프리셋 기본값 자동 적용 — 고급 설정 없을 시 장르 기본값(톤·스타일) 채우기 - 3순위

### 컷 생성
- [ ] API 연동 — GPT, Gemini, Claude API 연동
- [ ] 9컷 생성 요청 — 생성 버튼 클릭 시 AI에 스토리보드 값 전달, 가능하면 병렬 처리
- [ ] 컷별 프롬프트 적용 — 컷마다 프롬프트 연결
- [ ] 샷별 프롬프트 3000자 제한 — 서버 검증 — 컷별 프롬프트 적용과 함께
- [ ] AI 어댑터 GPT/Gemini/Claude 호출할 때 실패/타임아웃/503 처리(test파일로)
- [ ] 컷 생성 상태 확인 — 대기/생성/완료/실패 상태 주기적 확인 - 2순위
- [ ] 컷 재생성 — 특정 컷만 재생성, 컷ID 부여 필요 - 2순위
- [ ] 이미지, 파일 보존기간 후 삭제 (내보내기 애들도 포함인지 물어보고 작업하기)
+ 컷 재생성할때, 새 이미지로 덮어쓸 때 옛날 이미지를 즉시 지우거나(또는 같은 R2 경로에 덮어쓰기) 처리

### 내보내기
- [ ] 이미지 내보내기 — 3×3 그리드 이미지 1장 기본 내보내기, 추후 개별 이미지 옵션
- [ ] PDF 내보내기 — 9컷 이미지+프롬프트가 담긴 PDF 생성, 다운로드 링크 반환
- [ ] 내보내기 결과 확인 — 완료 여부·다운로드 링크 조회 - 2순위

### 캔버스
- [x] 캔버스 생성/개별조회/목록조회/저장 API 구현 (`POST,GET /canvases`, `GET,PUT /canvases/{id}`)
- [x] 캔버스 배치 저장 — 각 요소 위치(X,Y) + 연결/섹션까지 전체 교체 저장
- [x] 캔버스 첨부(이미지/영상) 업로드 API (`POST /canvases/{id}/attachments`)
- [ ] 캔버스 작성하다 저장을 안하게 되는 케이스 관련(r2 고아) 문제: 프론트분들이 이미지/영상 첨부하자마자 업로드 api를 연결하는지 아니면 캔버스저장버튼을 눌렀을때 업로드 api를 연결하는지에 따라 내 작업이 달라짐(전자면 CanvasPendingAttachment 작은추적테이블+24시간정리스윕/ 후자면 나 할거없음)

### 유저/인증 - 후순위
- [ ] users 테이블 설계 (이메일, 비밀번호 해시 등)
- [ ] 회원가입 API (이메일 도메인 화이트리스트 적용 여부 — 회의 결과 확정 전, A1팀 방식 참고 예정)
- [ ] 로그인 API (비밀번호 검증, JWT 발급)
- [ ] JWT 검증 dependency (다른 라우터에서 재사용)
- [ ] 계정 로그인 + 접근 권한 제한 (보안 Lv2) — 인증 구현과 함께
- [ ] 기존 엔드포인트들 인증, 로그인 필요(Authorization 헤더) 필요/불필요 표시 달고, user 관련 엔드포인트 추가

---

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
+ user 만들어지면 `storyboards`에 `user_id`(FK) 컬럼 추가해서 누가 만들었는지 기록, 라우터 전체 훑으면서 접근권한 필터 추가 (`WHERE user_id == current_user.id`), 프론트에 `Authorization` 헤더 고지


## ⚠️ Supabase 무료 플랜 주의사항
- 무료 프로젝트는 **7일간 API/DB 요청이 없으면 자동 일시정지**됨
- 개발 중엔 실제 API 테스트하면서 자연스럽게 활동이 잡히니 신경 안 써도 되지만, **며칠 이상 작업을 쉬었다 다시 시작할 때**는 대시보드 들어가서 상태 확인
- 멈춰있으면 대시보드에 "Restore project" 버튼 뜸 → 클릭 후 1~2분 대기하면 복구 (데이터는 유지됨, 연결 문자열도 안 바뀜)
- 대시보드 → Table Editor 열어서 살아있는지 확인할 것 (요청이 실제로 DB까지 도달해야 활동으로 잡힘 — 단순 로그인만으론 부족할 수 있음)


## 나중에 필요하면 고려 (지금은 안 함)
- **`POST /storyboards` idempotency key**: storyboard 저장은 성공했는데 9컷 생성 job 등록(enqueue)만 실패하는 경우, 재시도 시 storyboard row가 중복 생성될 수 있음. 문제 되면 같은 입력(or idempotency key)이면 기존 storyboard를 재사용하도록 처리하는 것 고려. 60시간 MVP라 지금 우선순위 아님 — 중복 row 생겨도 당장은 무해함.
- job 등록(enqueue) 실패"는 딱 두 가지
> storyboard/generation/cut row는 DB에 commit됐는데(=db.commit() 성공), 그 이후 서버 프로세스가 죽거나 add_task 자체가 뻗어서 run_generation이 실제로 스케줄조차 안 된 경우
> 클라이언트가 응답을 못 받아서(타임아웃/네트워크 끊김) 같은 요청을 재시도하는 경우


## 전체 파이프라인 그림 (A/B/C조)
> 회사 최종 목표: A/B/C 세 팀 프로젝트를 나중에 하나의 올인원 플랫폼으로 합침.
> 기획(텍스트) → [B] 스토리보드 9컷(그리드) 콘티 + 통합 프롬프트 → [A] 영상 AI(첫 프레임 정밀 생성 + 15초 영상화) → [C] 합치기 + 음악·자막

- **B조(우리)**: 시나리오 텍스트 → 9컷 스토리보드 그리드 + 통합 영문 프롬프트. **"기획/콘티" 단계 산출물만 만듦**
- **A조**: 프롬프트 → 이미지·영상 생성(보관함 저장) + 반대로 이미지 넣으면 프롬프트 역추출 → 재생성까지 이어지는 생성 허브. → PRD의 **"영상 AI"** 단계에 해당. B가 뽑은 그리드+프롬프트를 A 도구에 넣어서 실제 15초 영상으로 만드는 흐름.
- **C조**: 텍스트로 효과음 생성("유리 깨지는 소리" 같은 거) + 영상·음성 → 자막(SRT/ASS) 자동 생성. → PRD의 **"합치기 → 음악·자막"** 후반작업 단계, A조가 뽑은 영상을 받아서 처리.
