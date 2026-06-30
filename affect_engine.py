"""
affect_engine.py — Affect: 숫자만 받는 순수 함수 (LLM 0)
=======================================================
노션 'Arbiter → Affect' 문서의 Affect 책임만 담는다.

  입력: AffectRequest  (Arbiter 가 만든 정제 숫자 — valence·salience·prev_affect…)
  출력: AffectOutput   (E·A·intensity·expression_intent + route/path/tags/moment_id)

Affect 는 게임 룰도(어떤 이벤트가 +/-인지), 분기 정책도(언제 채팅할지) 모른다.
valence·salience 라는 숫자만 받아 기분(E·A)으로 적분하고, route·path·tags·moment_id 는
**변형 없이 통과(passthrough)**시킨다. prev_affect 를 동봉받아 무상태로 유지된다
— 같은 요청이면 같은 출력.

  affect(req, prev_state, cfg) -> (new_state, output, expr)
"""
from __future__ import annotations
from dataclasses import dataclass, field, replace
import math

from config import PersonalityConfig
from expression import express, expression_intent


# ---------------------------------------------------------------------------
# 지속 상태 (세션이 들고 다니는 누적치) & 한 턴 출력
# ---------------------------------------------------------------------------
@dataclass
class AffectState:
    E: float = 0.0         # 정서  -1~+1
    A: float = 0.2         # 세기   0~1
    openness: float = 0.0  # 열기   0~1
    intimacy: float = 0.0  # 친밀도 누적치


@dataclass
class AffectOutput:
    """노션 ② AffectState 출력. Arbiter 로 돌아가지 않고 앞으로만 흐른다."""
    E: float
    A: float
    intensity: float
    expression_intent: dict          # 표현층 직행 (표정·이펙트·톤). 항상.
    route: dict                      # 입력값 그대로 통과 (Affect 는 안 건드림)
    path: str
    tags: list[str] = field(default_factory=list)
    moment_id: str | None = None


# ---------------------------------------------------------------------------
# 감쇠 / 기저값 / 클램프
# ---------------------------------------------------------------------------
def _decay_toward(value, baseline, rate, dt):
    return baseline + (value - baseline) * math.exp(-rate * dt)


def openness_baseline(intimacy: float) -> float:
    """친밀도가 쌓일수록 열기 기저값이 오름 (sigmoid). 되먹임의 핵심 연결."""
    return 1.0 / (1.0 + math.exp(-(intimacy - 5.0) / 2.0))


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


# ---------------------------------------------------------------------------
# 적분 — valence·salience 숫자 한 쌍 -> 기분 변화
# ---------------------------------------------------------------------------
def _integrate(E, A, openness, valence, salience, cfg):
    strength = salience                  # 주목도(importance×부스터)가 곧 자극 세기
    dE = valence * strength * cfg.reactivity
    dA = strength * cfg.arousal_gain
    E = _clamp(E + dE, -1.0, 1.0)
    A = _clamp(A + dA, 0.0, 1.0)
    openness = _clamp(openness + max(0.0, dE) * cfg.warmup, 0.0, 1.0)
    return E, A, openness


# ---------------------------------------------------------------------------
# affect — 한 턴 (순수 함수)
# ---------------------------------------------------------------------------
def affect(req, prev_state: AffectState, cfg: PersonalityConfig, dt: float = 1.0):
    """AffectRequest -> (new_state, AffectOutput, Expression).
    req 는 arbiter.AffectRequest 이지만 import 하지 않고 덕타이핑으로 읽는다(순환참조 회피)."""
    # (a) 감쇠 — 동봉된 prev_affect 기준으로 기저로 식음 (무상태 유지)
    base_E = req.state.prev_affect.get("E", prev_state.E)
    base_A = req.state.prev_affect.get("A", prev_state.A)
    E = _decay_toward(base_E, cfg.valence_bias, cfg.decay_E, dt)
    A = _decay_toward(base_A, 0.15, cfg.decay_A, dt)
    openness = _decay_toward(prev_state.openness, openness_baseline(req.state.intimacy), 0.3, dt)

    # (b) 적분 — primary(+secondary) 의 valence·salience 로 기분 갱신
    for stim in (req.primary, req.secondary):
        if stim is None:
            continue
        E, A, openness = _integrate(E, A, openness, stim.valence, stim.salience, cfg)

    # (c) 종합 세기 — A 와 |E| 의 혼합 (노션 예: 0.5·A + 0.5·|E|)
    intensity = round(_clamp(0.5 * A + 0.5 * abs(E), 0.0, 1.0), 3)

    new_state = replace(prev_state, E=E, A=A, openness=openness)
    expr = express(new_state)

    output = AffectOutput(
        E=round(E, 3), A=round(A, 3), intensity=intensity,
        expression_intent=expression_intent(expr, new_state),
        route=req.route,                                  # 통과
        path=req.path,                                    # 통과
        tags=(list(req.primary.tags) if req.primary else []),
        moment_id=(req.primary.moment_id if req.primary else None),
    )
    return new_state, output, expr
