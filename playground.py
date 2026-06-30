"""
playground.py — 소스층(어펙트 엔진) 컨트롤 패널
================================================
브라우저에서 E/A/열기/친밀도 등 소스층 수치를 직접 조절하면,
실제 express() / build_context() 가 계산한 결과(표정·톤·파티클·조립 프롬프트·대사)와
어펙트 엔진 한 턴의 트레이스를 실시간으로 보여준다. 표준 라이브러리만 사용.

  python3 playground.py        # -> http://localhost:8765

트레이스는 두 갈래로 나눠 찍는다:
  - Salience · Arbiter : 주목 경쟁 (어디에 반응할지)
  - Affect Engine      : 상태 갱신 (decay → appraise → 통합)

대사는 기본 mock. 라이브 체크 시 ANTHROPIC_API_KEY 있으면 실제 Haiku 호출.
"""
from __future__ import annotations
import json
import math
import os
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from config import load_config
from affect_engine import (
    AffectState, Candidate, openness_baseline,
    salience, affect_mod, appraise, arbitrate, _decay_toward, _clamp,
    APPRAISAL_TABLE,
)
from expression import express
import chat

CFG = load_config(os.path.join(os.path.dirname(__file__), "character.json"))
KINDS = ["greeting", "smalltalk", "game_positive", "game_negative",
         "user_distress", "compliment", "insult", "goal_follow_nudge"]


def _payload_for(kind: str, text: str) -> dict:
    # 게임류는 팀/이벤트 payload 가 필요 -> 데모 기본값 채움
    if kind in ("game_positive", "game_negative"):
        return {"team": "T1", "event": "바론", "text": text}
    return {"text": text}


def trace_turn(state: AffectState, pool: list, cfg, dt: float = 1.0):
    """affect_engine.update() 한 턴을 재현. 계산은 전부 실제 엔진 함수를 그대로 쓴다.
    -> (arbiter_log, affect_log, result_state)
    실행 순서: decay(affect) → salience·arbiter(주목) → appraise·통합(affect).
    """
    # (a) 감쇠 — affect
    E = _decay_toward(state.E, cfg.valence_bias, cfg.decay_E, dt)
    A = _decay_toward(state.A, 0.15, cfg.decay_A, dt)
    op = _decay_toward(state.openness, openness_baseline(state.intimacy), 0.3, dt)
    work = replace(state, E=E, A=A, openness=op)

    # (b) salience + arbiter — 주목
    arb = []
    arb.append("※ decay 직후 상태 기준으로 끌림을 계산한다.")
    arb.append("")
    arb.append("salience  (base × weight × recency × mood = score)")
    score_by_id = {}
    for c in pool:
        base = c.intensity
        weight = cfg.weight(c.kind)
        recency = math.exp(-cfg.backlog_decay * c.age)
        mood = affect_mod(c.kind, work)
        score = salience(c, work, cfg)
        score_by_id[id(c)] = score
        arb.append(f"   {c.kind:<18} {base:.2f} × {weight:.2f} × {recency:.2f} "
                   f"× {mood:.2f} = {score:.3f}")
    winners, losers = arbitrate(pool, work, cfg)
    pol = cfg.select_policy
    arb.append("")
    arb.append(f"arbiter  (policy={pol.type}, thr={pol.threshold}, max={pol.max_winners})")
    for c in winners:
        arb.append(f"   WIN   {c.kind:<18} ({score_by_id[id(c)]:.3f})")
    for c in losers:
        tag = "thr 미만" if score_by_id[id(c)] < pol.threshold else "순위 밀림"
        arb.append(f"   lose  {c.kind:<18} ({score_by_id[id(c)]:.3f})  {tag}")

    # (c) appraise + integrate — affect
    aff = []
    aff.append("decay (지난 기분이 기저로 식음)")
    aff.append(f"   E {state.E:+.2f}→{E:+.2f}   A {state.A:.2f}→{A:.2f}   "
               f"열기 {state.openness:.2f}→{op:.2f}   (기저열기={openness_baseline(state.intimacy):.2f})")
    aff.append("")
    aff.append("appraise + integrate (이긴 자극 → 기분 갱신)")
    E2, A2, op2 = E, A, op
    for w in winners:
        dE, dA = appraise(w, cfg)
        val = APPRAISAL_TABLE.get(w.kind, (0.0, 0.3))[0]
        E2 = _clamp(E2 + dE, -1.0, 1.0)
        A2 = _clamp(A2 + dA, 0.0, 1.0)
        op2 = _clamp(op2 + max(0.0, dE) * cfg.warmup, 0.0, 1.0)
        aff.append(f"   {w.kind:<18} valence {val:+.2f} → dE {dE:+.3f}, dA {dA:+.3f}")
    aff.append(f"   결과 상태   E {E:+.2f}→{E2:+.2f}   A {A:.2f}→{A2:.2f}   열기 {op:.2f}→{op2:.2f}")

    result = {"E": round(E2, 3), "A": round(A2, 3), "openness": round(op2, 3)}
    return "\n".join(arb), "\n".join(aff), result


# ---- 데모 응답 뱅크 (키 없이도 LLM 경로에서 그럴듯한 대사) -------------------
# 자유 발화는 원래 LLM 이 만든다. 키 없는 데모용으로, Claude 가 친밀도 단계별로
# 미리 써둔 대사를 주입한다(= 방식 A 를 패널에 내장). 정형 발화는 chat.respond 의
# T1 템플릿이 실제 텍스트를 만들므로 여기 안 들어온다.
DEMO_LINES = {
    "user_distress": {
        "초면": "아 그래...? 무슨 일 있었는데.",
        "알아가는 중": "엥 뭔데, 무슨 일 있었어?",
        "친한 사이": "헐 왜, 무슨 일 있었어? 짜증 제대로 났나 보네.",
        "절친": "아우 또 뭔데~ 어떤 놈이 우리 열받게 한 거야, 다 말해봐.",
    },
    "smalltalk": {
        "초면": "음, 그렇구나.",
        "알아가는 중": "오 그래? 좀 더 얘기해봐.",
        "친한 사이": "ㅋㅋ 뭐야 그게, 재밌네.",
        "절친": "야 그거 완전 너답다 ㅋㅋㅋ 더 풀어봐~",
    },
    "compliment": {
        "초면": "어... 고마워.",
        "알아가는 중": "헤, 그런 말 들으니까 좋네.",
        "친한 사이": "뭐야~ 갑자기 칭찬이야? 기분 좋다 ㅋㅋ",
        "절친": "아 진짜? 너밖에 없다 진짜~ 좋아 죽겠네 ㅋㅋ",
    },
    "insult": {
        "초면": "...뭐라는 거야.",
        "알아가는 중": "어이없네. 왜 그래 갑자기.",
        "친한 사이": "야 너 진짜 ㅋㅋ 선 넘지 말고.",
        "절친": "또 시작이네 ㅋㅋ 너니까 봐준다.",
    },
    "game_negative": {
        "초면": "아 졌네... 별로다.",
        "알아가는 중": "아 방금 그거 던졌어, 봤어?",
        "친한 사이": "아 미쳤다 진짜 왜 저기서 짤려 ㅠㅠ",
        "절친": "야야 봤어?? 한타 그거 개답답해 진짜 ㅋㅋㅋ",
    },
}
_TONE_FALLBACK = {"distant": "...그렇구나.", "neutral": "음, 그래.",
                  "warm": "오~ 그래그래, 더 말해봐!"}


def _demo_llm(winner, state, expr, cfg) -> str:
    stage, _ = chat._intimacy_stage(state.intimacy)
    bank = DEMO_LINES.get(winner.kind)
    if bank and stage in bank:
        return bank[stage]
    return _TONE_FALLBACK.get(expr.tone, "음, 그래.")


def compute(q: dict) -> dict:
    def f(name, default):
        try:
            return float(q.get(name, [default])[0])
        except (TypeError, ValueError):
            return default

    E = f("E", 0.1)
    A = f("A", 0.4)
    openness = f("openness", 0.3)
    intimacy = f("intimacy", 1.0)
    kind = q.get("kind", ["user_distress"])[0]
    kind2 = q.get("kind2", ["(없음)"])[0]
    int1 = f("int1", 0.7)
    int2 = f("int2", 0.7)
    text = q.get("text", ["오늘 회사에서 진짜 짜증났어"])[0]
    live = q.get("live", ["0"])[0] == "1"

    state = AffectState(E=E, A=A, openness=openness, intimacy=intimacy)
    expr = express(state)

    # 후보 풀 구성 (자극1 + 선택적 경쟁 자극2)
    primary = Candidate(kind, int1, payload=_payload_for(kind, text))
    pool = [primary]
    if kind2 and kind2 != "(없음)":
        pool.append(Candidate(kind2, int2, payload=_payload_for(kind2, text)))

    system, user_text = chat.build_context(primary, state, expr, cfg=CFG)
    stage, stage_dir = chat._intimacy_stage(intimacy)
    arbiter_log, affect_log, trace_result = trace_turn(state, pool, CFG)

    # 실제 스킵 게이트(chat.respond)를 거친다: 정형=T1 템플릿(실제 텍스트), 자유=LLM
    if live:
        try:
            dialogue, route = chat.respond(primary, state, expr, CFG, [], use_llm=chat.call_haiku)
            mode = f"live (haiku) · route={route}"
        except Exception as e:  # 키/requests 없음 -> 데모 응답으로 폴백
            dialogue, route = chat.respond(primary, state, expr, CFG, [], use_llm=_demo_llm)
            mode = f"데모 (라이브 실패: {e}) · route={route}"
    else:
        dialogue, route = chat.respond(primary, state, expr, CFG, [], use_llm=_demo_llm)
        mode = f"데모(curated) · route={route}"

    return {
        "face": expr.face, "energy": expr.energy, "tone": expr.tone,
        "effect_color": expr.effect_color, "particles": expr.particles,
        "stage": stage, "stage_dir": stage_dir,
        "baseline_openness": round(openness_baseline(intimacy), 3),
        "system": system, "user": user_text,
        "dialogue": dialogue, "mode": mode,
        "trace_arbiter": arbiter_log, "trace_affect": affect_log,
        "trace_result": trace_result,
    }


HTML = """<!doctype html><html lang=ko><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>루나 — 소스층 컨트롤 패널</title>
<style>
  :root{color-scheme:dark}
  *{box-sizing:border-box}
  body{margin:0;font:15px/1.5 -apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo",sans-serif;
       background:#0e1116;color:#e6edf3}
  .wrap{display:grid;grid-template-columns:340px 1fr;gap:0;min-height:100vh}
  .panel{background:#161b22;border-right:1px solid #30363d;padding:22px;overflow:auto}
  .out{padding:26px;overflow:auto}
  h1{font-size:16px;margin:0 0 4px}
  .sub{color:#8b949e;font-size:12px;margin-bottom:18px}
  label{display:block;font-size:13px;color:#adbac7;margin:16px 0 6px;font-weight:600}
  .row{display:flex;justify-content:space-between;align-items:baseline}
  .val{font-variant-numeric:tabular-nums;color:#58a6ff;font-weight:700}
  input[type=range]{width:100%;accent-color:#58a6ff}
  select,input[type=text]{width:100%;padding:8px;background:#0e1116;color:#e6edf3;
       border:1px solid #30363d;border-radius:7px;font-size:13px}
  .chk{display:flex;align-items:center;gap:8px;margin-top:16px;font-size:13px;color:#adbac7}
  button{margin-top:8px;width:100%;padding:8px;border:1px solid #30363d;border-radius:7px;
       background:#21262d;color:#e6edf3;cursor:pointer;font-size:12px}
  button:hover{background:#30363d}
  .face{font-size:84px;line-height:1;text-align:center;margin:6px 0}
  .badges{display:flex;gap:8px;justify-content:center;flex-wrap:wrap;margin-bottom:8px}
  .badge{padding:4px 12px;border-radius:999px;font-size:12px;font-weight:700;
       background:#21262d;border:1px solid #30363d}
  .dots{text-align:center;font-size:20px;letter-spacing:3px;height:24px}
  .stage{text-align:center;color:#d2a8ff;font-weight:700;margin:8px 0 2px}
  .stage small{display:block;color:#8b949e;font-weight:400;font-size:12px}
  .grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
  @media(max-width:1100px){.grid2{grid-template-columns:1fr}}
  .card{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:16px;margin-top:18px}
  .card.arb{border-color:#1f6feb55}
  .card.aff{border-color:#d2a8ff55}
  .card h2{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:#8b949e;margin:0 0 10px}
  .card.arb h2{color:#58a6ff}
  .card.aff h2{color:#d2a8ff}
  pre{margin:0;white-space:pre-wrap;font:12.5px/1.6 ui-monospace,Menlo,monospace;color:#adbac7}
  .user{color:#8b949e;font-size:13px;margin-bottom:6px}
  .dialogue{font-size:18px;font-weight:700;color:#7ee787}
  .mode{float:right;font-size:11px;color:#8b949e;font-weight:400;text-transform:none}
</style>
<div class=wrap>
 <div class=panel>
  <h1>소스층 컨트롤</h1>
  <div class=sub>어펙트 엔진 수치 → 실시간 결과</div>

  <div class=row><label for=E>정서 E</label><span class=val id=Ev></span></div>
  <input type=range id=E min=-1 max=1 step=.05 value=.1>

  <div class=row><label for=A>세기 A</label><span class=val id=Av></span></div>
  <input type=range id=A min=0 max=1 step=.05 value=.4>

  <div class=row><label for=openness>열기 openness</label><span class=val id=ov></span></div>
  <input type=range id=openness min=0 max=1 step=.05 value=.3>

  <div class=row><label for=intimacy>친밀도 intimacy</label><span class=val id=iv></span></div>
  <input type=range id=intimacy min=0 max=10 step=.1 value=1>
  <button id=autoOpen>↳ 친밀도 기저값으로 열기 자동설정</button>

  <label for=kind>자극 1 (kind)</label>
  <select id=kind></select>
  <div class=row><span style="font-size:12px;color:#8b949e">강도</span><span class=val id=i1v></span></div>
  <input type=range id=int1 min=0 max=1 step=.05 value=.7>

  <label for=kind2>자극 2 (경쟁, 선택)</label>
  <select id=kind2></select>
  <div class=row><span style="font-size:12px;color:#8b949e">강도</span><span class=val id=i2v></span></div>
  <input type=range id=int2 min=0 max=1 step=.05 value=.7>

  <label for=text>유저 발화</label>
  <input type=text id=text value="오늘 회사에서 진짜 짜증났어">

  <div class=chk><input type=checkbox id=live><label for=live style="margin:0">라이브 (API 키 필요)</label></div>

  <button id=send style="margin-top:20px;background:#238636;border-color:#2ea043;color:#fff;font-size:14px;font-weight:700;padding:11px">▶ SEND — 한 턴 실행</button>
  <div id=hint style="font-size:11px;color:#8b949e;margin-top:8px;text-align:center;min-height:14px"></div>
 </div>

 <div class=out>
  <div class=face id=face>😐</div>
  <div class=badges>
    <span class=badge id=b_tone>tone</span>
    <span class=badge id=b_energy>energy</span>
    <span class=badge id=b_color>color</span>
  </div>
  <div class=dots id=dots></div>
  <div class=stage id=stage></div>

  <div class=grid2>
    <div class="card arb">
      <h2>① Salience · Arbiter <span style="float:right;font-weight:400;text-transform:none;color:#8b949e">주목 경쟁</span></h2>
      <pre id=arb></pre>
    </div>
    <div class="card aff">
      <h2>② Affect Engine
        <button id=applyTurn style="float:right;width:auto;margin:0;padding:3px 9px;font-size:11px">▶ 이 턴 적용</button>
      </h2>
      <pre id=aff></pre>
    </div>
  </div>

  <div class=card>
    <h2>조립된 컨텍스트 (build_context)</h2>
    <pre id=system></pre>
  </div>
  <div class=card>
    <h2>대사 <span class=mode id=mode></span></h2>
    <div class=user id=user></div>
    <div class=dialogue id=dialogue></div>
  </div>
 </div>
</div>
<script>
const KINDS = __KINDS__;
const $=id=>document.getElementById(id);
const sel=$('kind');
KINDS.forEach(k=>{const o=document.createElement('option');o.value=o.textContent=k;
  if(k==='user_distress')o.selected=true;sel.appendChild(o)});
const sel2=$('kind2');
['(없음)'].concat(KINDS).forEach(k=>{const o=document.createElement('option');
  o.value=o.textContent=k;if(k==='goal_follow_nudge')o.selected=true;sel2.appendChild(o)});

const ids=['E','A','openness','intimacy','kind','kind2','int1','int2','text','live'];
const COLOR={warm:'#f0883e',cool:'#58a6ff'};
let lastResult=null;

function render(d){
  $('face').textContent=d.face;
  $('b_tone').textContent='톤 · '+d.tone;
  $('b_energy').textContent='에너지 · '+d.energy;
  $('b_color').textContent='이펙트 · '+d.effect_color;
  $('b_color').style.borderColor=COLOR[d.effect_color];
  $('dots').textContent='●'.repeat(d.particles);
  $('dots').style.color=COLOR[d.effect_color];
  $('stage').innerHTML='친밀도 단계: '+d.stage+'<small>'+d.stage_dir+
     ' · 친밀도 기저 열기 ≈ '+d.baseline_openness+'</small>';
  $('arb').textContent=d.trace_arbiter;
  $('aff').textContent=d.trace_affect;
  $('system').textContent=d.system;
  $('user').textContent='유저: '+d.user;
  $('dialogue').textContent='루나: '+d.dialogue;
  $('mode').textContent=d.mode;
  lastResult=d.trace_result;
}
function setHint(m){$('hint').textContent=m;}
function syncLabels(){           // 숫자 표시만 즉시 갱신 (네트워크 X)
  $('Ev').textContent=(+$('E').value).toFixed(2);
  $('Av').textContent=(+$('A').value).toFixed(2);
  $('ov').textContent=(+$('openness').value).toFixed(2);
  $('iv').textContent=(+$('intimacy').value).toFixed(1);
  $('i1v').textContent=(+$('int1').value).toFixed(2);
  $('i2v').textContent=(+$('int2').value).toFixed(2);
}
function run(){                  // SEND: 실제 한 턴 로직 실행
  syncLabels();
  setHint('계산 중…');
  const p=new URLSearchParams({
    E:$('E').value,A:$('A').value,openness:$('openness').value,
    intimacy:$('intimacy').value,kind:$('kind').value,kind2:$('kind2').value,
    int1:$('int1').value,int2:$('int2').value,text:$('text').value,
    live:$('live').checked?'1':'0'});
  fetch('/api/compute?'+p).then(r=>r.json()).then(d=>{render(d);setHint('');});
}
// 입력 변경 시: 라벨만 갱신 + "변경됨" 표시 (계산은 SEND 때만)
ids.forEach(id=>$(id).addEventListener('input',()=>{syncLabels();setHint('● 입력 변경됨 — SEND 를 누르세요');}));
$('text').addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();run();}});
$('send').addEventListener('click',run);
$('autoOpen').addEventListener('click',()=>{
  const i=+$('intimacy').value, b=1/(1+Math.exp(-(i-5)/2));
  $('openness').value=b.toFixed(2); syncLabels(); setHint('● 입력 변경됨 — SEND 를 누르세요');
});
$('applyTurn').addEventListener('click',()=>{   // 결과 상태를 슬라이더로 옮기고, SEND 로 다음 턴
  if(!lastResult)return;
  $('E').value=lastResult.E; $('A').value=lastResult.A; $('openness').value=lastResult.openness;
  syncLabels(); setHint('● 결과 상태 적용됨 — SEND 로 다음 턴 실행');
});
syncLabels(); run();            // 첫 로드 1회만 실행
</script></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # 콘솔 조용히
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
    print(f"  캐릭터: {CFG.name} ({CFG.archetype})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n종료")
