"""R2 연결 확인용 수동 테스트 스크립트 (pytest 아님, 그냥 직접 실행해서 눈으로 확인하는 용도).

실행: 프로젝트 루트에서 `python test/check_r2.py`
"""

import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core import storage  # noqa: E402 (sys.path 세팅 이후에 import해야 함)

TEST_KEY = "_healthcheck/manual_check.png"
TEST_BYTES = b"\x89PNG\r\n\x1a\n" + b"0" * 16  # PNG 매직바이트만 맞춘 최소 더미 데이터


def main() -> None:
    url = storage.upload_bytes(TEST_BYTES, key=TEST_KEY, content_type="image/png")
    print("업로드 성공:", url)

    # Cloudflare가 urllib 기본 User-Agent(Python-urllib/x.x)를 차단해서 일반 UA로 위장
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    status = urllib.request.urlopen(request).status
    print("공개 URL 접근:", status)

    storage.delete_file(url)
    print("정리 완료")


if __name__ == "__main__":
    main()
