"""
config.py — SSOT 로더
JSON(성격 Config) -> 타입 있는 PersonalityConfig.
Affect / Arbiter / Chat / Expression 이 전부 이 한 객체를 참조한다.
"""
from __future__ import annotations
import json
from dataclasses import dataclass, field


@dataclass
class SelectPolicy:
    type: str = "argmax"        # argmax = 1등만 | top_k = 임계 이상 여러 개("둘 다")
    threshold: float = 0.20
    max_winners: int = 2


@dataclass
class PersonalityConfig:
    name: str
    archetype: str = ""
    attention: dict[str, float] = field(default_factory=dict)
    # 기질 파라미터 (Affect)
    reactivity: float = 1.0
    valence_bias: float = 0.0
    arousal_gain: float = 1.0
    decay_E: float = 0.5
    decay_A: float = 0.7
    warmup: float = 0.15
    backlog_decay: float = 0.4
    intimacy_gain: float = 0.7
    intimacy_decay: float = 0.97
    # 정책 / 말투
    select_policy: SelectPolicy = field(default_factory=SelectPolicy)
    voice: dict = field(default_factory=dict)

    def weight(self, kind: str) -> float:
        return self.attention.get(kind, 0.3)   # 미지정 자극 기본 가중치


def load_config(path: str) -> PersonalityConfig:
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    t = raw.get("traits", {})
    sp = raw.get("select_policy", {})
    return PersonalityConfig(
        name=raw["name"],
        archetype=raw.get("archetype", ""),
        attention=raw.get("attention", {}),
        reactivity=t.get("reactivity", 1.0),
        valence_bias=t.get("valence_bias", 0.0),
        arousal_gain=t.get("arousal_gain", 1.0),
        decay_E=t.get("decay_E", 0.5),
        decay_A=t.get("decay_A", 0.7),
        warmup=t.get("warmup", 0.15),
        backlog_decay=t.get("backlog_decay", 0.4),
        intimacy_gain=t.get("intimacy_gain", 0.7),
        intimacy_decay=t.get("intimacy_decay", 0.97),
        select_policy=SelectPolicy(
            type=sp.get("type", "argmax"),
            threshold=sp.get("threshold", 0.20),
            max_winners=sp.get("max_winners", 2),
        ),
        voice=raw.get("voice", {}),
    )
