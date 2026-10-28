# satisshin.top HTTPS 준비

2026-10-28 서버 적용과 공개 HTTPS·기존 키 인증·실제 추론을 확인했습니다.
[현재 운영 설정과 검증 결과](../boot/DEPLOYMENT.md)를 먼저 확인하세요.
인증서 자동 다운로드·Caddy reload와 공인 IPv4의 루트 A 자동 갱신을 서버에서 활성화했습니다.
SSH 비밀번호, Porkbun API 키, 개인키도 저장소에 저장하지 않습니다.

| 항목 | 준비한 값 |
| --- | --- |
| 공개 접속 주소 | `https://satisshin.top:4000` |
| SSH 대상 | `shin-pi@192.168.0.11` |
| 공유기 포워딩 | 외부 TCP 4000 → `192.168.0.11:4000` |
| TLS 처리 | Caddy가 외부 4000에서 처리하고 내부 llama-server로 전달 |
| 내부 LLM | `127.0.0.1:4001`, 기존 프로젝트의 API 키 사용 |
| 인증서 경로 | `/etc/satisshin/tls/current/fullchain.pem` |
| 개인키 경로 | `/etc/satisshin/tls/current/privkey.pem` |

브라우저·클라이언트 주소에는 `:4000`을 붙입니다. 인증서는 도메인을 확인하므로
포트 4000에서도 유효합니다. GPU가 없으면 LLM은 시작하지 않고 Caddy가 HTTP 503을
반환합니다. [부팅·GPU 확인·장애 응답 구성](../boot/README.md)을 함께 적용합니다.

## 1. 최초 발급은 Porkbun에서

Porkbun → Domain Management → `satisshin.top`의 Details → SSL을 엽니다.
2026-10-28 발급 완료를 확인했고 `satisshin.top`, `*.satisshin.top`을 포함합니다.
관리 화면의 만료일은 2027-01-26입니다. 내려받은 실제 묶음의 공인 CA 체인,
도메인, 유효기간, 개인키 일치도 로컬에서 확인했습니다.
Porkbun 기본 네임서버를 유지하면 DNS TXT로 소유권을 검증하므로 서버가 꺼져 있어도
발급할 수 있습니다. 공식 안내의 예상 소요 시간은 1~2시간이며 24시간을 넘기면 지원에 문의합니다.

발급 완료 후 **Download SSL Bundle**로 수동 다운로드하거나 아래 도구로 가져옵니다.
이번 묶음은 로컬의 `private/satisshin.top-ssl-bundle.zip`에 보관했습니다.
Git에서 제외되는 개인키 포함 자료이므로 이 파일은 커밋하거나 공개 공유하지 않습니다.
API의 `/ssl/retrieve/{domain}`은 이미 발급된 묶음을 가져오는 기능입니다.
아직 발급되지 않았다면 도구는 실패하고 기존 인증서를 유지합니다.
Porkbun의 자동 갱신과 서버 파일 교체는 별도 작업입니다.

API를 사용할 때는 사용자가 Porkbun 계정에서 API 키를 만들고 해당 도메인의 API Access를
활성화해야 합니다. 이번 배포에서는 사용자 승인 후 해당 도메인 하나만 활성화했습니다. 키는 서버의
`/etc/satisshin/porkbun.env`에만 입력하며 채팅이나 저장소에 복사하지 않습니다.
수동 다운로드 산출물을 로컬에 둘 때는 Git에서 제외한 `deploy/https/private/`를 사용합니다.

## 2. 서버 준비가 끝난 뒤 인증서 배치

이 예시는 Python 3.10 이상, OpenSSL, systemd가 있는 Linux 서버를 기준으로 합니다.
실제 OS와 서비스 이름은 적용 전에 확인합니다. Caddy를 공식 패키지로 설치해
`caddy` 계정·그룹이 있는 상태를 기준으로 합니다.
인증서 동기화 도구에는 별도 Python 패키지가 필요하지 않습니다.

Windows의 저장소 루트에서 파일만 복사합니다. 비밀번호 입력은 SSH가 요청할 때 직접 합니다.

```powershell
scp -r .\deploy\https shin-pi@192.168.0.11:~/satisshin-https
```

위 복사에는 `private/`의 인증서 묶음도 포함됩니다. 대상은 지정한 개인 서버로 한정합니다.
서버에서 다음을 실행하면 받은 ZIP을 검증한 뒤 배치합니다. 이 초기 배치에는 API 키가
필요 없으며, 도구가 ZIP의 파일 경로를 그대로 디스크에 추출하지도 않습니다.

```bash
sudo install -d -m 0755 -o root -g root /etc/satisshin
sudo install -d -m 0750 -o root -g caddy /etc/satisshin/tls
sudo install -d -m 0755 /opt/satisshin/https
sudo install -m 0755 ~/satisshin-https/sync_certificate.py /opt/satisshin/https/sync_certificate.py
sudo python3 /opt/satisshin/https/sync_certificate.py --bundle ~/satisshin-https/private/satisshin.top-ssl-bundle.zip --domain satisshin.top --output-dir /etc/satisshin/tls --group caddy
```

이 명령은 인증서를 검증·배치만 합니다. LLM을 실행하거나 재시작하지 않습니다.
체인 신뢰, `satisshin.top` 호스트명, 남은 유효기간, 개인키 일치를 확인하고
검증된 버전으로 `current` 심볼릭 링크를 교체합니다. 실패한 응답·인증서는 적용하지 않습니다.
키는 0640, 디렉터리는 0750으로 배치하고 `caddy` 그룹만 읽을 수 있게 합니다.
예전 버전도 개인키를 포함하므로 이 디렉터리 전체를 비밀 자료로 관리합니다.

## 3. 테스트를 마친 llama-server와 HTTPS 연결

[부팅 구성 안내](../boot/README.md)에 따라 기존 프로젝트의 실제 실행 파일·모델 인자·
API 키 파일 경로를 설정합니다. 이번 배포에서는 로컬 프로젝트의 기존 키 4개를 서버의
키 목록에 연결했습니다. 키 값을 새로 발급하지 않았으며 launcher는 그 파일을 직접 참조합니다.
TLS 개인키는 Caddy만 읽으며 llama-server에는 전달하지 않습니다.

Caddy는 외부 TCP 4000을 받고 llama-server는 `127.0.0.1:4001`에서 HTTP로 실행합니다.
같은 포트를 두 프로세스가 함께 사용하지 않습니다. 이전 직접 HTTPS 실행 옵션이나 서비스를
중복 적용하지 않습니다. 실제 포트 전환과 서비스 활성화는 서빙 테스트 완료 후 진행합니다.

DNS에는 **실제 공인 진입 IPv4**를 루트(`@`) A 레코드로 지정합니다.
`192.168.0.11`이나 VPN의 사설 주소를 공개 A 레코드에 넣지 않습니다.
이 구성은 ipTIME DDNS CNAME을 사용하지 않습니다. 유동 공인 IP의 DNS 갱신은 별도 작업입니다.

## 4. 추론 요청 없이 TLS 확인

서버가 준비된 뒤 실행합니다. DNS 전환 전에는 TCP 대상만 사설 IP로 지정해도
인증서 이름은 `satisshin.top`으로 검증합니다. `check_tls.py`는 TLS 연결만 하고 HTTP나
LLM 추론 요청을 보내지 않습니다. 인증서 검증을 생략하는 옵션은 없습니다.

```powershell
python .\deploy\https\check_tls.py --connect-host 192.168.0.11
python .\deploy\https\check_tls.py
```

두 번째 확인은 내부 공유기의 loopback 지원 여부에 영향을 받으므로 최종 공개 접속은
외부 네트워크에서도 확인합니다. 클라이언트 어댑터의 새 기본 URL은
`https://satisshin.top:4000`이고 CA·호스트명 검증이 켜져 있습니다.
실제 LLM 서빙의 공개 HTTPS 호출·인증·채팅·스트리밍 검증 결과는 운영 기록에 있습니다.

## 5. 갱신 예약과 HTTPS 인증서 반영

`systemd/porkbun-certificate.timer`는 하루에 한 번 최신 인증서를 가져오도록 준비한
설정입니다. 현재 서버에는 설치·활성화했고 첫 수동 실행도 성공했습니다. ZIP은 당시의 인증서 사본이므로
계속 재설치하는 것으로 갱신되지 않습니다. 아래 API 방식은 최신 발급본을 매번 가져옵니다.
Porkbun에서 API를 준비한 후 `sudoedit` 화면에서 두 API 키를 입력합니다.
환경 파일은 root 전용 0600을 유지합니다.

```bash
# 최초 인증서 배치에 성공한 뒤 필요할 때 설정
sudo install -m 0600 ~/satisshin-https/.env.example /etc/satisshin/porkbun.env
sudoedit /etc/satisshin/porkbun.env
sudo install -m 0644 ~/satisshin-https/systemd/porkbun-certificate.service /etc/systemd/system/
sudo install -m 0644 ~/satisshin-https/systemd/porkbun-certificate.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl start porkbun-certificate.service
sudo systemctl status porkbun-certificate.service
# 위 동기화가 성공한 뒤 활성화
sudo systemctl enable --now porkbun-certificate.timer
```

기본 서비스는 파일만 교체합니다. **Caddy 기동 확인 후**
`systemd/porkbun-certificate-reload.conf.example`을 동기화 서비스의 drop-in으로 적용합니다.
인증서가 바뀔 때만 `--reload-caddy`가 Caddy의 `reload --force`를 실행하며 LLM은
재시작하지 않습니다. reload 실패 시 파일 링크를 복구합니다. 먼저 파일만 바꾼 뒤 이
선택지를 켰다면 최초 한 번은 수동 Caddy reload가 필요합니다. 실제 제공 인증서는
TLS 확인 도구로 별도 확인합니다.

## 6. 공인 IPv4 자동 갱신

`sync_dns.py`는 서버에서 ipify와 AWS checkip을 HTTPS로 조회하고 두 값이 같은 공인 IPv4일
때만 사용합니다. 기본 실행은 조회 전용이며 `--apply`를 명시해야 루트 A 레코드 하나를
수정합니다. 없거나 여러 개인 레코드는 자동 생성·삭제하지 않고 실패합니다.
IP가 같으면 쓰지 않으며 변경 후에는 같은 ID를 재조회해 결과를 검증합니다.

`systemd/porkbun-dns.service`와 `.timer`를 설치하면 부팅 후 2분부터 약 5분 간격으로 확인합니다.
현재 서버에 활성화했고 실제 공인 IP와 DNS가 같음을 확인했습니다. IP 변경의 반영에는
확인 주기와 DNS TTL(600초)에 따른 시간이 걸릴 수 있습니다.

DNS 작업은 `DynamicUser`로 실행하며 디스크·장치·관리 권한을 제한합니다.
두 작업의 API 키는 systemd가 root:root 0600의 `/etc/satisshin/porkbun.env`에서 읽어 주입합니다.
키는 `satisshin.top` 하나에만 한정했고 필수 조회·DNS·인증서 포함 도메인 관리 권한만 있습니다.
구매·충전·메일·호스팅·계정 설정 권한은 주지 않았습니다. 도메인 관리 권한에는 업체 정책상
네임서버·연락처 변경도 포함되며 사용자 승인 후 생성했습니다.
공인 IP가 바뀌어도 갱신할 수 있도록 API 키의 호출 IP 제한은 두지 않았습니다.

```bash
sudo install -m 0755 sync_dns.py /opt/satisshin/https/sync_dns.py
sudo install -m 0644 systemd/porkbun-dns.service systemd/porkbun-dns.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl start porkbun-dns.service
sudo systemctl enable --now porkbun-dns.timer
```

기존 API 키 파일은 재설치 과정에서 빈 예제로 덮어쓰지 않습니다. 갱신 상태는
`systemctl list-timers porkbun-certificate.timer porkbun-dns.timer`와 각 서비스의 journal로 확인합니다.
API 응답·개인키·인증값은 작업 로그에 출력하지 않습니다.

## 로컬 검증

실제 도메인 API나 SSH 서버에 접속하지 않고 임시 인증서·모의 API로 검사합니다.

```powershell
.\src\localLLM_API_proxy\.venv\Scripts\python.exe -m pytest deploy/https/tests -q
.\src\localLLM_API_proxy\.venv\Scripts\python.exe deploy/https/sync_certificate.py --help
.\src\localLLM_API_proxy\.venv\Scripts\python.exe deploy/https/check_tls.py --help
```

## 공식 문서

- [Porkbun 무료 SSL 발급·다운로드](https://kb.porkbun.com/article/71-how-your-free-ssl-certificates-work)
- [Porkbun DNS TXT 검증](https://kb.porkbun.com/article/270-why-are-there-acme-challenge-dns-records-on-my-domain)
- [Porkbun API 명세](https://porkbun.com/api/json/v3/spec)
- [llama.cpp 서버의 TLS·API 인증 옵션](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md)
- [Caddy 인증서 파일 설정](https://caddyserver.com/docs/caddyfile/directives/tls)
- [Caddy 인증서 강제 reload](https://caddyserver.com/docs/command-line)
