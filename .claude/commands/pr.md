# PR 생성 또는 업데이트

현재 브랜치의 변경사항을 커밋하고, PR을 생성하거나 기존 PR을 업데이트한다.

## 실행 순서

### Step 0 — 브랜치 확인

```bash
git branch --show-current
```

- **현재 브랜치가 feature 브랜치(`develop`이 아닌 경우)** → 그대로 다음 Step으로 진행한다.
- **현재 브랜치가 `develop`인 경우** → 변경사항과 커밋 내역을 보고 적절한 이름의 feature 브랜치를 생성한 뒤 다음 Step으로 진행한다:

```bash
git checkout -b <feature-branch-name>
```

브랜치 이름은 커밋 내용 기반으로 `feat/`, `chore/`, `fix/` 등 prefix를 붙인다.

### Step 1 — 현재 상태 확인

```bash
git status
git branch --show-current
```

변경사항과 현재 브랜치명을 확인한다.

### Step 2 — 변경사항 스테이징 & 커밋

```bash
git diff HEAD
git log --oneline -10
```

변경된 파일을 확인하고, 커밋 메시지를 작성한다.

- 커밋 메시지는 최근 커밋 스타일을 참고한다
- 변경 내용을 명확히 요약한다
- 스테이징은 관련 파일만 명시적으로 추가한다 (`git add -A` 금지)

### Step 3 — PR body 작성

아래 정보를 수집한다. 베이스는 **로컬 `develop`이 아니라 `origin/develop`** 기준으로 비교한다 (로컬 develop이 오래되어 머지된 커밋이 섞이는 것을 방지).

```bash
# 원격 최신 develop을 가져온다 (로컬 ref는 이동하지 않음)
git fetch origin develop

# develop 이후 커밋 목록
git log origin/develop..HEAD --oneline

# 변경된 파일 목록
git diff origin/develop --name-only

# 테스트 실행
npm test -- --run
```

수집한 정보를 바탕으로 `.github/PULL_REQUEST_TEMPLATE.md` 템플릿의 각 섹션을 실제 내용으로 채운다:

- **변경 사항**: 변경된 파일과 각 파일에서 무엇을 왜 바꿨는지 bullet로 작성
- **관련 이슈**: 브랜치명·커밋 메시지에서 이슈 번호가 보이면 기입, 없으면 `-` 로 비워둠
- **체크리스트**: 실제 구현 내용 기반으로 통과 여부를 `[x]` / `[ ]` 로 표시. **테스트 통과** 항목은 `npm test` 결과를 기준으로 표시 — 통과 시 `[x]`, 실패 시 `[ ]`

### Step 4 — PR 상태 확인 후 분기

```bash
gh pr list --head $(git branch --show-current) --state all --json number,state,url
```

**열려 있는 PR이 있는 경우 (state: OPEN)** → 푸시 후 body도 업데이트:

```bash
git push origin $(git branch --show-current)
gh pr edit <number> --body "<Step 3에서 작성한 body>"
```

푸시 및 body 업데이트 완료 후 PR URL을 출력한다.

**PR이 없거나 이미 머지된 경우 (state: MERGED 또는 결과 없음)** → 새 PR 생성:

```bash
git push -u origin $(git branch --show-current)

gh pr create \
  --base develop \
  --title "<브랜치명·커밋 내용 기반 제목>" \
  --body "<Step 3에서 작성한 body>"
```

PR 제목은 브랜치명과 커밋 내용을 참고해 간결하게 작성한다.

### Step 5 — 완료 보고

생성 또는 업데이트된 PR URL을 출력한다.
