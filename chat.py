"""
chat.py — 스킵 게이트 + LLM 단일 티어
=====================================
respond() 가 채팅 텍스트를 만든다. 두 갈래뿐(캐스케이드 풀 티어는 MVP 제외):
  - 정형(greeting/game/CTA/상태멘트) -> T1 템플릿, 과금 0
  - 자유 발화(잡담/고민) or 반복 회피 -> LLM 1번 호출(Haiku)

데모는 keyless 로 돌도록 mock 생성기를 쓴다.
실제 호출부 call_haiku() 는 따로 있고, 데모는 호출하지 않는다.
"""
from __future__ import annotations
import os

from config import PersonalityConfig
from affect_engine import AffectState
from arbiter import Candidate, EMOTION_APPRAISAL
from expression import Expression

# 자유 발화 = 유저 텍스트(감정 kind) — 템플릿 없음, LLM 로 흐름.
# 감정 라벨은 arbiter.EMOTION_APPRAISAL 키셋과 일치.
FREEFORM_KINDS = set(EMOTION_APPRAISAL.keys())

# ---- T1 템플릿 (톤별 변주) ---------------------------------------------------
TEMPLATES: dict[str, dict[str, list[str]]] = {
    "greeting": {
        "warm":    ["어? 안녕! 또 왔네~ 반가워 :)", "왔구나! 기다렸잖아~"],
        "neutral": ["안녕. 왔구나.", "어, 안녕."],
        "distant": ["...왔네.", "어."],
    },
    "game_positive": {
        "warm":    ["야 방금 {team} {event} 먹었어!! 봤어?!"],
        "neutral": ["{team} 방금 {event} 먹었어."],
        "distant": ["{team} {event}."],
    },
    "goal_follow_nudge": {
        "warm":    ["어떤 팀 좋아해? 팔로우해두면 소식 바로 알려줄게~"],
        "neutral": ["팔로우해두면 그 팀 소식 떠. 해볼래?"],
        "distant": ["팔로우하면 소식 떠."],
    },
}


def skip_gate(winner: Candidate, history: list[dict]) -> str:
    """'T1' (템플릿) 또는 'LLM' 라우팅 결정."""
    if winner.kind in FREEFORM_KINDS:
        return "LLM"
    # 반복 회피: 같은 정형 발화가 최근 3번 이상 -> 변주 위해 LLM 승격 (문서 5-2)
    recent = [h for h in history[-5:] if h.get("kind") == winner.kind and h.get("route") == "T1"]
    if len(recent) >= 3:
        return "LLM"
    if winner.kind in TEMPLATES:
        return "T1"
    return "LLM"


def _fill_template(winner: Candidate, expr: Expression, history: list[dict]) -> str:
    variants = TEMPLATES[winner.kind].get(expr.tone) or TEMPLATES[winner.kind]["neutral"]
    used = {h["text"] for h in history if h.get("kind") == winner.kind}
    pick = next((v for v in variants if v.format(**winner.payload) not in used), variants[0])
    text = pick.format(**winner.payload)
    if expr.energy == "excited" and not text.endswith("!"):
        text += "!"
    return text


# ---- 컨텍스트 조립 (소스층 수치 -> LLM 지시문) ------------------------------
# 어펙트 엔진이 결정한 종합 수치를 "행동 지시"로 번역한다.
# 숫자만 던지면 모델이 잘 안 따르므로, 각 수치를 관계/기분/태도 directive 로 푼다.

def _intimacy_stage(intimacy: float) -> tuple[str, str]:
    """친밀도 누적치 -> 관계 단계(L0~L4) + 말투 지시. (친밀도 문서 3절 단계 모델)
    임계값은 초안 — 운영 데이터로 튜닝."""
    if intimacy < 1.0:
        return "L0 첫 만남", "정중하게 소개하듯. 존댓말, 중립 호칭. 사적인 건 깊이 안 묻는다."
    if intimacy < 3.0:
        return "L1 익숙", "밝고 친절하게. 가벼운 반말 섞고 이름을 부른다. 최근 사실 1~2개까지 언급."
    if intimacy < 6.0:
        return "L2 친근", "편안하고 장난기 있게. 반말 위주, 애칭도 가능. 좋아한 팀/플레이를 회상."
    if intimacy < 10.0:
        return "L3 신뢰·단골", "속깊게 응원하듯. 개인화 호칭, 과거 모멘트를 인용한다."
    return "L4 단짝", "깊은 유대와 내적 농담. 둘만의 말투, 장기 기억을 적극 활용한다."


def _mood_word(E: float) -> str:
    if E > 0.25:
        return "기분이 좋다"
    if E > -0.25:
        return "평온하다"
    return "기분이 가라앉아 있다"


def build_context(winner, state, expr, cfg) -> tuple[str, str]:
    """소스층(어펙트 엔진) 결과 -> (system, user). 수치가 곧 대화 톤을 지배한다."""
    stage, stage_dir = _intimacy_stage(state.intimacy)
    system = "\n".join([
        f"너는 '{cfg.name}'({cfg.archetype}) 라는 캐릭터다.",
        f"[관계] 친밀도 {state.intimacy:.1f} → '{stage}'. {stage_dir}",
        f"[기분] 정서 E={state.E:+.2f} → {_mood_word(state.E)}. "
        f"세기 A={state.A:.2f} → 에너지는 '{expr.energy}'.",
        f"[태도] 말투 톤은 '{expr.tone}'(친밀도 기반). 경기 열기 {state.heat:.2f}. 표정 {expr.face}.",
        "위 수치가 곧 너의 현재 상태다. 수치에 충실하게 한국어로 1~2문장만 말해라.",
    ])
    user_text = winner.payload.get("text", winner.kind)
    return system, user_text


def _mock_llm(winner, state, expr, cfg) -> str:
    """키 없이 루프를 돌리기 위한 결정론 스텁. 실제로는 call_haiku() 사용."""
    topic = winner.payload.get("text", winner.kind)
    stage, _ = _intimacy_stage(state.intimacy)
    tone = {"warm": "따뜻하게", "neutral": "담담하게", "distant": "짧고 거리 두고"}[expr.tone]
    return f"[mock-haiku {tone}/{stage}] ({cfg.name}) \"{topic}\"에 대한 즉흥 응답"


def respond(winner, state, expr, cfg, history, use_llm=_mock_llm):
    """-> (text, route). use_llm 에 실제 호출 함수를 주입하면 라이브로 동작."""
    route = skip_gate(winner, history)
    if route == "T1":
        return _fill_template(winner, expr, history), "T1"
    return use_llm(winner, state, expr, cfg), "LLM"


# ---- 실제 Haiku 호출부 (데모는 호출하지 않음) --------------------------------
def call_haiku(winner, state, expr, cfg) -> str:
    """
    단일 LLM 티어. ANTHROPIC_API_KEY 필요. 컨텍스트 조립은 별도 단계에서 채움.
    respond(..., use_llm=call_haiku) 로 주입해 쓴다.
    """
    import requests  # 지연 import — 데모 의존성 없음
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY 없음 — 라이브 호출 불가")

    system, user_text = build_context(winner, state, expr, cfg)
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json={
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 200,
            "system": system,
            "messages": [{"role": "user", "content": user_text}],
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
