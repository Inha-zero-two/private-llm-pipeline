# 4.1 PDDetector

**파일:** [pd_detector.py](../server/app/privacy/pd_detector.py)

## 구현 내역

- **`CATEGORY_LABEL` 상수 및 `PDItem` 모델 정의:** 개인정보 분류(`0: 식별정보`, `1: 민감정보`, `2: 속성정보`) 상수와 검출 항목 모델(`pd_id`, `text`, `category`, `allowed`, `allowed_by`) 구현
- **`detect(run_id, chat_id, message_id, question, *, found_items=None, ok=True) -> list[PDItem]` 구현:** 질문에서 검출한 개인정보 항목을 검증하고 기본 금지 상태(`allowed=False`, `allowed_by="none"`)로 묶어 반환 (`found_items`, `ok`로 검출 결과를 직접 전달할 수도 있음)

## 함수별 처리 흐름

| 함수 | 단계 | 처리 내용 | 실패 · 예외 처리 |
| --- | :---: | --- | --- |
| `detect()` | 1 | 입력 `question`이 비어 있는지 확인 | 비어 있거나 문자열이 아니면 `ValueError` |
| | 2 | 로컬 모델(`DETECT`) 호출 및 성공 여부(`ok`) 확인 | `ok=False`이거나 결과가 리스트가 아니면 `RuntimeError` |
| | 3 | 각 항목의 `text`, `category`(`0`~`2`)를 확인하고 `allowed=False`, `allowed_by="none"`을 붙여 `list[PDItem]` 반환 (빈 목록 `[]`은 정상 통과) | 항목 필드가 비어 있거나 분류 번호가 `0`~`2` 밖이면 `RuntimeError` |

## 설계서 대비 변경 사항

- **모듈 호출:** `LocalLLMRunner.request()` 호출을 연결했습니다. 검출 결과를 직접 전달하지 않으면 모델에 개인정보 검출을 요청합니다. `ChatManager`의 `set_pd_items()`와 `log_event()` 호출은 주석 상태입니다.
- **반환값 수용 방식:** 파서가 반환하는 `DetectedItem` 객체와 `dict` 형식을 모두 `PDItem`으로 변환하도록 처리
