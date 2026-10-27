# GitHub 컨벤션

## 작업 흐름

- 이슈는 쓰지 않고, 모든 작업은 **PR**로 올립니다.
- PR 본문은 [PR 템플릿](.github/PULL_REQUEST_TEMPLATE.md)을 따릅니다. PR을 열면 자동으로 채워집니다.

## 브랜치 전략

`main` 하나를 기준으로 두고, 작업마다 브랜치를 새로 만들어 PR로 합칩니다.

```
feat/planner          ●──●──●
                     /        \
main  ──●───────────●──────────●─────●──▶
                     \              /
fix/leakguard-regex   ●──●─────────●
```

- `main`에는 직접 push하지 않습니다. 항상 PR로 합칩니다.
- 브랜치는 **작업 하나당 하나**씩, 항상 **최신 `main`에서** 만듭니다.
- 머지된 브랜치는 삭제합니다. 이어서 작업할 게 있으면 최신 `main`에서 새로 만듭니다.

### 브랜치 이름

`라벨/짧은-설명` 형식으로, 영어 소문자와 `-`만 씁니다. 라벨은 아래 PR 라벨과 같습니다.

| 예시 | 작업 |
| --- | --- |
| `feat/planner` | Planner 모듈 개발 |
| `fix/leakguard-regex` | LeakGuard 정규식 버그 수정 |
| `docs/readme` | README 수정 |
| `chore/gitignore` | `.gitignore` 추가 |

### 커밋 메시지

`라벨: 한 줄 설명` 형식으로 씁니다. 설명은 한국어로 써도 됩니다.

```
feat: Planner 계획 생성 함수 추가
fix: 빈 입력일 때 LeakGuard 오류 수정
```

### 작업 순서

```bash
# 1. 최신 main 받기
git checkout main
git pull

# 2. 브랜치 만들기
git checkout -b feat/planner

# 3. 작업 후 커밋
git add .
git commit -m "feat: Planner 계획 생성 함수 추가"

# 4. 브랜치 올리기
git push -u origin feat/planner
```

5. GitHub에서 **Compare & pull request**를 눌러 PR을 엽니다. 본문은 템플릿대로 채우고 라벨을 붙입니다.
6. 리뷰가 끝나면 머지하고, **Delete branch**를 눌러 브랜치를 지웁니다.
7. 내 PC에서도 `main`으로 돌아가 최신 상태를 받습니다.

```bash
git checkout main
git pull
git branch -d feat/planner
```

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
