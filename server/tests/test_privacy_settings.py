"""PrivacySettings 단위 테스트. 실제 DB·모델 없이 저장소 대역과 스크립트 Runner 사용."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel

from app.models.enums import PromptIdx
from app.models.llm_results import Rule, RuleMatch
from app.privacy.pd_detector import PDItem
from app.privacy.privacy_settings import (
    UNCLEAR_MESSAGE,
    PrivacySettings,
    RulesInterpretationError,
)

AGE_ALLOW = Rule(target="나이", scope="category", action="allow")
PHONE_DENY = Rule(target="전화번호", scope="category", action="deny")
CHAT_ID = 7


class PrivacySetting(BaseModel):
    """#24 공유 자료형과 같은 모양의 테스트용 설정. (실제 자료형은 #34에서 추가)"""

    setting_id: int | None
    text: str
    rules: list[Rule]
    updated_at: str | None


def setting(
    setting_id: int | None, rules: list[Rule], text: str = ""
) -> PrivacySetting:
    return PrivacySetting(
        setting_id=setting_id, text=text, rules=rules, updated_at=None
    )


class FakeStore:
    """#24 ChatManager의 설정 저장 인터페이스 대역."""

    def __init__(self, latest: PrivacySetting | None = None) -> None:
        self.latest = latest or setting(None, [])
        self.history: dict[int, PrivacySetting] = {}
        if self.latest.setting_id is not None:
            self.history[self.latest.setting_id] = self.latest
        self.run_setting = self.latest
        self.saved: list[tuple[str, list[Rule]]] = []
        self.updates: list[tuple[int, list[PDItem]]] = []
        self.fail_update = False

    def save_setting(self, text: str, rules: list[Rule]) -> PrivacySetting:
        self.saved.append((text, rules))
        new_id = max(self.history, default=0) + 1
        self.latest = setting(new_id, rules, text)
        self.history[new_id] = self.latest
        return self.latest.model_copy(deep=True)

    def load_setting(self, setting_id: int | None = None) -> PrivacySetting:
        if setting_id is None:
            return self.latest
        return self.history[setting_id]

    def load_run_setting(self, run_id: int) -> PrivacySetting:
        return self.run_setting

    def update_pd_allowed(self, chat_id: int, items: list[PDItem]) -> None:
        if self.fail_update:
            raise RuntimeError("DB 저장 실패")
        self.updates.append((chat_id, [item.model_copy() for item in items]))

    def get_run(self, run_id: int) -> SimpleNamespace:
        return SimpleNamespace(chat_id=CHAT_ID)


class ScriptedRunner:
    """미리 정한 응답을 순서대로 돌려주는 Runner. 예외를 넣으면 그대로 발생한다."""

    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[Any, ...]] = []

    def request(
        self,
        run_id: int | None,
        input_: str,
        prompt_idx: PromptIdx,
        chat_id: int | None,
        use_context: bool = True,
    ) -> tuple[Any, ...]:
        self.calls.append((run_id, prompt_idx, chat_id, use_context))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def ok(result: Any) -> tuple[Any, ...]:
    return result, True, 1, 1, "stop"


def failed(reason: str) -> tuple[Any, ...]:
    return None, False, None, None, reason


def make(
    *responses: Any, store: FakeStore | None = None
) -> tuple[PrivacySettings, FakeStore, ScriptedRunner]:
    store = store or FakeStore()
    runner = ScriptedRunner(*responses)
    return PrivacySettings(store, runner), store, runner  # type: ignore[arg-type]


def table() -> list[PDItem]:
    return [
        PDItem(pd_id=11, text="27살", category=2),
        PDItem(pd_id=12, text="010-1234-5678", category=0),
        PDItem(pd_id=13, text="인하대학교", category=2),
    ]


# 설정 해석 (parse_rules)


def test_parse_blank_text_resets_without_model() -> None:
    privacy, _, runner = make()
    assert privacy.parse_rules("   ") == []
    assert runner.calls == []


def test_parse_rejects_non_string() -> None:
    privacy, _, _ = make()
    with pytest.raises(TypeError):
        privacy.parse_rules(None)  # type: ignore[arg-type]


def test_parse_returns_model_rules_without_context() -> None:
    privacy, _, runner = make(ok([AGE_ALLOW, PHONE_DENY]))
    assert privacy.parse_rules("나이는 공개, 전화번호는 금지") == [
        AGE_ALLOW,
        PHONE_DENY,
    ]
    assert runner.calls == [(None, PromptIdx.PARSE_RULES, None, False)]


@pytest.mark.parametrize(
    "response",
    [
        ok([]),  # 내용이 있는데 규칙을 하나도 찾지 못함
        ok([Rule(target=" ", scope="category", action="allow")]),  # 빈 대상
        failed("format_failed"),  # 형식·길이 실패
    ],
)
def test_parse_unclear_result_is_interpretation_error(response: Any) -> None:
    privacy, _, _ = make(response)
    with pytest.raises(RulesInterpretationError, match=UNCLEAR_MESSAGE):
        privacy.parse_rules("날씨 알려줘")


def test_parse_separates_unavailable_and_stopped() -> None:
    privacy, _, _ = make(failed("unavailable"), failed("stopped"))
    with pytest.raises(ConnectionError):
        privacy.parse_rules("나이는 공개")
    with pytest.raises(InterruptedError):
        privacy.parse_rules("나이는 공개")


# 저장과 조회 (save, load, load_rules)


def test_save_success_replaces_current_setting() -> None:
    privacy, store, _ = make(ok([AGE_ALLOW]))
    result = privacy.save("나이는 공개해도 돼")
    assert result.ok is True
    assert result.rules == [AGE_ALLOW]
    assert store.saved == [("나이는 공개해도 돼", [AGE_ALLOW])]
    assert privacy.setting_id == store.latest.setting_id


def test_save_interpretation_failure_keeps_previous_setting() -> None:
    store = FakeStore(setting(1, [PHONE_DENY], "전화번호는 금지"))
    privacy, store, _ = make(ok([]), store=store)
    privacy.load_rules()
    result = privacy.save("날씨 알려줘")
    assert result.ok is False
    assert result.message == UNCLEAR_MESSAGE
    assert result.rules == [PHONE_DENY]  # 기존 규칙을 돌려준다
    assert store.saved == []  # 저장하지 않는다
    assert privacy.setting_id == 1


def test_save_blank_text_stores_empty_setting() -> None:
    privacy, store, runner = make()
    result = privacy.save("  ")
    assert result.ok is True
    assert store.saved == [("", [])]
    assert runner.calls == []


def test_save_connection_error_propagates_without_saving() -> None:
    privacy, store, _ = make(failed("unavailable"))
    with pytest.raises(ConnectionError):
        privacy.save("나이는 공개")
    assert store.saved == []


def test_load_rules_restores_latest_and_returns_copies() -> None:
    store = FakeStore(setting(3, [AGE_ALLOW], "나이는 공개"))
    privacy, store, _ = make(store=store)
    assert privacy.setting_id is None
    rules = privacy.load_rules()
    assert rules == [AGE_ALLOW]
    assert privacy.setting_id == 3
    rules[0].action = "deny"  # 반환값을 바꿔도 내부 설정은 그대로
    assert store.latest.rules[0].action == "allow"


def test_load_past_setting_does_not_change_current_id() -> None:
    store = FakeStore(setting(1, [AGE_ALLOW]))
    privacy, store, _ = make(ok([PHONE_DENY]), store=store)
    privacy.save("전화번호는 금지")  # 2번 설정이 현재
    assert privacy.load(1).rules == [AGE_ALLOW]
    assert privacy.setting_id == 2


# 규칙 선택 결과 판정 (is_allowed)


@pytest.mark.parametrize(
    ("match", "expected"),
    [
        (RuleMatch(item=0, rules=[0]), True),  # 허용 규칙 선택
        (RuleMatch(item=0, rules=[1]), False),  # 금지 규칙 선택
        (RuleMatch(item=0, rules=[]), False),  # 미대응
        (RuleMatch(item=0, rules=[0, 1]), False),  # 복수 선택
        (RuleMatch(item=0, rules=[5]), False),  # 범위 밖 규칙 번호
        (None, False),
    ],
)
def test_is_allowed(match: RuleMatch | None, expected: bool) -> None:
    privacy, _, _ = make()
    assert privacy.is_allowed(match, [AGE_ALLOW, PHONE_DENY]) is expected


# 실행 시작 시 적용 (apply_to_table)


def test_apply_uses_run_setting_and_saves_table() -> None:
    store = FakeStore(setting(1, [AGE_ALLOW, PHONE_DENY]))
    store.latest = setting(2, [])  # 실행 시작 뒤 저장된 새 설정은 무시
    matches = [
        RuleMatch(item=0, rules=[0]),  # 27살 → 나이 허용
        RuleMatch(item=1, rules=[1]),  # 전화번호 → 금지
        RuleMatch(item=2, rules=[]),  # 인하대학교 → 미대응
    ]
    privacy, store, runner = make(ok(matches), store=store)
    pd_table = table()
    pd_table[2].allowed, pd_table[2].allowed_by = True, "consent"  # 이전 실행의 동의

    privacy.apply_to_table(5, pd_table)

    assert runner.calls == [(5, PromptIdx.MATCH_RULES, CHAT_ID, False)]
    assert [(i.allowed, i.allowed_by) for i in pd_table] == [
        (True, "setting"),
        (False, "setting"),
        (False, "none"),  # 이전 동의가 새 실행으로 넘어가지 않음
    ]
    saved_chat, saved_items = store.updates[0]
    assert saved_chat == CHAT_ID
    assert [i.allowed for i in saved_items] == [True, False, False]


def test_apply_without_rules_skips_model_and_denies_all() -> None:
    privacy, store, runner = make()
    pd_table = table()
    pd_table[0].allowed = True
    privacy.apply_to_table(5, pd_table)
    assert runner.calls == []
    assert all(not i.allowed and i.allowed_by == "none" for i in pd_table)
    assert len(store.updates) == 1


def test_apply_invalid_matches_deny_only_those_items() -> None:
    store = FakeStore(setting(1, [AGE_ALLOW]))
    matches = [
        RuleMatch(item=0, rules=[0]),  # 정상 → 허용
        RuleMatch(item=1, rules=[0]),
        RuleMatch(item=1, rules=[0]),  # 같은 항목 중복 → 금지
        RuleMatch(item=2, rules=[3]),  # 범위 밖 규칙 → 금지
        RuleMatch(item=9, rules=[0]),  # 범위 밖 항목 → 무시
    ]
    privacy, _, _ = make(ok(matches), store=store)
    pd_table = table()
    privacy.apply_to_table(5, pd_table)
    assert [i.allowed for i in pd_table] == [True, False, False]


@pytest.mark.parametrize(
    "response",
    [failed("format_failed"), ConnectionError("Runner 종료")],
)
def test_apply_model_failure_denies_all(response: Any) -> None:
    store = FakeStore(setting(1, [AGE_ALLOW]))
    privacy, store, _ = make(response, store=store)
    pd_table = table()
    privacy.apply_to_table(5, pd_table)
    assert not any(i.allowed for i in pd_table)
    assert len(store.updates) == 1  # 실패해도 기본 금지를 저장


def test_apply_save_failure_keeps_memory_denied() -> None:
    store = FakeStore(setting(1, [AGE_ALLOW]))
    store.fail_update = True
    privacy, _, _ = make(ok([RuleMatch(item=0, rules=[0])]), store=store)
    pd_table = table()
    with pytest.raises(RuntimeError):
        privacy.apply_to_table(5, pd_table)
    assert not any(i.allowed for i in pd_table)  # 메모리에서만 허용되는 상태 없음
