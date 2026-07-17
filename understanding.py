"""요청이해 층 — 텍스트에서 세그먼트·인텐트를 뽑는다.
노션 plan(39c32aa2a04b805488cef1e2ab7674e7) §2 룰 그대로. LLM 0.
phase 는 서버 상태 주입값이라 텍스트에서 추론하지 않는다.
"""
from __future__ import annotations
from dataclasses import dataclass


@dataclass
class Understanding:
    text: str
    phase: str
    segment: str
    intent: str | None
    confidence: dict
    needs_new_context: bool
    intent_source: str = "none"    # 'rule' | 'embed' | 'none' — 어느 경로가 잡았는지


# ── 인텐트 시그니처 (구체적 → 일반적 순, argmax 동률은 이 순서로 tie-break) ──
# 노션 plan §2.3 표 그대로. "champion_strength" 는 어휘가 매우 짧아 오탐 위험이
# 크므로 dict 뒤쪽. "draft_winrate" 처럼 구체어("1픽","표본")가 있는 인텐트는 앞.
INTENT_LEXICON: list[tuple[str, list[str]]] = [
    ("draft_winrate",           ["1픽", "표본", "이 패치", "승률"]),
    ("counter_matchup",         ["라인전 상성", "카운터", "상성", "매치업"]),
    ("ban_intent",              ["저격밴", "왜 밴", "밴"]),
    ("mvp_analysis",            ["MVP", "carried", "hero of the match",
                                 "오늘 최고", "캐리"]),
    ("defeat_analysis",         ["왜 졌", "왜 진", "패배", "아쉬웠", "결정적"]),
    ("form_vs_career",          ["career stats", "compared to career", "current form",
                                 "커리어 스탯", "커리어 대비", "오늘 폼"]),
    ("team_draft_profile",      ["사이드 승률", "밴 우선순위", "팀 승률", "프로파일"]),
    ("h2h_history",             ["H2H", "상대전적", "라이벌", "맞대결"]),
    ("roster_change_form",      ["since transfer", "after joining", "left the team",
                                 "이적 후 폼", "팀 바꾸고", "이적"]),
    ("patch_impact",            ["티어 바뀌", "너프", "버프", "패치"]),
    ("objective_value",         ["오브젝트 승률", "바론", "장로", "먹으면"]),
    ("laning_baseline",         ["골드 diff", "라인전", "CS", "평소"]),
    ("comp_timing",             ["파워스파이크", "스케일링", "언제 세져", "초반", "후반"]),
    ("comeback_odds",           ["졌잘싸", "해볼 만", "역전", "뒤집", "아직"]),
    ("win_probability",         ["골드차 승률", "승리확률", "누가 이겨", "앞서", "이기고", "유리"]),
    ("player_performance",      ["playing well", "not playing well", "form today",
                                 "is doing", "지금 잘하", "지금 못", "오늘 잘", "못하는"]),
    # "이 선수" 자리를 선수 이름으로 바꿔 부르는 게 흔해서 "누구" 를 별도 시그니처로 추가.
    # dict 순으로 mvp_analysis(4위) 등이 앞서므로 "MVP가 누구야?" 는 여전히 mvp_analysis 로 감.
    ("player_bio",              ["who is", "who's", "background of", "career of", "profile of",
                                 "이 선수 누구", "누구야", "누구지", "누구임", "누군", "누구",
                                 "어느 팀", "뭐 하던", "유명"]),
    ("comp_identity",           ["어떤 조합", "뭐가 좋아", "조합"]),
    ("player_champion_mastery", ["one trick", "otp", "signature champ", "mains",
                                 "원챔", "장인", "시그니처", "숙련", "잘해"]),
    ("champion_strength",       ["좋은 픽", "요즘 뜨", "티어", "op", "쎄", "센", "강"]),
]

# 임베딩 폴백용 예시 발화 — 룰 시그니처(짧은 키워드)는 e5 임베딩에서 변별력이 낮음
# ("누구"·"유명" 같은 흔한 짧은 단어는 임의 한국어 문장과 cos 0.80+ 로 붙어 노이즈).
# 대신 자연 발화 예시 2~4개씩 심어서 유의어·오타 발화가 그중 하나에 붙게 만든다.
INTENT_EXAMPLES: dict[str, list[str]] = {
    "champion_strength":       ["이 챔피언 강해?", "이 챔프 지금 오피지?",
                                 "이 챔피언 티어 어때?", "요즘 이 챔프 셈?"],
    "player_champion_mastery": ["Does this player main that champion?", "Is he a one trick?",
                                 "What's his signature champ?",
                                 "이 선수가 저 챔프 잘해?", "저 챔피언 장인이야?",
                                 "이 선수 원챔이지?", "시그니처 픽 뭐야?"],
    "comp_identity":           ["이 조합 컨셉이 뭐야?", "이 팀 어떤 조합이야?",
                                 "이 조합 뭐가 좋아?"],
    "draft_winrate":           ["이 패치에 이 챔프 픽률 어때?", "1픽 승률 얼마야?",
                                 "지금 이 챔프 표본 승률?"],
    "counter_matchup":         ["이 챔프 카운터 뭐야?", "라인전 상성 어때?",
                                 "매치업 유리해?"],
    "ban_intent":              ["왜 저 챔프 밴했지?", "저격밴 대상은 누구?",
                                 "밴 우선순위 뭐야?"],
    "comp_timing":             ["이 조합 파워스파이크 언제 와?", "스케일링 좋은 조합이야?",
                                 "언제부터 세지지?"],
    "win_probability":         ["누가 이길 것 같아?", "이 팀 이길 확률 얼마나?",
                                 "지금 상황 유리한 쪽 어디?"],
    "comeback_odds":           ["역전 가능해?", "지금 뒤집을 수 있어?",
                                 "아직 해볼 만해?", "졌잘싸야?"],
    "player_performance":      ["Is this player playing well today?", "He seems off right now",
                                 "How's his form?",
                                 "이 선수 오늘 잘하고 있어?", "지금 못하는 것 같은데?",
                                 "오늘 폼 좋아?"],
    "objective_value":         ["바론 먹으면 이겨?", "장로 승률 어때?",
                                 "지금 오브젝트 가치 어때?"],
    "laning_baseline":         ["라인전 CS 차이 얼마야?", "평소 라인전 어떻게 해?",
                                 "골드 diff 어때?"],
    "mvp_analysis":            ["Who's the MVP today?", "Who carried this match?",
                                 "Best player of the game?",
                                 "오늘 MVP 누구?", "이번 판 캐리 누구?",
                                 "오늘 최고 활약 선수?"],
    "defeat_analysis":         ["왜 졌을까?", "이 경기 패배 원인이 뭐야?",
                                 "결정적인 순간이 언제?"],
    "form_vs_career":          ["How's his form vs his career stats?", "Compared to career average?",
                                 "Any better than last season?",
                                 "커리어 스탯 대비 오늘 어때?", "이 선수 오늘 폼이 어떤데?",
                                 "요즘 폼 좋아?"],
    "team_draft_profile":      ["이 팀 사이드 승률 어때?", "이 팀 밴 우선순위 뭐야?",
                                 "팀 승률이 어때?"],
    "h2h_history":             ["두 팀 H2H 어때?", "상대 전적이 어때?",
                                 "이 라이벌 관계 어떻게 돼?"],
    "player_bio":              ["Who is Faker?", "What's this player's background?",
                                 "What team is he on?", "How famous is he?",
                                 "페이커가 누구야?", "이 선수 뭐 하던 사람이야?",
                                 "이 선수 어느 팀이야?", "얼마나 유명한 선수야?"],
    "roster_change_form":      ["Did this player transfer?", "Did he switch teams?",
                                 "How's his form after joining the new team?",
                                 "Any better since the transfer?",
                                 "이 선수 이적했어?", "이 선수 팀 옮겼어?",
                                 "팀 옮긴 뒤로 폼 어때?", "이적 후 성적 어때?",
                                 "언제 팀 바꾼 거야?"],
    "patch_impact":            ["이 패치로 티어 바뀌었어?", "너프됐어?",
                                 "이번 패치 영향 어때?"],
}

# 세그먼트 신호 — 노션 plan §2.2 표
MAN_LEXICON = [
    "승률", "%", "표본", "CS diff", "데미지비중", "백분위",
    "이 패치", "이 상황", "커리어 스탯", "라인전 매치업",
    "H2H", "저격밴", "파워스파이크", "스케일링", "인게이지", "Δ",
]
CAS_LEXICON = [
    "잘해", "센 거", "강해", "재밌어", "어때",
    "누가 이겨", "역전", "유명", "라이벌",
]

# 조사·어미 — 어미가 긴 것부터 (짧은 게 먼저 먹히면 긴 게 안 벗겨짐)
PARTICLES = ("이야", "야", "요", "이", "가", "을", "를", "은", "는", "의", "에")
# 어절 끝에 붙는 문장부호 — 조사·어미 정규화 전에 벗겨야 "누구야?" → "누구" 로 내려감
_TRIM_PUNCT = "?!.,;:\"'()[]{}~^"


# ── 한글 자모 정규화 (stdlib only, plan §2.3 코드 그대로) ────────────────────
def to_jamo(s: str) -> str:
    out = []
    for c in s.lower():
        if 0xAC00 <= ord(c) <= 0xD7A3:
            off = ord(c) - 0xAC00
            cho, jung, jong = off // 588, (off % 588) // 28, off % 28
            out.append(chr(0x1100 + cho))
            out.append(chr(0x1161 + jung))
            if jong:
                out.append(chr(0x11A7 + jong))
        else:
            out.append(c)
    return "".join(out)


def _strip_particles(text: str) -> str:
    """조사·어미 정규화 — 각 어절 양끝 문장부호 → 어미 조사 반복 제거."""
    tokens = text.split()
    out = []
    for tok in tokens:
        tok = tok.strip(_TRIM_PUNCT)
        prev = None
        while tok and tok != prev:
            prev = tok
            for p in PARTICLES:
                if tok.endswith(p) and len(tok) > len(p):
                    tok = tok[:-len(p)]
                    break
        out.append(tok)
    return " ".join(out)


def _levenshtein(a: str, b: str) -> int:
    if len(a) < len(b):
        a, b = b, a
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        curr = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            curr[j] = min(prev[j] + 1, curr[j - 1] + 1, prev[j - 1] + cost)
        prev = curr
    return prev[-1]


def _sliding_lev_hit(text_j: str, kw_j: str, tol: float = 0.2) -> bool:
    """텍스트 자모열에서 keyword 자모 길이 ±2 창을 훑으며 편집거리 ≤ tol·|kw_j|."""
    n = len(kw_j)
    if n == 0 or not text_j:
        return False
    max_dist = max(1, int(n * tol))
    for w in range(max(1, n - 2), n + 3):
        if w > len(text_j):
            break
        for i in range(0, len(text_j) - w + 1):
            if _levenshtein(text_j[i:i + w], kw_j) <= max_dist:
                return True
    return False


def _trigrams(s: str) -> set:
    if len(s) < 3:
        return {s} if s else set()
    return {s[i:i + 3] for i in range(len(s) - 2)}


def _jaccard_trigram(text: str, kw: str) -> float:
    a, b = _trigrams(text.lower()), _trigrams(kw.lower())
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def match_keyword(text: str, kw: str) -> tuple[bool, float]:
    """단계적 매칭 파이프라인 — plan §2.3 표 그대로. → (matched, confidence)."""
    t = text.lower()
    k = kw.lower()
    # ① 정확 매칭
    if k in t:
        return True, 1.0
    # ② 조사·어미 정규화 재매칭
    t2 = _strip_particles(t)
    if k in t2:
        return True, 0.9
    # 짧은 시그니처(자모 ≤ 4)는 오탐 위험 커서 ①·②까지만
    kj = to_jamo(k)
    if len(kj) <= 4:
        return False, 0.0
    # ③ 자모 substring
    tj = to_jamo(t)
    if kj in tj:
        return True, 0.8
    # ④ 자모 Levenshtein 슬라이딩 (≤ 20% 자모수)
    if _sliding_lev_hit(tj, kj, 0.2):
        return True, 0.6
    # ⑤ 3-gram 자카드 ≥ 0.5
    if _jaccard_trigram(t, k) >= 0.5:
        return True, 0.4
    return False, 0.0


# ── §2.2 세그먼트 분류 (MAN 우선) ────────────────────────────────────────────
def classify_segment(text: str) -> tuple[str, float]:
    t = text.lower()
    man = sum(1 for w in MAN_LEXICON if w.lower() in t)
    cas = sum(1 for w in CAS_LEXICON if w.lower() in t)
    if man >= 1:
        return "MAN", min(1.0, 0.6 + 0.15 * man)
    if cas >= 1:
        return "CAS", min(1.0, 0.5 + 0.15 * cas)
    return "CAS", 0.3


# ── §2.3 인텐트 분류 — 룰(rule) fast-path → 임베딩(embed) 폴백 ───────────────
def _rule_classify(text: str) -> tuple[str | None, float]:
    """룰 매칭 argmax (동률은 INTENT_LEXICON dict 순서 앞쪽이 이김)."""
    best_intent: str | None = None
    best_conf = 0.0
    for intent, kws in INTENT_LEXICON:
        for kw in kws:
            ok, conf = match_keyword(text, kw)
            if ok and conf > best_conf:
                best_conf = conf
                best_intent = intent
    return best_intent, best_conf


# ── 임베딩 폴백: multilingual-e5-small ───────────────────────────────────────
# 룰이 못 잡거나 conf<0.5 일 때만 태움. 시그니처를 startup 에서 한 번 인코딩해
# 캐시(_EMBED) — 이후 매 쿼리는 임베딩 1회 + 정규화된 내적만 하면 됨 (수 ms).
# e5 는 입력에 "query: " / "passage: " 프리픽스가 필수 (모델 카드 명세).
_EMBED: dict = {"model": None, "labels": None, "vecs": None}
_EMBED_MODEL = "intfloat/multilingual-e5-small"
# e5 는 문장-문장 대칭 매칭에서 유의어는 0.88+, 무관은 0.80- 로 갈리는 경향.
# 예시 발화 세트를 쓸 때 실측(스모크) 근거로 정한 임계값.
_EMBED_MIN_COS = 0.88
_EMBED_TRIED = False         # 설치·로드 실패한 뒤에도 계속 시도하지 않게


def warmup_embed() -> bool:
    """모델·예시발화 벡터 프리로드. 첫 사용자 요청이 2~4초 스톨하는 걸 방지.
    설치 안 됐거나 로드 실패면 False (rule-only 다운그레이드)."""
    global _EMBED_TRIED
    if _EMBED["model"] is not None:
        return True
    if _EMBED_TRIED:
        return False
    _EMBED_TRIED = True
    try:
        from sentence_transformers import SentenceTransformer  # type: ignore
        import numpy as np                                     # type: ignore
    except ImportError:
        return False
    try:
        model = SentenceTransformer(_EMBED_MODEL)
    except Exception:
        return False
    # 대칭(STS류) 매칭이라 양쪽 모두 "query: " 프리픽스 (e5 모델 카드 권고).
    # 예시는 자연 발화 문장이라 짧은 키워드보다 임베딩 변별력이 훨씬 좋음.
    labels, texts = [], []
    for intent, exs in INTENT_EXAMPLES.items():
        for ex in exs:
            labels.append(intent)
            texts.append(f"query: {ex}")
    vecs = model.encode(texts, normalize_embeddings=True,
                        convert_to_numpy=True, show_progress_bar=False)
    _EMBED.update(model=model, labels=labels, vecs=vecs, np=np)
    return True


def _embed_classify(text: str) -> tuple[str | None, float]:
    """임베딩 코사인 argmax → (intent, cosine)  ·  임계 미달이면 (None, cosine)."""
    if not warmup_embed():
        return None, 0.0
    model, labels, vecs, np = _EMBED["model"], _EMBED["labels"], _EMBED["vecs"], _EMBED["np"]
    qv = model.encode([f"query: {text}"], normalize_embeddings=True,
                      convert_to_numpy=True, show_progress_bar=False)[0]
    sims = vecs @ qv                     # 둘 다 정규화됐으니 내적 = 코사인
    # intent 별 max cosine 취해 argmax (같은 intent 의 여러 시그니처는 대표값만)
    per_intent: dict[str, float] = {}
    for lbl, s in zip(labels, sims):
        v = float(s)
        if v > per_intent.get(lbl, -1.0):
            per_intent[lbl] = v
    best_intent = max(per_intent, key=per_intent.get) if per_intent else None
    best_cos = per_intent[best_intent] if best_intent else 0.0
    if best_cos < _EMBED_MIN_COS:
        return None, round(best_cos, 3)
    return best_intent, round(best_cos, 3)


def _cos_to_conf(cos: float) -> float:
    """cosine ∈ [_EMBED_MIN_COS, 1.0] → confidence ∈ [0.50, 0.75].
    룰 스테이지 ⑤ (0.4) 보다 위, 스테이지 ③ (0.8) 아래 — 임베딩 폴백임을 명시."""
    span = max(1e-6, 1.0 - _EMBED_MIN_COS)
    c = 0.50 + (cos - _EMBED_MIN_COS) / span * 0.25
    return round(min(0.75, max(0.5, c)), 3)


def classify_intent(text: str) -> dict:
    """룰이 conf≥0.5 로 잡으면 그대로. 아니면 임베딩 폴백. 실패시 None.
    → {intent, confidence, source, embed_cos}  source ∈ 'rule'|'embed'|'none'."""
    rule_intent, rule_conf = _rule_classify(text)
    if rule_intent and rule_conf >= 0.5:
        return {"intent": rule_intent, "confidence": rule_conf,
                "source": "rule", "embed_cos": None}
    embed_intent, embed_cos = _embed_classify(text)
    if embed_intent is not None:
        return {"intent": embed_intent, "confidence": _cos_to_conf(embed_cos),
                "source": "embed", "embed_cos": embed_cos}
    # 임베딩도 실패 — 룰의 저신뢰 결과라도 넘기고, needs_new_context 는 상위에서 켬
    if rule_intent:
        return {"intent": rule_intent, "confidence": rule_conf,
                "source": "rule", "embed_cos": embed_cos or None}
    return {"intent": None, "confidence": 0.0,
            "source": "none", "embed_cos": embed_cos or None}


# ── §2.1 phase 는 서버 주입 (텍스트 무관) ────────────────────────────────────
def resolve_phase(game_state: str | None) -> str:
    if game_state == "draft":
        return "DR"
    if game_state == "live":
        return "LV"
    if game_state == "post_game":
        return "PG"
    return "MT"


def understand(text: str, game_state: str | None = None) -> Understanding:
    phase = resolve_phase(game_state)
    segment, seg_conf = classify_segment(text)
    ic = classify_intent(text)
    conf = {"segment": round(seg_conf, 2), "intent": round(ic["confidence"], 2)}
    if ic["embed_cos"] is not None:
        conf["embed_cos"] = ic["embed_cos"]
    # intent 불명(None) 또는 confidence < 0.5 → 다음 레이어에 불확실성 신호
    needs_new = (ic["intent"] is None) or (ic["confidence"] < 0.5)
    return Understanding(
        text=text, phase=phase, segment=segment, intent=ic["intent"],
        confidence=conf, needs_new_context=needs_new,
        intent_source=ic["source"],
    )
