"""감성 태깅 · FanTagger — 텍스트 → (감정·엔티티) → 이번 턴 팬심 부호+점수.

노션 문서 '감성 태깅 — 개발용 상세'(39e32aa2a04b81f59574c7248087810a) §2~6 구현.
- 감정 분류: 사전학습 GoEmotions 모델(§4.3) — 예시·프로토타입 없음, raw text → 라벨
- 엔티티 인식(§5) — 다국어 별칭 사전 + 관대 매칭
- FanTagger 결합 규칙(§6) — (intent, emotion, entities, follow_target) →
  fan_delta · fan_points · fan_category (팔로우 여부에 따라 정격/소프트)

언어 축: **English primary, Korean secondary.** GoEmotions 는 영문 학습 모델이므로
한국어 인풋은 low-confidence → neutral 로 다운그레이드.
EmotionClassifier.classify() 계약(§3)만 유지하면 내부 모델은 나중에 교체 가능.
"""
from __future__ import annotations
import json
import os
from dataclasses import dataclass, field

from understanding import _strip_particles, to_jamo


# ── §4.1 감정 라벨 (9종) + polarity 룩업 ─────────────────────────────────────
POLARITY_MAP: dict[str, str] = {
    "joy":            "positive",
    "excitement":     "positive",
    "surprise":       "positive",   # v1: 부정 놀람은 anger/disappointment 로 흡수
    "irritation":     "negative",
    "anger":          "negative",
    "sadness":        "negative",
    "disappointment": "negative",
    "confusion":      "neutral",
    "neutral":        "neutral",
}

# §6.2 카테고리 게이팅 — arousal '강함' 셋
AROUSAL_STRONG = {"excitement", "anger", "surprise"}

# §6.2 muse_question 진입 인텐트 (understanding.py INTENT_LEXICON 과 일치해야 함)
MUSE_INTENTS = {"player_bio", "player_performance", "player_champion_mastery",
                "mvp_analysis", "form_vs_career", "roster_change_form"}


# ── §4.3 GoEmotions → 우리 9클래스 매핑 (예시 문장·프로토타입 없음) ───────────
# SamLowe/roberta-base-go_emotions (Google Research GoEmotions 데이터 58K 학습).
# 28-클래스 multi-label 로 예측 → 우리 9-클래스로 집계 (감정별 max prob).
# v1 은 ONNX 양자화(int8) 버전 사용 — 원본 500MB → ~120MB (§4.6 콜드 스타트).
# 학습된 지식은 동일, 저장 포맷·정밀도만 압축. 정확도 손실 1~2%p 이내.
_GOEMOTIONS_MODEL_ID = "SamLowe/roberta-base-go_emotions-onnx"
_GOEMOTIONS_ONNX_FILE = "onnx/model_quantized.onnx"     # int8 양자화 · ~120MB
_EMOTION_MIN_PROB = 0.30    # 감정별 max prob 임계. 미달이면 neutral (오탐 방지)

GOEMOTIONS_TO_OURS: dict[str, str] = {
    # positive strong (arousal↑) — GoEmotions "admiration"("cracked!", "goated!") 도 포함
    "excitement":    "excitement",
    "admiration":    "excitement",
    "desire":        "excitement",
    # positive normal
    "joy":           "joy",
    "amusement":     "joy",
    "approval":      "joy",
    "gratitude":     "joy",
    "love":          "joy",
    "optimism":      "joy",
    "caring":        "joy",
    "pride":         "joy",
    "relief":        "joy",
    # surprise (arousal↑)
    "surprise":      "surprise",
    "realization":   "surprise",
    # negative strong (arousal↑)
    "anger":         "anger",
    "disgust":       "anger",
    # negative normal
    "annoyance":     "irritation",
    "disappointment": "disappointment",
    "disapproval":   "disappointment",
    "sadness":       "sadness",
    "grief":         "sadness",
    "remorse":       "sadness",
    # low arousal / uncertain
    "confusion":     "confusion",
    "curiosity":     "neutral",       # 질문성은 인텐트가 이미 잡음
    "nervousness":   "confusion",
    "fear":          "confusion",     # 게임 문맥에선 드묾
    "embarrassment": "neutral",
    "neutral":       "neutral",
}


# ── §5 엔티티 사전 ─────────────────────────────────────────────────────────
# entities.json 에서 로드. 다국어 별칭(EN + KO) 함께. 시즌·이적마다 data owner 갱신.
_ENTITIES_PATH = os.path.join(os.path.dirname(__file__), "entities.json")


def _load_entities() -> tuple[dict[str, list[str]], dict[str, list[str]], set[str]]:
    """entities.json → (ENTITY_DICT, TEAM_ROSTER, CASE_SENSITIVE_ALIASES)."""
    try:
        with open(_ENTITIES_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return ({"T1": ["T1", "티원", "SKT"], "Gen.G": ["Gen.G", "젠지"],
                 "Faker": ["Faker", "페이커", "이상혁"]},
                {"T1": ["Faker"]},
                set())
    ed: dict[str, list[str]] = {}
    for team, info in data.get("teams", {}).items():
        ed[team] = info.get("aliases", [team])
    for player, info in data.get("players", {}).items():
        ed[player] = info.get("aliases", [player])
    tr = {team: info.get("players", []) for team, info in data.get("teams", {}).items()}
    cs = set(data.get("_case_sensitive_aliases", []))
    return ed, tr, cs


ENTITY_DICT, TEAM_ROSTER, _CASE_SENSITIVE_ALIASES = _load_entities()

# 별칭 인덱스 — (alias_lower, alias_original, canonical, case_sensitive) 튜플 리스트.
# case_sensitive 별칭은 일반 영어 단어와 겹치므로 원 대소문자 그대로만 매칭.
# 예: "perfect" (일반어) 는 "PerfecT" (KT 선수) 로 오탐되면 안 됨.
_ALIAS_INDEX: list[tuple[str, str, str, bool]] = [
    (alias.lower(), alias, canonical, alias in _CASE_SENSITIVE_ALIASES)
    for canonical, aliases in ENTITY_DICT.items()
    for alias in aliases
]


def expand_follow_targets(followed_teams: list[str]) -> set[str]:
    """팔로잉 팀들 → follow_target 셋 (팀 자체 + 소속 선수)."""
    out: set[str] = set()
    for team in followed_teams:
        out.add(team)
        out.update(TEAM_ROSTER.get(team, []))
    return out


# 선수 → 소속 팀 역인덱스 (TEAM_ROSTER 반전). fan_delta 는 팀 단위 관계이므로
# 엔티티가 선수여도 팬심은 그 선수의 소속 팀에 누적된다.
_PLAYER_TO_TEAM: dict[str, str] = {
    player: team
    for team, players in TEAM_ROSTER.items()
    for player in players
}


def resolve_to_teams(entities: list[str]) -> list[str]:
    """엔티티(팀 or 선수) 리스트 → 팀 리스트 (순서 유지, 중복 제거).
    선수는 소속 팀으로 매핑, 팀은 그대로, 미지 엔티티도 그대로 유지."""
    out: list[str] = []
    seen: set[str] = set()
    for e in entities:
        team = _PLAYER_TO_TEAM.get(e, e)   # 선수 → 팀 / 팀·미지 → 자기 자신
        if team not in seen:
            seen.add(team)
            out.append(team)
    return out


# ── §2 FanSignal 출력 스키마 ────────────────────────────────────────────────
@dataclass
class FanSignal:
    text: str
    intent: str | None
    emotion: str
    polarity: str                         # positive / neutral / negative
    emotion_conf: float                   # 감정 신뢰도 (모델 raw prob)
    emotion_source: str                   # 'model' | 'model_below_threshold' | 'unavailable'
    entities: list[str] = field(default_factory=list)
    fan_category: str = "none"            # strong_reaction(_soft) / muse_question(_soft) / none
    fan_delta: dict[str, str] = field(default_factory=dict)
    fan_points: int = 0


# ── (A) EmotionClassifier — 사전학습 GoEmotions 모델 (예시 매핑 없음) ─────────
class EmotionClassifier:
    """text → (emotion, polarity, confidence, source).

    v1: SamLowe/roberta-base-go_emotions-onnx (ONNX int8 양자화 · ~120MB).
    Tokenizer 는 transformers, 모델 추론은 onnxruntime 직접 세션. 28 라벨 sigmoid
    확률을 우리 9클래스로 집계(감정별 max) 후 argmax. 임계 미달이면 neutral.
    로드 실패 시 unavailable → neutral 다운그레이드.
    """

    # GoEmotions 원본 28 라벨 순서 (config.json id2label 그대로).
    # ONNX 세션은 model config 를 안 갖고 있어 하드코딩. 학습 시 고정 순서라 안전.
    _LABELS_ORDERED = [
        "admiration", "amusement", "anger", "annoyance", "approval", "caring",
        "confusion", "curiosity", "desire", "disappointment", "disapproval",
        "disgust", "embarrassment", "excitement", "fear", "gratitude", "grief",
        "joy", "love", "nervousness", "optimism", "pride", "realization",
        "relief", "remorse", "sadness", "surprise", "neutral",
    ]

    def __init__(self):
        self._tok = None
        self._session = None    # onnxruntime InferenceSession
        self._np = None
        self._tried_load = False

    def _sigmoid(self, x):
        np = self._np
        return 1.0 / (1.0 + np.exp(-x))

    def _ensure_model(self) -> bool:
        if self._session is not None:
            return True
        if self._tried_load:
            return False
        self._tried_load = True
        try:
            from transformers import AutoTokenizer
            from huggingface_hub import hf_hub_download
            import onnxruntime as ort
            import numpy as np
        except ImportError:
            return False
        try:
            tok = AutoTokenizer.from_pretrained(_GOEMOTIONS_MODEL_ID)
            onnx_path = hf_hub_download(repo_id=_GOEMOTIONS_MODEL_ID,
                                        filename=_GOEMOTIONS_ONNX_FILE)
            # CPU provider 만 사용 (배포 환경 GPU 유무 무관하게 안정)
            session = ort.InferenceSession(onnx_path, providers=["CPUExecutionProvider"])
        except Exception:
            return False
        self._tok = tok
        self._session = session
        self._np = np
        return True

    def classify(self, text: str) -> dict:
        if not self._ensure_model():
            return {"emotion": "neutral", "polarity": "neutral",
                    "confidence": 0.0, "source": "unavailable"}
        np = self._np
        # ONNX 세션은 numpy tensor 입력. transformers tokenizer 로 준비.
        inputs = self._tok(text, return_tensors="np", truncation=True, max_length=128)
        feed = {k: v for k, v in inputs.items()
                if k in {i.name for i in self._session.get_inputs()}}
        logits = self._session.run(None, feed)[0][0]     # [batch=1, 28] → [28]
        probs = self._sigmoid(logits).tolist()

        # 28 라벨 → 우리 9클래스로 집계 (감정별 max prob)
        our_probs: dict[str, float] = {}
        for i, p in enumerate(probs):
            src_label = self._LABELS_ORDERED[i]
            tgt = GOEMOTIONS_TO_OURS.get(src_label, "neutral")
            if p > our_probs.get(tgt, 0.0):
                our_probs[tgt] = p

        best_emo = max(our_probs, key=our_probs.get)
        best_p = float(our_probs[best_emo])
        if best_p < _EMOTION_MIN_PROB:
            return {"emotion": "neutral", "polarity": "neutral",
                    "confidence": round(best_p, 3), "source": "model_below_threshold"}
        return {"emotion": best_emo, "polarity": POLARITY_MAP[best_emo],
                "confidence": round(best_p, 3), "source": "model"}


# ── (B) EntityRecognizer — §5 다국어 별칭 사전 + 관대 매칭 ────────────────────
class EntityRecognizer:
    """텍스트 → canonical 엔티티 중복제거 리스트."""

    def extract(self, text: str) -> list[str]:
        hits: list[str] = []
        seen: set[str] = set()
        # 오탐 방지: ① 정확 · ② 조사·어미 벗김 · ③ 자모 substring 만.
        # 케이스 민감 별칭(_case_sensitive_aliases)은 원 대소문자 매칭 필수 —
        # "perfect"(일반어) 가 "PerfecT"(선수) 로 오탐되는 걸 방지.
        t_lower = text.lower()
        t_stripped_lc = _strip_particles(t_lower)
        t_stripped = _strip_particles(text)          # case-sensitive 매칭용
        t_jamo = to_jamo(t_lower)
        for alias_lc, alias_orig, canonical, cs in _ALIAS_INDEX:
            if canonical in seen:
                continue
            if cs:
                # 케이스 민감 — 원 대소문자 그대로 매칭 (Substring)
                if alias_orig in text or alias_orig in t_stripped:
                    seen.add(canonical)
                    hits.append(canonical)
                continue     # 자모 fallback 등 다른 경로는 안 씀 (오탐 위험)
            # 일반 별칭 — 케이스 무시
            if alias_lc in t_lower or alias_lc in t_stripped_lc:
                seen.add(canonical)
                hits.append(canonical)
                continue
            aj = to_jamo(alias_lc)
            if len(aj) > 4 and aj in t_jamo:
                seen.add(canonical)
                hits.append(canonical)
        return hits


# ── (C) FanTagger — §6 rules with soft accumulation ─────────────────────────
class FanTagger:
    """(intent, emotion, polarity, entities, follow_target) → category · delta · points.

    Mentioning any known entity nudges fan_points even without an explicit follow
    (soft signal). Follow_target entities get full score; others get SOFT_FACTOR (~30%).
    """

    STRONG = 15
    MUSE = 10
    MENTION = 5             # 언급 자체를 팬심 신호로 인정 (§6.2 mention 카테고리)
    SOFT_FACTOR = 0.3

    def tag(self, intent: str | None, emotion: str, polarity: str,
            entities: list[str], follow_target: set[str] | None) -> dict:
        # 팬심은 팀 단위 관계 — 선수 엔티티도 소속 팀으로 정규화한 뒤 delta 기록.
        # "Faker is good" → entities=[Faker] → teams=[T1] → fan_delta={T1: +}
        teams = resolve_to_teams(entities)

        delta: dict[str, str] = {}
        if polarity == "positive":
            delta = {t: "+" for t in teams}
        elif polarity == "negative":
            delta = {t: "-" for t in teams}

        if not teams:
            return {"fan_category": "none", "fan_delta": delta, "fan_points": 0}

        # has_follow 체크는 원 엔티티/팀 모두 지원 (expand_follow_targets 가 팀+선수 확장)
        has_follow = bool(follow_target) and (
            any(e in follow_target for e in entities) or
            any(t in follow_target for t in teams)
        )

        if emotion in AROUSAL_STRONG:
            base = self.STRONG if polarity == "positive" \
                   else -self.STRONG if polarity == "negative" \
                   else 0
            if has_follow:
                return {"fan_category": "strong_reaction",
                        "fan_delta": delta, "fan_points": base}
            soft = int(round(base * self.SOFT_FACTOR))
            return {"fan_category": "strong_reaction_soft",
                    "fan_delta": delta, "fan_points": soft}

        if intent in MUSE_INTENTS:
            if has_follow:
                return {"fan_category": "muse_question",
                        "fan_delta": delta, "fan_points": self.MUSE}
            soft = int(round(self.MUSE * self.SOFT_FACTOR))
            return {"fan_category": "muse_question_soft",
                    "fan_delta": delta, "fan_points": soft}

        # ③ mention — 엔티티 언급 + polarity ≠ negative → 언급 자체를 팬심 신호로 인정.
        # "T1 is funny" (joy) 나 "when does T1 play?" (neutral) 처럼 관심-유형 인텐트도
        # 아니고 강한 감정도 아닌 언급을, engagement 축으로 흡수한다.
        if polarity != "negative":
            if has_follow:
                return {"fan_category": "mention",
                        "fan_delta": delta, "fan_points": self.MENTION}
            soft = int(round(self.MENTION * self.SOFT_FACTOR))
            return {"fan_category": "mention_soft",
                    "fan_delta": delta, "fan_points": soft}

        # ④ 부정적 언급(약한 부정) — 부호는 delta 에 기록되지만 점수는 0
        return {"fan_category": "none", "fan_delta": delta, "fan_points": 0}


# ── FanSignalPipeline — §3 조립 ─────────────────────────────────────────────
_EMO = EmotionClassifier()
_ENT = EntityRecognizer()
_TAG = FanTagger()


def fan_signal(text: str, intent: str | None = None,
               follow_target: set[str] | None = None) -> FanSignal:
    """text (+intent, +follow_target) → FanSignal. Understanding 층 뒤에 붙임."""
    emo = _EMO.classify(text)
    ents = _ENT.extract(text)
    tag = _TAG.tag(intent=intent, emotion=emo["emotion"], polarity=emo["polarity"],
                   entities=ents, follow_target=follow_target)
    return FanSignal(
        text=text, intent=intent,
        emotion=emo["emotion"], polarity=emo["polarity"],
        emotion_conf=emo["confidence"], emotion_source=emo["source"],
        entities=ents,
        fan_category=tag["fan_category"], fan_delta=tag["fan_delta"],
        fan_points=tag["fan_points"],
    )


def warmup_emotion_model() -> bool:
    """부팅 시 사전 로드 (첫 요청 스톨 방지). 실패 시 unavailable 로 다운그레이드."""
    return _EMO._ensure_model()
