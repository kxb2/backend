# API 명세

| 기능 | Method | Param | URL | 설명 |
|---|---|---|---|---|
| 스토리보드 생성 *(안 쓸 예정)* | POST | - | `/storyboards` | 시나리오, 장르, 고급 설정, 레퍼런스 이미지를 받아 스토리보드를 생성합니다. |
| 스토리보드 조회 | GET | storyboardId | `/storyboards/{storyboardId}` | 저장된 스토리보드 입력값과 레퍼런스 이미지를 조회합니다. |
| 프롬프트 조회 | GET | storyboardId | `/storyboards/{storyboardId}/prompt` | - |
| 9컷 생성 | POST | storyboardId | `/storyboards/{storyboardId}/generation` | 스토리보드 입력값을 기반으로 AI에게 9컷 생성 작업을 요청합니다. |
| 9컷 조회 | GET | generationId | `/generations/{generationId}` | 9컷 생성 진행 상태를 조회합니다. |
| 특정 컷 재생성 | POST | storyboardId, cutId | `/storyboards/{storyboardId}/cuts/{cutId}/regeneration` | 특정 컷 1개만 재생성합니다. |
| 재생성 결과 확인 | GET | regenerationId | `/regenerations/{regenerationId}` | 특정 컷 재생성 작업의 진행 상태와 결과를 조회합니다. |
| 캔버스 조회 | GET | canvasId | `/canvases/{canvasId}` | 생성된 컷과 프롬프트의 캔버스 배치 정보를 조회합니다. |
| 캔버스 저장 | PUT | canvasId | `/canvases/{canvasId}` | 캔버스 위치를 저장합니다. |
| PDF Export | POST | storyboardId | `/storyboards/{storyboardId}/exports/pdf` | 이미지와 프롬프트를 PDF로 생성합니다. |
| 이미지 Export | POST | storyboardId | `/storyboards/{storyboardId}/exports/image` | 3×3 그리드 이미지 1장을 Export합니다. 옵션으로 개별 컷 이미지도 포함할 수 있습니다. |
| Export 결과 조회 | GET | exportId | `/exports/{exportId}` | PDF 또는 이미지 Export 완료 여부와 다운로드 링크를 조회합니다. |
