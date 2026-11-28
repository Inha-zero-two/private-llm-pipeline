"""로컬 단독의 앞 대화 만들기 · 길이 처리.

파이프라인과 같은 모양의 글을 만들지만 ContextManager를 쓰지 않는다.
길면 요약하지 않고 오래된 질문·답변 쌍부터 뺀다.
"""

from __future__ import annotations

from collections.abc import Callable

from .models import ErrorCode, ModelFailure, Turn

CONTEXT_TOKENS = 32768
MAX_OUTPUT_TOKENS = 5000
MARGIN_TOKENS = 1024
INPUT_LIMIT = CONTEXT_TOKENS - MAX_OUTPUT_TOKENS - MARGIN_TOKENS  # 26,744토큰


def build_input(turns: list[Turn], question: str) -> str:
    """앞 대화와 질문을 하나의 글로 잇는다. 앞 대화가 없으면 질문만."""
    if not turns:
        return question
    history = "".join(
        f"\n[user]\n{turn.question}\n\n[assistant]\n{turn.answer}\n" for turn in turns
    )
    return (
        "[이전 대화: 현재 입력을 이해하기 위한 참고 자료]\n"
        f"{history}\n\n[현재 입력]\n{question}"
    )


def fit_input(
    turns: list[Turn], question: str, count_tokens: Callable[[str], int]
) -> tuple[str, int]:
    """한도에 맞는 글과 뺀 쌍의 수. 오래된 쌍부터 하나씩 뺀 결과와 같다.

    길이를 셀 때마다 모델 서버에 묻는다. 쌍을 뺄수록 짧아지므로 이분 탐색으로
    가장 적게 빼는 수를 찾아 묻는 횟수를 줄인다.
    """

    def text_after(dropped: int) -> str:
        return build_input(turns[dropped:], question)

    def fits(dropped: int) -> bool:
        return count_tokens(text_after(dropped)) <= INPUT_LIMIT

    if fits(0):
        return text_after(0), 0
    if not turns or not fits(len(turns)):
        raise ModelFailure(
            ErrorCode.INPUT_TOO_LONG, "local input", dropped_pairs=len(turns)
        )
    low, high = 1, len(turns)  # high는 항상 한도 안
    while low < high:
        middle = (low + high) // 2
        if fits(middle):
            high = middle
        else:
            low = middle + 1
    return text_after(high), high
