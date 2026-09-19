"""대시보드 데이터(dashboard.json)를 로그인해야만 풀리는 암호문(dashboard.enc.json)으로 바꾼다.

GitHub Pages는 정적 파일만 서빙해서 서버에서 로그인을 검사할 수 없다. 대신 데이터를 암호화해서 올리고, 브라우저의 로그인 창이
아이디/비밀번호로 그 암호를 푼다. 데이터 자체가 암호화돼 있어서 dashboard.enc.json 주소를 알아도 비밀번호 없이는 못 읽는다.

구조 (LUKS의 키슬롯과 같은 방식):
  - 스캔마다 무작위 데이터 키 K를 만들어 AES-256-GCM으로 dashboard.json을 암호화한다.
  - 사용자마다 PBKDF2-HMAC-SHA256(비밀번호, 아이디에서 정해지는 고정 솔트, 600,000회)로 래핑 키 W를 만들고, K를 W로 암호화해 슬롯에 넣는다.
  - 브라우저는 아이디/비밀번호로 같은 W를 만들어 자기 슬롯에서 K를 꺼내 데이터를 푼다.
  - 슬롯 이름은 아이디의 해시라서 아이디 목록이 그대로 드러나지 않는다.
  - 솔트를 아이디로 고정해 두었기 때문에 '로그인 유지'를 켠 브라우저는 W만 저장해 두고 다음 갱신 때 비밀번호 없이 다시 푼다.

사용자는 환경변수 DASHBOARD_USERS에 '아이디:비밀번호'를 줄바꿈이나 세미콜론(;)으로 구분해서 넣는다 (GitHub 저장소 시크릿).
  - 설정이 없으면 암호화하지 않고 공개 상태를 유지한다 (기존 동작).
  - 설정이 있는데 형식이 잘못됐거나 비밀번호가 약하면 실패로 끝낸다 — 잘못된 설정 때문에 데이터가 평문으로 배포되는 일이 없게 한다.

사용법: DASHBOARD_USERS='kim:비밀번호...;lee:비밀번호...' python scripts/encrypt_dashboard.py [--keep-plain]
"""

import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import sys
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

DASHBOARD_DIR = Path(__file__).resolve().parent.parent / "dashboard"
ITERATIONS = 600_000
MIN_PASSWORD_LENGTH = 10
ID_PATTERN = re.compile(r"^[a-z0-9._-]{2,32}$")


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def normalize_id(user_id: str) -> str:
    return user_id.strip().lower()


def slot_key(user_id: str) -> str:
    return hashlib.sha256(f"id|{user_id}".encode()).hexdigest()


def salt_for(user_id: str) -> bytes:
    return hashlib.sha256(f"salt|{user_id}".encode()).digest()[:16]


def parse_users(raw: str) -> dict[str, str]:
    users: dict[str, str] = {}
    for entry in re.split(r"[\n;]", raw):
        entry = entry.strip()
        if not entry:
            continue
        if ":" not in entry:
            raise ValueError("DASHBOARD_USERS 형식은 '아이디:비밀번호'여야 해요 (여러 명은 줄바꿈이나 ; 로 구분).")
        user_id, password = entry.split(":", 1)
        user_id = normalize_id(user_id)
        password = password.strip()  # 시크릿에 붙여넣다 생긴 앞뒤 공백은 비밀번호로 치지 않는다
        if not ID_PATTERN.match(user_id):
            raise ValueError("아이디는 영문 소문자·숫자·. _ - 로 2~32자여야 해요.")
        if len(password) < MIN_PASSWORD_LENGTH:
            raise ValueError(f"비밀번호는 {MIN_PASSWORD_LENGTH}자 이상이어야 해요 (오프라인 추측 공격을 막으려면 길수록 좋아요).")
        if user_id in users:
            raise ValueError(f"아이디가 중복됐어요: {user_id}")
        users[user_id] = password
    if not users:
        raise ValueError("DASHBOARD_USERS에 사용자가 하나도 없어요.")
    return users


def wrapping_key(user_id: str, password: str) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt_for(user_id), ITERATIONS, dklen=32)


def encrypt(plain: bytes, users: dict[str, str]) -> dict:
    data_key = secrets.token_bytes(32)
    iv = secrets.token_bytes(12)
    slots = []
    for user_id, password in users.items():
        slot_iv = secrets.token_bytes(12)
        wrapped = AESGCM(wrapping_key(user_id, password)).encrypt(slot_iv, data_key, None)
        slots.append({"k": slot_key(user_id), "iv": b64(slot_iv), "w": b64(wrapped)})
    return {
        "v": 1,
        "iter": ITERATIONS,
        "iv": b64(iv),
        "ct": b64(AESGCM(data_key).encrypt(iv, plain, None)),
        "slots": slots,
    }


def decrypt(package: dict, user_id: str, password: str) -> bytes:
    """검증/테스트용 (브라우저가 하는 일과 같다). 아이디나 비밀번호가 틀리면 예외."""
    user_id = normalize_id(user_id)
    slot = next(s for s in package["slots"] if s["k"] == slot_key(user_id))
    data_key = AESGCM(wrapping_key(user_id, password)).decrypt(
        base64.b64decode(slot["iv"]), base64.b64decode(slot["w"]), None
    )
    return AESGCM(data_key).decrypt(base64.b64decode(package["iv"]), base64.b64decode(package["ct"]), None)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", type=Path, default=DASHBOARD_DIR)
    parser.add_argument("--keep-plain", action="store_true", help="평문 dashboard.json을 지우지 않는다 (로컬 테스트용)")
    args = parser.parse_args()

    raw = os.environ.get("DASHBOARD_USERS", "").strip()
    if not raw:
        print("DASHBOARD_USERS 미설정: 대시보드 데이터를 암호화하지 않고 공개 상태로 둡니다.")
        return 0

    plain_path = args.dir / "dashboard.json"
    enc_path = args.dir / "dashboard.enc.json"
    try:
        users = parse_users(raw)
    except ValueError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1
    if not plain_path.exists():
        print(f"오류: {plain_path} 가 없어요 (export_dashboard.py를 먼저 실행하세요).", file=sys.stderr)
        return 1

    package = encrypt(plain_path.read_bytes(), users)
    enc_path.write_text(json.dumps(package, separators=(",", ":")), encoding="utf-8")
    if not args.keep_plain:
        plain_path.unlink()
    print(f"대시보드 데이터 암호화 완료: 사용자 {len(users)}명 -> {enc_path.name} (평문 {'유지' if args.keep_plain else '삭제'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
