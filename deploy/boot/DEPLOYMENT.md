# satisshin.top 운영 연결 기록

2026-10-28 Asia/Seoul 기준입니다. 실제 키 값과 SSH 비밀번호는 기록하지 않습니다.

## 현재 접속

- 공개 주소: `https://satisshin.top:4000`
- 상태 확인: `https://satisshin.top:4000/health`
- OpenAI 호환 API: `https://satisshin.top:4000/v1`
- 모델 ID: `gemma4-e4b-qat`
- DNS A: `satisshin.top` → `27.235.241.38`, TTL 600
- 포워딩: 외부 TCP 4000 → `192.168.0.11:4000`

기본 Porkbun 파킹 ALIAS만 A 레코드로 바꾸고 인증서 검증용 TXT와 네임서버는 유지했습니다.
와일드카드 파킹 레코드는 변경하지 않았으므로 이 운영 주소는 루트 `satisshin.top`입니다.

## 실제 서버

| 항목 | 적용한 값 |
| --- | --- |
| SSH | `shin-pi@192.168.0.11` |
| OS·장비 | Debian 13.7 aarch64, Raspberry Pi 5, RTX 3060 12GB |
| 프로젝트 | `/home/shin-pi/gemma-setup` |
| 바이너리 | `/home/shin-pi/gemma-setup/llama.cpp/build/bin/llama-server` |
| 기존 LLM 서비스 | `gemma4-e4b.service`, enabled / active |
| HTTPS 서비스 | Debian 패키지 Caddy 2.6.2, `caddy.service`, enabled / active |
| 공개 수신 | Caddy TCP 4000 |
| 내부 LLM | `127.0.0.1:4001`, HTTP |
| Caddy 관리 | `127.0.0.1:2019` |
| 기존 8080 수신 | 종료됨 |

기존 실행 중 프로세스의 인자 59개에서 host·port만 분리하고 모델·MTP·GPU·캐시·샘플링
설정을 그대로 보존했습니다. **6슬롯 × 65,536 문맥**, 전체 393,216, F16 KV, MTP,
RAM 캐시 2048 MiB 설정입니다. 별도 모델을 중복 적재하지 않았습니다.

## 적용 파일

- `/opt/satisshin/boot/start_llm.py`: GPU 확인 후 기존 바이너리 실행
- `/etc/satisshin/boot.json`: 실제 경로와 보존한 모델 인자, root:shin-pi 0640
- `/etc/satisshin/llama-runtime.env`: 기존 `LD_LIBRARY_PATH`, root:shin-pi 0640
- `/etc/systemd/system/gemma4-e4b.service.d/20-satisshin-boot.conf`: 기존 unit의 시작 명령 교체 및 30초 재시도
- `/etc/caddy/Caddyfile`: 공개 TLS와 내부 상태 확인, 503 JSON 처리
- `/etc/satisshin/tls/current/`: 공인 인증서·개인키, root:caddy, 파일 0640·디렉터리 0750
- `/opt/satisshin/https/sync_certificate.py`: 인증서 검증·교체·Caddy reload 도구

원래 unit의 실행 계정·작업 폴더·환경 파일·종료 신호 등은 유지했습니다.
새 `satisshin-llm.service`는 설치하지 않았습니다. 두 system 서비스가 enabled인 것을
확인했고 실제 서비스 전환을 통해 GPU 검사 후 모델 시작을 확인했습니다. OS 전체 재부팅은 하지 않았습니다.

## 키와 인증

로컬 `컴종설/keys/llama_api_key_01.key`~`04.key`의 기존 값 4개를 재사용했습니다.
서버 목록은 `/home/shin-pi/gemma-setup/keys/project-api-keys.key`, shin-pi:shin-pi 0600입니다.
키 생성·회전은 하지 않았고, 명령줄이나 문서에 키 값을 넣지 않았습니다.

이전 서비스는 API 키 없이 LAN 8080을 받았으나 현재는 내부 4001에서 기존 키 인증을 요구합니다.
Caddy가 인증 헤더를 전달하며 `/health`는 공개 상태 확인용입니다.

## 검증 결과

- 공개 주소에서 CA 체인·호스트명 검증을 켠 HTTPS `/health`: **200**, `{"status":"ok"}`.
- 기존 키 4개 각각 `/v1/models`: **200**.
- 익명·잘못된 키: **401**.
- 전환 시 확인한 슬롯: 6개, 각 `n_ctx=65536`. 이후 보안 보완으로 외부 `/slots`는 404로 차단했습니다.
- 로컬 어댑터의 새 기본 URL로 실제 채팅: **연결 성공**, 정상 종료.
- 실제 스트리밍: **안녕하세요.**, 콘텐츠 청크 수신 완료.
- GPU를 해당 검사 프로세스에서만 숨긴 시험: launcher가 모델을 실행하지 않고 종료 코드 1.
- 백엔드 연결 전 공개 HTTPS: **503 JSON**, Caddy가 계속 응답함.
- 모델 준비 후 상태 확인을 통해 **200으로 자동 복구**.
- 별도 미국 Los Angeles 노드: 2026-10-28 **22:28:23 KST**, HTTP **200**, 약 **0.42초**.

[최종 외부 측정 결과](https://check-host.net/check-report/540c5a7akcf0).
외부 측정에는 공개 health URL만 사용했고 인증키는 전송하지 않았습니다.

## 자동 갱신과 보안 보완

현재 설치된 인증서의 만료일은 **2027-01-26**입니다. Porkbun 쪽 자동 갱신과 외부 서버
파일 교체는 별도입니다. 사용자 승인 후 `satisshin.top` 전용 API 키를 생성했고 root:root 0600의
`/etc/satisshin/porkbun.env`에 설치했습니다. 키의 조회·DNS·도메인 관리 권한만 활성화했으며
구매·충전·메일·호스팅·계정 설정 권한은 없습니다. 로컬과 전송 경로의 임시 키 파일은 제거했습니다.
인증서 helper가 현재 인증서를 다시 검증한 실호출과 DNS 작업의 실호출이 모두 성공했습니다.
환경 프록시 사용을 차단했고 openssl/Caddy 등 자식 프로세스에 API 키를 전달하지 않습니다.

- `porkbun-certificate.timer`: 매일 확인, 변경 시 Caddy만 `--force`로 reload.
- `porkbun-dns.timer`: 부팅 후 2분, 이후 약 5분 간격으로 공인 IPv4와 루트 A 확인.
- 두 timer 모두 enabled / active입니다. 동일 인증서·동일 IP는 변경하지 않습니다.
- DNS 서비스는 DynamicUser, 읽기 전용 파일시스템, capability 제거로 실행합니다.

공인 IP가 바뀌면 두 HTTPS 출처의 일치 여부와 공인 IPv4 여부를 확인한 뒤 기존 루트 A 하나만
수정·재조회합니다. ipTIME DDNS CNAME은 사용하지 않습니다. DNS 캐시·확인 주기에 따른 지연은 가능합니다.

클라이언트는 명시한 HTTP URL도 거부하고 HTTPS만 사용합니다. 외부의 `/slots`, `/metrics`,
`/lora-adapters` 및 `/props` 변경 요청은 인코딩된 경로를 포함해 404로 차단했습니다.
어댑터가 필요한 GET `/props`, 정상 인증, 채팅·스트리밍은 유지됩니다.
GPU launcher는 llama.cpp의 underscore 옵션 별칭까지 정규화해 키 인자·CPU 강제 실행 우회를 거부합니다.

23:10 KST 미국 LA 한 노드의 최종 관측에서 공개 4000 health는 200이며
SSH 22·내부 4001·관리 2019 연결은 시간 초과였습니다.
[최종 공개 상태 측정](https://check-host.net/check-report/540e2794k43e).
이는 한 외부 관측점의 결과이며 서버의 loopback 바인딩·인증 검사와 함께 확인했습니다.
[최종 보안 검토 범위와 한계](SECURITY.md)에 자세한 내용을 기록했습니다.

## 백업·복구

원래 unit·실행 스크립트·환경 파일·기본 Caddy 설정의 백업은 서버의
`/home/shin-pi/.local/state/satisshin-deploy-20261028/backup/`에 있습니다.
이 상위 배포 폴더에는 개인키·기존 API 키가 포함되므로 0700을 유지하고 공개 공유하지 않습니다.

GPU·모델 기동 장애는 `journalctl -u gemma4-e4b.service`로, HTTPS는 `caddy.service`로 확인합니다.
이전 무인증 8080으로 복원하면 공개 보안 설정도 달라지므로 자동으로 되돌리지 않습니다.
