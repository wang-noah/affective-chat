"""
playground.py — 소스층 컨트롤 패널 (입력 분리 + 결정론 대사)
============================================================
노션 문서의 소스층 입력을 개별 컨트롤로 분리하고, 각 입력에 '포함' 체크박스를 둔다.
체크된 입력만 SEND 요청으로 전송되고, 빠진 입력은 중립값(신규 유저처럼)으로 처리된다.

대사는 LLM 을 거치지 않는다. affect engine 출력값(winner kind + E/A/열기/친밀도)을
결정론 규칙/템플릿에 매핑해 만든다 (노션 'T1 = LLM 0' 철학).

  python3 playground.py        # -> http://localhost:8765
"""
from __future__ import annotations
import csv
import json
import math
import os
import sys
from dataclasses import replace

csv.field_size_limit(sys.maxsize)
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from config import load_config
from affect_engine import (
    AffectState, Candidate, openness_baseline,
    salience, affect_mod, appraise, arbitrate, _decay_toward, _clamp,
    APPRAISAL_TABLE,
)
from expression import express
from engine import speech_primary
import chat

CFG = load_config(os.path.join(os.path.dirname(__file__), "character.json"))
KINDS = ["greeting", "smalltalk", "game_positive", "game_negative",
         "user_distress", "compliment", "insult", "goal_follow_nudge"]


# ---- 메시지 키워드 → kind 자동 분류 -----------------------------------------
_KEYWORDS = [
    ("insult",           ["바보", "멍청", "꺼져", "닥쳐", "못생", "미워", "재수", "한심", "찌질"]),
    ("compliment",       ["고마", "멋지", "멋있", "예뻐", "예쁘", "최고", "잘했", "대단", "사랑",
                          "좋아해", "귀여", "짱", "고생했"]),
    ("user_distress",    ["짜증", "힘들", "힘드", "우울", "슬퍼", "슬프", "화나", "열받", "지쳐",
                          "지친", "속상", "스트레스", "죽겠", "빡쳐", "눈물", "외로", "포기"]),
    ("game_positive",    ["이겼", "이김", "우승", "바론", "드래곤", "에이스", "역전승", "캐리", "승리", "꿀잼"]),
    ("game_negative",    ["졌", "패배", "던졌", "짤려", "트롤", "망했", "역전패", "지고"]),
    ("goal_follow_nudge", ["팔로우", "팔로", "follow"]),
    ("greeting",         ["안녕", "하이", "ㅎㅇ", "안뇽", "왔어", "또 왔", "잘 가", "잘가",
                          "바이", "ㅂㅂ", "반가", "오랜만"]),
]


# ---- 실시간 델타: CSV 게임 이벤트 → 자극 매핑 -------------------------------
# 관점 팀(PERSPECTIVE_TEAM)을 '우리/팔로우 팀'으로 본다. Riot 이벤트를
# game_positive/game_negative + 강도로 환산해 패널 자극으로 연결한다.
PERSPECTIVE_TEAM = 100
CSV_PATH = os.path.join(os.path.dirname(__file__), "raw_frame_202606281750.csv")
_MAJOR_MONSTER = {"baron": (1.0, "바론"), "dragon": (0.9, "드래곤"),
                  "elderDragon": (1.0, "장로 드래곤"), "riftHerald": (0.85, "전령"),
                  "voidGrub": (0.55, "공허충"), "horde": (0.55, "공허충")}
_TURRET_TIER = {"outer": 0.6, "inner": 0.7, "base": 0.85, "nexus": 1.0}


def _pteam(pid):
    return 100 if isinstance(pid, int) and 1 <= pid <= 5 else 200


def map_event(p: dict, team: int = PERSPECTIVE_TEAM):
    """게임 이벤트 payload → (kind, intensity, 설명) 또는 None(델타 아님).
    건물/포탑 teamID 는 '잃은(소유) 팀'으로 가정한다."""
    s = p.get("rfc461Schema")

    def kind(is_pos):
        return "game_positive" if is_pos else "game_negative"

    if s == "epic_monster_kill":
        mt = p.get("monsterType")
        if mt not in _MAJOR_MONSTER:
            return None              # 정글 잡몹(raptor 등)은 델타로 안 침
        inten, nm = _MAJOR_MONSTER[mt]
        return kind(p.get("killerTeamID") == team), inten, f"{nm} 처치"
    if s == "champion_kill":
        b = p.get("bounty") or 0
        return kind(p.get("killerTeamID") == team), min(0.95, 0.65 + (0.1 if b >= 300 else 0)), "챔피언 킬"
    if s == "champion_kill_special":
        kt = p.get("killType", "")
        return kind(_pteam(p.get("killer")) == team), 0.85 if kt == "firstBlood" else 0.8, kt or "특수 킬"
    if s == "building_destroyed":
        bt, tier = p.get("buildingType"), p.get("turretTier")
        inten = 0.8 if bt == "inhibitor" else _TURRET_TIER.get(tier, 0.7)
        nm = f"{tier} 타워" if bt == "turret" else (bt or "건물")
        return kind(p.get("teamID") != team), inten, f"{nm} 파괴"
    if s == "turret_plate_destroyed":
        return kind(p.get("teamID") != team), 0.45, "포탑 방패"
    if s == "game_end":
        win = p.get("winningTeam") == team
        return kind(win), 1.0, "게임 종료(승)" if win else "게임 종료(패)"
    return None


def load_events(path: str = CSV_PATH, team: int = PERSPECTIVE_TEAM, cap: int = 400):
    out = []
    if not os.path.exists(path):
        return out
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            try:
                p = json.loads(row["payload"])
            except Exception:
                continue
            m = map_event(p, team)
            if not m:
                continue
            k, inten, desc = m
            gt = (p.get("gameTime") or 0) / 1000.0
            sign = "＋" if k == "game_positive" else "－"
            out.append({"seq": p.get("sequenceIndex"), "gt": gt, "kind": k,
                        "intensity": round(inten, 2),
                        "label": f"{int(gt // 60):02d}:{int(gt % 60):02d} {desc} {sign}"})
    out.sort(key=lambda e: e["gt"])
    return out[:cap]


EVENTS = load_events()


def classify_kind(text: str) -> str:
    t = (text or "").lower()
    for kind, kws in _KEYWORDS:
        if any(k.lower() in t for k in kws):
            return kind
    return "smalltalk"


def fan_grade(score: float) -> str:
    for thr, name in [(800, "다이아"), (500, "플래티넘"), (200, "골드"), (50, "실버")]:
        if score >= thr:
            return name
    return "브론즈"


def _payload_for(kind: str, text: str) -> dict:
    if kind in ("game_positive", "game_negative"):
        return {"team": "T1", "event": "바론", "text": text}
    return {"text": text}


# 대사는 LLM/문장 뱅크를 쓰지 않는다. affect engine 출력값을 JSON 으로 그대로 내보낸다.


# ---- 어펙트 엔진 한 턴 트레이스 ---------------------------------------------
def trace_turn(state: AffectState, pool: list, cfg, dt: float = 1.0):
    """-> (arbiter_log, affect_log, result_state, winners)"""
    E = _decay_toward(state.E, cfg.valence_bias, cfg.decay_E, dt)
    A = _decay_toward(state.A, 0.15, cfg.decay_A, dt)
    op = _decay_toward(state.openness, openness_baseline(state.intimacy), 0.3, dt)
    work = replace(state, E=E, A=A, openness=op)

    arb = ["※ decay 직후 상태 기준으로 끌림을 계산한다.", "",
           "salience  (base × weight × recency × mood = score)"]
    if not pool:
        arb.append("   (후보 없음 — 소스층 입력이 비었음)")
    score_by_id = {}
    for c in pool:
        base, weight = c.intensity, cfg.weight(c.kind)
        recency = math.exp(-cfg.backlog_decay * c.age)
        mood = affect_mod(c.kind, work)
        score = salience(c, work, cfg)
        score_by_id[id(c)] = score
        arb.append(f"   {c.kind:<18} {base:.2f} × {weight:.2f} × {recency:.2f} × {mood:.2f} = {score:.3f}")
    winners, losers = arbitrate(pool, work, cfg)
    pol = cfg.select_policy
    arb += ["", f"arbiter  (policy={pol.type}, thr={pol.threshold}, max={pol.max_winners})"]
    for c in winners:
        arb.append(f"   WIN   {c.kind:<18} ({score_by_id[id(c)]:.3f})")
    for c in losers:
        tag = "thr 미만" if score_by_id[id(c)] < pol.threshold else "순위 밀림"
        arb.append(f"   lose  {c.kind:<18} ({score_by_id[id(c)]:.3f})  {tag}")

    aff = ["decay (지난 기분이 기저로 식음)",
           f"   E {state.E:+.2f}→{E:+.2f}   A {state.A:.2f}→{A:.2f}   "
           f"열기 {state.openness:.2f}→{op:.2f}   (기저열기={openness_baseline(state.intimacy):.2f})",
           "", "appraise + integrate (이긴 자극 → 기분 갱신)"]
    E2, A2, op2 = E, A, op
    for w in winners:
        dE, dA = appraise(w, cfg)
        val = APPRAISAL_TABLE.get(w.kind, (0.0, 0.3))[0]
        E2 = _clamp(E2 + dE, -1.0, 1.0)
        A2 = _clamp(A2 + dA, 0.0, 1.0)
        op2 = _clamp(op2 + max(0.0, dE) * cfg.warmup, 0.0, 1.0)
        aff.append(f"   {w.kind:<18} valence {val:+.2f} → dE {dE:+.3f}, dA {dA:+.3f}")
    if not winners:
        aff.append("   (반응할 자극 없음)")
    aff.append(f"   결과 상태   E {E:+.2f}→{E2:+.2f}   A {A:.2f}→{A2:.2f}   열기 {op:.2f}→{op2:.2f}")
    result = {"E": round(E2, 3), "A": round(A2, 3), "openness": round(op2, 3)}
    return "\n".join(arb), "\n".join(aff), result, winners


def compute(q: dict) -> dict:
    def has(name):
        return name in q

    def fv(name, default):
        try:
            return float(q[name][0])
        except (KeyError, TypeError, ValueError):
            return default

    # ── 소스층 입력 (체크된 것만 q 에 들어온다. 빠지면 중립값) ──────────────
    src = []          # 소스층 요약 로그
    # 유저 발화 + 자동 분류
    text = q["text"][0] if has("text") else None
    auto = q.get("auto", ["0"])[0] == "1"
    # 대화 기록 (반복 횟수)
    repeat = int(fv("repeat", 0)) if has("repeat") else 0
    # 유저 팬심
    fan = fv("fan", 0.0) if has("fan") else 0.0
    # 친밀도
    intimacy = fv("intimacy", 0.0) if has("intimacy") else 0.0
    # 온톨로지 토픽
    onto = q["onto"][0] if has("onto") else ""
    # 실시간 델타 (게임 이벤트): CSV 실데이터 우선, 없으면 합성 드롭다운
    evt_idx = q["evt"][0] if has("evt") and q["evt"][0] not in ("", "-1") else None
    delta_label = None
    if evt_idx is not None and EVENTS and 0 <= int(evt_idx) < len(EVENTS):
        ev = EVENTS[int(evt_idx)]
        game, gint, delta_label = ev["kind"], ev["intensity"], "CSV " + ev["label"]
    else:
        game = q["game"][0] if has("game") and q["game"][0] not in ("", "(없음)") else None
        gint = fv("gint", 0.7)
        delta_label = f"합성 {game}" if game else None
    # 팔로잉
    following = ["T1"] if (has("following") and q["following"][0] == "1") else []
    # 열기
    openness = fv("openness", 0.0) if has("openness") else 0.0
    # 어펙트 상태값
    E = fv("E", 0.0) if has("E") else 0.0
    A = fv("A", 0.15) if has("A") else 0.15

    state = AffectState(E=E, A=A, openness=openness, intimacy=intimacy)
    expr = express(state)

    # ── 후보 풀 (소스층 → 어펙트 입력) ────────────────────────────────────
    pool = []
    primary_kind = None
    if text is not None:
        primary_kind = classify_kind(text) if auto else q.get("kind", ["smalltalk"])[0]
        pool.append(Candidate(primary_kind, 0.7, payload=_payload_for(primary_kind, text)))
    if game:
        pool.append(Candidate(game, gint, payload=_payload_for(game, "")))
    # 목표 엔진: 신규(친밀도<0.5) + 팔로잉 없음 → 팔로우 유도
    goal_fired = False
    if intimacy < 0.5 and not following:
        pool.append(Candidate("goal_follow_nudge", 0.5, is_goal=True))
        goal_fired = True

    # 대화기록(반복) → skip_gate 변주 트리거용 history
    history = [{"kind": primary_kind, "route": "T1", "text": ""} for _ in range(repeat)] \
        if primary_kind else []

    arbiter_log, affect_log, trace_result, winners = trace_turn(state, pool, CFG)

    # ── 대사 = affect engine 출력 (JSON, LLM 0) ───────────────────────────
    # 자유 발화 우선: winner 중 자유 발화가 있으면 그것이 발화 대상 (engine.speech_primary)
    primary = speech_primary(winners)
    # skip_gate 는 결정론 분기 판정만 한다 (LLM 호출 X). 대화기록(반복)이 여기에 작용.
    route = chat.skip_gate(primary, history) if primary else "—"
    priority = bool(winners and primary is not winners[0])  # 자유발화 우선이 살리언스 1등을 덮었나
    affect_output = {
        "winners": [w.kind for w in winners],
        "speech_primary": primary.kind if primary else None,
        "freeform_priority": priority,
        "skip_gate": route,            # T1=템플릿 / LLM=캐스케이드 (실제 호출은 안 함)
        "affect_state": {"E": round(state.E, 3), "A": round(state.A, 3),
                         "openness": round(state.openness, 3),
                         "intimacy": round(state.intimacy, 3)},
        "expression": {"face": expr.face, "energy": expr.energy, "tone": expr.tone,
                       "effect_color": expr.effect_color, "particles": expr.particles},
        "next_state": trace_result,    # appraise 후 상태
    }

    # ── 소스층 요약 로그 ──────────────────────────────────────────────────
    def row(on, label, val):
        src.append(f"{'☑' if on else '☐'} {label:<14} {val}")
    row(text is not None, "유저 발화", f'"{text}" → kind={primary_kind}' if text is not None else "(미입력)")
    row(has("repeat"), "대화 기록", f"같은 발화 {repeat}회 반복" if has("repeat") else "(미입력→0)")
    row(has("fan"), "유저 팬심", f"{int(fan)}점 → {fan_grade(fan)}" if has("fan") else "(미입력→0)")
    row(has("intimacy"), "친밀도", f"{intimacy:.1f}" if has("intimacy") else "(미입력→0)")
    row(has("onto"), "온톨로지", f'"{onto}" (표시용·미연결)' if has("onto") else "(미입력)")
    row(game is not None, "실시간 델타", f"{delta_label} → {game} (강도 {gint:.2f})" if game else "(미입력/없음)")
    row(has("following"), "팔로잉", ("팔로우함" if following else "팔로우 안 함") if has("following") else "(미입력→없음)")
    row(has("openness"), "열기/인텐스", f"{openness:.2f}" if has("openness") else "(미입력→0)")
    row(has("E"), "정서 E", f"{E:+.2f}" if has("E") else "(미입력→0)")
    row(has("A"), "세기 A", f"{A:.2f}" if has("A") else "(미입력→0.15)")
    if goal_fired:
        src.append("→ 목표엔진: 신규+팔로잉없음 → '팔로우 유도' 자극 생성")

    stage, stage_dir = chat._intimacy_stage(intimacy)
    return {
        "face": expr.face, "energy": expr.energy, "tone": expr.tone,
        "effect_color": expr.effect_color, "particles": expr.particles,
        "stage": stage, "stage_dir": stage_dir,
        "baseline_openness": round(openness_baseline(intimacy), 3),
        "affect_output": affect_output,
        "kind_used": primary_kind or "", "auto": auto,
        "source_log": "\n".join(src),
        "trace_arbiter": arbiter_log, "trace_affect": affect_log,
        "trace_result": trace_result,
    }


HTML = """<!doctype html><html lang=ko><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>루나 — 소스층 컨트롤 패널</title>
<style>
  :root{color-scheme:dark}*{box-sizing:border-box}
  body{margin:0;font:14px/1.5 -apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo",sans-serif;background:#0e1116;color:#e6edf3}
  .wrap{display:grid;grid-template-columns:360px 1fr;min-height:100vh}
  .panel{background:#161b22;border-right:1px solid #30363d;padding:20px;overflow:auto}
  .out{padding:24px;overflow:auto}
  h1{font-size:15px;margin:0 0 2px}.sub{color:#8b949e;font-size:12px;margin-bottom:14px}
  .grp{margin:18px 0 6px;font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:#d2a8ff;border-top:1px solid #30363d;padding-top:14px}
  .src{margin:11px 0}
  .src .top{display:flex;align-items:center;gap:7px}
  .src .top input[type=checkbox]{accent-color:#2ea043}
  .src label{font-size:13px;color:#adbac7;font-weight:600;margin:0;flex:1}
  .src .v{font-variant-numeric:tabular-nums;color:#58a6ff;font-weight:700;font-size:12px}
  input[type=range]{width:100%;accent-color:#58a6ff;margin-top:4px}
  select,input[type=text]{width:100%;padding:7px;background:#0e1116;color:#e6edf3;border:1px solid #30363d;border-radius:6px;font-size:13px;margin-top:4px}
  .sub2{display:flex;align-items:center;gap:6px;font-size:12px;color:#8b949e;margin-top:5px}
  button{margin-top:6px;width:100%;padding:8px;border:1px solid #30363d;border-radius:7px;background:#21262d;color:#e6edf3;cursor:pointer;font-size:12px}
  button:hover{background:#30363d}
  #send{margin-top:20px;background:#238636;border-color:#2ea043;color:#fff;font-size:14px;font-weight:700;padding:11px}
  .face{font-size:78px;line-height:1;text-align:center;margin:4px 0}
  .badges{display:flex;gap:8px;justify-content:center;flex-wrap:wrap;margin-bottom:6px}
  .badge{padding:4px 12px;border-radius:999px;font-size:12px;font-weight:700;background:#21262d;border:1px solid #30363d}
  .dots{text-align:center;font-size:20px;letter-spacing:3px;height:24px}
  .stage{text-align:center;color:#d2a8ff;font-weight:700;margin:6px 0 2px}
  .stage small{display:block;color:#8b949e;font-weight:400;font-size:12px}
  .grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}@media(max-width:1100px){.grid2{grid-template-columns:1fr}}
  .card{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:15px;margin-top:16px}
  .card.arb{border-color:#1f6feb55}.card.aff{border-color:#d2a8ff55}.card.src{border-color:#2ea04355}
  .card h2{font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:#8b949e;margin:0 0 9px}
  .card.arb h2{color:#58a6ff}.card.aff h2{color:#d2a8ff}.card.src h2{color:#3fb950}
  pre{margin:0;white-space:pre-wrap;font:12.5px/1.6 ui-monospace,Menlo,monospace;color:#adbac7}
  pre.affjson{color:#7ee787}
  .user{color:#8b949e;font-size:13px;margin-bottom:6px}
  .dialogue{font-size:19px;font-weight:700;color:#7ee787}
  .mode{float:right;font-size:11px;color:#8b949e;font-weight:400;text-transform:none}
  #hint{font-size:11px;color:#8b949e;margin-top:8px;text-align:center;min-height:14px}
</style>
<div class=wrap>
 <div class=panel>
  <h1>소스층 컨트롤</h1>
  <div class=sub>체크된 입력만 SEND 로 전송됨</div>

  <div class=grp>소스층 입력 (노션 7가지)</div>

  <div class=src><div class=top><input type=checkbox class=use id=use_text checked><label>유저 발화 (대화)</label></div>
    <input type=text id=text value="오늘 회사에서 진짜 짜증났어">
    <div class=sub2><input type=checkbox id=auto checked><label for=auto style="font-weight:400">키워드로 kind 자동 분류</label></div>
    <select id=kind></select></div>

  <div class=src><div class=top><input type=checkbox class=use id=use_repeat checked><label>대화 기록 (반복)</label><span class=v id=repeatv></span></div>
    <input type=range id=repeat min=0 max=5 step=1 value=0></div>

  <div class=src><div class=top><input type=checkbox class=use id=use_fan checked><label>유저 팬심</label><span class=v id=fanv></span></div>
    <input type=range id=fan min=0 max=1000 step=10 value=100></div>

  <div class=src><div class=top><input type=checkbox class=use id=use_intimacy checked><label>친밀도</label><span class=v id=iv></span></div>
    <input type=range id=intimacy min=0 max=10 step=.1 value=1></div>

  <div class=src><div class=top><input type=checkbox class=use id=use_onto checked><label>온톨로지 토픽</label></div>
    <input type=text id=onto value="롤 e스포츠"></div>

  <div class=src><div class=top><input type=checkbox class=use id=use_game checked><label>실시간 델타 (게임)</label><span class=v id=gintv></span></div>
    <select id=evt></select>
    <select id=game></select>
    <input type=range id=gint min=0 max=1 step=.05 value=.9>
    <div class=sub2 id=evtnote>CSV 이벤트 선택 시 합성/강도 무시 · 관점=팀100</div></div>

  <div class=src><div class=top><input type=checkbox class=use id=use_following checked><label>팔로잉</label></div>
    <div class=sub2><input type=checkbox id=following><label for=following style="font-weight:400">팀 팔로우함</label></div></div>

  <div class=src><div class=top><input type=checkbox class=use id=use_openness checked><label>열기 / 인텐스</label><span class=v id=ov></span></div>
    <input type=range id=openness min=0 max=1 step=.05 value=.3>
    <button id=autoOpen style="font-size:11px">↳ 친밀도 기저값으로 자동설정</button></div>

  <div class=grp>어펙트 상태값 (E·A)</div>
  <div class=src><div class=top><input type=checkbox class=use id=use_E checked><label>정서 E</label><span class=v id=Ev></span></div>
    <input type=range id=E min=-1 max=1 step=.05 value=.1></div>
  <div class=src><div class=top><input type=checkbox class=use id=use_A checked><label>세기 A</label><span class=v id=Av></span></div>
    <input type=range id=A min=0 max=1 step=.05 value=.4></div>

  <button id=send>▶ SEND — 한 턴 실행</button>
  <div id=hint></div>
 </div>

 <div class=out>
  <div class=face id=face>😐</div>
  <div class=badges>
    <span class=badge id=b_tone>tone</span><span class=badge id=b_energy>energy</span><span class=badge id=b_color>color</span>
  </div>
  <div class=dots id=dots></div>
  <div class=stage id=stage></div>

  <div class="card src"><h2>소스층 입력 요약 (이번 SEND)</h2><pre id=srclog></pre></div>

  <div class=grid2>
    <div class="card arb"><h2>① Salience · Arbiter <span style="float:right;font-weight:400;text-transform:none;color:#8b949e">주목</span></h2><pre id=arb></pre></div>
    <div class="card aff"><h2>② Affect Engine <button id=applyTurn style="float:right;width:auto;margin:0;padding:3px 9px;font-size:11px">▶ 이 턴 적용</button></h2><pre id=aff></pre></div>
  </div>

  <div class=card><h2>대사 = affect engine 출력 (JSON · LLM 0)</h2>
    <pre id=affjson class=affjson></pre></div>
 </div>
</div>
<script>
const KINDS = __KINDS__;
const $=id=>document.getElementById(id);
KINDS.forEach(k=>{const o=document.createElement('option');o.value=o.textContent=k;if(k==='user_distress')o.selected=true;$('kind').appendChild(o)});
['(없음)','game_positive','game_negative'].forEach(k=>{const o=document.createElement('option');o.value=o.textContent=k;$('game').appendChild(o)});
// CSV 실시간 델타 이벤트 채우기
fetch('/api/events').then(r=>r.json()).then(list=>{
  const e=$('evt');const o0=document.createElement('option');o0.value='-1';o0.textContent='(합성 사용)';e.appendChild(o0);
  list.forEach((ev,i)=>{const o=document.createElement('option');o.value=i;o.textContent=ev.label+' ['+ev.kind.replace('game_','')+' '+ev.intensity+']';e.appendChild(o)});
  $('evtnote').textContent='CSV 이벤트 '+list.length+'개 로드됨 · 선택 시 합성/강도 무시 · 관점=팀100';
});

const COLOR={warm:'#f0883e',cool:'#58a6ff'};
let lastResult=null;
function setHint(m){$('hint').textContent=m;}
function dirty(){setHint('● 입력 변경됨 — SEND 를 누르세요');}

function syncLabels(){
  $('Ev').textContent=(+$('E').value).toFixed(2);
  $('Av').textContent=(+$('A').value).toFixed(2);
  $('ov').textContent=(+$('openness').value).toFixed(2);
  $('iv').textContent=(+$('intimacy').value).toFixed(1);
  $('repeatv').textContent=$('repeat').value+'회';
  $('fanv').textContent=$('fan').value+'점';
  $('gintv').textContent=(+$('gint').value).toFixed(2);
  $('kind').disabled=$('auto').checked;
}
function render(d){
  $('face').textContent=d.face;
  $('b_tone').textContent='톤 · '+d.tone;
  $('b_energy').textContent='에너지 · '+d.energy;
  $('b_color').textContent='이펙트 · '+d.effect_color;
  $('b_color').style.borderColor=COLOR[d.effect_color];
  $('dots').textContent='●'.repeat(d.particles);$('dots').style.color=COLOR[d.effect_color];
  $('stage').innerHTML='친밀도 단계: '+d.stage+'<small>'+d.stage_dir+' · 친밀도 기저 열기 ≈ '+d.baseline_openness+'</small>';
  $('srclog').textContent=d.source_log;
  $('arb').textContent=d.trace_arbiter;$('aff').textContent=d.trace_affect;
  $('affjson').textContent=JSON.stringify(d.affect_output,null,2);
  if(d.auto&&d.kind_used)$('kind').value=d.kind_used;
  lastResult=d.trace_result;
}
function run(){
  syncLabels();setHint('계산 중…');
  const P=new URLSearchParams();
  if($('use_text').checked){P.set('text',$('text').value);P.set('auto',$('auto').checked?'1':'0');P.set('kind',$('kind').value);}
  if($('use_repeat').checked)P.set('repeat',$('repeat').value);
  if($('use_fan').checked)P.set('fan',$('fan').value);
  if($('use_intimacy').checked)P.set('intimacy',$('intimacy').value);
  if($('use_onto').checked)P.set('onto',$('onto').value);
  if($('use_game').checked){P.set('game',$('game').value);P.set('gint',$('gint').value);P.set('evt',$('evt').value);}
  if($('use_following').checked)P.set('following',$('following').checked?'1':'0');
  if($('use_openness').checked)P.set('openness',$('openness').value);
  if($('use_E').checked)P.set('E',$('E').value);
  if($('use_A').checked)P.set('A',$('A').value);
  fetch('/api/compute?'+P).then(r=>r.json()).then(d=>{render(d);setHint('');});
}
// 모든 입력 변경 → 라벨 갱신 + dirty (계산은 SEND 때만)
document.querySelectorAll('input,select').forEach(el=>el.addEventListener('input',()=>{syncLabels();dirty();}));
$('text').addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();run();}});
$('send').addEventListener('click',run);
$('autoOpen').addEventListener('click',()=>{const i=+$('intimacy').value,b=1/(1+Math.exp(-(i-5)/2));$('openness').value=b.toFixed(2);syncLabels();dirty();});
$('applyTurn').addEventListener('click',()=>{if(!lastResult)return;
  $('use_E').checked=$('use_A').checked=$('use_openness').checked=true;
  $('E').value=lastResult.E;$('A').value=lastResult.A;$('openness').value=lastResult.openness;
  syncLabels();setHint('● 결과 상태 적용됨 — SEND 로 다음 턴');});
syncLabels();run();
</script></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            html = HTML.replace("__KINDS__", json.dumps(KINDS, ensure_ascii=False))
            self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
        elif parsed.path == "/api/events":
            self._send(200, json.dumps(EVENTS, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
        elif parsed.path == "/api/compute":
            data = compute(parse_qs(parsed.query))
            self._send(200, json.dumps(data, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
        else:
            self._send(404, b"not found", "text/plain")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8765"))
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"▶ 소스층 컨트롤 패널: http://localhost:{port}  (Ctrl+C 종료)")
    print(f"  캐릭터: {CFG.name} ({CFG.archetype}) · 실시간 델타 이벤트 {len(EVENTS)}개 로드")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n종료")
