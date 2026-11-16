"""대역 모듈과 동의 해석의 개인정보 관련 규칙."""

from app.api.fake_interpret import interpret_consent
from app.api.schemas import PDItem as ScreenItem
from app.api.stand_ins import ConsentResult, ScreenConsentManager
from app.api.store import Store
from app.privacy.pd_detector import PDItem


def test_denied_items_are_not_asked_again_in_same_run() -> None:
    table = [
        PDItem(pd_id=1, text="27살", category=2),
        PDItem(pd_id=2, text="삼성전자", category=2),
    ]
    manager = ScreenConsentManager(Store())
    req = manager.build_message("1", [1, 2], ["이직"], table)
    assert req is not None
    manager.apply(ConsentResult({1}), req, table)
    assert (table[0].allowed, table[0].allowed_by) == (True, "consent")
    # 거부한 직장은 denied로 남아 Planner가 같은 실행에서 동의 후보로 고르지 않는다
    assert (table[1].allowed, table[1].allowed_by) == (False, "denied")
    manager.release("1", table)
    assert [(i.allowed, i.allowed_by) for i in table] == [(False, "none")] * 2


def test_answer_about_other_item_is_not_read_as_allow_all() -> None:
    items = [ScreenItem(pd_id=2, label="직장", text="삼성전자", category=2)]
    # 직장만 묻는데 나이에 대해 답하면, 전체 허용으로 읽지 않고 다시 묻는다
    assert interpret_consent("나이는 공개해도 돼", items).ok is False
    assert interpret_consent("공개해도 돼", items).decisions[0].allow is True
    assert interpret_consent("회사는 빼줘", items).decisions[0].allow is False


def test_build_message_skips_when_nothing_to_ask() -> None:
    table = [PDItem(pd_id=1, text="27살", category=2, allowed=True)]
    assert ScreenConsentManager(Store()).build_message("1", [1], [], table) is None


def test_request_writer_masks_need_names_too() -> None:
    from app.api.stand_ins import MaskingRequestWriter, TextLeakGuard

    table = [PDItem(pd_id=2, text="삼성전자", category=2)]
    text = MaskingRequestWriter().write(
        "삼성전자 다니는데 이직 고민", "", table, ["현재 직장(삼성전자)의 장단점 분석"]
    )
    assert "삼성전자" not in text
    assert TextLeakGuard().detect("1", text, table).passed
