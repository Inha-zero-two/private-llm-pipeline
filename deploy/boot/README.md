# 부팅 시 GPU 확인과 LLM 자동 시작

2026-10-28 실제 서버에 적용했습니다. 기존 `gemma4-e4b.service`를 재사용하며
`caddy.service`와 함께 부팅 자동 시작이 켜져 있습니다.
[현재 운영 설정과 검증 결과](DEPLOYMENT.md)를 먼저 확인하세요.
아래 `satisshin-llm.service`는 다른 서버를 위한 예제이며 현재 서버에 중복 설치하지 않습니다.

1. 부팅하면 Caddy가 `https://satisshin.top:4000`을 받습니다.
2. 별도 LLM 서비스가 **실제로 사용할 llama-server 바이너리**의 `--list-devices`로 CUDA GPU를 확인합니다.
3. GPU가 보이면 기존 모델 인자와 기존 프로젝트의 API 키 파일로 `127.0.0.1:4001`에서 시작합니다.
4. GPU가 없거나 감지·모델 실행이 실패하면 외부 요청은 HTTP **503**입니다. CPU 대체는 하지 않습니다.
5. LLM 서비스는 30초 뒤 재시도합니다. GPU와 모델이 준비되면 Caddy의 상태 확인을 통해 자동 복구됩니다.

장애 응답은 다음 JSON입니다. `/health`도 실제 백엔드 상태를 반영하며 고정된 성공 응답을 만들지 않습니다.

```json
{"error":{"code":"llm_service_unavailable","message":"LLM service is unavailable."}}
```

Caddy는 LLM과 독립적으로 유지됩니다. GPU가 준비되기 전에도 TLS 인증서가 유효한 HTTPS 응답을
제공할 수 있습니다. Caddy 자체나 서버 전원이 꺼져 있으면 503 응답도 제공할 수 없습니다.
GPU가 실행 중에 고장 난 경우에는 llama-server가 실패하거나 `/health`가 비정상으로 바뀌었을 때
장애가 감지됩니다. 이미 HTTP 헤더를 보낸 스트리밍 도중 장애는 새 503으로 바꿀 수 없습니다.

## 기존 프로젝트와 연결할 정보

다른 대화에서 작성한 `컴종설/LLM_작업_정리.md`를 확인했습니다. 그 문서의 성공 검증은
이전 서버 `shin@192.168.0.2`와 `/home/shin/gemma4-server` 기준입니다.
현재 서버에서는 바탕화면의 `Pi5_Gemma4_E4B_6슬롯_실행환경_인계.md`를 추가 확인했습니다.
실제 프로젝트는 `/home/shin-pi/gemma-setup`이고 RTX 3060 12GB, 6슬롯 구성입니다.

`boot.json.example`의 경로와 `model_arguments`는 **미완성 예시**이며 그대로 실행되지 않습니다.
새 서버의 서빙 테스트가 끝난 뒤 다음을 확정합니다.

- 실제 `llama-server` 절대 경로와 작업 폴더
- 검증한 모델·컨텍스트·슬롯·캐시·GPU 오프로딩 인자 전체
- 기존 프로젝트에 있는 서버용 API 키 파일 절대 경로
- 기존 실행 환경에서 필요한 GPU 표시 범위와 라이브러리 경로

예제의 GPU 레이어 수는 실제 튜닝 값이 아닙니다. 성공한 설정을 그대로 옮기고 임의로 바꾸지 않습니다.
`api_key_file`은 기존 키 목록 파일을 직접 읽습니다. launcher는 키를 생성·복사·회전하지 않으며
파일 내용은 출력하지 않습니다. 클라이언트에 전달된 키들이 서버 목록에도 있어야 합니다.
이번 배포에서는 `컴종설/keys/`의 기존 키 4개를 값 변경 없이
`/home/shin-pi/gemma-setup/keys/project-api-keys.key`에 연결했습니다.

`model_arguments`에는 모델 관련 인자만 넣습니다. host/port/API 인증은 launcher가 관리하며
`--api-key`처럼 키 값을 명령줄에 넣는 방법은 거부합니다. TLS는 Caddy가 처리하므로
llama-server의 `--ssl-*` 옵션도 넣지 않습니다. API 키 파일이 없거나 빈 줄·주석뿐이면 시작하지 않습니다.

이 구성은 기존 CUDA 서빙 프로필을 기준으로 합니다. GPU 목록에 RPC·CPU만 있는 경우는 통과하지 않습니다.
GPU가 표시되어도 VRAM 부족·모델 로딩 실패가 없다는 보장은 없으며 그런 실패도 외부 503으로 처리합니다.
다른 GPU 백엔드로 변경할 때는 `gpu_device_pattern`과 실제 서버 빌드를 함께 확인해야 합니다.

## 파일 구성

| 파일 | 역할 |
| --- | --- |
| `start_llm.py` | 설정·기존 키 확인, GPU 확인 후 기존 바이너리로 프로세스 교체 |
| `boot.json.example` | 실제 바이너리·기존 모델 인자·키 경로를 채울 설정 예제 |
| `llama-runtime.env.example` | 기존 프로젝트에서 필요한 환경변수 예제 |
| `systemd/satisshin-llm.service` | GPU 확인·시작·실패 후 재시도·부팅 자동 시작 |
| `Caddyfile` | 외부 TLS 4000 → 내부 4001, 준비 상태 확인, 503 JSON 응답 |

기본 실행은 설정 검사만 합니다. `--run`을 명시해야 GPU 검사와 모델 실행을 시작합니다.

```bash
python3 start_llm.py --config /etc/satisshin/boot.json
```

## 서빙 테스트 완료 후 적용 순서

현재 코드는 Linux, Python 3.10 이상, systemd, 공식 Caddy 패키지를 기준으로 준비했습니다.
서버 OS·실행 계정·실제 파일 경로가 확정되면 적용합니다.

1. [Caddy 공식 설치 안내](https://caddyserver.com/docs/install)에 따라 Caddy와 공식 `caddy.service`를 준비합니다.
2. [HTTPS 안내](../https/README.md)에 따라 인증서를 `caddy` 그룹으로 검증·배치합니다.
3. `start_llm.py`는 `/opt/satisshin/boot/`, 채운 설정은 `/etc/satisshin/boot.json`에 둡니다.
   설정은 root:shin-pi, 0640으로 제한합니다. API 키 파일의 기존 위치·내용은 유지하고 실행 계정의 읽기 권한을 확인합니다.
4. 필요한 기존 환경만 `/etc/satisshin/llama-runtime.env`에 옮깁니다. `CUDA_VISIBLE_DEVICES` 등은
   검증한 값을 유지하며 CPU 강제 설정을 넣지 않습니다. API 키 값이나 이전 TLS 환경변수는 넣지 않습니다.
5. 기본 검증 모드가 성공한 뒤 Caddyfile을 `/etc/caddy/Caddyfile`에 둡니다.
   기존 Caddy 설정이 있으면 먼저 병합하며 덮어쓰지 않습니다. `caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile`로 검사합니다.
6. 기존 LLM 자동 시작 항목·사용자 서비스·수동 프로세스와 중복되지 않도록 전환합니다.
   특히 외부 4000은 Caddy만, 내부 4001은 llama-server 하나만 사용해야 합니다.
7. 준비한 LLM unit을 설치하고 다음 명령은 **기존 연결·테스트가 끝난 뒤** 실행합니다.

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now caddy.service
sudo systemctl enable --now satisshin-llm.service
```

`enable`은 다음 부팅부터의 시작, `--now`는 즉시 시작입니다. 적용 때 기존 프로세스와 포트 전환을
마친 뒤 사용합니다. 현재 서버에는 새 예제 unit 대신 기존 `gemma4-e4b.service`의
시작 명령을 교체했습니다. system 서비스이므로 바탕화면 로그인 없이 시작합니다.
기존 `gemma4-server.service` 사용자 서비스에 그대로 중복 적용하지 않습니다.

인증서 갱신은 `--reload-caddy` 설정을 사용해 Caddy만 재로드합니다. LLM 재시작은 필요 없습니다.
공개 DNS는 실제 공인 IPv4를 가리켜야 하며 공유기 포워딩은 외부 4000 → 내부 4000을 유지합니다.

## 확인

실제 서버 적용 때 GPU 정상 상태의 기존 키 호출, 익명·잘못된 키의 401,
GPU 미감지·모델 중지 시 TLS가 유효한 503, 준비 후 자동 복구를 확인합니다.
GPU를 끄거나 드라이버를 제거하는 테스트는 자동으로 수행하지 않습니다.

자동 테스트는 모의 GPU 검사와 임시 loopback HTTPS를 사용합니다.
실제 서버·모델·기존 키를 사용한 배포 검증은 운영 기록에 별도로 남겼습니다.

```powershell
.\src\localLLM_API_proxy\.venv\Scripts\python.exe -m pytest deploy/boot/tests -q
# 로컬 테스트용 Caddy가 있다면 CADDY_BIN 환경변수로 경로를 지정합니다.
```

## 공식 참고

- [llama.cpp 장치 목록과 서버 옵션](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md)
- [systemd 서비스 재시도](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html)
- [Caddy 프록시·상태 확인·스트리밍](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy)
- [Caddy 오류 처리](https://caddyserver.com/docs/caddyfile/directives/handle_errors)
