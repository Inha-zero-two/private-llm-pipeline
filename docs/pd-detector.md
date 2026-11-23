# 4.1 PDDetector

**파일:** [pd_detector.py](../server/app/privacy/pd_detector.py)

## 구현 내역

- **분류:** 0 식별정보, 1 민감정보, 2 속성정보, 3 기타. 기타는 실행의 추가 보호 규칙으로 검출한 질문 속 원문이다. 같은 원문이 기존 분류와 겹치면 기존 분류를 우선한다.
- **`detect(run_id, chat_id, message_id, question, setting=None, *, found_items=None, ok=True) -> list[PDItem]`:** `Main`이 실행 시작 시 고정한 설정을 전달한다. 금지 규칙이 있으면 검출 대상의 영문 설명을 DETECT 프롬프트에 붙이고, 이전 형식의 규칙에는 저장된 원문 설정을 함께 쓴다. 검출 결과는 기본 금지 상태로 반환한다.

## 함수별 처리 흐름

| 함수 | 단계 | 처리 내용 | 실패 · 예외 처리 |
| --- | :---: | --- | --- |
| `detect()` | 1 | 입력 `question`이 비어 있는지 확인 | 비어 있거나 문자열이 아니면 `ValueError` |
| | 2 | 로컬 모델(`DETECT`) 호출 및 성공 여부(`ok`) 확인 | `ok=False`이거나 결과가 리스트가 아니면 `RuntimeError` |
| | 3 | 각 항목의 원문과 분류를 확인하고 기본 금지 상태로 반환 (빈 목록도 허용) | 유효하지 않은 항목, 금지 규칙이 없거나 질문에 없는 분류 3 값은 `RuntimeError` |

## 설계서 대비 변경 사항

- **모듈 호출:** 검출 결과를 직접 전달하지 않으면 `LocalLLMRunner.request()`로 검출한다. `Main`이 반환된 항목을 `ChatManager.set_pd_items()`로 저장한다.
- **반환값 수용 방식:** 파서가 반환하는 `DetectedItem` 객체와 `dict` 형식을 모두 `PDItem`으로 변환하도록 처리
