"""설계 4.2: 자연어 공개 설정 저장과 실행 시작 시 사전 허용 적용."""

from __future__ import annotations

import json
from threading import Lock
from typing import TYPE_CHECKING, Protocol

from app.models.enums import PromptIdx
from app.models.llm_results import Rule, RuleMatch, RuleParseFailure
from app.models.privacy_results import SaveResult
from app.privacy.pd_detector import PDTable

if TYPE_CHECKING:
    from app.models.schemas import PrivacySetting
    from app.runtime.local_llm_runner import LocalLLMRunner


class _RunChat(Protocol):
    chat_id: int


class _SettingsStore(Protocol):
    """#24의 동기 저장 인터페이스. 구현체는 생성자로 주입한다."""

    def save_setting(self, text: str, rules: list[Rule]) -> PrivacySetting: ...

    def load_setting(self, setting_id: int | None = None) -> PrivacySetting: ...

    def load_run_setting(self, run_id: int) -> PrivacySetting: ...

    def update_pd_allowed(self, chat_id: int, items: PDTable) -> None: ...

    def get_run(self, run_id: int) -> _RunChat: ...


class RulesInterpretationError(ValueError):
    """기존 설정을 유지하고 사용자에게 수정을 안내할 해석 실패."""


class PrivacySettings:
    """모델이 의미와 우선순위를 판단하고 서버는 결과 구조를 검증한다."""

    def __init__(
        self, chat_manager: _SettingsStore, local_llm_runner: LocalLLMRunner
    ) -> None:
        self._chat_manager = chat_manager
        self._runner = local_llm_runner
        self._setting: PrivacySetting | None = None
        self._lock = Lock()
        self._write_lock = Lock()

    @property
    def setting_id(self) -> int | None:
        """다음 실행 시작 시 고정할 현재 설정 ID."""
        with self._lock:
            return self._setting.setting_id if self._setting is not None else None

    def load(self, setting_id: int | None = None) -> PrivacySetting:
        """저장된 설정을 조회한다. 과거 설정 조회는 현재 설정을 바꾸지 않는다."""
        return self._chat_manager.load_setting(setting_id).model_copy(deep=True)

    def load_rules(self) -> list[Rule]:
        """시작 시 최신 설정을 복원한다. 조회 실패를 빈 설정으로 처리하지 않는다."""
        with self._write_lock:
            setting = self.load()
            with self._lock:
                self._setting = setting
            return [rule.model_copy(deep=True) for rule in setting.rules]

    def parse_rules(self, text: str) -> list[Rule]:
        """모델의 규칙을 반환한다. 빈 입력은 명시적인 설정 초기화다."""
        if not isinstance(text, str):
            raise TypeError("text는 문자열이어야 합니다")
        if not text.strip():
            return []
        result, ok, *_, reason = self._runner.request(
            None,
            json.dumps({"text": text}, ensure_ascii=False),
            PromptIdx.PARSE_RULES,
            None,
            use_context=False,
        )
        if reason == "unavailable":
            raise ConnectionError("로컬 LLM에 설정 해석을 요청하지 못했습니다.")
        if reason == "stopped":
            raise InterruptedError("설정 해석이 중지되었습니다.")
        if not ok:
            message = (
                result.message
                if isinstance(result, RuleParseFailure)
                else "공개 허용 대상을 확인하지 못했습니다. 설정문을 구체적으로 작성해 주세요."
            )
            raise RulesInterpretationError(message)
        if not isinstance(result, list) or not all(
            isinstance(rule, Rule) and rule.target.strip() for rule in result
        ):
            raise RulesInterpretationError("설정 해석 결과 형식이 올바르지 않습니다.")
        return [rule.model_copy(deep=True) for rule in result]

    def save(self, text: str) -> SaveResult:
        """해석과 저장이 모두 성공한 후에만 현재 설정을 교체한다."""
        # 모델 호출 중에도 실행 시작이 현재 ID를 즉시 읽을 수 있게 분리한다.
        with self._write_lock:
            try:
                rules = self.parse_rules(text)
            except RulesInterpretationError as exc:
                return SaveResult(ok=False, rules=self.load().rules, message=str(exc))
            normalized_text = text if text.strip() else ""
            setting = self._chat_manager.save_setting(normalized_text, rules)
            with self._lock:
                self._setting = setting.model_copy(deep=True)
            return SaveResult(
                ok=True,
                rules=setting.rules,
                message="설정을 저장했습니다. 다음 실행부터 적용됩니다.",
            )

    def is_allowed(self, match: RuleMatch | None, rules: list[Rule]) -> bool:
        """모델이 최종 선택한 규칙만 참조한다. 미대응이나 복수 선택은 금지다."""
        if not isinstance(match, RuleMatch) or len(match.rules) != 1:
            return False
        index = match.rules[0]
        if type(index) is not int or not 0 <= index < len(rules):
            return False
        return rules[index].action == "allow"

    def apply_to_table(self, run_id: int, pd_table: PDTable) -> None:
        """실행 시작 시 고정된 설정으로 전체 테이블을 평가하고 영구 상태를 저장한다."""
        # 이전 실행의 허용이나 임시 동의가 새 실행으로 넘어가지 않게 먼저 금지한다.
        for item in pd_table:
            item.allowed = False
            item.allowed_by = "none"
        setting = self._chat_manager.load_run_setting(run_id)
        chat_id = self._chat_manager.get_run(run_id).chat_id
        decisions = [item.model_copy(deep=True) for item in pd_table]
        if setting.rules and pd_table:
            match_input = json.dumps(
                {
                    "items": [
                        {"text": item.text, "category": item.category}
                        for item in pd_table
                    ],
                    "rules": [rule.model_dump() for rule in setting.rules],
                },
                ensure_ascii=False,
            )
            try:
                result, ok, *_ = self._runner.request(
                    run_id,
                    match_input,
                    PromptIdx.MATCH_RULES,
                    chat_id,
                    use_context=False,
                )
            except ConnectionError:
                # Runner 종료·대기 시간 초과도 항목 대응 실패로 기본 금지를 유지한다.
                result, ok = None, False
            # 오류가 있는 항목만 금지한다. DB ID와 입력 목록의 인덱스는 다르다.
            if ok:
                matches = self._valid_matches(result, len(pd_table), len(setting.rules))
                for index, match in matches.items():
                    item = decisions[index]
                    item.allowed = self.is_allowed(match, setting.rules)
                    item.allowed_by = "setting" if match.rules else "none"
        self._chat_manager.update_pd_allowed(chat_id, decisions)
        # 저장 실패 시 메모리에서만 허용되는 상태를 방지한다.
        for item, decision in zip(pd_table, decisions, strict=True):
            item.allowed = decision.allowed
            item.allowed_by = decision.allowed_by

    @staticmethod
    def _valid_matches(
        result: object, count: int, rule_count: int
    ) -> dict[int, RuleMatch]:
        """항목별로 구조를 검사한다. 중복·오류 항목은 유효한 일부도 적용하지 않는다."""
        if not isinstance(result, list) or not all(
            isinstance(match, RuleMatch) for match in result
        ):
            return {}
        matches: dict[int, RuleMatch] = {}
        seen: set[int] = set()
        for match in result:
            if type(match.item) is not int or not 0 <= match.item < count:
                continue
            if match.item in seen:
                matches.pop(match.item, None)
                continue
            seen.add(match.item)
            if len(match.rules) > 1 or any(
                type(i) is not int or not 0 <= i < rule_count for i in match.rules
            ):
                continue
            matches[match.item] = match
        return matches
