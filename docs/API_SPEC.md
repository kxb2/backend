# API 명세

> 2026-07-09 회의 결정: "스토리보드 생성"과 "9컷 생성 요청"은 프론트에서 하나의 버튼(스토리보드 생성) 클릭으로 발생하는 동작이라 `POST /storyboards` 하나로 통합한다. 조회(GET)는 기존대로 스토리보드/생성 결과를 분리해서 각자 유지한다.

**공통 status 값** (`generations` / `regenerations` / `exports` 조회 응답에 공통 적용 — 백로그 "대기/생성/완료/실패"와 매핑)

| status 값 | 의미 |
|---|---|
| `pending` | 대기 — job은 등록됐지만 아직 처리 시작 전 |
| `processing` | 생성 중 — AI 호출(이미지/프롬프트 모델) 진행 중 |
| `completed` | 완료 — 결과(이미지·프롬프트·다운로드 링크 등) 조회 가능 |
| `failed` | 실패 — 실패 사유와 함께 재시도 필요 |

| 기능 | Method | Param | URL | 설명 |
|---|---|---|---|---|
| 스토리보드 생성 + 9컷 생성 요청 | POST | - | `/storyboards` | 시나리오, 장르, 이미지 모델 선택(GPT image/Gemini), 고급 설정, 레퍼런스 이미지(0~10장)를 받아 스토리보드를 저장함과 동시에 AI에게 9컷 생성 작업을 요청합니다. 응답: `{ storyboardId, generationId, status: "pending" }` |
| 스토리보드 조회 | GET | storyboardId | `/storyboards/{storyboardId}` | 저장된 스토리보드 입력값과 레퍼런스 이미지를 조회합니다. |
| 프롬프트 조회 | GET | storyboardId | `/storyboards/{storyboardId}/prompt` | 생성된 통합 프롬프트(Shot 1~9 구분 포함, 영어 고정)를 조회합니다. |
| 9컷 생성 상태/결과 조회 | GET | generationId | `/generations/{generationId}` | 9컷 생성 진행 상태(status)를 조회하고, `completed`면 결과(그리드 이미지, 컷 목록)를 함께 반환합니다. |
| 특정 컷 재생성 | POST | storyboardId, cutId | `/storyboards/{storyboardId}/cuts/{cutId}/regeneration` | 특정 컷 1개만 재생성합니다(현재 선택된 이미지 모델로만 수행). 응답: `{ regenerationId, status: "pending" }` |
| 재생성 결과 확인 | GET | regenerationId | `/regenerations/{regenerationId}` | 특정 컷 재생성 작업의 진행 상태(status)와 결과를 조회합니다. |
| 캔버스 생성 | POST | - | `/canvases` | 빈 캔버스를 새로 생성합니다. storyboard와 무관하게 독립적으로 생성 가능(연결 시 storyboardId는 선택 입력). 응답: `{ canvasId }` |
| 캔버스 목록조회 | GET | - | `/canvases` | 캔버스 전체 목록을 요약 정보(id, storyboardId, createdAt, updatedAt)로 조회합니다. |
| 캔버스 조회 | GET | canvasId | `/canvases/{canvasId}` | 생성된 컷과 프롬프트의 캔버스 배치 정보를 조회합니다. |
| 캔버스 저장 | PUT | canvasId | `/canvases/{canvasId}` | 캔버스 요소(elements)·연결(connections) 전체를 요청 내용으로 교체 저장합니다(전체 교체 방식). |
| 캔버스 이미지/영상 업로드 | POST | canvasId | `/canvases/{canvasId}/attachments` | 이미지/영상 파일을 R2에 업로드하고 url만 반환합니다(요소로 저장하려면 이후 캔버스 저장 API에 포함해서 호출). 응답: `{ contentUrl, thumbnailUrl, type }` |
| PDF Export | POST | storyboardId | `/storyboards/{storyboardId}/exports/pdf` | 이미지와 프롬프트를 PDF로 생성합니다. 응답: `{ exportId, status: "pending" }` |
| 이미지 Export | POST | storyboardId | `/storyboards/{storyboardId}/exports/image` | 3×3 그리드 이미지 1장을 Export합니다. 옵션으로 개별 컷 이미지도 포함할 수 있습니다. 응답: `{ exportId, status: "pending" }` |
| Export 결과 조회 | GET | exportId | `/exports/{exportId}` | PDF 또는 이미지 Export 완료 여부(status)와 다운로드 링크를 조회합니다. |
