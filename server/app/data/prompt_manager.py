"""설계 6.1: 단계별 시스템 프롬프트와 프롬프트 파일 버전 조회."""

import json
from pathlib import Path

DEFAULT_PROMPT_FILE = Path(__file__).resolve().parent.parent / "prompts" / "prompts.json"


class PromptManager:
    # 멤버 변수 초기화 후 프롬프트 파일 최초 1회 로드 시도
    def __init__(self, prompt_file: Path | str = DEFAULT_PROMPT_FILE) -> None:
        self._prompt_file = Path(prompt_file)
        self._version: str = ""
        self._prompts: dict[int | str, str] = {}
        self._loaded = False
        self.load_prompts()

    # JSON 파일에서 버전과 단계별 프롬프트를 읽어 멤버 변수에 저장
    def load_prompts(self) -> bool:
        # 이미 로드가 완료된 경우 파일 재읽기 생략
        if self._loaded:
            return True

        try:
            # 1. JSON 파일 읽기 및 최상위 버전·목록 확인
            data = json.loads(self._prompt_file.read_text(encoding="utf-8"))
            version = data["version"]
            raw_prompts = data["prompts"]
            if (
                not isinstance(version, str)
                or not version.strip()
                or not isinstance(raw_prompts, list)
                or not raw_prompts
            ):
                return False

            # 2. 항목별 유효성·중복 검사 및 번호(idx)·이름(name) 동시 매핑
            prompts: dict[int | str, str] = {}
            for item in raw_prompts:
                idx, name, prompt = item["idx"], item["name"], item["prompt"]
                if isinstance(idx, bool) or not isinstance(idx, int) or idx < 0:
                    return False
                if not isinstance(name, str) or not name.strip():
                    return False
                # 줄 단위 문자열 배열이면 줄바꿈으로 합쳐 단일 문자열로 변환
                if isinstance(prompt, list) and all(isinstance(s, str) for s in prompt):
                    prompt = "\n".join(prompt)
                if not isinstance(prompt, str) or not prompt.strip():
                    return False
                if idx in prompts or name in prompts:
                    return False

                prompts[idx] = prompt
                prompts[name] = prompt
        except (OSError, ValueError, KeyError, TypeError):
            # 파일 없음, JSON 문법 오류, 필수 키 누락 시 실패 반환
            return False

        # 3. 검증을 통과한 값만 멤버 변수에 반영하고 로드 완료 표시
        self._version = version
        self._prompts = prompts
        self._loaded = True
        return True

    # 열거형(PromptIdx), 번호(idx) 또는 이름(str)으로 시스템 프롬프트 문자열 반환
    def get_prompt(self, prompt_idx: int | str) -> str:
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

    # 프롬프트 파일 최상단에 정의된 전체 버전 문자열 반환
    # 추후 LocalLLMRunner.request() 연동 시 기존의
    # `system_prompt, prompt_version = self.prompt_manager.get_prompt(prompt_idx)` 주석 규격을
    # `get_prompt(prompt_idx)`와 `get_version()`을 각각 호출하는 방식으로 변경한다.
    def get_version(self) -> str:
        if not self._loaded:
            raise RuntimeError("시스템 프롬프트가 로드되지 않았습니다")
        return self._version
