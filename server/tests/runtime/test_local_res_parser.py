"""작업별 반환 타입, 엄격한 형식 검사와 정상 빈 결과를 검증한다."""

import pytest

from app.models.enums import PromptIdx
from app.models.local_llm_outputs import (
    ConsentText,
    DetectedItem,
    ParsedResult,
    Plan,
    Rule,
    RuleMatch,
)
from app.runtime.local_res_parser import OutputFormatError, parser


@pytest.mark.parametrize(
    ("task", "raw", "expected"),
    [
        (
            "DETECT",
            '{"items":[{"text":"샘플 정보","category":2}]}',
            [DetectedItem(text="샘플 정보", category=2)],
        ),
        ("NEEDS", '{"needs":["직무 요건"]}', ["직무 요건"]),
        (
            "JUDGE",
            '{"enough":false,"needConsent":true,"consentItems":[17],"need":["요건"]}',
            Plan(enough=False, need_consent=True, consent_items=[17], need=["요건"]),
        ),
        ("GUARD", '{"hits":[0,2]}', [0, 2]),
        (
            "PARSE_RULES",
            '{"rules":[{"target":"직업","scope":"category","action":"allow"}]}',
            [Rule(target="직업", scope="category", action="allow")],
        ),
        (
            "MATCH_RULES",
            '{"matches":[{"item":0,"rules":[1,2]}]}',
            [RuleMatch(item=0, rules=[1, 2])],
        ),
        (
            "CONSENT",
            '{"purpose":"목적","benefit":"이점","risk":"불이익"}',
            ConsentText(purpose="목적", benefit="이점", risk="불이익"),
        ),
        ("DECIDE_CONTEXT", '{"context":"keep"}', "keep"),
        ("DECIDE_CONTEXT", '{"context":"reset"}', "reset"),
        ("WRITE", "요청문", "요청문"),
        ("REWRITE", "수정문", "수정문"),
        ("ANSWER", "  첫 줄\n\n    다음 줄\n", "  첫 줄\n\n    다음 줄\n"),
        ("MEMO_SUMMARY", "요약", "요약"),
    ],
)
def test_task_returns_typed_value(task: str, raw: str, expected: ParsedResult) -> None:
    result = parser(raw, task)
    assert result == expected
    assert type(result) is type(expected)


@pytest.mark.parametrize(
    "field,task",
    [
        ("items", "DETECT"),
        ("needs", "NEEDS"),
        ("hits", "GUARD"),
        ("rules", "PARSE_RULES"),
        ("matches", "MATCH_RULES"),
    ],
)
def test_empty_list_is_valid(field: str, task: str) -> None:
    assert parser(f'{{"{field}":[]}}', task) == []


def test_outer_json_fence_is_removed_once() -> None:
    assert parser('  ```json\n{"hits":[1]}\n```  ', PromptIdx.GUARD) == [1]
    assert parser('```\r\n{"hits":[]}\r\n```', "GUARD") == []


@pytest.mark.parametrize(
    ("task", "raw"),
    [
        ("ANSWER", " \n\t"),
        ("GUARD", ""),
        ("GUARD", "{"),
        ("GUARD", '설명 {"hits":[]}'),
        ("GUARD", '```json\n{"hits":[]}'),
        ("GUARD", "[]"),
        ("GUARD", "null"),
        ("GUARD", "{}"),
        ("GUARD", '{"hits":null}'),
        ("GUARD", '{"hits":"[]"}'),
        ("GUARD", '{"hits":["0"]}'),
        ("GUARD", '{"hits":[true]}'),
        ("GUARD", '{"hits":[-1]}'),
        ("NEEDS", '{"needs":[1]}'),
        ("DETECT", '{"items":[{"text":"샘플"}]}'),
        ("DETECT", '{"items":[{"text":"샘플","category":3}]}'),
        ("DETECT", '{"items":[{"text":"샘플","category":true}]}'),
        ("DETECT", '{"items":[{"text":"샘플","category":1.0}]}'),
        ("JUDGE", '{"enough":"false","needConsent":false,"consentItems":[],"need":[]}'),
        ("JUDGE", '{"enough":true}'),
        ("PARSE_RULES", '{"rules":[{"target":"나이","scope":"all","action":"allow"}]}'),
        ("PARSE_RULES", '{"rules":[{"target":"나이","scope":"item","action":"yes"}]}'),
        ("MATCH_RULES", '{"matches":[{"item":0,"rules":[-1]}]}'),
        ("CONSENT", '{"purpose":"목적","benefit":"이점"}'),
        ("DECIDE_CONTEXT", '{"context":true}'),
    ],
)
def test_invalid_output_raises_format_error(task: str, raw: str) -> None:
    with pytest.raises(OutputFormatError):
        parser(raw, task)


def test_error_locates_invalid_list_field() -> None:
    with pytest.raises(OutputFormatError, match="0.category"):
        parser('{"items":[{"text":"샘플"}]}', "DETECT")


@pytest.mark.parametrize("task", ["UNKNOWN", "FRONTIER"])
def test_wrong_task_is_configuration_error(task: str) -> None:
    with pytest.raises(ValueError) as raised:
        parser("{}", task)
    assert not isinstance(raised.value, OutputFormatError)
