"""로컬 LLM이 할 판단을 키워드 규칙으로 흉내 낸다 (web/src/demo의 로직을 옮김).

- detect(): 질문 속 개인정보 찾기 (실제: PDDetector)
- interpret_consent(): 자연어 동의 응답 해석 (실제: ConsentManager + 로컬 LLM)
- parse_rules(): 공개 허용 설정문 해석 (실제: PrivacySettings + 로컬 LLM)
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from app.api.schemas import Decision, PDItem, Rule


@dataclass(frozen=True)
class Detector:
    pattern: re.Pattern[str]
    category: Literal[0, 1, 2]
    label: str


DETECTORS = [
    Detector(re.compile(r"01[016789]-?\d{3,4}-?\d{4}"), 0, "전화번호"),
    Detector(re.compile(r"이름은\s*([가-힣]{2,3})"), 0, "이름"),
    Detector(re.compile(r"\d{1,3}\s*살"), 2, "나이"),
    Detector(re.compile(r"[가-힣A-Za-z]+(?:전자|그룹|은행|대학교)"), 2, "직장"),
]


def detect(question: str, next_id: Callable[[], int]) -> list[PDItem]:
    """질문에서 개인정보 항목을 찾는다. '동의'가 들어가면 예시 항목을 만든다."""
    items: list[PDItem] = []
    for d in DETECTORS:
        for m in d.pattern.finditer(question):
            text = m.group(1) if m.groups() else m.group(0)
            items.append(
                PDItem(pd_id=next_id(), label=d.label, text=text, category=d.category)
            )
    if not items and "동의" in question:
        items = [
            PDItem(pd_id=next_id(), label="직무", text="개발자", category=2),
            PDItem(pd_id=next_id(), label="경력", text="3년", category=2),
        ]
    return items


def is_allowed(item: PDItem, rules: list[Rule]) -> bool:
    """설정에서 허용한 항목이면 동의를 묻지 않는다. 대상 이름이 종류 이름에 들어가는지만 본다."""
    matched = [r for r in rules if r.target in item.label or item.label in r.target]
    return bool(matched) and all(r.action == "allow" for r in matched)


def mask(question: str, items: list[PDItem]) -> str:
    for item in items:
        question = question.replace(item.text, f"[{item.label}]")
    return question


# 동의 응답 해석

DENY = re.compile(
    r"(하지\s*마|말아|말고|숨겨|빼|제외|비공개|안\s*돼|안\s*할|싫|금지|보내지\s*마"
    r"|공개\s*안|하지\s*않|아니)"
)
ALLOW = re.compile(r"(공개|괜찮|보내|허용|돼|좋아|해도|응|네)")
ALIASES = {"직장": ["회사", "학교", "소속"], "전화번호": ["번호", "연락처"]}
REST = "나머지"


@dataclass(frozen=True)
class ConsentInterpretation:
    ok: bool
    decisions: list[Decision]
    message: str


def _verdict(text: str) -> bool | None:
    if DENY.search(text):
        return False
    if ALLOW.search(text):
        return True
    return None


def _mention_at(text: str, item: PDItem) -> int:
    words = [item.label, item.text, *ALIASES.get(item.label, [])]
    found = [i for i in (text.find(w) for w in words) if i >= 0]
    return min(found) if found else -1


def interpret_consent(answer: str, items: list[PDItem]) -> ConsentInterpretation:
    """언급한 항목은 그 뒤에 오는 말로, 언급하지 않은 항목은 금지로 정한다."""
    text = answer.strip()
    fail = ConsentInterpretation(
        ok=False,
        decisions=[],
        message=(
            "어떤 정보를 공개할지 알아듣지 못했어요. "
            f'"{example(items)}"처럼 답해 주세요.'
        ),
    )

    marks = {item.pd_id: _mention_at(text, item) for item in items}
    rest_at = text.find(REST)
    points = sorted(i for i in [*marks.values(), rest_at] if i >= 0)

    # 아무 항목도 짚지 않았으면 답변 전체가 모든 항목에 대한 답이다 ("모두 공개해도 돼")
    if not points:
        v = _verdict(text)
        if v is None:
            return fail
        decisions = [Decision(pd_id=item.pd_id, allow=v) for item in items]
        return ConsentInterpretation(True, decisions, summarize(items, decisions))

    def verdict_from(at: int) -> bool | None:
        # 언급 위치부터 다음 언급 전까지가 그 항목에 대한 말이다.
        # 판단할 말이 없으면 뒤 구간을 따른다 ("나이랑 직장 다 공개해")
        for i in range(points.index(at), len(points)):
            end = points[i + 1] if i + 1 < len(points) else len(text)
            v = _verdict(text[points[i] : end])
            if v is not None:
                return v
        return None

    decisions = []
    for item in items:
        at = marks[item.pd_id]
        if at >= 0:
            allow = verdict_from(at)
        elif rest_at >= 0:
            allow = verdict_from(rest_at)
        else:
            allow = False
        if allow is None:
            return fail
        decisions.append(Decision(pd_id=item.pd_id, allow=allow))
    return ConsentInterpretation(True, decisions, summarize(items, decisions))


def josa(word: str, pair: Literal["은/는", "이/가", "을/를"]) -> str:
    """낱말 끝 받침에 맞는 조사를 붙인다. josa('나이', '은/는') → '나이는'"""
    with_batchim, without_batchim = pair.split("/")
    code = ord(word[-1]) - 0xAC00
    has_batchim = 0 <= code <= 11171 and code % 28 != 0
    return word + (with_batchim if has_batchim else without_batchim)


def example(items: list[PDItem]) -> str:
    if not items:
        return "모두 공개해도 돼"
    if len(items) == 1:
        return f"{josa(items[0].label, '은/는')} 공개해도 돼"
    first, second = items[0], items[1]
    return (
        f"{josa(first.label, '은/는')} 공개하고 "
        f"{josa(second.label, '은/는')} 공개하지 마"
    )


def summarize(items: list[PDItem], decisions: list[Decision]) -> str:
    """'나이는 공개하고 직장은 공개하지 않습니다.' 같은 안내 문구."""
    allow = {d.pd_id for d in decisions if d.allow}
    shown = [i.label for i in items if i.pd_id in allow]
    hidden = [i.label for i in items if i.pd_id not in allow]
    if not hidden:
        return "모든 항목을 공개합니다."
    if not shown:
        return "어떤 항목도 공개하지 않습니다."
    return (
        f"{josa(', '.join(shown), '은/는')} 공개하고 "
        f"{josa(', '.join(hidden), '은/는')} 공개하지 않습니다."
    )


# 공개 허용 설정문 해석

SETTING_DENY = re.compile(r"(하지\s*마|숨겨|말아|금지|안\s*돼|보내지\s*마)")
SETTING_ALLOW = re.compile(r"(공개|괜찮|보내도|허용|돼)")


def parse_rules(text: str) -> list[Rule]:
    """문장을 나눠 대상과 허용·금지 여부만 본다. 해석할 문장이 없으면 빈 목록."""
    rules: list[Rule] = []
    for sentence in re.split(r"[.\n]|그리고|[,，]", text):
        s = sentence.strip()
        if not s:
            continue
        deny = bool(SETTING_DENY.search(s))
        if not deny and not SETTING_ALLOW.search(s):
            continue
        target = re.sub(r"(은|는|이|가|도)\s.*$", "", s)
        target = re.sub(r"\s*(정보)$", "", target).strip()
        for t in (x.strip() for x in re.split(r"와|과|및", target)):
            if t:
                rules.append(
                    Rule(target=t, scope="category", action="deny" if deny else "allow")
                )
    return rules
