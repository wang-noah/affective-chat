"""
expression.py — 3숫자 -> 프리셋 (결정론, LLM 0)
E(정서) -> 표정,  A(세기) -> 에너지/파티클,  열기 -> 거리감/길이.
"수치면 이 프리셋"이라는 매핑 표만으로 처리. 3D 대신 라벨/이모지로 스텁.

state 는 E/A/openness 속성을 가진 어떤 객체든 받는다(덕타이핑) — affect_engine 을
import 하지 않아 순환참조를 피한다.
"""
from __future__ import annotations
from dataclasses import dataclass


@dataclass
class Expression:
    face: str          # 표정 (이모지 스텁)
    energy: str        # calm | lively | excited
    tone: str          # distant | neutral | warm
    effect_color: str  # warm | cool
    particles: int     # 이펙트 파티클 수 (A에 비례)


def express(state) -> Expression:
    # E -> 표정
    if state.E > 0.25:
        face = "😊"
    elif state.E > -0.25:
        face = "😐"
    else:
        face = "😒"

    # A -> 에너지 (말 속도 / 느낌표 / 파티클)
    if state.A > 0.66:
        energy = "excited"
    elif state.A > 0.33:
        energy = "lively"
    else:
        energy = "calm"

    # 열기 -> 톤 (짧고 거리감 ~ 따뜻)
    if state.openness < 0.25:
        tone = "distant"
    elif state.openness < 0.60:
        tone = "neutral"
    else:
        tone = "warm"

    return Expression(
        face=face,
        energy=energy,
        tone=tone,
        effect_color="warm" if state.E >= 0 else "cool",
        particles=int(round(state.A * 5)),
    )


def expression_intent(expr: Expression, state) -> dict:
    """Affect 출력의 expression_intent — 표현층 직행용 지시(LLM 0).
    노션 예: {face: 'hyped', effect: 'spark_high', tone: 'excited'}."""
    if state.E > 0.5:
        face = "hyped"
    elif state.E > 0.25:
        face = "happy"
    elif state.E > -0.25:
        face = "neutral"
    elif state.E > -0.5:
        face = "down"
    else:
        face = "upset"
    effect = {"excited": "spark_high", "lively": "spark_low", "calm": "spark_idle"}[expr.energy]
    return {
        "face": face,
        "effect": effect,
        "tone": expr.tone,
        "emoji": expr.face,                 # 표현층 스텁(이모지)
        "energy": expr.energy,
        "effect_color": expr.effect_color,
        "particles": expr.particles,
    }
