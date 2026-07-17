# TODO: Canvas, CanvasElement, CanvasConnection 모델 정의
from app.db.base import Base

# 구현 목표: 생성된 캔버스 저장 / 저장된 캔버스 조회
# 캔버스 기능: 이미지 넣기, 동영상(1개) 넣기, 노드(선 연결), 그룹화 섹션, 메모
# R2에 캔버스에 올리는 이미지, 동영상도 올라감
#
# --- 필드 설계 메모 (2026-07-16, 프론트 피그마 툴바 스샷 기준) ---
#
# 확정:
# - canvases: storyboard_id(FK, nullable) — 독립 보드라 nullable
# - canvas_elements: canvas_id(FK), type, x, y
#   - type 후보: image, video, memo, 그룹화섹션 등(아직 제대로 설계 안함)
#   - content_url: image/video 공용 (R2 URL)
#   - thumbnail_url: video 전용, 프론트에서 만들어서 같이 업로드 (백엔드는 저장만)
#   - storyboard_id/cut_id 참조 들고있기(생성된 스토리보드의 통합 프롬프트, 컷별 프롬프트, 9컷 이미지 불러올수있게)
#     이거 nullable, 왜냐면 사용자가 업로드하는 이미지는 참조 없이 content_url만 있으니까
# - canvas_connections: canvas_id(FK), from_element_id, to_element_id — 노드 연결(선 잇기)
# - 영상 업로드: content-type mp4/webm 등으로 제한, 용량 캡 50~100MB 예정 (reference_images 검증 로직 재사용)
#              + 영상만 백엔드 안 거치고 url 올리는 R2 방식 사용할까 고려중
# - 이미지/영상 첨부는 툴바 `+` 버튼
# - 그룹화 섹션 추가: 안의 요소들을 자식으로 관리하고, 마우스 드래그하면 여러개 묶이는 컨테이너 개념(자기참조 FK 필요?)
# - 메모(포스트잇처럼 블록덩어리): 상단바에 메모 제목, 하단에 메모 내용(텍스트 입력)
#   메모도 노드 연결 가능하고, 색깔 넣는거 일단은 model 설계할때 넣어놓으려고.
# - 프론트: 툴바에 마우스 포인터와 화면 이동하는(한 지점에 고정하고 화면 움직이는) 포인터 2개 있음
#   => 포인터 / 뷰어포인터 (선택 / 화면이동)
#
# 미확정 (프론트/PM 확인 필요, 확인되면 필드 추가):
# - 드로잉 기능 확정되면 type에 drawing 추가 + drawing_data 필드 사용
#   drawing_data(JSONB): 벡터 좌표/경로 데이터 저장
# - 특정 요소에 붙는 댓글 기능과 일반 텍스트 삽입 기능은 메모가 대체할것같아서 보류
# - 메모의 하단 내용 입력에 프롬프트 내용을 가져올 수 있는지
# - 메모가 크기 조절이 가능하면 나도 model 설계에 뭔가 추가되나?
# - ui ux 담당분이 누크처럼 미니맵 기능 제안하쎴음(여유있거나 프론트 구현 가능하면 들어갈듯)
#
