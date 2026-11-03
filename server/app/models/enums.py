"""모듈 사이에서 공유하는 작업 종류."""

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
