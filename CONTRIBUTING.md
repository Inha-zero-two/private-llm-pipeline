# GitHub 컨벤션

## 작업 흐름

- 이슈는 쓰지 않고, 모든 작업은 **PR**로 올립니다.
- PR 본문은 [PR 템플릿](.github/PULL_REQUEST_TEMPLATE.md)을 따릅니다. PR을 열면 자동으로 채워집니다.

## PR 라벨

PR마다 아래 라벨 중 하나를 붙입니다.

| 라벨 | 용도 |
| --- | --- |
| `feat` | 새로운 기능 추가 |
| `fix` | 버그 수정 |
| `docs` | 문서 추가 · 수정 |
| `chore` | 빌드, 설정, 패키지 등 기능과 무관한 작업 |

## PR 본문 구성

1. **개요 및 작업 배경** — 기획서 번호를 포함해 작성합니다. (ex. 3.1 Planner 작업)
2. **주요 개발 및 변경 사항** — 파일별 상세 내역보다 핵심 로직 · 구조적 변경 위주로 요약합니다.
3. **스크린샷 또는 화면 기록** — UI/UX 변경이 있으면 전/후 스크린샷이나 동작 영상(GIF)을 첨부합니다. (ex. 로컬 LLM 작동 영상)
4. **리뷰어 집중 요청 사항** — 특히 봐주었으면 하는 로직이나 고민한 부분(성능, 예외처리 등)을 적습니다.
5. **기타 참고 사항** — 마이그레이션 필요 여부, 환경 변수(`.env`) 추가 여부, 다른 팀원 작업에 영향을 줄 수 있는 리스크를 적습니다.

## Llama 어댑터 개발 규칙

[코드 컨벤션](https://github.com/Inha-zero-two/private-llm-pipeline/blob/de871642f8d8ab37701351fe2af1efe1c2513cb2/docs/convention-code.md)에 따라
함수·변수는 `snake_case`, 클래스는 `PascalCase`, 상수는 대문자와 `_`를 사용합니다.
함수 인자와 반환값에 타입 힌트를 붙이고 import 순서와 서식은 Ruff로 검사합니다.
변경되는 내부 상태는 `_last_native_diagnostic`처럼 소문자로 씁니다.

어댑터를 수정할 때 다음 호환성을 유지합니다.

- `from llama_wrapper import Llama`, 공개 클래스·예외 이름과 호출 규격을 유지합니다.
  `create_embedding(input=...)`와 `embed(input=...)`의 `input`도 원본 API 이름입니다.
  공인 HTTPS 전환에 따른 기본 서버 URL 변경은 의도한 변경입니다.
- `create_chat_completion`의 dict 응답과 스트리밍 청크는 추가 필드를 유지합니다.
  `create_completion`은 정해진 필드의 호환 dict로 재구성하며,
  `create_chat_completion_openai_v1`은 OpenAI SDK의 Pydantic 모델로 검증·변환합니다.
- 공개된 `CudaDevice`, `RoutingInfo`와 설치기 데이터 구조의 위치 인자 생성 및 frozen 속성을 유지합니다.
- 단일 파일 복사와 기존 예제 실행을 지원하도록 `src/localLLM_API_proxy/llama_wrapper.py` 경로를 유지합니다.
- 네이티브 전달과 확장 가능한 JSON 값의 `Any`는 의도한 사용입니다.

## Llama 어댑터 검증

저장소 루트에서 `cd src/localLLM_API_proxy`로 이동하고 가상환경의 Python으로 실행합니다.

```bash
python -m pip install -r requirements-dev.txt
python -m ruff check .
python -m ruff format --check .
python -m pytest -q
```

테스트는 호출 규격·dict 응답·스트리밍·GPU 선택·설치 계획과 TLS를 검사합니다.
실제 서버·인증키·GPU 모델 대신 테스트 대역과 임시 인증서를 사용합니다.
`tests/public_api.json`은 기존 호출 규격을 기준으로 하며 승인한 서버 URL 기본값 변경을 반영했습니다.
원본 llama-cpp-python의 모든 기능을 검증하는 것은 아닙니다.
