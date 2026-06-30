"""
affect_engine.py — 결정론(LLM 0) 코어
=====================================
한 턴: update(state, candidates, cfg, dt) -> (new_state, winners, losers)

salience 와 Arbiter 의 관계:
  - salience()  : 후보 하나를 비교 가능한 숫자 하나로 환산하는 '화폐'.
                  자극 / Backlog(묵은 자극) / 목표를 같은 척도로 만든다.
                  현재 감정 상태(E/A/열기)를 입력으로 받아 끌림을 변조 = 되먹임.
  - arbitrate() : salience 점수를 받아 누가 이기는지만 정하는 '경매'.
                  점수를 소비만 하고 계산은 안 한다. 선택 정책은 성격이 정함.
"""
from __future__ import annotations
from dataclasses import dataclass, field, replace
import math

from config import PersonalityConfig


# ---------------------------------------------------------------------------
# 상태 & 후보
# ---------------------------------------------------------------------------
@dataclass
class AffectState:
    E: float = 0.0         # 정서  -1~+1
    A: float = 0.2         # 세기   0~1
    openness: float = 0.0  # 열기   0~1
    intimacy: float = 0.0  # 친밀도 누적치


@dataclass
class Candidate:
    kind: str
    intensity: float = 0.5     # 자극의 세기 (목표면 urgency로 해석)
    is_goal: bool = False      # 목표 엔진이 만든 내부 목표인가
    age: float = 0.0           # Backlog에서 묵은 턴 수 (0 = 이번 턴 새 자극)
    payload: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Appraisal — 자극 -> (valence, intensity) 결정론 룩업
# ---------------------------------------------------------------------------
APPRAISAL_TABLE: dict[str, tuple[float, float]] = {
    "greeting":          ( 0.30, 0.20),
    "smalltalk":         ( 0.05, 0.30),
    "game_positive":     ( 0.60, 0.85),
    "game_negative":     (-0.55, 0.80),
    "user_distress":     (-0.15, 0.55),
    "compliment":        ( 0.50, 0.50),
    "insult":            (-0.60, 0.70),
    "goal_follow_nudge": ( 0.10, 0.30),
}

SOCIAL_KINDS = {"greeting", "smalltalk", "compliment", "user_distress", "goal_follow_nudge"}


def appraise(c: Candidate, cfg: PersonalityConfig) -> tuple[float, float]:
    valence, base_int = APPRAISAL_TABLE.get(c.kind, (0.0, 0.3))
    strength = base_int * c.intensity
    dE = valence * strength * cfg.reactivity
    dA = strength * cfg.arousal_gain
    return dE, dA


# ---------------------------------------------------------------------------
# salience — 후보 1개 -> 점수 1개 (되먹임 포함)
# ---------------------------------------------------------------------------
def affect_mod(kind: str, state: AffectState) -> float:
    """현재 감정 상태가 '무엇에 끌리는가'를 변조한다 = Arbiter 쪽 되먹임 통로."""
    m = 1.0
    # 열기가 낮으면 사교적 자극에 덜 끌림 (0.4 ~ 1.0)
    if kind in SOCIAL_KINDS:
        m *= 0.4 + 0.6 * state.openness
    # mood-congruent: 기분 좋을 땐 긍정 자극, 나쁠 땐 부정 자극에 더 끌림
    if kind in ("game_positive", "compliment", "greeting"):
        m *= 1.0 + 0.3 * max(0.0, state.E)
    if kind in ("game_negative", "insult"):
        m *= 1.0 + 0.3 * max(0.0, -state.E)
    return m


def salience(c: Candidate, state: AffectState, cfg: PersonalityConfig) -> float:
    base = c.intensity                          # 목표/자극 공통 척도
    weight = cfg.weight(c.kind)                 # 성격 주목 가중치
    recency = math.exp(-cfg.backlog_decay * c.age)  # 묵을수록 끌림 ↓
    mood = affect_mod(c.kind, state)            # 현재 상태 되먹임
    return base * weight * recency * mood


# ---------------------------------------------------------------------------
# Arbiter — 점수 받아 승자만 결정 (정책은 성격이 정함)
# ---------------------------------------------------------------------------
def arbitrate(
    candidates: list[Candidate], state: AffectState, cfg: PersonalityConfig
) -> tuple[list[Candidate], list[Candidate]]:
    if not candidates:
        return [], []
    scored = sorted(
        ((salience(c, state, cfg), c) for c in candidates),
        key=lambda x: x[0], reverse=True,
    )
    pol = cfg.select_policy
    if pol.type == "argmax":
        winners = [scored[0][1]]
    else:  # top_k: 임계 이상을 max_winners 까지 ("둘 다" 가능)
        winners = [c for s, c in scored if s >= pol.threshold][: pol.max_winners]
        if not winners:
            winners = [scored[0][1]]            # 전부 임계 미만이면 최소 1개
    win_ids = {id(c) for c in winners}
    losers = [c for _, c in scored if id(c) not in win_ids]
    return winners, losers


# ---------------------------------------------------------------------------
# Decay + 통합 = 한 턴
# ---------------------------------------------------------------------------
def _decay_toward(value, baseline, rate, dt):
    return baseline + (value - baseline) * math.exp(-rate * dt)


def openness_baseline(intimacy: float) -> float:
    """친밀도가 쌓일수록 열기 기저값이 오름 (sigmoid). 되먹임의 핵심 연결."""
    return 1.0 / (1.0 + math.exp(-(intimacy - 5.0) / 2.0))


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def update(
    state: AffectState,
    candidates: list[Candidate],
    cfg: PersonalityConfig,
    dt: float = 1.0,
) -> tuple[AffectState, list[Candidate], list[Candidate]]:
    # (a) 감쇠 — 지난 기분이 시간만큼 기저로 식음
    E = _decay_toward(state.E, cfg.valence_bias, cfg.decay_E, dt)
    A = _decay_toward(state.A, 0.15, cfg.decay_A, dt)
    openness = _decay_toward(state.openness, openness_baseline(state.intimacy), 0.3, dt)
    work = replace(state, E=E, A=A, openness=openness)

    # (b) 주목 — 무엇에 반응할지 (salience -> Arbiter)
    winners, losers = arbitrate(candidates, work, cfg)

    # (c) 평가 + 통합 — 이긴 자극들로 기분 갱신
    for w in winners:
        dE, dA = appraise(w, cfg)
        E = _clamp(E + dE, -1.0, 1.0)
        A = _clamp(A + dA, 0.0, 1.0)
        openness = _clamp(openness + max(0.0, dE) * cfg.warmup, 0.0, 1.0)

    return replace(state, E=E, A=A, openness=openness), winners, losers
