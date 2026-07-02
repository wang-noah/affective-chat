"""
arbiter_cli.py — 소스층 컨트롤 패널의 CLI 버전 (Arbiter 결과만)
================================================================
playground.py 의 웹 UI 와 똑같은 소스층 입력을 터미널에서 받고,
Affect·대사는 빼고 **Arbiter 결과만** 출력한다.

계산 로직은 playground.compute() 를 그대로 재사용한다 (UI 와 동일 결과 보장).
여기선 입력 프롬프트 + Arbiter 뷰 렌더만 담당한다.

  python3 arbiter_cli.py            # 대화형 입력 → Arbiter 결과
  python3 arbiter_cli.py --raw      # 원본 트레이스 로그도 함께 출력
"""
from __future__ import annotations
import sys

import playground
from playground import compute


# ── ANSI 색 (터미널이 지원할 때만) ─────────────────────────────────────────
_TTY = sys.stdout.isatty()


def c(s, code):
    return f"\033[{code}m{s}\033[0m" if _TTY else str(s)


def dim(s):   return c(s, "2")
def bold(s):  return c(s, "1")
def blue(s):  return c(s, "38;5;75")
def green(s): return c(s, "38;5;71")
def red(s):   return c(s, "38;5;203")
def purple(s): return c(s, "38;5;183")


# ── 입력 헬퍼 ───────────────────────────────────────────────────────────────
def ask(label, default, note=""):
    """blank=기본값(포함) · 's'=스킵(제외→중립값). 반환: (값 or None)."""
    hint = f" [{default}]" if default != "" else ""
    tail = dim(f"  ({note})") if note else ""
    raw = input(f"  {label}{hint}{tail}\n  › ").strip()
    if raw.lower() in ("s", "skip", "-"):
        return None
    return raw if raw else str(default)


def ask_choice(label, choices, default):
    print(f"  {label}  " + dim("[" + " / ".join(choices) + "]"))
    raw = input(f"  › [{default}] ").strip()
    if raw.lower() in ("s", "skip", "-"):
        return None
    return raw if raw in choices else str(default)


# ── 소스층 입력 수집 → compute() 쿼리(dict of lists) ─────────────────────────
def collect() -> dict:
    print(bold("\n═══ 소스층 입력 (노션 소스층) ═══"))
    print(dim("  Enter=기본값 사용 · 's'=이 소스층 제외(중립값 처리)\n"))
    q: dict = {}

    def put(key, val):
        if val is not None:
            q[key] = [val]

    # 1) 유저 발화 (+ 키워드 자동 kind 분류)
    print(blue("① 유저 발화 (대화)"))
    text = ask("발화 텍스트", "오늘 회사에서 진짜 짜증났어")
    if text is not None:
        put("text", text)
        put("auto", "1")   # 키워드 자동 분류 ON (UI 기본)

    # 2) 대화 기록 (같은 발화 반복 횟수)
    print(blue("\n② 대화 기록 (반복 횟수 0~5)"))
    put("repeat", ask("반복 횟수", "0"))

    # 3) 유저 팬심 (누적 점수)
    print(blue("\n③ 유저 팬심 (누적 0~10000)"))
    put("fan", ask("팬심 점수", "1500"))

    # 4) 친밀도
    print(blue("\n④ 친밀도 (0~12)"))
    put("intimacy", ask("친밀도", "1"))

    # 5) 온톨로지 토픽
    print(blue("\n⑤ 온톨로지 토픽 (표시용)"))
    put("onto", ask("토픽", "롤 e스포츠"))

    # 6) 팔로잉
    print(blue("\n⑥ 팔로잉 (팀 팔로우 여부)"))
    fol = ask_choice("팔로우함?", ["1", "0"], "0")
    put("following", fol)

    # (부가) 어펙트 상태값 E·A — Arbiter 계산에 필요한 직전 상태
    print(purple("\n· 직전 정서 E (-1~1)"))
    put("E", ask("E", "0.1"))
    print(purple("· 직전 세기 A (0~1)"))
    put("A", ask("A", "0.4"))

    return q


# ── Arbiter 뷰 렌더 (playground 웹 UI 의 renderArb 와 동일 순서) ──────────────
def sgn(n):
    return f"{n:+.2f}"


def render_arbiter(d: dict):
    a = d.get("arbiter_view")
    print(bold("\n╔══════════════════════════════════════════════════════════╗"))
    print(bold("║  ① Arbiter — 선별 · 값매기기 · 분기  (노션 §4 기준)        ║"))
    print(bold("╚══════════════════════════════════════════════════════════╝"))

    # 소스층 요약 (열기는 CLI 에서 다루지 않으므로 요약에서도 숨김)
    print(green("\n┃ 소스층 입력 요약 (이번 SEND)"))
    for line in d["source_log"].splitlines():
        if "열기(경기)" in line:
            continue
        print("  " + line)

    if not a:
        print(dim("\n  (arbiter_view 없음)"))
        return

    # ── ① 선택 ──────────────────────────────────────────────────────────
    print(blue("\n① 선택 — 누구에게 반응할지"))
    print(dim("   select_score = 세기 × 성격가중 × 최신성 × 기분되먹임"))
    rows = a["candidates"]
    if not rows:
        print(dim("   후보 없음 — 소스층 입력이 비었음"))
    else:
        hdr = f"   {'자극':<20}{'세기':>6}{'성격가중':>9}{'최신성':>8}{'기분':>7}{'= 점수':>9}   상태"
        print(dim(hdr))
        for r in rows:
            if r["status"] == "win":
                st = green("WIN")
            elif r["status"] == "defer_thr":
                st = dim("thr 미만")
            else:
                st = dim("순위 밀림")
            line = (f"   {r['kind']:<20}{r['intensity']:>6.2f}{r['weight']:>9.2f}"
                    f"{r['recency']:>8.2f}{r['mood']:>7.2f}{r['score']:>9.3f}   {st}")
            print(bold(line) if r["status"] == "win" else line)
        p = a["policy"]
        print(dim(f"   선택 정책: {p['type']} · 임계 {p['threshold']} 이상을 최대 {p['max']}개"))

    # ── ② 값매기기 ──────────────────────────────────────────────────────
    print(blue("\n② 값매기기 — 승자 자극에 valence · salience"))
    print(dim("   salience = 기본중요도 × 세기 × 부스터 × [팬심배율]"))
    fan = a.get("fan") or {}
    if fan.get("factor", 1) != 1:
        print(f"   팬심 {bold(fan['tier_ko'])} → 팔로우팀(data) 자극 salience ×{fan['factor']:.2f}")
    if not a["stimuli"]:
        print(dim("   승자 자극 없음"))
    for s in a["stimuli"]:
        b = s["sal"]
        slot = "주자극" if s["slot"] == "primary" else "보조자극"
        print(f"\n   ◆ {bold(slot)}  [{s['type']}] {s['kind']}  {dim('src=' + str(s['source']))}")
        vcol = green if s["valence"] >= 0 else red
        vtxt = vcol(sgn(s["valence"]))
        note = dim(f"← 표 {s['kind']!r} 고정값 (변형 없음)")
        print(f"     valence   {vtxt}   {note}")
        eq = f"기본중요도 {b['base_imp']:.2f} × 세기 {b['intensity']:.2f} × 부스터 {b['booster']:.2f}"
        if b["fan_applies"]:
            eq += f" × 팬심 ×{b['fan_factor']:.2f}"
        print(f"     salience  {blue(format(s['salience'], '.3f'))}   {dim('= ' + eq)}")

    # ── ③ 분기 ──────────────────────────────────────────────────────────
    print(blue("\n③ 분기"))
    r = a["route"]
    print(f"   말하기(chat): {bold('예' if r['chat'] else '아니오')}   ({a['route_reason']})")
    print(f"   표현 모드: {bold(a['path'])}")

    # 원본 트레이스 로그 (옵션)
    if "--raw" in sys.argv:
        print(dim("\n── 원본 Arbiter 트레이스 로그 ──"))
        print(dim(d["trace_arbiter"]))


def main():
    print(bold(f"\n▶ Arbiter CLI — 캐릭터 {playground.CFG.name}"))
    q = collect()
    d = compute(q)
    render_arbiter(d)
    print()


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print("\n종료")
