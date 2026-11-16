"""서버 설정값 조회. 환경변수 → server/.env 파일 → 기본값 순서로 읽는다.

server/.env.example을 server/.env로 복사해 값을 채운다. .env는 git에 올라가지 않는다.
"""

from __future__ import annotations

import os
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parents[1]  # server/
ENV_FILE = SERVER_DIR / ".env"
# 채팅·실행 기록 DB의 기본 위치. 실행 위치와 상관없이 server/ 기준이다. (git 제외)
DEFAULT_DB_PATH = SERVER_DIR / "data" / "chat.sqlite3"


def read_env_file(path: Path) -> dict[str, str]:
    """KEY=VALUE 형식의 환경변수 파일을 읽어 대문자 키로 반환. 파일이 없으면 빈 dict."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip().upper()] = value.strip().strip("\"'")
    return values


def setting(
    name: str, env_file: dict[str, str] | None = None, default: str | None = None
) -> str | None:
    """환경변수 → .env 파일 → 기본값 순서로 설정값 조회. 빈 값은 없는 것으로 본다."""
    if env_file is None:
        env_file = read_env_file(ENV_FILE)
    return os.environ.get(name) or env_file.get(name) or default


def db_path() -> Path:
    """CHAT_DB_PATH 설정값. 상대 경로는 server/ 기준으로 해석한다."""
    path = Path(setting("CHAT_DB_PATH", default=str(DEFAULT_DB_PATH)) or "")
    return path if path.is_absolute() else SERVER_DIR / path
