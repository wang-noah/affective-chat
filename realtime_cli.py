"""
realtime_cli.py — 온톨로지 실시간 데이터(raw_frame) 재생 → 감정 스트림
=====================================================================
raw_frame_202606281750.csv 를 '온톨로지의 실시간 데이터'로 보고, 주목할 게임
이벤트를 시간 순서대로 TICK_SECONDS(기본 10초)마다 하나씩 재생한다. 매 틱마다
Arbiter → Affect 를 한 턴 돌려 감정(표정·정서 E/A·열기·발화 여부)을 출력한다.

실시간 델타는 사람이 고르는 소스층이 아니라 이 스트림이 자동으로 먹인다.
나머지 소스층(팬심·친밀도·팔로우팀)은 아래 '설정값'으로 고정하며 env 로 뺀다.

  python3 realtime_cli.py
  RT_FOLLOW_TEAM=200 python3 realtime_cli.py      # 반대 팀 팔로우
  RT_TICK_SECONDS=2 RT_MAX_EVENTS=20 python3 realtime_cli.py   # 빠른 미리보기

E/A/열기는 턴을 넘겨 이어진다(직전 결과가 다음 턴의 기저). 친밀도는 설정값으로 고정.
"""
from __future__ import annotations
import csv
import json
import os
import sys
import time

csv.field_size_limit(sys.maxsize)

from config import load_config
from affect_engine import AffectState
from arbiter import Candidate, map_event
from expression import express
from playground import trace_turn


# ── .env 로더 (표준 라이브러리만; python-dotenv 불필요) ─────────────────────
def load_dotenv(path: str | None = None):
    """스크립트 옆 .env 의 KEY=VALUE 를 os.environ 에 채운다.
    이미 셸에 있는 값은 덮지 않는다(셸 > .env). 주석(#)·빈 줄 무시."""
    path = path or os.path.join(os.path.dirname(__file__), ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip()
            if val[:1] in ("'", '"'):              # 따옴표 값: 닫는 따옴표까지
                q, end = val[0], val.find(val[0], 1)
                val = val[1:end] if end != -1 else val[1:]
            else:                                   # 비따옴표 값: 인라인 주석(공백+#) 제거
                val = val.split(" #", 1)[0].split("\t#", 1)[0].strip()
            os.environ.setdefault(key, val)


load_dotenv()

# ── 설정값 (.env 로 분리 — .env 파일에서 편집, 셸 env 로 일시 override) ───────
#   실시간 델타를 뺀 나머지 소스층은 여기서 고정한다. 값은 .env 참조.
FAN          = float(os.environ.get("RT_FAN", "1500"))        # 유저 팬심(누적)
INTIMACY     = float(os.environ.get("RT_INTIMACY", "7"))      # 친밀도 (고정)
FOLLOW_TEAM  = int(os.environ.get("RT_FOLLOW_TEAM", "100"))   # 팔로우 팀: 100 or 200
TICK_SECONDS = float(os.environ.get("RT_TICK_SECONDS", "10")) # 이벤트 재생 간격(초)
MAX_EVENTS   = int(os.environ.get("RT_MAX_EVENTS", "0"))      # 0 = 전부
DT           = float(os.environ.get("RT_DT", "1.0"))          # 감쇠용 턴 간격
VERBOSE      = os.environ.get("RT_VERBOSE", "0") == "1"       # 원본 Arbiter 로그 동봉

CFG = load_config(os.path.join(os.path.dirname(__file__), "character.json"))
CSV_PATH = os.path.join(os.path.dirname(__file__), "raw_frame_202606281750.csv")


# ── ANSI 색 (터미널일 때만) ──────────────────────────────────────────────
_TTY = sys.stdout.isatty()
def _c(s, code): return f"\033[{code}m{s}\033[0m" if _TTY else str(s)
def dim(s):    return _c(s, "2")
def bold(s):   return _c(s, "1")
def blue(s):   return _c(s, "38;5;75")
def green(s):  return _c(s, "38;5;71")
def red(s):    return _c(s, "38;5;203")
def orange(s): return _c(s, "38;5;215")


# ── 온톨로지 실시간 데이터 로딩 (주목 이벤트만, 시간순) ──────────────────────
def load_events(path: str = CSV_PATH):
    """raw_frame → 주목할 이벤트 payload 리스트 (gameTime·sequenceIndex 순)."""
    out = []
    if not os.path.exists(path):
        return out
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            try:
                p = json.loads(row["payload"])
            except Exception:
                continue
            if map_event(p, 100) is None:      # notable 여부는 관점 팀과 무관
                continue
            out.append(p)
    out.sort(key=lambda p: (p.get("gameTime") or 0, p.get("sequenceIndex") or 0))
    return out


# ── 한 틱 렌더 ────────────────────────────────────────────────────────────
def render_tick(i: int, total: int, gt_ms: int, desc: str, kind: str, inten: float,
                av: dict, output, expr, arb_log: str):
    mm, ss = int((gt_ms or 0) // 60000), int(((gt_ms or 0) // 1000) % 60)
    sign = green("＋") if kind == "game_positive" else red("－")
    print(bold(f"\n━━ #{i:03d}/{total}  ⏱ {mm:02d}:{ss:02d}  {sign} {desc}  ")
          + dim(f"({kind} · 세기 {inten:.2f})"))

    stim = av["stimuli"][0] if av["stimuli"] else None
    if stim is None:
        # select_score 가 임계 미만 → 반응 안 함(속으로만, 감쇠된 상태 표시)
        print("   " + dim("반응 임계 미만 — 겉으로 반응 없음 (감정만 감쇠)"))
    else:
        vcol = green if stim["valence"] >= 0 else red
        b = stim["sal"]
        salnote = ""
        if b["fan_applies"]:
            salnote = orange(f"  (팬심 {av['fan']['tier_ko']} ×{b['fan_factor']:.2f})")
        speak = "발화" if av["route"]["chat"] else "속으로"
        vtxt = vcol(format(stim["valence"], "+.2f"))
        saltxt = blue(format(stim["salience"], ".3f"))
        print(f"   valence {vtxt}   salience {saltxt}{salnote}"
              f"   → 반응={bold(speak)} · {av['path']}")

    # 감정(표현 + 상태 이행)
    heat_new = bold(format(av["heat"]["new"], ".2f"))
    tone_note = dim("톤=" + expr.tone + " · 에너지=" + expr.energy)
    print(f"   {bold(expr.face)}  {tone_note}"
          f"   E {output.E:+.2f}  A {output.A:.2f}"
          f"   열기 {av['heat']['prev']:.2f}→{heat_new}")

    if VERBOSE:
        print(dim("   ┈┈ 원본 Arbiter 로그 ┈┈"))
        for ln in arb_log.splitlines():
            print(dim("   " + ln))


def main():
    if FOLLOW_TEAM not in (100, 200):
        print(f"RT_FOLLOW_TEAM 은 100 또는 200 이어야 합니다 (현재 {FOLLOW_TEAM}).")
        return
    print(bold(f"\n▶ 실시간 감정 스트림 — 캐릭터 {CFG.name} ({CFG.archetype})"))
    print(dim(f"  설정값: 팬심 {FAN:.0f} · 친밀도 {INTIMACY:.0f} · "
              f"팔로우 팀 {FOLLOW_TEAM} · {TICK_SECONDS:.0f}초/틱  (env 로 조정)"))

    events = load_events()
    if not events:
        print("이벤트를 찾지 못했습니다 (raw_frame CSV 확인).")
        return
    if MAX_EVENTS > 0:
        events = events[:MAX_EVENTS]
    print(dim(f"  온톨로지 실시간 데이터: 주목 이벤트 {len(events)}개 로드 · 시간순 재생\n"))

    # 상태: E/A/열기는 턴을 넘겨 이어짐. 친밀도는 설정값으로 고정.
    state = AffectState(E=0.0, A=0.15, heat=0.0, intimacy=INTIMACY)
    total = len(events)
    for i, p in enumerate(events, 1):
        kind, inten, desc = map_event(p, FOLLOW_TEAM)
        pool = [Candidate(kind, inten, source="delta",
                          payload={"event": desc, "team": f"T{FOLLOW_TEAM}"})]
        arb_log, _aff, req, output, next_state, winners, av = trace_turn(
            state, pool, CFG, turn_id="rt", tick=i, user_spoke=False,
            dt=DT, fan=FAN, fan_target=True)
        expr = express(next_state)
        render_tick(i, total, p.get("gameTime"), desc, kind, inten, av, output, expr, arb_log)

        # 다음 턴 기저로 이행 (E/A/열기 이어받고, 친밀도는 고정)
        state = AffectState(E=next_state.E, A=next_state.A,
                            heat=next_state.heat, intimacy=INTIMACY)
        if i < total:
            time.sleep(TICK_SECONDS)

    print(bold("\n■ 재생 종료"))


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print("\n종료")
