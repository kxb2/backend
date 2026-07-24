"""9컷 생성 상태 조회, AI 어댑터(app/ai) 호출 전체 과정 명령·재시도

ㅡ run_generation 순서: 스토리보드 로드 → Claude 통합 프롬프트 생성+컷별 분리(형식 오류 시 재시도)
→ 9컷 이미지 병렬 생성(실패한 컷만 FAILED) → 전부 성공 시 3x3 그리드 합성 → Generation 상태 확정.
"""

import logging
import re
from concurrent.futures import ThreadPoolExecutor, wait
from io import BytesIO

from PIL import Image
from sqlalchemy.orm import Session

from app.ai.base import GeneratedImage, ImageAdapter, PromptAdapter
from app.ai.exceptions import AIAdapterError
from app.ai.image_adapter import get_image_adapter
from app.ai.prompt_adapter import ClaudePromptAdapter
from app.core import storage
from app.core.constants import CUT_COUNT
from app.core.enums import JobStatus
from app.db.session import SessionLocal
from app.generations.models import Cut, Generation
from app.storyboards.models import Storyboard

logger = logging.getLogger(__name__)

MAX_INTEGRATED_PROMPT_LENGTH = 3000
MAX_PROMPT_ATTEMPTS = 3
# 원래 2번이었는데 생각보다 프롬프트 제한수 잘 걸려서 3회로 늘림

# 컷당 실제 모델 호출에 넘기는 레퍼런스 장수 상한(방어적 cap) — SYSTEM_PROMPT가 Claude한테도
# "컷당 최대 3장"을 지시하지만, 혹시 어겨도 여기서 한 번 더 자름.
# REFMAP이 없거나(파싱 실패) 다운로드 자체가 실패했을 때는 "앞 N장을 전 컷에 공유"하는
# 폴백 세트 크기로도 쓰임(기존 동작).
# 사용자는 최대 10장까지 업로드 가능하지만,
# 이미지 모델에 그대로 다 넣으면 참조끼리 섞여서 결과가 나빠지는 문제가 있대서
# 실제 모델 호출에 넘기는 장수를 1, 3, 8 -> 3가지 경우 테스트해보려고
MAX_MODEL_REFERENCE_IMAGES = 3

GRID_IMAGE_FOLDER = "grids"
GRID_JPEG_QUALITY = 90
# 크로마 서브샘플링 무압축 4:4:4 + jpeg 퀄 90

# 미확정 사항: 통합 프롬프트에서 핵심 키워드 추출 확정되면
# - SYSTEM_PROMPT에 키워드 출력 규칙 추가 → service.py에 키워드 파싱 로직 추가
#   → 키워드 컬럼 추가 → 응답 스키마 반영 (Claude 프롬프트 생성할때 출력포맷 확장)

# ======= 1. Claude 응답 파싱/검증
# Claude 출력의 "Shot 1: ...", "Shot 2: ..." 라벨을 순번과 함께 찾는 정규식
_SHOT_PATTERN = re.compile(r"Shot\s*(\d+)\s*:\s*", re.IGNORECASE)

# Claude 출력 맨 끝의 "REFMAP: 1=[..]; 2=[..]; ..." 줄을 찾는 정규식
_REFMAP_LINE_PATTERN = re.compile(r"^REFMAP:\s*(.+)$", re.IGNORECASE | re.MULTILINE)
_REFMAP_ENTRY_PATTERN = re.compile(r"(\d+)\s*=\s*\[([^\]]*)\]")


def _extract_refmap(raw_prompt: str) -> tuple[str, dict[int, list[int]] | None]:
    """Claude 출력에서 REFMAP 줄을 분리.

    반환: (REFMAP 줄이 제거된 텍스트, 파싱된 {컷 순번: [레퍼런스 인덱스]}).
    ㅡ 항상 이 함수가 반환한 "제거된 텍스트"만 이후(validate_prompt_length/apply_integrated_prompt)에
      써야 함 — 안 그러면 REFMAP 글자수가 3000자 검증에 섞이고, split_shots가 마지막 Shot 끝을
      문자열 끝으로 잡는 로직 때문에 REFMAP이 Shot 9 본문에 그대로 흡수됨.
    ㅡ REFMAP 줄 자체가 없거나 "N=[..]" 항목을 하나도 못 뽑으면 파싱 결과 None
      (호출부가 "전체 폴백"으로 처리). 일부 컷 순번이 빠진 건 여기서 실패로 안 침 —
      호출부가 컷 단위로 개별 폴백 처리.
    """
    match = _REFMAP_LINE_PATTERN.search(raw_prompt)
    if match is None:
        return raw_prompt, None

    stripped_text = (raw_prompt[: match.start()] + raw_prompt[match.end() :]).rstrip()
    entries = _REFMAP_ENTRY_PATTERN.findall(match.group(1))
    if not entries:
        return stripped_text, None

    refmap: dict[int, list[int]] = {}
    for order_no_str, indices_str in entries:
        indices = [int(i) for i in indices_str.split(",") if i.strip()]
        refmap[int(order_no_str)] = indices

    return stripped_text, refmap


class PromptValidationError(Exception):
    """Claude가 생성한 통합 프롬프트가 길이/형식 요구사항을 만족 못한 경우."""


def validate_prompt_length(
    integrated_prompt: str, *, max_length: int = MAX_INTEGRATED_PROMPT_LENGTH
) -> None:
    """샷별 프롬프트 합계(통합 프롬프트 전체 길이)가 max_length를 넘지 않는지 검증."""
    if len(integrated_prompt) > max_length:
        raise PromptValidationError(
            f"통합 프롬프트가 {max_length}자를 초과했습니다 (현재 {len(integrated_prompt)}자)"
        )


def split_shots(integrated_prompt: str) -> dict[int, str]:
    """"Shot 1: ...\\nShot 2: ..." 형태의 통합 프롬프트를 {컷 순번: 프롬프트 텍스트}로 분리."""
    matches = list(_SHOT_PATTERN.finditer(integrated_prompt))
    if len(matches) != CUT_COUNT:
        raise PromptValidationError(
            f"통합 프롬프트에서 Shot {CUT_COUNT}개를 찾지 못했습니다 (찾은 개수: {len(matches)})"
        )

    shots: dict[int, str] = {}
    for index, match in enumerate(matches):
        order_no = int(match.group(1))
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(integrated_prompt)
        text = integrated_prompt[start:end].strip()
        if not text:
            raise PromptValidationError(f"Shot {order_no}의 내용이 비어 있습니다.")
        shots[order_no] = text

    if set(shots) != set(range(1, CUT_COUNT + 1)):
        raise PromptValidationError(
            f"컷 순번이 1~{CUT_COUNT}로 정확히 매겨지지 않았습니다 (찾은 순번: {sorted(shots)})"
        )

    return shots


def apply_integrated_prompt(db: Session, storyboard: Storyboard, integrated_prompt: str) -> None:
    """Claude가 생성한 통합 프롬프트 → 길이 검증, 샷 분리, 순번 일치 확인후 → 스토리보드와 컷에 반영."""
    validate_prompt_length(integrated_prompt)
    shots = split_shots(integrated_prompt)

    order_nos = {cut.order_no for cut in storyboard.cuts}
    if order_nos != set(range(1, CUT_COUNT + 1)):
        raise PromptValidationError(
            f"스토리보드의 컷 순번이 1~{CUT_COUNT}와 일치하지 않습니다 (실제: {sorted(order_nos)})"
        )

    storyboard.integrated_prompt = integrated_prompt
    for cut in storyboard.cuts:
        cut.prompt_text = shots[cut.order_no]

    db.commit()


# ====== 2. 오케스트레이션(9컷 생성 전반 과정)이 쓰는 헬퍼 함수들
def _generate_and_apply_prompt(
    db: Session, storyboard: Storyboard, prompt_adapter: PromptAdapter
) -> tuple[str | None, int, dict[int, list[int]] | None]:
    """Claude 호출 + REFMAP 분리 + 분리/검증. 형식이 이상하면 MAX_PROMPT_ATTEMPTS까지 새로 생성해서 재시도.

    반환: (에러 메시지, 실제 시도 횟수, REFMAP). 성공하면 에러는 None, 횟수는 1이 재시도 없는 첫 시도 성공 의미.
    MAX_PROMPT_ATTEMPTS까지 다 실패: 마지막 시도의 에러 메시지 + MAX_PROMPT_ATTEMPTS + REFMAP None 반환.
    ㅡ DB로 재시도 여부나 횟수 확인하고 싶어서 추가.
    ㅡ REFMAP 파싱 실패는 재시도 사유가 아님(9컷 자체는 정상일 수 있음) — refmap=None으로 흘려보내고
      호출부(run_generation)가 전체 폴백으로 처리.
    """
    last_error: str | None = None
    for attempt in range(1, MAX_PROMPT_ATTEMPTS + 1):
        integrated_prompt: str | None = None  # 매 시도마다 리셋(이전 시도 값 남아있으면 로그 헷갈려서)
        try:
            raw_prompt = prompt_adapter.generate_prompt(
                scenario_text=storyboard.scenario_text,
                genre=storyboard.genre,
                style=storyboard.style,
                tone=storyboard.tone,
                aspect_ratio=storyboard.aspect_ratio,
                era=storyboard.era,
                reference_image_urls=[ref.image_url for ref in storyboard.reference_images],
            )
            # REFMAP 줄을 여기서 바로 떼어내고, 이후로는 제거된 텍스트만 씀
            # (3000자 검증/Shot 9 본문 오염 방지 — _extract_refmap 문서 참고)
            integrated_prompt, refmap = _extract_refmap(raw_prompt)
            apply_integrated_prompt(db, storyboard, integrated_prompt)
            return None, attempt, refmap
        except (AIAdapterError, PromptValidationError) as exc:
            last_error = str(exc)
            logger.warning("통합 프롬프트 생성/검증 실패(시도 %d/%d): %s", attempt, MAX_PROMPT_ATTEMPTS, exc)
            # generate_prompt까지는 성공하고 apply_integrated_prompt(검증)에서만 실패한 경우에만 원문 존재.
            # 이 원문을 봐야 어느 요소(Camera/Setting 등)가 길어서 넘겼는지 다음 프롬프트 튜닝에 참고 가능.
            if integrated_prompt is not None:
                logger.warning(
                    "실패한 통합 프롬프트 원문(시도 %d/%d):\n%s", attempt, MAX_PROMPT_ATTEMPTS, integrated_prompt
                )

    return last_error, MAX_PROMPT_ATTEMPTS, None


def download_reference_images(reference_image_urls: list[str]) -> list[tuple[bytes, str]]:
    """레퍼런스 이미지 URL들 병렬로 한 번만 다운로드해서 (바이트, content_type) 목록으로 반환.

    ㅡ 최초 생성(_generate_cut_images 호출 전 1회) / 컷 재생성(앵커) 양쪽에서 공용으로 사용.
    ㅡ MAX_MODEL_REFERENCE_IMAGES로 앞에서 자른 뒤 다운로드
    """
    urls = reference_image_urls[:MAX_MODEL_REFERENCE_IMAGES]
    if not urls:
        return []

    def _load(url: str) -> tuple[bytes, str]:
        return storage.download_bytes(url), storage.content_type_from_url(url)

    with ThreadPoolExecutor(max_workers=len(urls)) as executor:
        return list(executor.map(_load, urls))


def download_reference_images_by_index(
    urls: list[str], indices: set[int]
) -> dict[int, tuple[bytes, str]]:
    """1-based 인덱스 집합에 해당하는 URL만 병렬 다운로드해서 {인덱스: (바이트, content_type)}로 반환.

    ㅡ REFMAP 기반 컷별 분배용 — 같은 이미지를 여러 컷이 참조해도 한 번만 받도록 인덱스 단위로 캐싱.
    ㅡ 범위 밖 인덱스는 조용히 무시(호출부가 필터링해서 넘기는 걸 기대하되, 방어적으로 한 번 더 거름).
    ㅡ 인덱스 하나의 다운로드 실패가 배치 전체를 죽이지 않도록 인덱스별로 격리 — 실패한 인덱스만
      결과에서 빠지고 나머지는 정상 반환(그 이미지가 필요 없는 다른 컷들의 REFMAP 정밀도를 보존).
    """
    valid_indices = sorted(i for i in indices if 1 <= i <= len(urls))
    if not valid_indices:
        return {}

    def _load(index: int) -> tuple[int, tuple[bytes, str] | None]:
        url = urls[index - 1]
        try:
            return index, (storage.download_bytes(url), storage.content_type_from_url(url))
        except Exception as exc:  # noqa: BLE001 — 이 인덱스만 제외하고 나머지는 정상 진행
            logger.warning("레퍼런스 이미지 다운로드 실패(인덱스 %d), 이 이미지만 제외: %s", index, exc)
            return index, None

    with ThreadPoolExecutor(max_workers=len(valid_indices)) as executor:
        results = dict(executor.map(_load, valid_indices))

    return {index: data for index, data in results.items() if data is not None}


def _build_reference_images_by_cut(
    storyboard: Storyboard, refmap: dict[int, list[int]] | None
) -> dict[int, list[tuple[bytes, str]]]:
    """REFMAP 기반으로 컷마다 다른 레퍼런스 세트를 구성. 실패/부재 시 안전하게 폴백.

    ㅡ refmap이 None(REFMAP 줄 자체가 없거나 파싱 실패) → 기존 동작(캡 3, 전 컷 공유)로 폴백.
    ㅡ 다운로드 자체가 실패해도(R2 등) 9컷 전체를 안 죽이고 텍스트 기반으로 강등
      ("해당 컷만 재시도, 전체 파이프라인 중단 금지" 원칙 — 레퍼런스도 예외 아님).
    ㅡ 컷 단위 폴백: REFMAP에 컷 번호 자체가 없으면(누락) 앞 N장으로 채움 + 경고 로그.
      명시적 빈 배정(`[]`)은 폴백 대상이 절대 아님 — Claude가 "레퍼런스 불필요"로 판단한 걸 그대로 존중.
    """
    all_urls = [ref.image_url for ref in storyboard.reference_images]
    if not all_urls:
        return {}

    def _shared_fallback() -> dict[int, list[tuple[bytes, str]]]:
        try:
            shared = download_reference_images(all_urls)
        except Exception as exc:  # noqa: BLE001 — 원인 무엇이든 레퍼런스 없이 진행
            logger.warning("레퍼런스 이미지 다운로드 실패, 텍스트 기반으로 진행: %s", exc)
            shared = []
        return {cut.id: shared for cut in storyboard.cuts}

    if refmap is None:
        return _shared_fallback()

    fallback_indices = [i for i in (1, 2, 3) if i <= len(all_urls)]
    # 컷 단위 폴백이 앞 N장을 쓸 수 있으므로, REFMAP에 등장한 인덱스뿐 아니라 앞 N장도 미리 받아둠
    union_indices = {idx for indices in refmap.values() for idx in indices} | set(fallback_indices)

    try:
        by_index = download_reference_images_by_index(all_urls, union_indices)
    except Exception as exc:  # noqa: BLE001
        logger.warning("REFMAP 레퍼런스 다운로드 실패, 전체 폴백으로 전환: %s", exc)
        return _shared_fallback()

    reference_images_by_cut_id: dict[int, list[tuple[bytes, str]]] = {}
    for cut in storyboard.cuts:
        indices = refmap.get(cut.order_no)
        if indices is None:
            logger.warning("REFMAP에 컷 %d 항목이 없어 앞 %d장으로 폴백", cut.order_no, len(fallback_indices))
            indices = fallback_indices
        else:
            valid_indices = [i for i in indices if 1 <= i <= len(all_urls)]
            if len(valid_indices) != len(indices):
                logger.warning("컷 %d의 REFMAP에 범위 밖 인덱스가 있어 제외: %s", cut.order_no, indices)
            if len(valid_indices) > MAX_MODEL_REFERENCE_IMAGES:
                # 인물>소품>장소 우선순위는 SYSTEM_PROMPT 지시일 뿐 여기서 종류를 알 수 없어 강제 못 함 —
                # Claude가 지시를 안 지켰을 때 뒤쪽부터 잘려나간다는 걸 로그로만 남겨서 추적 가능하게
                logger.warning(
                    "컷 %d에 REFMAP이 %d장을 배정했으나 컷당 상한 %d장을 넘어 뒤쪽부터 잘림: %s",
                    cut.order_no, len(valid_indices), MAX_MODEL_REFERENCE_IMAGES, valid_indices,
                )
            indices = valid_indices[:MAX_MODEL_REFERENCE_IMAGES]
        reference_images_by_cut_id[cut.id] = [by_index[i] for i in indices if i in by_index]

    return reference_images_by_cut_id


def _generate_one_cut_image(
    image_adapter: ImageAdapter,
    cut_id: int,
    order_no: int,
    prompt_text: str,
    aspect_ratio: str | None,
    reference_images: list[tuple[bytes, str]],
) -> tuple[int, GeneratedImage | None, str | None]:
    """스레드에서 실행, DB/ORM 접근 X(순수 값만 받음)

    성공 시 에러 메시지는 None, 실패 시 생성 결과가 None이고 에러 메시지가 채워짐
    """
    try:
        result = image_adapter.generate_image(
            prompt_text=prompt_text, aspect_ratio=aspect_ratio, reference_images=reference_images
        )
        return cut_id, result, None
    except Exception as exc:  # noqa: BLE001 — AIAdapterError 밖의 에러도 이 컷만 실패 처리
        logger.error("컷 %d(id=%d) 이미지 생성 실패: %s", order_no, cut_id, exc)
        return cut_id, None, str(exc)


def _generate_cut_images(
    image_adapter: ImageAdapter,
    cuts: list[Cut],
    aspect_ratio: str | None,
    reference_images_by_cut_id: dict[int, list[tuple[bytes, str]]] | None = None,
) -> dict[int, tuple[GeneratedImage | None, str | None]]:
    """위의 _generate_one_cut_image 함수를 9개 컷에 대해 스레드풀로 동시 실행.

    ㅡ cut의 필요한 값들은 스레드 넘기기 전에 메인 스레드에서 미리 뽑아둠
    ㅡ Session은 스레드 세이프하지 않아서, 살아있는 ORM 객체를 그대로 넘기면 X
    ㅡ reference_images_by_cut_id: REFMAP 기반으로 컷마다 다른(미리 다운로드된) 레퍼런스 세트를 줌
      (바이트는 불변이라 여러 컷이 같은 이미지를 참조해도 공유 안전). 없는 cut_id는 빈 리스트 취급.
    ㅡ 반환값: {cut_id: (생성 결과, 에러 메시지)}, 성공한 컷은 에러 메시지 None
    """
    reference_images_by_cut_id = reference_images_by_cut_id or {}
    cut_inputs = [(cut.id, cut.order_no, cut.prompt_text) for cut in cuts]

    with ThreadPoolExecutor(max_workers=len(cut_inputs)) as executor:
        futures = [
            executor.submit(
                _generate_one_cut_image,
                image_adapter,
                cut_id,
                order_no,
                prompt_text,
                aspect_ratio,
                reference_images_by_cut_id.get(cut_id, []),
            )
            for cut_id, order_no, prompt_text in cut_inputs
        ]
        wait(futures)

    return {cut_id: (result, error) for cut_id, result, error in (future.result() for future in futures)}


def _download_image(url: str) -> Image.Image:
    """스레드에서 실행, DB/ORM 접근 X(HTTP 다운로드) - URL → PIL Image.

    ㅡ storage.py의 R2 다운로드 재시도 로직 사용(download_bytes)
    """
    return Image.open(BytesIO(storage.download_bytes(url)))


def build_grid_image(cut_image_urls: list[str], known_image_bytes: dict[str, bytes] | None = None) -> bytes:
    """order_no 순서로 정렬된 9개 이미지 URL을 병렬로 내려받아 3x3 그리드 1장(JPEG)으로 합성.

    ㅡ executor.map은 입력 순서를 그대로 보존해서 반환하므로 order_no 순서가 깨지지 않음.
    ㅡ PNG는 9장을 합치면 용량이 커서(약 20MB) JPEG로 저장
      (해상도는 유지, 화질 손실 거의 없이 용량만 축소)
    ㅡ known_image_bytes에 해당 URL의 바이트가 이미 있으면(방금 생성/재생성한 이미지),
       R2 재다운로드 없이 그대로 사용 — 없는 URL만 _download_image로 내려받음.
    """
    known_image_bytes = known_image_bytes or {}

    def _load(url: str) -> Image.Image:
        if url in known_image_bytes:
            return Image.open(BytesIO(known_image_bytes[url]))
        return _download_image(url)

    with ThreadPoolExecutor(max_workers=len(cut_image_urls)) as executor:
        images = list(executor.map(_load, cut_image_urls))

    tile_size = images[0].size
    resized_images = []
    for image in images:
        if image.size != tile_size:
            logger.warning("그리드 타일 크기가 달라 리사이즈합니다: %s -> %s", image.size, tile_size)
            image = image.resize(tile_size)
        resized_images.append(image)

    grid = Image.new("RGB", (tile_size[0] * 3, tile_size[1] * 3))
    for index, image in enumerate(resized_images):
        row, col = divmod(index, 3)
        grid.paste(image, (col * tile_size[0], row * tile_size[1]))

    buffer = BytesIO()
    grid.save(buffer, format="JPEG", quality=GRID_JPEG_QUALITY, subsampling=0)
    return buffer.getvalue()


def get_generation(db: Session, generation_id: int) -> Generation | None:
    """9컷 생성 상태/결과 조회(GET 라우터가 사용)"""
    return db.get(Generation, generation_id)


# 서버 떠있는 상태에서 백그라운드 태스크 멈출때(스레드 데드락)도 주기적으로 훑는거 추가 고려
def recover_stuck_generations(db: Session) -> int:
    """서버 시작 시(배포로 인한 컨테이너 재시작 등) 호출
    
    — pending/processing으로 멈춰있던 generation/cut을 failed로 정리.
    ㅡ run_generation은 BackgroundTasks로 도는 별도 스레드라서 필요
    """
    stuck_generations = (
        db.query(Generation).filter(Generation.status.in_([JobStatus.PENDING, JobStatus.PROCESSING])).all()
    )
    for generation in stuck_generations:
        generation.status = JobStatus.FAILED
        generation.error_message = "서버 재시작으로 중단된 작업"
        for cut in generation.storyboard.cuts:
            if cut.status != JobStatus.COMPLETED:
                cut.status = JobStatus.FAILED
    db.commit()
    return len(stuck_generations)


# ======= 3. 본격적인 9컷 생성 과정
def run_generation(storyboard_id: int) -> None:
    """스토리보드 생성 직후 BackgroundTasks로 호출되는 9컷 생성 오케스트레이션 진입점.

    ㅡ 요청-응답 사이클과 독립적으로 실행, 넘겨받은 세션을 재사용 X, 자체 DB 세션을 열고 닫음.
    """
    db = SessionLocal()
    try:
        storyboard = db.get(Storyboard, storyboard_id)
        generation = storyboard.generation
        generation.status = JobStatus.PROCESSING
        db.commit()

        prompt_error, prompt_attempts, refmap = _generate_and_apply_prompt(db, storyboard, ClaudePromptAdapter())
        generation.prompt_attempt_count = prompt_attempts
        if prompt_error is not None:
            for cut in storyboard.cuts:
                cut.status = JobStatus.FAILED
            generation.status = JobStatus.FAILED
            generation.error_message = prompt_error
            db.commit()
            return

        for cut in storyboard.cuts:
            cut.status = JobStatus.PROCESSING
        db.commit()

        image_adapter = get_image_adapter(storyboard.image_model)
        # REFMAP 있으면 컷마다 다른 레퍼런스 세트, 없으면(파싱 실패 등) 기존처럼 전 컷 공유 폴백
        reference_images_by_cut_id = _build_reference_images_by_cut(storyboard, refmap)
        results = _generate_cut_images(
            image_adapter, storyboard.cuts, storyboard.aspect_ratio, reference_images_by_cut_id
        )

        known_image_bytes: dict[str, bytes] = {}
        for cut in storyboard.cuts:
            result, error_message = results.get(cut.id, (None, None))
            cut.image_url = result.url if result else None
            cut.status = JobStatus.COMPLETED if result else JobStatus.FAILED
            cut.error_message = error_message
            if result:
                known_image_bytes[result.url] = result.data
        db.commit()

        if all(cut.status == JobStatus.COMPLETED for cut in storyboard.cuts):
            # 방금 생성한 9장의 바이트를 이미 들고 있으므로 그리드때 R2 재다운로드가 필요 X
            grid_bytes = build_grid_image(
                [cut.image_url for cut in storyboard.cuts], known_image_bytes=known_image_bytes
            )
            generation.grid_image_url = storage.upload_image_bytes(
                grid_bytes, content_type="image/jpeg", folder=GRID_IMAGE_FOLDER
            )
            generation.status = JobStatus.COMPLETED
        else:
            generation.status = JobStatus.FAILED
            generation.error_message = "일부 컷 이미지 생성 실패 (각 컷의 error_message 참고)"
        db.commit()
    except Exception as exc:
        # 위에서 에러(그리드 다운로드 실패 등)가 나도 PROCESSING에 영원히 멈추지 않도록,
        # 최종적으로는 반드시 FAILED로 확정
        logger.exception("run_generation 실패 (storyboard_id=%d)", storyboard_id)
        try:
            db.rollback()
            storyboard = db.get(Storyboard, storyboard_id)
            storyboard.generation.status = JobStatus.FAILED
            storyboard.generation.error_message = str(exc)
            for cut in storyboard.cuts:
                if cut.status != JobStatus.COMPLETED:
                    cut.status = JobStatus.FAILED
            db.commit()
        except Exception:
            # FAILED 기록 시도조차 실패하는 경우,
            # 예외가 새어나가지 않게 로그만 남기고 조용히 끝냄.
            logger.exception("run_generation 실패 후 FAILED 상태 기록도 실패 (storyboard_id=%d)", storyboard_id)
    finally:
        db.close()
