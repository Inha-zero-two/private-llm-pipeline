"""임베딩 유사도 기반으로 단계별 동적 후보 프롬프트를 선택해 결합하는 모듈."""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Callable, Sequence
from pathlib import Path
from threading import Lock
from time import monotonic
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from app.config import EMBEDDING_GEMMA2_REPO
from app.models.enums import PromptIdx

if TYPE_CHECKING:
    from app.data.chat_manager import ChatManager
    from app.data.prompt_manager import PromptManager

log = logging.getLogger(__name__)

DEFAULT_DYNAMIC_PROMPT_FILE = (
    Path(__file__).resolve().parent.parent / "prompts" / "dynamic_prompts.json"
)
# 임의 후보군 직접 호출 시 캐시가 무한정 커지지 않도록 상한을 둔다.
MAX_VECTOR_CACHE_SIZE = 1024
EMBEDDING_GEMMA2_QUERY_PREFIX = "task: search result | query: "
EMBEDDING_GEMMA2_DOC_PREFIX = "title: none | text: "
EMBEDDING_GEMMA2_MAX_LENGTH = 8192

Embedder = Callable[[Sequence[str]], Sequence[Sequence[float]]]


class PromptCandidate(BaseModel):
    """유사도 비교용 기준 문장(query)과 실제 프롬프트에 붙일 텍스트(prompt)."""

    query: str
    prompt: str


class DynamicPromptChooser:
    """CPU 임베딩 모델로 타겟 입력과 유사한 후보 프롬프트를 골라 결합한다."""

    def __init__(
        self,
        prompt_manager: PromptManager,
        *,
        dynamic_prompt_file: Path | str = DEFAULT_DYNAMIC_PROMPT_FILE,
        chat_manager: ChatManager | None = None,
        embedder: Embedder | None = None,
        preload: bool = False,
    ) -> None:
        """후보 프롬프트 파일을 읽고, 필요 시 임베딩 모델과 후보 벡터를 사전 로드한다."""
        self._prompt_manager = prompt_manager
        self._dynamic_prompt_file = Path(dynamic_prompt_file)
        self._chat_manager = chat_manager
        self._embedder = embedder
        self._lock = Lock()
        self._version: str = ""
        self._candidates: dict[str, list[PromptCandidate]] = {}
        self._loaded = False
        self._model: _OnnxEmbeddingGemma2 | None = None
        self._closed = False
        self._vector_cache: dict[str, tuple[float, ...]] = {}
        self._last_target_vector: tuple[str, tuple[float, ...]] | None = None
        self.load_candidates()
        if preload:
            self.warmup()

    def load_candidates(self) -> bool:
        """JSON 파일에서 버전과 단계별 동적 후보 목록을 읽어 검증·저장한다."""
        if self._loaded:
            return True

        try:
            raw_bytes = self._dynamic_prompt_file.read_bytes()
        except OSError:
            log.warning(
                "[DynamicPromptChooser] 동적 프롬프트 파일 없음: %s",
                self._dynamic_prompt_file,
            )
            return False

        try:
            data = json.loads(raw_bytes.decode("utf-8"))
            version = data["version"]
            raw_candidates = data["candidates"]
            if (
                not isinstance(version, str)
                or not version.strip()
                or not isinstance(raw_candidates, dict)
            ):
                raise ValueError

            parsed: dict[str, list[PromptCandidate]] = {}
            for stage_key, items in raw_candidates.items():
                if not isinstance(stage_key, str) or not isinstance(items, list):
                    raise ValueError
                stage = PromptIdx(stage_key).name
                stage_list: list[PromptCandidate] = []
                for item in items:
                    if not isinstance(item, dict):
                        raise ValueError
                    query = item["query"]
                    prompt = item["prompt"]
                    if isinstance(prompt, list) and all(
                        isinstance(line, str) for line in prompt
                    ):
                        prompt = "\n".join(prompt)
                    if (
                        not isinstance(query, str)
                        or not query.strip()
                        or not isinstance(prompt, str)
                        or not prompt.strip()
                    ):
                        raise ValueError
                    stage_list.append(
                        PromptCandidate(query=query.strip(), prompt=prompt)
                    )
                parsed[stage] = stage_list
        except (ValueError, KeyError, TypeError):
            log.warning(
                "[DynamicPromptChooser] 동적 프롬프트 형식 오류: %s",
                self._dynamic_prompt_file,
            )
            return False

        self._version = version.strip()
        self._candidates = parsed
        self._loaded = True
        return True

    def warmup(self) -> bool:
        """임베딩 모델을 초기화하고 등록된 후보 문장 벡터를 메모리에 미리 캐싱한다."""
        if not self._loaded and not self.load_candidates():
            return False
        unique_queries = list(
            dict.fromkeys(
                candidate.query
                for stage_items in self._candidates.values()
                for candidate in stage_items
            )
        )
        try:
            with self._lock:
                self._ensure_model_locked()
                self._embed_candidates_locked(unique_queries)
        except Exception as exc:
            log.warning("[DynamicPromptChooser] 임베딩 모델 사전 로드 실패: %s", exc)
            return False
        return True

    def select_similar(
        self,
        target: str,
        candidates: Sequence[str],
        top_n: int,
    ) -> list[str]:
        """타겟 문장과 후보 프롬프트군을 임베딩으로 비교해 유사한 상위 top_n개를 반환한다."""
        if not isinstance(target, str):
            raise TypeError("target은 문자열이어야 합니다")
        if isinstance(top_n, bool) or not isinstance(top_n, int):
            raise TypeError("top_n은 정수여야 합니다")
        with self._lock:
            if self._closed:
                raise RuntimeError("DynamicPromptChooser is closed")
        if top_n <= 0 or not target.strip() or not candidates:
            return []

        scored = self._score_candidates(target, candidates)
        return [candidate for _, candidate, _ in scored[:top_n]]

    def build_prompt(
        self,
        prompt_idx: PromptIdx | str,
        target: str,
        top_n: int,
        *,
        run_id: int | None = None,
    ) -> tuple[str, str]:
        """고정 시스템 프롬프트에 타겟과 유사한 단계별 후보 top_n개를 결합해 반환한다."""
        if not isinstance(target, str):
            raise TypeError("target은 문자열이어야 합니다")
        if isinstance(top_n, bool) or not isinstance(top_n, int):
            raise TypeError("top_n은 정수여야 합니다")
        with self._lock:
            if self._closed:
                raise RuntimeError("DynamicPromptChooser is closed")

        stage = PromptIdx(prompt_idx)
        base_prompt = self._prompt_manager.get_prompt(stage)
        base_version = self._prompt_manager.get_version()

        stage_candidates = self._candidates.get(stage.name, [])
        if not stage_candidates or top_n <= 0 or not target.strip():
            return base_prompt, base_version

        queries = [item.query for item in stage_candidates]
        started = monotonic()
        fallback = False
        selected_indices: list[int] = []
        scores: list[float] = []

        try:
            selected_queries = self.select_similar(target, queries, top_n)
            selected_indices = _match_selected_indices(queries, selected_queries)
            if not selected_indices:
                raise RuntimeError("유사 후보를 찾지 못했습니다")
            scores = self._lookup_scores(target, [queries[i] for i in selected_indices])
        except Exception as exc:
            log.warning(
                "[DynamicPromptChooser] 동적 선택 실패(%s), 기본 후보로 폴백: %s",
                stage.name,
                exc,
            )
            fallback = True
            selected_indices = list(range(min(top_n, len(stage_candidates))))
            scores = []

        selected_prompts = [stage_candidates[i].prompt for i in selected_indices]
        if not selected_prompts:
            return base_prompt, base_version

        composed_prompt = f"{base_prompt}\n\n" + "\n\n".join(selected_prompts)
        suffix = ":fallback" if fallback else ""
        composed_version = f"{base_version}+dyn:{self._version}{suffix}"
        elapsed_ms = round((monotonic() - started) * 1000, 2)

        self._log_selection(
            run_id=run_id,
            stage=stage,
            base_version=base_version,
            top_n=top_n,
            selected_indices=selected_indices,
            scores=scores,
            elapsed_ms=elapsed_ms,
            fallback=fallback,
        )
        return composed_prompt, composed_version

    def close(self) -> None:
        """보관 중인 임베딩 모델 참조와 벡터 캐시를 정리한다."""
        with self._lock:
            self._closed = True
            self._model = None
            self._vector_cache.clear()
            self._last_target_vector = None

    def _score_candidates(
        self, target: str, candidates: Sequence[str]
    ) -> list[tuple[int, str, float]]:
        """후보 문자열마다 코사인 유사도를 계산해 내림차순으로 정렬한다."""
        with self._lock:
            self._ensure_model_locked()
            target_vec = self._embed_texts_locked([target], is_query=True)[0]
            self._last_target_vector = (target, target_vec)
            cand_vecs = self._embed_candidates_locked(candidates)

        scored = [
            (index, candidate, _cosine_similarity(target_vec, cand_vec))
            for index, (candidate, cand_vec) in enumerate(zip(candidates, cand_vecs))
        ]
        scored.sort(key=lambda item: (-item[2], item[0]))
        return scored

    def _lookup_scores(self, target: str, queries: Sequence[str]) -> list[float]:
        """방금 select_similar에서 계산한 벡터 캐시를 재사용해 점수를 추출한다."""
        with self._lock:
            last = self._last_target_vector
            if last is None or last[0] != target:
                return []
            target_vec = last[1]
            scores: list[float] = []
            for query in queries:
                cand_vec = self._vector_cache.get(query)
                if cand_vec is None:
                    return []
                scores.append(round(_cosine_similarity(target_vec, cand_vec), 4))
            return scores

    def _ensure_model_locked(self) -> _OnnxEmbeddingGemma2 | None:
        """락 내부에서 호출. CPU ONNX EmbeddingGemma 2 모델 준비."""
        if self._closed:
            raise RuntimeError("DynamicPromptChooser is closed")
        if self._embedder is not None:
            return None
        if self._model is None:
            self._model = _OnnxEmbeddingGemma2()
        return self._model

    def _embed_candidates_locked(
        self, candidates: Sequence[str]
    ) -> list[tuple[float, ...]]:
        """캐시에 없는 후보 문장만 임베딩한 뒤 후보 순서대로 벡터를 반환한다."""
        missing = list(
            dict.fromkeys(text for text in candidates if text not in self._vector_cache)
        )
        if missing:
            computed = self._embed_texts_locked(missing, is_query=False)
            if len(self._vector_cache) < MAX_VECTOR_CACHE_SIZE:
                remaining = MAX_VECTOR_CACHE_SIZE - len(self._vector_cache)
                for text, vec in list(zip(missing, computed))[:remaining]:
                    self._vector_cache[text] = vec
            if all(text in self._vector_cache for text in candidates):
                return [self._vector_cache[text] for text in candidates]
            temp = dict(zip(missing, computed))
            return [
                self._vector_cache[text] if text in self._vector_cache else temp[text]
                for text in candidates
            ]
        return [self._vector_cache[text] for text in candidates]

    def _embed_texts_locked(
        self, texts: Sequence[str], *, is_query: bool
    ) -> list[tuple[float, ...]]:
        """락 내부에서 실제 임베딩 추론을 수행한다."""
        if not texts:
            return []
        if self._embedder is not None:
            raw_vectors: Any = self._embedder(list(texts))
        else:
            model = self._ensure_model_locked()
            assert model is not None
            raw_vectors = model.embed(texts, is_query=is_query)
        vectors = [tuple(float(x) for x in vec) for vec in raw_vectors]
        if len(vectors) != len(texts):
            raise RuntimeError("임베딩 결과 개수가 입력 개수와 일치하지 않습니다")
        return vectors

    def _log_selection(
        self,
        *,
        run_id: int | None,
        stage: PromptIdx,
        base_version: str,
        top_n: int,
        selected_indices: list[int],
        scores: list[float],
        elapsed_ms: float,
        fallback: bool,
    ) -> None:
        """선택된 동적 프롬프트 메타데이터를 ChatManager run_events에 기록한다."""
        if self._chat_manager is None or run_id is None:
            return
        from app.data.chat_manager import StorageError

        try:
            self._chat_manager.log_event(
                run_id,
                "dynamic_prompt",
                {
                    "prompt_idx": stage.name,
                    "base_version": base_version,
                    "dynamic_version": self._version,
                    "top_n": top_n,
                    "selected_indices": selected_indices,
                    "scores": scores,
                    "elapsed_ms": elapsed_ms,
                    "fallback": fallback,
                },
            )
        except StorageError as error:
            if error.code != "RUN_FINISHED":
                raise


class _OnnxEmbeddingGemma2:
    """EmbeddingGemma 2 텍스트 전용 INT8 ONNX CPU 실행 래퍼."""

    def __init__(self) -> None:
        import onnxruntime as ort
        from huggingface_hub import hf_hub_download
        from tokenizers import Tokenizer

        model_path = hf_hub_download(EMBEDDING_GEMMA2_REPO, "model_q8.onnx")
        tokenizer_path = hf_hub_download(EMBEDDING_GEMMA2_REPO, "tokenizer.json")

        tokenizer = Tokenizer.from_file(tokenizer_path)
        tokenizer.enable_padding()
        tokenizer.enable_truncation(max_length=EMBEDDING_GEMMA2_MAX_LENGTH)

        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        options.inter_op_num_threads = 1
        session = ort.InferenceSession(
            model_path, sess_options=options, providers=["CPUExecutionProvider"]
        )
        self._tokenizer = tokenizer
        self._session = session

    def embed(self, texts: Sequence[str], *, is_query: bool) -> list[tuple[float, ...]]:
        import numpy as np

        prefix = (
            EMBEDDING_GEMMA2_QUERY_PREFIX if is_query else EMBEDDING_GEMMA2_DOC_PREFIX
        )
        encodings = self._tokenizer.encode_batch([prefix + text for text in texts])
        input_ids = np.array([enc.ids for enc in encodings], dtype=np.int64)
        attention_mask = np.array(
            [enc.attention_mask for enc in encodings], dtype=np.int64
        )
        hidden = self._session.run(
            ["last_hidden_state"],
            {"input_ids": input_ids, "attention_mask": attention_mask},
        )[0].astype(np.float32)
        mask_expanded = attention_mask[:, :, np.newaxis].astype(np.float32)
        token_counts = np.clip(mask_expanded.sum(axis=1), 1e-9, None)
        pooled = (hidden * mask_expanded).sum(axis=1) / token_counts
        norms = np.clip(np.linalg.norm(pooled, axis=1, keepdims=True), 1e-12, None)
        normalized = pooled / norms
        return [tuple(float(v) for v in row) for row in normalized]


def _match_selected_indices(
    queries: Sequence[str], selected: Sequence[str]
) -> list[int]:
    """select_similar가 반환한 문자열 목록을 원본 후보 인덱스로 순서대로 매핑한다."""
    used: set[int] = set()
    indices: list[int] = []
    for text in selected:
        for idx, query in enumerate(queries):
            if idx not in used and query == text:
                used.add(idx)
                indices.append(idx)
                break
    return indices


def _cosine_similarity(vec_a: Sequence[float], vec_b: Sequence[float]) -> float:
    """두 벡터의 코사인 유사도를 계산한다. 길이가 다르거나 영벡터이면 0.0을 반환한다."""
    if not vec_a or len(vec_a) != len(vec_b):
        return 0.0
    dot = sum(a * b for a, b in zip(vec_a, vec_b))
    norm_a = math.sqrt(sum(a * a for a in vec_a))
    norm_b = math.sqrt(sum(b * b for b in vec_b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)
