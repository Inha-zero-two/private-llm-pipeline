# DynamicPromptChooser 사용과 확장 가이드

**대상:** [dynamic_prompt_chooser.py](../server/app/data/dynamic_prompt_chooser.py)의 `DynamicPromptChooser`와 [dynamic_prompts.json](../server/app/prompts/dynamic_prompts.json).

**목적:** `PromptManager`가 보관하는 고정 시스템 프롬프트 뒤에 사용자 입력과 의미적으로 가장 유사한 후보 프롬프트(예: 도메인별 퓨샷 예시)를 CPU 임베딩 모델로 선택해 붙여 준다.

## 파일 형식 (`server/app/prompts/dynamic_prompts.json`)

- 최상위 키는 `version`과 `candidates` 두 개만 둔다.
- `candidates`는 `PromptIdx` 단계 이름을 키로 하는 사전이며, 각 항목은 유사도 비교용 기준 문장(`query`)과 실제 프롬프트에 붙일 내용(`prompt`, 문자열 또는 줄 단위 문자열 배열)으로 구성한다.
- `NEEDS` 고정 프롬프트에는 범용 예시 2개(외부 정보 필요 / 로컬 직답 가능 시 `{"needs": []}`)를 유지하고, 후보 풀은 PUPA 데이터셋 기반 도메인 유형과 일반 LLM 다빈도 질문 유형을 포괄하는 영어 퓨샷 예시(직답 가능 유형은 `{"needs": []}` 포함)로 구성한다. 다국어 임베딩으로 사용자 질문의 언어와 무관하게 유사한 예시를 선택해 뒤에 붙인다.

```json
{
  "version": "0.3",
  "candidates": {
    "NEEDS": [
      {
        "query": "corporate strategy business plan market expansion competition analysis",
        "prompt": [
          "Example:",
          "Input: ...",
          "Output: {\"needs\": [...]}"
        ]
      }
    ]
  }
}
```

## 주요 API

| 메서드 | 설명 |
| --- | --- |
| `select_similar(target, candidates, top_n) -> list[str]` | 타겟 문장과 후보 문자열 목록을 코사인 유사도로 비교해 상위 `top_n`개 반환 |
| `build_prompt(prompt_idx, target, top_n, *, run_id=None) -> tuple[str, str]` | 고정 시스템 프롬프트에 유사한 단계별 후보 `top_n`개를 결합한 `(프롬프트, 버전)` 반환. 등록되지 않은 단계는 고정 프롬프트 그대로 반환 |
| `warmup() -> bool` | 임베딩 모델 초기화 및 등록된 후보 `query` 벡터 사전 캐싱 |
| `close() -> None` | 모델 참조 및 벡터 캐시 정리 |

## 임베딩 모델과 설정 (`server/.env`)

- 기본 모델은 `EmbeddingGemma 2` 텍스트 전용 INT8 ONNX 모델인 `surus-ai/embeddinggemma-2-onnx` (CPU 실행, 768차원)이며, `fastembed` 내장 모델(`google/embeddinggemma-300m`, `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` 등)로도 설정으로 전환할 수 있다.
- `EMBEDDING_MODEL`: 사용할 임베딩 모델명 지정
- `DYNAMIC_PROMPT_ENABLED`: `false` / `0` / `no` / `off` 시 동적 프롬프트 비활성화 (기본 `true`)
- `DYNAMIC_PROMPT_TOP_N`: `NEEDS` 단계에서 선택할 후보 개수 (기본 `3`)

## 장애 폴백과 로그 기록

- 임베딩 로드나 추론에 실패하면 파이프라인을 중단하지 않고 해당 단계의 앞 `top_n`개 기본 후보로 폴백(`fallback=True`, 버전 접미사 `:fallback`)한다.
- `ChatManager`와 `run_id`가 주어지면 `run_events` 표에 `type="dynamic_prompt"`로 `prompt_idx`, `base_version`, `dynamic_version`, `top_n`, `selected_indices`, `scores`, `elapsed_ms`, `fallback`을 기록한다. (`SCREEN_EVENTS`에 포함되지 않으므로 화면 SSE로는 전송되지 않는다.)

## 다른 모듈로 확장하기

1. [dynamic_prompts.json](../server/app/prompts/dynamic_prompts.json)의 `candidates` 아래에 대상 단계 키(예: `"DETECT"`, `"JUDGE"`)와 `{query, prompt}` 목록을 추가한다.
2. 대상 모듈 생성자에 선택 인자 `dynamic_prompt_chooser: DynamicPromptChooser | None = None`를 추가하고, [services.py](../server/app/api/services.py) `Pipeline.startup()`에서 공유 `chooser` 인스턴스를 주입한다.
3. 대상 모듈의 모델 호출 직전에 `chooser.build_prompt(PromptIdx.<STAGE>, user_question, top_n, run_id=run_id)`를 호출해 받은 `(system_prompt, prompt_version)`을 `LocalLLMRunner.request()`에 전달한다.
