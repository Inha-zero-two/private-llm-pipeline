# web — 채팅 화면

React + Vite (TypeScript). 상세 설계서 9장의 화면 세 개(메인 채팅, 추가 개인정보 공개 요청, 공개 허용 설정)를 만든다.

## 실행

화면은 앱 서버(`server/app/api`, 명세서 `docs/screen-api-spec.md`)에 연결된다. 서버를 먼저 켠다.

```bash
# 1) 앱 서버 (저장소 루트에서)
cd server
# 실제 파이프라인: 개발된 모듈(검출·판단·메모·로컬 LLM)은 실제, 없는 모듈은 대역
#   원격 모델 서버 키를 src/localLLM_API_proxy/keys/llama_api_key.key에 둔다
CUDA_VISIBLE_DEVICES=-1 PYTHONPATH=.. ../src/localLLM_API_proxy/.venv/Scripts/python -m uvicorn app.api.app:app --port 8000
# 모델 없이 흐름만 볼 때
PIPELINE=fake PYTHONPATH=.. ../src/localLLM_API_proxy/.venv/Scripts/python -m uvicorn app.api.app:app --port 8000

# 2) 화면 (다른 터미널)
cd web
npm install
npm run dev        # http://localhost:5173, /api 요청은 127.0.0.1:8000으로 프록시
```

| 명령               | 내용                                      |
| ------------------ | ----------------------------------------- |
| `npm run dev`      | 개발 서버 (앱 서버 연결)                  |
| `npm run dev:demo` | 개발 서버 (서버 없이 브라우저 안 데모)    |
| `npm run build`    | 타입 검사 + 빌드                          |
| `npm run lint`     | oxlint                                    |
| `npm run format`   | Prettier로 정리 (`format:check`는 검사만) |

## 데모 동작

`npm run dev:demo`로 켜면 서버 없이 브라우저 안 데모(`src/demo`)가 설계서 흐름(①~⑧)대로 진행 단계와 답변을 채운다. 데이터는 메모리에만 있어 새로고침하면 사라진다. 서버의 `PIPELINE=fake`도 같은 시나리오를 쓴다.

### 데모 시나리오

질문에 아래 낱말을 넣으면 해당 상황을 볼 수 있다.

| 질문에 넣을 것                                                                               | 상황                                     |
| -------------------------------------------------------------------------------------------- | ---------------------------------------- |
| 나이(`27살`), 전화번호, 이름(`제 이름은 홍길동`), 회사 · 학교 이름(`삼성전자`, `인하대학교`) | 추가 공개 동의 요청                      |
| `동의`                                                                                       | 위 항목이 없어도 예시 항목으로 동의 요청 |
| `오류`                                                                                       | 실행 오류                                |
| `오프라인`                                                                                   | 외부 모델 연결 불가                      |
| `상한`                                                                                       | 질문 상한 도달                           |

- **공개 동의 요청**은 입력창 위에 상자로 뜬다. 자연어로 답하면(예: "나이는 공개하고 직장은 공개하지 마") 항목별로 공개 여부를 정한다. 언급하지 않은 항목은 공개하지 않는다. 하나도 공개하지 않으면 거절로 본다.
- **공개 허용 설정**은 앱 전체에 하나다. 사이드바 왼쪽 아래 버튼으로 연다. 허용한 종류(예: "나이는 공개해도 돼")는 동의를 묻지 않는다.

## 구조

```
src/
  types.ts          화면에서 쓰는 자료형
  backend/          화면이 쓰는 백엔드 약속(types) · 앱 서버 연결(remote) · 데모 연결(demo)
  demo/             브라우저 안 데모 실행 · 시나리오 · 설정문 해석
  components/       사이드바, 대화, 입력창, 동의 창, 설정 창 등
  App.tsx           채팅 목록 · 실행 상태를 들고 화면을 조립한다
```
