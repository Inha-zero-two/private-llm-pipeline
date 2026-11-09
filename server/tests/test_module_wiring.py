"""실제 백엔드 모듈 연결 검사. 모델 전송과 미구현 모듈만 테스트 대역 사용."""

import asyncio
import json
from collections import deque
from collections.abc import Iterator
from typing import Any
from unittest.mock import Mock, call

import pytest

from app.data.prompt_manager import PromptManager
from app.main import Main
from app.models.enums import EndReason, PromptIdx
from app.pipeline.planner import Plan, Planner
from app.privacy.pd_detector import PDDetector, PDItem
from app.runtime.local_llm_runner import LocalLLMRunner
from app.runtime.local_res_parser import OutputFormatError, parser


class ScriptedLlama:
    """실제 Runner의 스트림 수신·파싱을 통과시키는 모델 전송 대역."""

    def __init__(self) -> None:
        self.outputs: deque[str] = deque(["OK"])
        self.messages: list[list[dict[str, str]]] = []

    def tokenize(self, text: bytes, **kwargs: Any) -> list[int]:
        return list(range(len(text.decode("utf-8"))))

    def create_chat_completion(
        self, *, messages: list[dict[str, str]], **kwargs: Any
    ) -> Iterator[dict[str, Any]]:
        self.messages.append(messages)
        yield {
            "choices": [
                {"delta": {"content": self.outputs.popleft()}, "finish_reason": "stop"}
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }

    def close(self) -> None:
        pass


@pytest.fixture
def connected(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[LocalLLMRunner, ScriptedLlama]]:
    model = ScriptedLlama()
    monkeypatch.setattr(
        "src.localLLM_API_proxy.llama_wrapper.Llama", Mock(return_value=model)
    )
    with LocalLLMRunner(prompt_manager=PromptManager()) as runner:
        yield runner, model


def test_detector_and_planner_use_prompts_and_parser(
    connected: tuple[LocalLLMRunner, ScriptedLlama], monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, model = connected
    log_call = Mock()
    monkeypatch.setattr(runner, "_log_call", log_call)
    model.outputs.extend(
        [
            '{"items": [{"text": "김민수", "category": 0}]}',
            '{"needs": ["지원동기 구성"]}',
            '{"enough": false, "needConsent": true, '
            '"consentItems": [1, 2, 3, 999, 1], "need": [" 지원동기 구성 "]}',
        ]
    )
    question = "김민수의 지원동기를 작성해 줘"
    detector, planner = PDDetector(runner), Planner(runner)
    items = detector.detect("run-1", "chat-1", "msg-1", question)
    assert items == [PDItem(text="김민수", category=0)]
    assert planner.list_needs(question, "run-1", "chat-1") == ["지원동기 구성"]
    table = [
        items[0].model_copy(update={"pd_id": 1}),
        PDItem(pd_id=2, text="허용 항목", category=2, allowed=True),
        PDItem(pd_id=3, text="거부 항목", category=2, allowed_by="denied"),
    ]
    plan = planner.judge(question, "Answers:", table, "run-1", "chat-1")
    assert type(plan) is Plan
    assert not plan.enough and plan.need_consent
    assert plan.consent_items == [1]
    assert plan.need == ["지원동기 구성"]
    prompts = runner.prompt_manager
    assert prompts is not None
    stages = [PromptIdx.DETECT, PromptIdx.NEEDS, PromptIdx.JUDGE]
    assert [messages[0]["content"] for messages in model.messages[1:]] == [
        prompts.get_prompt(stage) for stage in stages
    ]
    assert "[메모]\nAnswers:" in model.messages[-1][1]["content"]
    assert "id=1" in model.messages[-1][1]["content"]
    assert [entry.args[0] for entry in log_call.call_args_list] == ["run-1"] * 3
    assert [entry.kwargs["prompt_version"] for entry in log_call.call_args_list] == [
        prompts.get_version()
    ] * 3


def test_parser_uses_planner_plan_and_requires_all_model_output_fields() -> None:
    result = parser(
        '{"enough": true, "needConsent": false, "consentItems": [], "need": []}',
        PromptIdx.JUDGE,
    )
    assert type(result) is Plan
    assert Plan(enough=True).need == []  # Planner 직접 사용 시 기본값 유지
    complete = result.model_dump()
    for field in Plan.model_fields:
        incomplete = {key: value for key, value in complete.items() if key != field}
        with pytest.raises(OutputFormatError):
            parser(json.dumps(incomplete), PromptIdx.JUDGE)
    with pytest.raises(OutputFormatError):
        parser(
            '{"enough": "true", "needConsent": false, "consentItems": [], "need": []}',
            PromptIdx.JUDGE,
        )


@pytest.mark.parametrize("stage", ["detect", "needs", "judge"])
def test_invalid_model_output_propagates_after_runner_retries(
    connected: tuple[LocalLLMRunner, ScriptedLlama], stage: str
) -> None:
    runner, model = connected
    model.outputs.extend(["invalid json"] * 3)
    with pytest.raises(RuntimeError):
        if stage == "detect":
            PDDetector(runner).detect("run-1", "chat-1", "msg-1", "질문")
        elif stage == "needs":
            Planner(runner).list_needs("질문", "run-1", "chat-1")
        else:
            Planner(runner).judge("질문", "메모", [], "run-1", "chat-1")
    assert len(model.messages) == 4  # 연결 확인 한 번 + 실제 작업 세 번


def test_explicit_prompt_and_results_remain_supported(
    connected: tuple[LocalLLMRunner, ScriptedLlama], monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, model = connected
    log_call = Mock()
    monkeypatch.setattr(runner, "_log_call", log_call)
    model.outputs.append('{"needs": ["직접 요청"]}')
    result = runner.request(
        "run-1",
        "질문",
        PromptIdx.NEEDS,
        "chat-1",
        system_prompt="직접 지정",
        prompt_version="custom",
    )
    assert result[:2] == (["직접 요청"], True)
    assert model.messages[-1][0]["content"] == "직접 지정"
    assert log_call.call_args.kwargs["prompt_version"] == "custom"
    assert (
        PDDetector(runner).detect("run-1", "chat-1", "msg-1", "질문", found_items=[])
        == []
    )
    planner = Planner(runner)
    assert planner.list_needs("질문", needs=["기존 결과"]) == ["기존 결과"]
    assert planner.judge("질문", "", [], raw_plan={"enough": True}).enough
    assert len(model.messages) == 2


def test_managed_prompts_work_with_context_isolation(
    connected: tuple[LocalLLMRunner, ScriptedLlama],
) -> None:
    runner, model = connected
    model.outputs.extend(['{"needs": ["항목"]}'] * 3)
    for run_id, question in [
        ("run-1", "첫 질문"),
        ("run-1", "다음 질문"),
        ("run-2", "새 질문"),
    ]:
        result = runner.request(
            run_id, question, PromptIdx.NEEDS, "chat-1", use_context=True
        )
        assert result[1]
    assert "첫 질문" in model.messages[2][1]["content"]
    assert "다음 질문" in model.messages[2][1]["content"]
    assert model.messages[3][1]["content"] == "새 질문"


def test_main_calls_connected_modules(
    connected: tuple[LocalLLMRunner, ScriptedLlama], monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, model = connected
    log_call = Mock()
    monkeypatch.setattr(runner, "_log_call", log_call)
    model.outputs.extend(
        [
            '{"items": []}',
            '{"needs": ["설명"]}',
            '{"enough": true, "needConsent": false, "consentItems": [], "need": []}',
        ]
    )
    # dev에 없는 모듈만 대역으로 두어 Main → 실제 검출·분류·판단 경로 검사
    chat = Mock()
    chat.save_message.side_effect = ["msg-user", "msg-answer"]
    chat.start_run.return_value = "run-main"
    chat.get_pd_table.return_value = []
    memo, frontier, answer = Mock(), Mock(), Mock()
    memo.read_all.return_value = "Answers: 설명"
    frontier.check_connection.return_value = True
    answer.generate.return_value = iter(["답변"])
    main = Main(
        call_limit=1,
        regen_limit=1,
        budget_limit=1.0,
        local_llm_runner=runner,
        prompt_manager=runner.prompt_manager,
        pd_detector=PDDetector(runner),
        planner=Planner(runner),
        chat_manager=chat,
        memo_manager=memo,
        frontier_client=frontier,
        privacy_settings=Mock(setting_id=1),
        consent_manager=Mock(),
        request_writer=Mock(),
        leak_guard=Mock(),
        answer_generator=answer,
    )
    asyncio.run(main.run_pipeline("chat-main", "질문"))
    assert [entry.args[0] for entry in log_call.call_args_list] == ["run-main"] * 3
    memo.set_needs.assert_called_once_with(["설명"])
    chat.finish_run.assert_called_once_with(
        "run-main", EndReason.INFO_ENOUGH, "msg-answer", "Answers: 설명"
    )
    assert chat.save_message.call_args_list[-1] == call(
        "chat-main", "assistant", "답변"
    )
    frontier.send.assert_not_called()
