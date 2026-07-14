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


# ── 인텐트 시그니처 (구체적 → 일반적 순, argmax 동률은 이 순서로 tie-break) ──
# 노션 plan §2.3 표 그대로. "champion_strength" 는 어휘가 매우 짧아 오탐 위험이
# 크므로 dict 뒤쪽. "draft_winrate" 처럼 구체어("1픽","표본")가 있는 인텐트는 앞.
INTENT_LEXICON: list[tuple[str, list[str]]] = [
    ("draft_winrate",           ["1픽", "표본", "이 패치", "승률"]),
    ("counter_matchup",         ["라인전 상성", "카운터", "상성", "매치업"]),
    ("ban_intent",              ["저격밴", "왜 밴", "밴"]),
    ("mvp_analysis",            ["MVP", "오늘 최고", "캐리"]),
    ("defeat_analysis",         ["왜 졌", "왜 진", "패배", "아쉬웠", "결정적"]),
    ("form_vs_career",          ["커리어 스탯", "커리어 대비", "오늘 폼"]),
    ("team_draft_profile",      ["사이드 승률", "밴 우선순위", "팀 승률", "프로파일"]),
    ("h2h_history",             ["H2H", "상대전적", "라이벌", "맞대결"]),
    ("roster_change_form",      ["이적 후 폼", "팀 바꾸고", "이적"]),
    ("patch_impact",            ["티어 바뀌", "너프", "버프", "패치"]),
    ("objective_value",         ["오브젝트 승률", "바론", "장로", "먹으면"]),
    ("laning_baseline",         ["골드 diff", "라인전", "CS", "평소"]),
    ("comp_timing",             ["파워스파이크", "스케일링", "언제 세져", "초반", "후반"]),
    ("comeback_odds",           ["졌잘싸", "해볼 만", "역전", "뒤집", "아직"]),
    ("win_probability",         ["골드차 승률", "승리확률", "누가 이겨", "앞서", "이기고", "유리"]),
    ("player_performance",      ["지금 잘하", "지금 못", "오늘 잘", "못하는"]),
    # "이 선수" 자리를 선수 이름으로 바꿔 부르는 게 흔해서 "누구" 를 별도 시그니처로 추가.
    # dict 순으로 mvp_analysis(4위) 등이 앞서므로 "MVP가 누구야?" 는 여전히 mvp_analysis 로 감.
    ("player_bio",              ["이 선수 누구", "누구야", "누구지", "누구임", "누군", "누구",
                                  "어느 팀", "뭐 하던", "유명"]),
    ("comp_identity",           ["어떤 조합", "뭐가 좋아", "조합"]),
    ("player_champion_mastery", ["원챔", "장인", "시그니처", "숙련", "잘해"]),
    ("champion_strength",       ["좋은 픽", "요즘 뜨", "티어", "op", "쎄", "센", "강"]),
]

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


# ── §2.3 인텐트 분류 (argmax, 동률은 dict 순서) ──────────────────────────────
def classify_intent(text: str) -> tuple[str | None, float]:
    best_intent: str | None = None
    best_conf = 0.0
    for intent, kws in INTENT_LEXICON:
        for kw in kws:
            ok, conf = match_keyword(text, kw)
            # >= 를 쓰면 뒤쪽 인텐트가 앞쪽을 덮음 → dict 앞쪽 우선을 위해 > 만
            if ok and conf > best_conf:
                best_conf = conf
                best_intent = intent
    return best_intent, best_conf


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
    intent, int_conf = classify_intent(text)
    # intent 불명(None) 또는 confidence < 0.5 → 다음 레이어에 불확실성 신호
    needs_new = (intent is None) or (int_conf < 0.5)
    return Understanding(
        text=text, phase=phase, segment=segment, intent=intent,
        confidence={"segment": round(seg_conf, 2), "intent": round(int_conf, 2)},
        needs_new_context=needs_new,
    )
