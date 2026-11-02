"""여러 모듈이 공유하는 프롬프트 작업 이름."""

from enum import StrEnum


class PromptIdx(StrEnum):
    DETECT = "DETECT"
    NEEDS = "NEEDS"
    JUDGE = "JUDGE"
    WRITE = "WRITE"
    REWRITE = "REWRITE"
    GUARD = "GUARD"
    ANSWER = "ANSWER"
    PARSE_RULES = "PARSE_RULES"
    MATCH_RULES = "MATCH_RULES"
    CONSENT = "CONSENT"
    DECIDE_CONTEXT = "DECIDE_CONTEXT"
    FRONTIER = "FRONTIER"
    MEMO_SUMMARY = "MEMO_SUMMARY"
