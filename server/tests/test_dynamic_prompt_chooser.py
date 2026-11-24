"""DynamicPromptChooser 단위·연동 및 실제 프롬프트 파일 스키마 테스트."""

from __future__ import annotations

import json
import math
import sys
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import numpy as np
import pytest

from app import config
from app.data.chat_manager import StorageError
from app.data.dynamic_prompt_chooser import DynamicPromptChooser
from app.data.prompt_manager import PromptManager
from app.models.enums import PromptIdx
from app.pipeline.planner import Planner


def _write_json(path: Path, data: Any) -> Path:
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def _make_embedder(
    mapping: dict[str, list[float]], calls: list[list[str]] | None = None
) -> Any:
    def embed(texts: Sequence[str]) -> list[list[float]]:
        batch = list(texts)
        if calls is not None:
            calls.append(batch)
        return [mapping.get(text, [0.0, 0.0, 1.0]) for text in batch]

    return embed


def test_select_similar_ranking_and_edge_cases(tmp_path: Path) -> None:
    dyn_file = _write_json(
        tmp_path / "dyn.json",
        {"version": "0.1", "candidates": {}},
    )
    embedder = _make_embedder(
        {
            "타겟": [1.0, 0.0, 0.0],
            "후보A": [0.2, 0.9, 0.0],
            "후보B": [0.95, 0.1, 0.0],
            "후보C": [0.7, 0.5, 0.0],
            "영벡터": [0.0, 0.0, 0.0],
        }
    )
    chooser = DynamicPromptChooser(
        PromptManager(), dynamic_prompt_file=dyn_file, embedder=embedder
    )

    assert chooser.select_similar("타겟", ["후보A", "후보B", "후보C"], 2) == [
        "후보B",
        "후보C",
    ]
    assert chooser.select_similar("타겟", ["후보A", "후보B"], 5) == ["후보B", "후보A"]
    assert chooser.select_similar("타겟", ["후보A", "후보B"], 0) == []
    assert chooser.select_similar("타겟", ["후보A", "후보B"], -1) == []
    assert chooser.select_similar("타겟", [], 2) == []
    assert chooser.select_similar("   ", ["후보A"], 2) == []
    assert chooser.select_similar("타겟", ["영벡터", "후보B"], 2) == [
        "후보B",
        "영벡터",
    ]
    assert chooser.select_similar("타겟", ["후보A", "후보B", "후보B"], 2) == [
        "후보B",
        "후보B",
    ]

    with pytest.raises(TypeError):
        chooser.select_similar(123, ["후보A"], 1)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        chooser.select_similar("타겟", ["후보A"], True)


def test_build_prompt_calls_select_similar_and_passes_through_unregistered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dyn_file = _write_json(
        tmp_path / "dyn.json",
        {
            "version": "0.2",
            "candidates": {
                "NEEDS": [
                    {"query": "식단", "prompt": "예시_식단"},
                    {"query": "취업", "prompt": "예시_취업_1"},
                    {"query": "취업", "prompt": "예시_취업_2"},
                ]
            },
        },
    )
    prompts = PromptManager()
    calls: list[list[str]] = []
    embedder = _make_embedder(
        {
            "이직 질문": [1.0, 0.0],
            "취업": [0.9, 0.1],
            "식단": [0.1, 0.9],
        },
        calls,
    )
    chooser = DynamicPromptChooser(
        prompts, dynamic_prompt_file=dyn_file, embedder=embedder
    )

    observed_calls: list[tuple[str, list[str], int]] = []
    original_select = chooser.select_similar

    def spy_select(target: str, candidates: Sequence[str], top_n: int) -> list[str]:
        observed_calls.append((target, list(candidates), top_n))
        return original_select(target, candidates, top_n)

    monkeypatch.setattr(chooser, "select_similar", spy_select)

    composed, version = chooser.build_prompt(PromptIdx.NEEDS, "이직 질문", 2)
    base_needs = prompts.get_prompt(PromptIdx.NEEDS)
    assert observed_calls == [("이직 질문", ["식단", "취업", "취업"], 2)]
    assert composed == f"{base_needs}\n\n예시_취업_1\n\n예시_취업_2"
    assert version == f"{prompts.get_version()}+dyn:0.2"

    # 등록되지 않은 단계(JUDGE 등)와 top_n <= 0은 고정 프롬프트 그대로 반환
    judge_prompt, judge_version = chooser.build_prompt(PromptIdx.JUDGE, "이직 질문", 2)
    assert judge_prompt == prompts.get_prompt(PromptIdx.JUDGE)
    assert judge_version == prompts.get_version()

    zero_prompt, zero_version = chooser.build_prompt(PromptIdx.NEEDS, "이직 질문", 0)
    assert zero_prompt == base_needs
    assert zero_version == prompts.get_version()


def test_vector_cache_avoids_reembedding_candidates(tmp_path: Path) -> None:
    dyn_file = _write_json(
        tmp_path / "dyn.json",
        {
            "version": "0.1",
            "candidates": {
                "NEEDS": [
                    {"query": "후보1", "prompt": "P1"},
                    {"query": "후보2", "prompt": "P2"},
                ]
            },
        },
    )
    calls: list[list[str]] = []
    embedder = _make_embedder(
        {
            "후보1": [1.0, 0.0],
            "후보2": [0.0, 1.0],
            "질문1": [0.9, 0.1],
            "질문2": [0.1, 0.9],
        },
        calls,
    )
    chooser = DynamicPromptChooser(
        PromptManager(), dynamic_prompt_file=dyn_file, embedder=embedder, preload=True
    )
    assert calls == [["후보1", "후보2"]]

    chooser.build_prompt(PromptIdx.NEEDS, "질문1", 1)
    chooser.build_prompt(PromptIdx.NEEDS, "질문2", 1)
    assert calls == [["후보1", "후보2"], ["질문1"], ["질문2"]]


def test_build_prompt_fallback_and_logging(tmp_path: Path) -> None:
    dyn_file = _write_json(
        tmp_path / "dyn.json",
        {
            "version": "0.3",
            "candidates": {
                "NEEDS": [
                    {"query": "Q1", "prompt": "기본1"},
                    {"query": "Q2", "prompt": "기본2"},
                    {"query": "Q3", "prompt": "기본3"},
                ]
            },
        },
    )
    prompts = PromptManager()
    chat = Mock()

    def failing_embedder(_: Sequence[str]) -> list[list[float]]:
        raise RuntimeError("임베딩 실패")

    chooser = DynamicPromptChooser(
        prompts,
        dynamic_prompt_file=dyn_file,
        chat_manager=chat,
        embedder=failing_embedder,
    )
    prompt, version = chooser.build_prompt(PromptIdx.NEEDS, "질문", 2, run_id=42)
    base_needs = prompts.get_prompt(PromptIdx.NEEDS)
    assert prompt == f"{base_needs}\n\n기본1\n\n기본2"
    assert version == f"{prompts.get_version()}+dyn:0.3:fallback"

    chat.log_event.assert_called_once()
    run_id, event_type, payload = chat.log_event.call_args.args
    assert (run_id, event_type) == (42, "dynamic_prompt")
    assert payload["prompt_idx"] == "NEEDS"
    assert payload["base_version"] == prompts.get_version()
    assert payload["dynamic_version"] == "0.3"
    assert payload["top_n"] == 2
    assert payload["selected_indices"] == [0, 1]
    assert payload["scores"] == []
    assert payload["fallback"] is True
    assert isinstance(payload["elapsed_ms"], float)


def test_logging_ignores_run_finished_and_propagates_other_storage_errors(
    tmp_path: Path,
) -> None:
    dyn_file = _write_json(
        tmp_path / "dyn.json",
        {
            "version": "0.1",
            "candidates": {"NEEDS": [{"query": "Q1", "prompt": "P1"}]},
        },
    )
    prompts = PromptManager()
    chat = Mock()
    chat.log_event.side_effect = StorageError("RUN_FINISHED", "종료됨")
    chooser = DynamicPromptChooser(
        prompts,
        dynamic_prompt_file=dyn_file,
        chat_manager=chat,
        embedder=_make_embedder({"Q1": [1.0, 0.0], "질문": [1.0, 0.0]}),
    )
    # RUN_FINISHED 오류는 무시하고 정상 반환
    prompt, _ = chooser.build_prompt(PromptIdx.NEEDS, "질문", 1, run_id=1)
    assert "P1" in prompt

    chat.log_event.side_effect = StorageError("NOT_FOUND", "실행 없음")
    with pytest.raises(StorageError):
        chooser.build_prompt(PromptIdx.NEEDS, "질문", 1, run_id=999)


def test_planner_list_needs_with_and_without_chooser(tmp_path: Path) -> None:
    dyn_file = _write_json(
        tmp_path / "dyn.json",
        {
            "version": "0.1",
            "candidates": {
                "NEEDS": [
                    {"query": "건강", "prompt": "건강_예시"},
                    {"query": "여행", "prompt": "여행_예시"},
                ]
            },
        },
    )
    prompts = PromptManager()
    chooser = DynamicPromptChooser(
        prompts,
        dynamic_prompt_file=dyn_file,
        embedder=_make_embedder(
            {"건강": [1.0, 0.0], "여행": [0.0, 1.0], "당뇨 식단": [1.0, 0.0]}
        ),
    )

    runner_with = Mock()
    runner_with.request.return_value = (["식단 원칙"], True, 10, 5, "stop")
    planner_with = Planner(runner_with, dynamic_prompt_chooser=chooser, needs_top_n=1)
    assert planner_with.list_needs("당뇨 식단", 7, 3) == ["식단 원칙"]
    assert runner_with.request.call_args.args == (7, "당뇨 식단", PromptIdx.NEEDS, 3)
    assert "건강_예시" in runner_with.request.call_args.kwargs["system_prompt"]
    assert (
        runner_with.request.call_args.kwargs["prompt_version"]
        == f"{prompts.get_version()}+dyn:0.1"
    )

    # 선택기 미주입 시 기존 시그니처·호출 형태 유지
    runner_without = Mock()
    runner_without.request.return_value = (["일반 항목"], True, 10, 5, "stop")
    planner_without = Planner(runner_without)
    assert planner_without.list_needs("질문", 8, 4) == ["일반 항목"]
    runner_without.request.assert_called_once_with(8, "질문", PromptIdx.NEEDS, 4)


def test_real_prompt_files_schema_and_needs_split() -> None:
    prompts = PromptManager()
    assert prompts.get_version() == "0.8"
    needs_prompt = prompts.get_prompt(PromptIdx.NEEDS)
    assert "Example 1:" in needs_prompt
    assert "Example 2:" in needs_prompt
    assert '{"needs": []}' in needs_prompt
    assert "in the same language as the user's question" in needs_prompt

    chooser = DynamicPromptChooser(prompts)
    assert chooser.load_candidates() is True
    assert chooser._version == "0.3"
    needs_candidates = chooser._candidates[PromptIdx.NEEDS.name]
    assert len(needs_candidates) == 29
    direct_count = 0
    for candidate in needs_candidates:
        lines = candidate.prompt.splitlines()
        assert len(lines) == 3
        assert lines[0] == "Example:"
        assert lines[1].startswith("Input: ")
        assert lines[2].startswith("Output: ")
        payload = json.loads(lines[2].removeprefix("Output: "))
        needs = payload["needs"]
        assert isinstance(needs, list)
        assert len(needs) == 0 or 2 <= len(needs) <= 4
        assert all(isinstance(topic, str) and topic.strip() for topic in needs)
        if not needs:
            direct_count += 1
    assert direct_count == 4


def test_close_prevents_subsequent_calls(tmp_path: Path) -> None:
    dyn_file = _write_json(
        tmp_path / "dyn.json",
        {"version": "0.1", "candidates": {"NEEDS": [{"query": "Q", "prompt": "P"}]}},
    )
    chooser = DynamicPromptChooser(
        PromptManager(),
        dynamic_prompt_file=dyn_file,
        embedder=_make_embedder({"Q": [1.0], "T": [1.0]}),
        preload=True,
    )
    chooser.close()
    with pytest.raises(RuntimeError, match="closed"):
        chooser.select_similar("T", ["Q"], 1)
    with pytest.raises(RuntimeError, match="closed"):
        chooser.build_prompt(PromptIdx.NEEDS, "T", 1)


def test_onnx_embeddinggemma2_default_and_fastembed_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dyn_file = _write_json(
        tmp_path / "dyn.json",
        {"version": "0.1", "candidates": {"NEEDS": [{"query": "Q", "prompt": "P"}]}},
    )

    downloads: list[tuple[str, str]] = []
    encode_batches: list[list[str]] = []
    session_configs: list[tuple[str, int, int, list[str]]] = []

    class FakeEncoding:
        def __init__(self, ids: list[int], attention_mask: list[int]) -> None:
            self.ids = ids
            self.attention_mask = attention_mask

    class FakeTokenizer:
        def __init__(self) -> None:
            self.padded = False
            self.max_len: int | None = None

        @classmethod
        def from_file(cls, _: str) -> FakeTokenizer:
            return cls()

        def enable_padding(self) -> None:
            self.padded = True

        def enable_truncation(self, *, max_length: int) -> None:
            self.max_len = max_length

        def encode_batch(self, texts: list[str]) -> list[FakeEncoding]:
            encode_batches.append(texts)
            # 첫 번째 토큰만 유효(mask=1), 두 번째 토큰은 패딩(mask=0)
            return [FakeEncoding([1, 0], [1, 0]) for _ in texts]

    class FakeSessionOptions:
        def __init__(self) -> None:
            self.intra_op_num_threads = 0
            self.inter_op_num_threads = 0

    class FakeInferenceSession:
        def __init__(
            self,
            model_path: str,
            *,
            sess_options: FakeSessionOptions,
            providers: list[str],
        ) -> None:
            session_configs.append(
                (
                    model_path,
                    sess_options.intra_op_num_threads,
                    sess_options.inter_op_num_threads,
                    providers,
                )
            )

        def run(self, outputs: list[str], feed: dict[str, Any]) -> list[np.ndarray]:
            assert outputs == ["last_hidden_state"]
            batch = feed["input_ids"].shape[0]
            # 유효 토큰(0번)=[3.0, 4.0], 패딩 토큰(1번)=[99.0, 99.0] -> 정규화 시 [0.6, 0.8]
            hidden = np.zeros((batch, 2, 2), dtype=np.float32)
            hidden[:, 0, :] = [3.0, 4.0]
            hidden[:, 1, :] = [99.0, 99.0]
            return [hidden]

    def fake_download(repo_id: str, filename: str) -> str:
        downloads.append((repo_id, filename))
        return f"/fake/{repo_id}/{filename}"

    monkeypatch.setitem(
        sys.modules,
        "huggingface_hub",
        SimpleNamespace(hf_hub_download=fake_download),
    )
    monkeypatch.setitem(
        sys.modules, "tokenizers", SimpleNamespace(Tokenizer=FakeTokenizer)
    )
    monkeypatch.setitem(
        sys.modules,
        "onnxruntime",
        SimpleNamespace(
            SessionOptions=FakeSessionOptions,
            InferenceSession=FakeInferenceSession,
        ),
    )

    prompts = PromptManager()
    default_chooser = DynamicPromptChooser(prompts, dynamic_prompt_file=dyn_file)
    prompt, _ = default_chooser.build_prompt(PromptIdx.NEEDS, "질문", 1)
    assert "P" in prompt
    assert downloads == [
        (config.DEFAULT_EMBEDDING_MODEL, "model_q8.onnx"),
        (config.DEFAULT_EMBEDDING_MODEL, "tokenizer.json"),
    ]
    assert session_configs == [
        (
            f"/fake/{config.DEFAULT_EMBEDDING_MODEL}/model_q8.onnx",
            2,
            1,
            ["CPUExecutionProvider"],
        )
    ]
    assert encode_batches == [
        ["task: search result | query: 질문"],
        ["title: none | text: Q"],
    ]
    cached_vec = default_chooser._vector_cache["Q"]
    assert math.isclose(cached_vec[0], 0.6, rel_tol=1e-5)
    assert math.isclose(cached_vec[1], 0.8, rel_tol=1e-5)

    created: list[tuple[str, dict[str, list[str]]]] = []

    class FakeTextEmbedding:
        def __init__(self, model_name: str) -> None:
            self.calls: dict[str, list[str]] = {}
            created.append((model_name, self.calls))

        def embed(self, docs: Sequence[str]) -> list[list[float]]:
            self.calls.setdefault("embed", []).extend(docs)
            return [[1.0, 0.0] for _ in docs]

        def query_embed(self, docs: Sequence[str]) -> list[list[float]]:
            self.calls.setdefault("query_embed", []).extend(docs)
            return [[1.0, 0.0] for _ in docs]

        def passage_embed(self, docs: Sequence[str]) -> list[list[float]]:
            self.calls.setdefault("passage_embed", []).extend(docs)
            return [[1.0, 0.0] for _ in docs]

    monkeypatch.setitem(
        sys.modules, "fastembed", SimpleNamespace(TextEmbedding=FakeTextEmbedding)
    )

    minilm_chooser = DynamicPromptChooser(
        prompts,
        dynamic_prompt_file=dyn_file,
        model_name="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
    )
    minilm_chooser.build_prompt(PromptIdx.NEEDS, "질문", 1)
    assert (
        created[0][0] == "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    )
    assert "embed" in created[0][1]
    assert "query_embed" not in created[0][1]

    e5_chooser = DynamicPromptChooser(
        prompts,
        dynamic_prompt_file=dyn_file,
        model_name="intfloat/multilingual-e5-large",
    )
    e5_chooser.build_prompt(PromptIdx.NEEDS, "질문", 1)
    assert created[1][0] == "intfloat/multilingual-e5-large"
    assert created[1][1]["query_embed"] == ["질문"]
    assert created[1][1]["passage_embed"] == ["Q"]


def test_config_dynamic_prompt_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EMBEDDING_MODEL", raising=False)
    monkeypatch.delenv("DYNAMIC_PROMPT_ENABLED", raising=False)
    monkeypatch.delenv("DYNAMIC_PROMPT_TOP_N", raising=False)
    assert config.embedding_model() == config.DEFAULT_EMBEDDING_MODEL
    assert config.dynamic_prompt_enabled() is True
    assert config.dynamic_prompt_top_n() == 3

    monkeypatch.setenv("DYNAMIC_PROMPT_ENABLED", "false")
    assert config.dynamic_prompt_enabled() is False
    monkeypatch.setenv("DYNAMIC_PROMPT_ENABLED", "1")
    assert config.dynamic_prompt_enabled() is True
    monkeypatch.setenv("DYNAMIC_PROMPT_TOP_N", "5")
    assert config.dynamic_prompt_top_n() == 5

    monkeypatch.setenv("DYNAMIC_PROMPT_TOP_N", "0")
    with pytest.raises(ValueError):
        config.dynamic_prompt_top_n()
    monkeypatch.setenv("DYNAMIC_PROMPT_TOP_N", "abc")
    with pytest.raises(ValueError):
        config.dynamic_prompt_top_n()
