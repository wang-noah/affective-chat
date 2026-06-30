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

csv.field_size_limit(sys.maxsize)
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from config import load_config
from affect_engine import AffectState, affect, openness_baseline, _decay_toward
from arbiter import Candidate, build_request, select_score, affect_mod, map_event
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


# ---- 실시간 델타: CSV 게임 이벤트 로딩 (변환 로직은 arbiter.map_event) -------
# Riot 이벤트 → 자극 변환은 Arbiter 책임(arbiter.map_event). 여기선 CSV 를 읽어
# 패널 드롭다운에 채우는 plumbing 만 한다.
CSV_PATH = os.path.join(os.path.dirname(__file__), "raw_frame_202606281750.csv")

# 원시 이벤트는 팀 무관하게 저장하고, 관점(team)별 매핑은 요청 때 한다.
_EVT_KEYS = ("rfc461Schema", "monsterType", "killerTeamID", "victimTeamID", "teamID",
             "winningTeam", "killer", "bounty", "buildingType", "turretTier",
             "gameTime", "sequenceIndex")


def load_events_raw(path: str = CSV_PATH, cap: int = 400):
    out = []
    if not os.path.exists(path):
        return out
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            try:
                p = json.loads(row["payload"])
            except Exception:
                continue
            if map_event(p, 100) is None:    # notable 여부는 팀과 무관
                continue
            out.append({k: p.get(k) for k in _EVT_KEYS})
    out.sort(key=lambda d: (d.get("gameTime") or 0))
    return out[:cap]


EVENTS_RAW = load_events_raw()


def event_view(d: dict, team: int) -> dict:
    """원시 이벤트 + 관점 팀 → {label, kind, intensity}."""
    k, inten, desc = map_event(d, team)
    gt = (d.get("gameTime") or 0) / 1000.0
    sign = "○" if team == 0 else ("＋" if k == "game_positive" else "－")
    return {"label": f"{int(gt // 60):02d}:{int(gt % 60):02d} {desc} {sign}",
            "kind": k, "intensity": inten}


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


# ---- Arbiter → Affect 한 턴 트레이스 ----------------------------------------
def _stim_dict(st):
    if st is None:
        return None
    return {"moment_id": st.moment_id, "type": st.type, "valence": st.valence,
            "salience": st.salience, "source": st.source, "tags": st.tags}


def trace_turn(state: AffectState, pool: list, cfg, turn_id: str, tick: int,
               user_spoke: bool, dt: float = 1.0):
    """-> (arbiter_log, affect_log, req, output, new_state, winners)"""
    prev = {"E": state.E, "A": state.A, "openness": state.openness}

    # ── ① Arbiter: 선택(prev_affect 되먹임) + AffectRequest 조립 ──────────
    arb = ["Arbiter — Riot 흡수 + 선택 (직전 감정 prev_affect 로 되먹임)", "",
           "select_score  (intensity × weight × recency × mood)"]
    if not pool:
        arb.append("   (후보 없음 — 소스층 입력이 비었음)")
    score_by_id = {}
    for c in pool:
        recency = math.exp(-cfg.backlog_decay * c.age)
        mood = affect_mod(c.kind, prev)
        sc = select_score(c, prev, cfg)
        score_by_id[id(c)] = sc
        arb.append(f"   {c.kind:<18} {c.intensity:.2f} × {cfg.weight(c.kind):.2f} × "
                   f"{recency:.2f} × {mood:.2f} = {sc:.3f}")

    req, winners, losers = build_request(
        char_id=cfg.name, turn_id=turn_id, tick=tick, candidates=pool,
        intimacy=state.intimacy, prev_affect={"E": state.E, "A": state.A},
        prev_openness=state.openness, cfg=cfg, user_spoke=user_spoke)

    pol = cfg.select_policy
    arb += ["", f"select  (policy={pol.type}, thr={pol.threshold}, max={pol.max_winners})"]
    for c in winners:
        arb.append(f"   WIN   {c.kind:<18} ({score_by_id[id(c)]:.3f})")
    for c in losers:
        tag = "thr 미만" if score_by_id[id(c)] < pol.threshold else "순위 밀림"
        arb.append(f"   defer {c.kind:<18} ({score_by_id[id(c)]:.3f})  {tag}")

    arb += ["", "→ AffectRequest (Affect 엔 숫자만 넘긴다)"]

    def stim_line(tag, st):
        if st is None:
            arb.append(f"   {tag}: (없음)")
        else:
            arb.append(f"   {tag}: type={st.type:<6} valence={st.valence:+.2f} "
                       f"salience={st.salience:.3f} src={st.source} tags={st.tags}")
    stim_line("primary  ", req.primary)
    stim_line("secondary", req.secondary)
    arb.append(f"   path={req.path}   route={req.route}")
    arb.append(f"   deferred_ids={req.deferred_ids}")
    arb.append(f"   state: intimacy={req.state.intimacy:.2f}  match_heat={req.state.match_heat:.2f}  "
               f"prev_affect={req.state.prev_affect}")

    # ── ② Affect: 숫자로 기분 계산 (순수 함수) ──────────────────────────
    new_state, output, _expr = affect(req, state, cfg, dt)
    base_E = req.state.prev_affect["E"]
    base_A = req.state.prev_affect["A"]
    E0 = _decay_toward(base_E, cfg.valence_bias, cfg.decay_E, dt)
    A0 = _decay_toward(base_A, 0.15, cfg.decay_A, dt)
    op0 = _decay_toward(state.openness, openness_baseline(state.intimacy), 0.3, dt)
    aff = ["decay (prev_affect 가 기저로 식음)",
           f"   E {base_E:+.2f}→{E0:+.2f}   A {base_A:.2f}→{A0:.2f}   "
           f"열기 {state.openness:.2f}→{op0:.2f}   (기저열기={openness_baseline(state.intimacy):.2f})",
           "", "integrate (valence × salience × 기질 → 기분 갱신)"]
    for st in (req.primary, req.secondary):
        if st is None:
            continue
        dE = st.valence * st.salience * cfg.reactivity
        dA = st.salience * cfg.arousal_gain
        aff.append(f"   {st.type:<8} valence {st.valence:+.2f} × salience {st.salience:.3f} "
                   f"→ dE {dE:+.3f}, dA {dA:+.3f}")
    if req.primary is None:
        aff.append("   (반응할 자극 없음)")
    aff.append(f"   결과   E {output.E:+.2f}   A {output.A:.2f}   "
               f"intensity {output.intensity:.2f}   열기 {new_state.openness:.2f}")
    return "\n".join(arb), "\n".join(aff), req, output, new_state, winners


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
    team = int(fv("team", 100))     # 관점 팀: 100 / 200 / 0(팔로우 없음)
    evt_idx = q["evt"][0] if has("evt") and q["evt"][0] not in ("", "-1") else None
    delta_label = None
    if evt_idx is not None and EVENTS_RAW and 0 <= int(evt_idx) < len(EVENTS_RAW):
        v = event_view(EVENTS_RAW[int(evt_idx)], team)
        game, gint, delta_label = v["kind"], v["intensity"], "CSV " + v["label"]
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
    expr = express(state)              # 입력 상태의 표현 (상단 표시용)

    # ── 후보 풀 (소스층 → Arbiter 입력) ───────────────────────────────────
    pool = []
    primary_kind = None
    user_spoke = text is not None
    if text is not None:
        primary_kind = classify_kind(text) if auto else q.get("kind", ["smalltalk"])[0]
        pool.append(Candidate(primary_kind, 0.7, source="user",
                              payload=_payload_for(primary_kind, text)))
    if game:
        pool.append(Candidate(game, gint, source="delta", payload=_payload_for(game, "")))
    # 목표 엔진: 신규(친밀도<0.5) + 팔로잉 없음 → 팔로우 유도
    goal_fired = False
    if intimacy < 0.5 and not following:
        pool.append(Candidate("goal_follow_nudge", 0.5, source="goal", is_goal=True))
        goal_fired = True

    # 대화기록(반복) → skip_gate 변주 트리거용 history
    history = [{"kind": primary_kind, "route": "T1", "text": ""} for _ in range(repeat)] \
        if primary_kind else []

    turn_id = "t_pg"
    tick = int(fv("tick", 0))
    arbiter_log, affect_log, req, output, next_state, winners = \
        trace_turn(state, pool, CFG, turn_id, tick, user_spoke)
    trace_result = {"E": output.E, "A": output.A, "openness": round(next_state.openness, 3)}

    # ── 대사 = affect engine 출력 (JSON, LLM 0) ───────────────────────────
    # 자유 발화 우선: winner 중 자유 발화가 있으면 그것이 발화 대상 (engine.speech_primary)
    primary = speech_primary(winners)
    priority = bool(winners and primary is not winners[0])  # 자유발화 우선이 살리언스 1등을 덮었나
    # skip_gate(T1/LLM)는 route.chat==true 일 때만 의미. LLM 실제 호출은 안 함.
    skip = (chat.skip_gate(primary, history) if (output.route.get("chat") and primary) else "—")
    affect_output = {
        "request": {                   # ① AffectRequest (Arbiter → Affect)
            "char_id": req.char_id, "turn_id": req.turn_id, "seed": req.seed, "tick": req.tick,
            "primary": _stim_dict(req.primary), "secondary": _stim_dict(req.secondary),
            "path": req.path, "route": req.route, "deferred_ids": req.deferred_ids,
            "state": {"intimacy": req.state.intimacy, "match_heat": req.state.match_heat,
                      "prev_affect": req.state.prev_affect},
        },
        "output": {                    # ② AffectOutput (Affect → 표현/채팅)
            "E": output.E, "A": output.A, "intensity": output.intensity,
            "expression_intent": output.expression_intent,
            "route": output.route, "path": output.path,
            "tags": output.tags, "moment_id": output.moment_id,
        },
        "speech": {                    # 발화 분기 (route.chat 이후 단계)
            "speech_primary": primary.kind if primary else None,
            "freeform_priority": priority,
            "skip_gate": skip,         # T1=템플릿 / LLM=캐스케이드
        },
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
    <div class=sub2>관점 팀
      <select id=team style="width:auto;display:inline-block;margin:0 0 0 6px;padding:3px 6px">
        <option value=100>팀100 (우리)</option>
        <option value=200>팀200</option>
        <option value=0>팔로우 없음(중립)</option>
      </select></div>
    <select id=evt></select>
    <select id=game></select>
    <input type=range id=gint min=0 max=1 step=.05 value=.9>
    <div class=sub2 id=evtnote>CSV 이벤트 선택 시 합성/강도 무시</div></div>

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
// CSV 실시간 델타 이벤트 채우기 (관점 팀에 따라 +/− 라벨이 바뀜)
function loadEvents(){
  const e=$('evt'); const prev=e.value;
  fetch('/api/events?team='+$('team').value).then(r=>r.json()).then(list=>{
    e.innerHTML='';
    const o0=document.createElement('option');o0.value='-1';o0.textContent='(합성 사용)';e.appendChild(o0);
    list.forEach((ev,i)=>{const o=document.createElement('option');o.value=i;o.textContent=ev.label+' ['+ev.kind.replace('game_','')+' '+ev.intensity+']';e.appendChild(o)});
    e.value=prev && prev!=='' ? prev : '-1';   // 선택 인덱스 유지
    $('evtnote').textContent='CSV 이벤트 '+list.length+'개 · 선택 시 합성/강도 무시';
  });
}
$('team').addEventListener('change',()=>{loadEvents();dirty();});
loadEvents();

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
  if($('use_game').checked){P.set('game',$('game').value);P.set('gint',$('gint').value);P.set('evt',$('evt').value);P.set('team',$('team').value);}
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
            qs = parse_qs(parsed.query)
            team = int(qs.get("team", ["100"])[0] or 100)
            data = [event_view(d, team) for d in EVENTS_RAW]
            self._send(200, json.dumps(data, ensure_ascii=False).encode("utf-8"),
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
    print(f"  캐릭터: {CFG.name} ({CFG.archetype}) · 실시간 델타 이벤트 {len(EVENTS_RAW)}개 로드")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n종료")
