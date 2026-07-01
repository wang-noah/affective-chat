# develop 머지 (현재 브랜치)

현재 작업 브랜치를 떠나지 않고 원격 최신 `develop`을 **현재 브랜치에 머지**한다. 충돌 시 **develop을 우선순위**로 두되, 충돌이 없는 내 작업물은 최대한 그대로 보존한다. 자동 해소가 애매한 부분은 사용자에게 검토를 요청한다.

> 원칙
> - **비충돌 변경**: develop의 변경과 내 작업물 모두 그대로 적용된다(일반 머지 동작).
> - **충돌 부분**: develop 쪽(incoming)을 우선한다.
> - **판단이 필요한 충돌**: 임의로 덮어쓰지 말고 사용자에게 물어본다.
>
> 로컬 `develop` 자체를 원격과 맞추기만 할 거라면 `/sync-develop`을 쓴다.

### Step 0 — 현재 브랜치 확인

```bash
git branch --show-current
```

`develop`이면 머지할 대상이 없다. 이 스킬을 중단하고 `/sync-develop`을 안내한다. 그 외 브랜치가 머지 대상(target)이다.

### Step 1 — 워킹트리 확인

```bash
git status --porcelain
```

출력이 있으면(커밋되지 않은 변경) 머지가 거부되거나 변경이 꼬일 수 있다. 사용자에게 알리고 **커밋 또는 `git stash` 후 진행**한다. 비어 있으면 다음 Step으로 진행한다.

### Step 2 — 원격 develop 최신화

현재 브랜치를 벗어나지 않고 원격 상태만 받아온다.

```bash
git fetch origin develop
```

### Step 3 — develop 머지

```bash
git merge origin/develop
```

- **클린 머지 / fast-forward**: 그대로 완료. Step 5로 반영 커밋을 확인한다.
- **충돌 발생**: **자동으로 develop을 전부 덮어쓰지 않는다.** 먼저 충돌 범위를 파악한다:

```bash
git diff --name-only --diff-filter=U
```

### Step 4 — 충돌 분류 및 해소

충돌 파일마다 내용을 열어 아래 기준으로 나눈다. `<<<<<<< HEAD`(내 작업) / `>>>>>>> origin/develop`(develop) 마커 기준.

1. **명백히 develop이 맞는 경우** (예: develop이 이후 리팩터링·버그픽스로 갱신했고, 내 변경은 낡은 버전에 기반) → develop 우선으로 해소한다.

   특정 파일 전체를 develop 버전으로 채택:
   ```bash
   git checkout --theirs <file> && git add <file>
   ```

2. **내 작업물이 사라지면 안 되는 경우** (예: 이 브랜치에서 새로 추가한 기능·로직이 충돌 블록 안에 섞여 있음) → develop 구조를 기준으로 하되 **내 변경을 그 위에 다시 얹어** 양쪽 의도를 살린다. 자동으로 한쪽을 버리지 않는다.

3. **판단이 애매한 경우** → **사용자에게 물어본다.** 파일 경로와 충돌 블록(내 버전 vs develop 버전)을 요약해 제시하고, 어느 쪽을 택할지 또는 어떻게 합칠지 확인한 뒤 반영한다.

> "develop 우선"은 **겹치는 부분에서 develop을 기본값으로 삼는다**는 뜻이지, 내 작업을 통째로 버린다는 뜻이 아니다. 충돌이 없는 내 변경은 항상 보존된다.

전부 해소했으면 스테이징하고 미해소가 없는지 확인한다:

```bash
git add -A && git status --porcelain
```

`UU`/`AA` 등 미해소 표시가 남아 있지 않으면 커밋한다:

```bash
git commit --no-edit
```

### Step 5 — 반영된 커밋 출력

```bash
git branch --show-current && git log --oneline ORIG_HEAD..HEAD
```

> 머지 직전 HEAD는 `ORIG_HEAD`에 저장된다. 이와 비교해 이번 머지로 새로 들어온 커밋만 출력한다. fast-forward도 동일하게 동작한다.

### 중단이 필요할 때

해소가 불가능하거나 사용자가 원복을 원하면 머지 이전 상태로 되돌린다:

```bash
git merge --abort
```
