"""
affect_engine.py — Affect: E·A 감정 적분 (LLM 0)
=================================================
valence·salience 숫자만 받아 기분(E·A)으로 적분하는 순수 함수.
route·path·tags·moment_id 는 변형 없이 통과시킨다. prev_affect 동봉으로 무상태 유지
— 같은 요청이면 같은 출력.

열기(match heat)는 Affect 가 아니라 **Arbiter 가 계산**해 req.state.heat 로 넘겨준다.
여기선 그 값을 상태에 실어 통과시킬 뿐이다 (열기 = 경기가 얼마나 뜨거운가, 게임 도메인).

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
    E: float = 0.0         # 정서   -1~+1
    A: float = 0.2         # 세기    0~1
    heat: float = 0.0      # 열기(경기 heat) 0~1 — Arbiter 가 계산, 여기선 상태로 보유
    intimacy: float = 0.0  # 친밀도 누적치 (외부 입력, 비소모)


@dataclass
class AffectOutput:
    """Affect 출력. 앞(표현/채팅)으로만 흐른다."""
    E: float
    A: float
    intensity: float
    expression_intent: dict          # 표현층 직행 (표정·이펙트·톤). 항상.
    route: dict                      # 분기값 그대로 통과
    path: str
    tags: list[str] = field(default_factory=list)
    moment_id: str | None = None


# ---------------------------------------------------------------------------
# 감쇠 / 클램프
# ---------------------------------------------------------------------------
def _decay_toward(value, baseline, rate, dt):
    return baseline + (value - baseline) * math.exp(-rate * dt)


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def decay_state(state: "AffectState", cfg: PersonalityConfig, dt: float) -> "AffectState":
    """자극 없이 시간 dt 만큼 기저로 식힘 — 스왑 공백(캐릭터를 안 보던 동안) 재개 시 호출.
    E·A 는 기저로, 열기는 0 으로 식음. 친밀도는 비소모(안 줄어듦)."""
    E = _decay_toward(state.E, cfg.valence_bias, cfg.decay_E, dt)
    A = _decay_toward(state.A, 0.15, cfg.decay_A, dt)
    heat = _decay_toward(state.heat, 0.0, cfg.decay_heat, dt)
    return replace(state, E=E, A=A, heat=heat)


# ---------------------------------------------------------------------------
# 적분 — valence·salience 숫자 한 쌍 -> E·A 변화
# ---------------------------------------------------------------------------
def _integrate(E, A, valence, salience, cfg):
    strength = salience                  # 주목도(importance×부스터)가 곧 자극 세기
    dE = valence * strength * cfg.reactivity
    dA = strength * cfg.arousal_gain
    return _clamp(E + dE, -1.0, 1.0), _clamp(A + dA, 0.0, 1.0)


# ---------------------------------------------------------------------------
# affect — 한 턴 (순수 함수). E·A 만 계산하고 열기는 Arbiter 가 준 값을 실어 통과.
# ---------------------------------------------------------------------------
def affect(req, prev_state: AffectState, cfg: PersonalityConfig, dt: float = 1.0):
    """AffectRequest -> (new_state, AffectOutput, Expression)."""
    # (a) 감쇠 — 동봉된 prev_affect 기준으로 기저로 식음 (무상태 유지)
    base_E = req.state.prev_affect.get("E", prev_state.E)
    base_A = req.state.prev_affect.get("A", prev_state.A)
    E = _decay_toward(base_E, cfg.valence_bias, cfg.decay_E, dt)
    A = _decay_toward(base_A, 0.15, cfg.decay_A, dt)

    # (b) 적분 — primary(+secondary) 의 valence·salience 로 E·A 갱신
    for stim in (req.primary, req.secondary):
        if stim is None:
            continue
        E, A = _integrate(E, A, stim.valence, stim.salience, cfg)

    # (c) 종합 세기 — A 와 |E| 의 혼합 (노션 예: 0.5·A + 0.5·|E|)
    intensity = round(_clamp(0.5 * A + 0.5 * abs(E), 0.0, 1.0), 3)

    # 열기는 Arbiter 가 계산해 req.state.heat 로 준 값을 그대로 상태에 싣는다.
    new_state = replace(prev_state, E=E, A=A, heat=req.state.heat)
    expr = express(new_state)

    output = AffectOutput(
        E=round(E, 3), A=round(A, 3), intensity=intensity,
        expression_intent=expression_intent(expr, new_state),
        route=req.route,
        path=req.path,
        tags=(list(req.primary.tags) if req.primary else []),
        moment_id=(req.primary.moment_id if req.primary else None),
    )
    return new_state, output, expr
