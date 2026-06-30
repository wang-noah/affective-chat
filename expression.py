"""
expression.py — 3숫자 -> 프리셋 (결정론, LLM 0)
E(정서) -> 표정,  A(세기) -> 에너지/파티클,  열기 -> 거리감/길이.
"수치면 이 프리셋"이라는 매핑 표만으로 처리. 3D 대신 라벨/이모지로 스텁.
"""
from __future__ import annotations
from dataclasses import dataclass

from affect_engine import AffectState


@dataclass
class Expression:
    face: str          # 표정 (이모지 스텁)
    energy: str        # calm | lively | excited
    tone: str          # distant | neutral | warm
    effect_color: str  # warm | cool
    particles: int     # 이펙트 파티클 수 (A에 비례)


def express(state: AffectState) -> Expression:
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
