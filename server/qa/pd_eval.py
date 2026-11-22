"""개인정보 검출(PDDetector) 평가: 정답이 달린 문장을 하나씩 넣어 정확도와 안정성을 잰다.

    cd server
    python -m qa.pd_eval 데이터.jsonl [--limit N] [--out 폴더]

데이터는 한 줄에 문장 하나인 JSONL이다. id는 생략하면 줄 번호.
    {"id": "s1", "text": "제 번호는 010-1234-5678이에요", "pd": [{"text": "010-1234-5678", "category": 0}]}
category는 0 식별 · 1 민감 · 2 속성이고, 생략하면 분류는 채점하지 않는다.

결과 폴더에 문장별 결과(results.jsonl, 진행 중에도 한 줄씩 기록)와 요약(summary.md)을 남긴다.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from time import monotonic
from typing import Any

from pydantic import BaseModel, Field

SERVER_DIR = Path(__file__).resolve().parents[1]
# app(server/)과 래퍼 src(저장소 루트)를 PYTHONPATH 없이도 찾게 한다
for _path in (SERVER_DIR, SERVER_DIR.parent):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from app.data.prompt_manager import PromptManager  # noqa: E402
from app.models.enums import PromptIdx  # noqa: E402
from app.privacy.pd_detector import CATEGORY_LABEL, PDDetector  # noqa: E402

RESULTS_DIR = SERVER_DIR / "qa" / "results"
# 표기 차이(공백·하이픈·점 등)는 무시하고 비교한다. 010-1234-5678 == 01012345678
_SEPARATORS = re.compile(r"[\s\-_.·,/()\[\]]+")


class Label(BaseModel):
    text: str = Field(min_length=1)
    category: int | None = Field(default=None, ge=0, le=2)


class Sample(BaseModel):
    id: str
    text: str = Field(min_length=1)
    pd: list[Label] = Field(default_factory=list)


class SampleResult(BaseModel):
    id: str
    ok: bool  # 검출이 끝까지 성공했는지 (형식 오류·연결 실패·예외면 False)
    reason: str | None = None  # 실패 사유 또는 Runner 종료 사유
    elapsed_s: float
    input_tokens: int = 0
    output_tokens: int = 0
    expected: list[Label]
    detected: list[Label] = Field(default_factory=list)
    tp: int = 0
    fn: list[str] = Field(default_factory=list)  # 놓친 정답
    fp: list[str] = Field(default_factory=list)  # 정답에 없는 검출
    wrong_category: list[str] = Field(default_factory=list)
    categorized: int = 0  # 찾은 정답 중 분류를 채점한 수


def normalize(text: str) -> str:
    return _SEPARATORS.sub("", text).casefold()


def same(a: str, b: str) -> bool:
    """한쪽이 다른 쪽을 포함하면 같은 항목. ("27살" ↔ "저는 27살")"""
    x, y = normalize(a), normalize(b)
    return bool(x and y) and (x in y or y in x)


def load_samples(path: Path) -> list[Sample]:
    samples: list[Sample] = []
    seen: set[str] = set()
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            data = json.loads(line)
            data.setdefault("id", str(line_no))
            data["id"] = str(data["id"])
            sample = Sample.model_validate(data)
        except ValueError as error:
            raise ValueError(f"{path}:{line_no} 형식 오류: {error}") from error
        if sample.id in seen:
            raise ValueError(f"{path}:{line_no} id가 겹칩니다: {sample.id}")
        seen.add(sample.id)
        samples.append(sample)
    return samples


def grade(result: SampleResult) -> SampleResult:
    """정답과 검출을 항목 단위로 맞춘다. 정답 하나에 검출 하나만 짝짓는다."""
    unused = list(range(len(result.detected)))
    for label in result.expected:
        match = next(
            (i for i in unused if same(label.text, result.detected[i].text)), None
        )
        if match is None:
            result.fn.append(label.text)
            continue
        unused.remove(match)
        result.tp += 1
        if label.category is not None:
            result.categorized += 1
            if result.detected[match].category != label.category:
                result.wrong_category.append(label.text)
    result.fp = [result.detected[i].text for i in unused]
    return result


def evaluate(sample: Sample, runner: Any, detector: PDDetector) -> SampleResult:
    """문장 하나를 실제 검출 경로(Runner의 DETECT 요청 → PDDetector 검사)로 돌린다."""
    started = monotonic()
    result = SampleResult(id=sample.id, ok=False, elapsed_s=0.0, expected=sample.pd)
    try:
        found, ok, input_tokens, output_tokens, reason = runner.request(
            None, sample.text, PromptIdx.DETECT, None
        )
        result.input_tokens, result.output_tokens = input_tokens, output_tokens
        result.reason = reason
        if ok:
            items = detector.detect(0, 0, 0, sample.text, found_items=found, ok=ok)
            result.detected = [Label(text=i.text, category=i.category) for i in items]
            result.ok = True
    except Exception as error:
        result.reason = f"{type(error).__name__}: {error}"
    result.elapsed_s = round(monotonic() - started, 3)
    return grade(result) if result.ok else result


def _ratio(num: float, den: float) -> float | None:
    return num / den if den else None


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value * 100:.1f}%"


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * p))]


def summarize(results: list[SampleResult], total_s: float) -> dict[str, Any]:
    done = [r for r in results if r.ok]
    tp = sum(r.tp for r in done)
    fn = sum(len(r.fn) for r in done)
    fp = sum(len(r.fp) for r in done)
    precision, recall = _ratio(tp, tp + fp), _ratio(tp, tp + fn)
    f1 = 2 * precision * recall / (precision + recall) if precision and recall else None
    categorized = sum(r.categorized for r in done)
    per_category: dict[str, dict[str, int]] = {}
    for r in done:
        for label in r.expected:
            if label.category is None:
                continue
            row = per_category.setdefault(
                CATEGORY_LABEL[label.category], {"expected": 0, "found": 0}
            )
            row["expected"] += 1
            row["found"] += label.text not in r.fn
    latencies = [r.elapsed_s for r in results]
    tenth = max(1, len(latencies) // 10)
    return {
        "samples": len(results),
        "succeeded": len(done),
        "failed": len(results) - len(done),
        "failure_reasons": dict(Counter(r.reason or "-" for r in results if not r.ok)),
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "category_accuracy": _ratio(
            categorized - sum(len(r.wrong_category) for r in done), categorized
        ),
        "per_category": per_category,
        # 문장 단위로 정답과 검출이 완전히 같은 비율 (놓침·과검출·분류 오류 모두 없음)
        "exact_match": _ratio(
            sum(1 for r in done if not r.fn and not r.fp and not r.wrong_category),
            len(done),
        ),
        "latency_avg_s": sum(latencies) / len(latencies) if latencies else 0.0,
        "latency_p50_s": _percentile(latencies, 0.5),
        "latency_p95_s": _percentile(latencies, 0.95),
        "latency_max_s": max(latencies, default=0.0),
        # 처음과 마지막 10%의 평균 지연. 오래 돌릴수록 느려지는지 본다
        "latency_first10_avg_s": sum(latencies[:tenth]) / tenth if latencies else 0.0,
        "latency_last10_avg_s": sum(latencies[-tenth:]) / tenth if latencies else 0.0,
        "input_tokens": sum(r.input_tokens for r in results),
        "output_tokens": sum(r.output_tokens for r in results),
        "total_s": round(total_s, 1),
    }


def render(summary: dict[str, Any], results: list[SampleResult], dataset: Path) -> str:
    s = summary
    lines = [
        "# 개인정보 검출 평가",
        "",
        f"- 데이터: {dataset} ({s['samples']}문장)",
        f"- 총 시간: {s['total_s']}s",
        "",
        "## 정확도 (성공한 문장 기준)",
        "",
        "| 지표 | 값 |",
        "| --- | --- |",
        f"| 정밀도 (검출 중 정답) | {_pct(s['precision'])} ({s['tp']}/{s['tp'] + s['fp']}) |",
        f"| 재현율 (정답 중 검출) | {_pct(s['recall'])} ({s['tp']}/{s['tp'] + s['fn']}) |",
        f"| F1 | {_pct(s['f1'])} |",
        f"| 분류 정확도 | {_pct(s['category_accuracy'])} |",
        f"| 문장 완전 일치 | {_pct(s['exact_match'])} |",
        "",
        "| 분류 | 정답 수 | 찾음 | 재현율 |",
        "| --- | --- | --- | --- |",
    ]
    for name, row in s["per_category"].items():
        lines.append(
            f"| {name} | {row['expected']} | {row['found']} | "
            f"{_pct(_ratio(row['found'], row['expected']))} |"
        )
    reasons = ", ".join(f"{k} {v}" for k, v in s["failure_reasons"].items()) or "없음"
    lines += [
        "",
        "## 안정성",
        "",
        "| 지표 | 값 |",
        "| --- | --- |",
        f"| 성공 / 실패 | {s['succeeded']} / {s['failed']} |",
        f"| 실패 사유 | {reasons} |",
        f"| 지연 평균 / p50 / p95 / 최대 | {s['latency_avg_s']:.2f}s / "
        f"{s['latency_p50_s']:.2f}s / {s['latency_p95_s']:.2f}s / {s['latency_max_s']:.2f}s |",
        f"| 처음 10% → 마지막 10% 평균 지연 | {s['latency_first10_avg_s']:.2f}s → "
        f"{s['latency_last10_avg_s']:.2f}s |",
        f"| 토큰 (입력 / 출력) | {s['input_tokens']} / {s['output_tokens']} |",
        "",
    ]

    def examples(title: str, rows: list[str]) -> None:
        if rows:
            lines.extend([f"## {title} (최대 30개)", "", *rows[:30], ""])

    examples(
        "실패한 문장",
        [f"- `{r.id}`: {r.reason}" for r in results if not r.ok],
    )
    examples(
        "놓친 개인정보",
        [f"- `{r.id}`: {', '.join(r.fn)}" for r in results if r.ok and r.fn],
    )
    examples(
        "과검출",
        [f"- `{r.id}`: {', '.join(r.fp)}" for r in results if r.ok and r.fp],
    )
    examples(
        "분류 오류",
        [
            f"- `{r.id}`: {', '.join(r.wrong_category)}"
            for r in results
            if r.wrong_category
        ],
    )
    return "\n".join(lines) + "\n"


def run(
    samples: list[Sample], runner: Any, out_dir: Path, dataset: Path
) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    detector = PDDetector(runner)
    results: list[SampleResult] = []
    started = monotonic()
    with (out_dir / "results.jsonl").open("w", encoding="utf-8") as file:
        try:
            for index, sample in enumerate(samples, 1):
                result = evaluate(sample, runner, detector)
                results.append(result)
                file.write(result.model_dump_json() + "\n")
                file.flush()
                mark = (
                    "ERR "
                    if not result.ok
                    else ("OK  " if not (result.fn or result.fp) else "DIFF")
                )
                print(
                    f"[{index}/{len(samples)}] {mark} {sample.id} {result.elapsed_s:.1f}s",
                    flush=True,
                )
        except KeyboardInterrupt:
            print("중단: 지금까지의 결과로 요약합니다.", flush=True)
    summary = summarize(results, monotonic() - started)
    (out_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / "summary.md").write_text(
        render(summary, results, dataset), encoding="utf-8"
    )
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m qa.pd_eval")
    parser.add_argument("dataset", type=Path, help="정답이 달린 JSONL 파일")
    parser.add_argument("--limit", type=int, help="앞에서부터 N문장만")
    parser.add_argument(
        "--out", type=Path, help="결과 폴더 (기본: qa/results/pd-<시각>)"
    )
    args = parser.parse_args(argv)

    samples = load_samples(args.dataset)[: args.limit]
    out_dir = args.out or RESULTS_DIR / datetime.now().strftime("pd-%Y%m%d-%H%M%S")

    from app.runtime.local_llm_runner import LocalLLMRunner

    with LocalLLMRunner(prompt_manager=PromptManager()) as runner:
        summary = run(samples, runner, out_dir, args.dataset)
    print(
        f"\n정밀도 {_pct(summary['precision'])} · 재현율 {_pct(summary['recall'])} · "
        f"F1 {_pct(summary['f1'])} · 실패 {summary['failed']}/{summary['samples']}"
    )
    print(f"요약: {out_dir / 'summary.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
