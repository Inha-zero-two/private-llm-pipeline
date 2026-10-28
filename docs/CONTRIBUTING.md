# 기여 가이드

이 레포에서 작업하기 전에 아래 문서를 순서대로 읽어 주세요.

| 순서 | 문서 | 내용 |
| --- | --- | --- |
| 1 | [Git · PR 컨벤션](convention-git.md) | 브랜치 전략, 브랜치 이름, 커밋 메시지, PR 라벨 · 본문 |
| 2 | [코드 컨벤션](convention-code.md) | 이름 규칙, 폴더 구조, 데이터 형식 · 타입 힌트 |
| 3 | [설계서 ↔ 코드 이름 대응표](naming-map.md) | 상세 설계서의 모듈 · 함수 · 변수 이름을 코드에서 쓰는 이름 |

## 컨벤션을 바꾸고 싶을 때

컨벤션 문서도 코드와 똑같이 브랜치를 만들어 PR로 고칩니다. 라벨은 `chore`를 붙입니다.

## Llama 어댑터 개발 규칙

[코드 컨벤션](convention-code.md)에 따라
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
