# develop 브랜치 동기화

develop 브랜치로 이동하고 원격 최신 상태를 반영한다.

### Step 1 — 워킹트리 확인

```bash
git status --porcelain
```

출력이 있으면(커밋되지 않은 변경) `checkout`이 실패하거나 변경이 딸려갈 수 있다. 사용자에게 알리고 커밋 또는 `git stash` 후 진행한다. 비어 있으면 다음 Step으로 진행한다.

### Step 2 — 이동 및 pull

```bash
git checkout develop && git pull origin develop
```

### Step 3 — 받은 커밋 출력

```bash
git branch --show-current && git log --oneline develop@{1}..develop
```

> `pull` 직후 `develop`은 `origin/develop`과 동일하므로, pull **이전** 위치(`develop@{1}`, reflog)와 비교해 새로 받은 커밋만 출력한다. fast-forward가 없었으면(이미 최신) 결과는 비어 있다.
