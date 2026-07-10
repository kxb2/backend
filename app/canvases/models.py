# TODO: Canvas, CanvasElement, CanvasConnection 모델 정의
from app.db.base import Base

# 구현 목표: 생성된 캔버스 저장 / 저장된 캔버스 조회
# 캔버스 기능: 이미지 넣기, 동영상(1개) 넣기, 텍스트 추가(캔버스에), 댓글달기(특정요소에), 노드(선 연결)
# + 드로잉 기능도 있을수있음(확정은 아니지만 하게되면 벡터-좌표/경로,json-기반)
# R2에 캔버스에 올리는 이미지, 동영상도 올라감
#
# --- 필드 설계 메모 (2026-07-10, 프론트 피그마 툴바 스샷 기준) ---
#
# 확정:
# - canvases: storyboard_id(FK, nullable) — 독립 보드라 nullable
# - canvas_elements: canvas_id(FK), type, x, y
#   - type 후보: image, video, text, (frame — 미확정)
#   - content_url: image/video 공용 (R2 URL)
#   - thumbnail_url: video 전용, 프론트에서 만들어서 같이 업로드 (백엔드는 저장만)
#   - text_content: text 타입 전용 (순수 문자열, 드로잉 데이터랑 필드 분리)
#   - drawing_data(JSONB): 드로잉 기능 확정되면 벡터 좌표/경로 데이터 저장 (지금은 기능 미확정이라 보류)
# - canvas_connections: canvas_id(FK), from_element_id, to_element_id — 노드 연결(선 잇기)
# - canvas_comments (신규 테이블 필요): element_id(FK) — 댓글은 캔버스 전체가 아니라 특정 요소에 붙음
# - 영상 업로드: content-type mp4/webm 등으로 제한, 용량 캡 50~100MB 예정 (reference_images 검증 로직 재사용)
# - 이미지/영상 첨부는 툴바 `+` 버튼
#
# 미확정 (프론트/PM 확인 필요, 확인되면 필드 추가):
#   프레임이 그냥 시각적 박스 도형인지, 실제 그룹 컨테이너(안의 요소들을 자식으로 관리)인지 모름...
#   -> 그룹 컨테이너면 canvas_elements.parent_frame_id(자기참조 FK) 추가 필요
# - 툴바의 "동그라미, 네모 겹쳐있는 아이콘" 뭔지... 도형 삽입 도구인지
# - 툴바의 "포스트잇 메모 처럼 생긴 아이콘" 뭔지... (일반 text가 아닌지)
# - 드로잉 기능 확정되면 type에 drawing 추가 + drawing_data 필드 사용
#
# 혼자 툴바 보고 추정해본것:
# 프레임 = 그룹 컨테이너, 도형 삽입 도구, 메모 블록(포스트잇) 추가
# type enum에 frame, shape, memo 추가
# width, height(frame + shape 공용)
# parent_frame_id (frame 전용, 자기참조)
# shape_type (shape 전용)
# color (shape + memo 공용)
