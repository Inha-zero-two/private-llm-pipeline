"""설계 6.1: 단계별 시스템 프롬프트와 프롬프트 파일 버전 조회."""

import json
from pathlib import Path

DEFAULT_PROMPT_FILE = (
    Path(__file__).resolve().parent.parent / "prompts" / "prompts.json"
)

# 파이프라인에서 필수로 사용하는 단계 이름. 기존 번호 뒤에 새 단계를 추가한다.
REQUIRED_PROMPTS = (
    "DETECT",
    "NEEDS",
    "JUDGE",
    "WRITE",
    "REWRITE",
    "GUARD",
    "ANSWER",
    "PARSE_RULES",
    "MATCH_RULES",
    "CONSENT",
    "DECIDE_CONTEXT",
    "FRONTIER",
    "MEMO_SUMMARY",
    "INTERPRET_CONSENT",
)


class PromptManager:
    """JSON 파일에서 시스템 프롬프트와 전체 버전을 읽어 보관하는 클래스."""

    def __init__(self, prompt_file: Path | str = DEFAULT_PROMPT_FILE) -> None:
        """멤버 변수 초기화 후 프롬프트 파일 최초 1회 로드 시도."""
        self._prompt_file = Path(prompt_file)
        self._version: str = ""
        self._prompts: dict[int | str, str] = {}
        self._loaded = False
        self.load_prompts()

    def load_prompts(self) -> bool:
        """JSON 파일에서 버전과 단계별 프롬프트를 읽어 멤버 변수에 저장."""
        # 이미 로드가 완료된 경우 파일 재읽기 생략
        if self._loaded:
            return True

        # 1. 프롬프트 파일 존재 및 읽기 확인
        try:
            raw_bytes = self._prompt_file.read_bytes()
        except OSError:
            print(f"[PromptManager] 프롬프트 파일 없음: {self._prompt_file}")
            return False

        # 2. UTF-8 디코딩, JSON 문법 및 항목별 형식·중복 검사
        try:
            data = json.loads(raw_bytes.decode("utf-8"))
            version = data["version"]
            raw_prompts = data["prompts"]
            if (
                not isinstance(version, str)
                or not version.strip()
                or not isinstance(raw_prompts, list)
                or not raw_prompts
            ):
                raise ValueError

            prompts: dict[int | str, str] = {}
            for item in raw_prompts:
                idx, name, prompt = item["idx"], item["name"], item["prompt"]
                if isinstance(idx, bool) or not isinstance(idx, int) or idx < 0:
                    raise ValueError
                if not isinstance(name, str) or not name.strip():
                    raise ValueError
                # 필수 단계(0~13)는 지정된 번호와 이름이 서로 일치해야 함
                if idx < len(REQUIRED_PROMPTS) or name in REQUIRED_PROMPTS:
                    if idx >= len(REQUIRED_PROMPTS) or REQUIRED_PROMPTS[idx] != name:
                        raise ValueError
                # 줄 단위 문자열 배열이면 줄바꿈으로 합쳐 단일 문자열로 변환
                if isinstance(prompt, list) and all(isinstance(s, str) for s in prompt):
                    prompt = "\n".join(prompt)
                if not isinstance(prompt, str) or not prompt.strip():
                    raise ValueError
                if idx in prompts or name in prompts:
                    raise ValueError

                prompts[idx] = prompt
                prompts[name] = prompt
        except (ValueError, KeyError, TypeError):
            print(f"[PromptManager] 프롬프트 형식 안 맞음: {self._prompt_file}")
            return False

        # 3. 필수 프롬프트 항목 누락 여부 확인
        missing = [name for name in REQUIRED_PROMPTS if name not in prompts]
        if missing:
            print(f"[PromptManager] 기본 프롬프트 항목 누락: {', '.join(missing)}")
            return False

        # 4. 검증을 통과한 값만 멤버 변수에 반영하고 로드 완료 표시
        self._version = version
        self._prompts = prompts
        self._loaded = True
        return True

    def get_prompt(self, prompt_idx: int | str) -> str:
        """열거형(PromptIdx), 번호(idx) 또는 이름(str)으로 시스템 프롬프트 문자열 반환."""
        # 로드 상태와 입력 키 자료형·등록 여부 확인 후 본문 반환
        if not self._loaded:
            raise RuntimeError("시스템 프롬프트가 로드되지 않았습니다")
        if isinstance(prompt_idx, bool) or not isinstance(prompt_idx, (int, str)):
            raise TypeError(
                f"지원하지 않는 프롬프트 키 자료형: {type(prompt_idx).__name__}"
            )
        if prompt_idx not in self._prompts:
            raise KeyError(f"등록되지 않은 프롬프트 키: {prompt_idx}")
        return self._prompts[prompt_idx]

    def get_version(self) -> str:
        """프롬프트 파일 최상단의 버전 반환. Runner에서 본문과 별도로 조회."""
        if not self._loaded:
            raise RuntimeError("시스템 프롬프트가 로드되지 않았습니다")
        return self._version
