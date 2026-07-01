"""
arbiter.py — 이벤트/발화 흡수부터 감정 계산까지 (단일 엔진)
==========================================================
소스층 자극을 받아 선택 → 값매기기 → 감정(E·A·열기) 적분까지 한 곳에서 한다.
(예전엔 Arbiter / Affect 두 모듈이었지만 하나로 합쳤다.)

  자극 풀 ─> [선택·값매기기] ─> [감정 적분] E·A·열기 ─> 표현/채팅

하는 일:
  1) 변환    — Riot 원본 이벤트를 자극으로 (kind·intensity, 팀 → valence 부호).
  2) 값매기기 — 자극종류 → valence, importance × 부스터 × 팬심 → salience.
  3) 선택    — salience·성격가중치로 승자(primary/secondary)를 고른다.
  4) 분기    — route.chat (말까지 할지)을 판단한다.
  5) 적분    — valence·salience 로 감정 상태(E·A·열기)를 갱신한다 (구 Affect).

감정 수학 자체는 게임을 몰라도 되는 순수 계산이지만, 게임 지식(팀·부스터·팬심)과
한 모듈에 둔다 — 이벤트 하나가 감정까지 되는 흐름을 한 파일에서 읽을 수 있게.
"""
from __future__ import annotations
from dataclasses import dataclass, field, replace
import hashlib
import math

from config import PersonalityConfig
from expression import express, expression_intent


# ===========================================================================
# 입력 자극(moment)
# ===========================================================================
@dataclass
class Candidate:
    """Arbiter 가 소화하기 전의 한 자극(raw moment)."""
    kind: str
    intensity: float = 0.5          # 자극 세기 (목표면 urgency)
    source: str = "user"            # delta | user | goal | backlog
    age: float = 0.0                # Backlog 에서 묵은 턴 수 (0 = 이번 턴 새 자극)
    is_goal: bool = False           # 목표 엔진이 만든 내부 목표인가
    payload: dict = field(default_factory=dict)
    moment_id: str = ""             # 원본 참조 id (idempotency_key 기반)


# ===========================================================================
# 출력 계약 — AffectRequest (Affect 로 넘기는 정제 숫자)
# ===========================================================================
@dataclass
class Stim:
    """primary / secondary — 한 자극에서 뽑아낸 숫자 스칼라."""
    moment_id: str
    type: str                       # social | data | goal | system
    valence: float                  # -1~1 (아군 이벤트=+, 적군=-)
    salience: float                 # 주목도 = importance × 부스터
    source: str                     # delta | user | goal | backlog
    tags: list[str] = field(default_factory=list)  # 템플릿 선택용 라벨


@dataclass
class ReqState:
    intimacy: float = 0.0           # 친밀도 (유저↔캐릭터 관계)
    match_heat: float = 0.0         # 경기 열기 ← stats_update
    fan_tier: str = "rookie"        # 유저↔팔로우팀 Fan심 등급 (표시/로그용)
    fan_factor: float = 1.0         # 팔로우팀(data) 자극 salience 배율
    prev_affect: dict = field(default_factory=lambda: {"E": 0.0, "A": 0.15})


@dataclass
class AffectRequest:
    char_id: str                    # 어느 성격 Config 를 쓸지 (세션 정보)
    turn_id: str                    # 시스템 턴 일련번호 (로그 추적용)
    seed: int                       # char_id + turn_id 해시 (결정론 재현용)
    tick: int                       # 논리 시계 = gameTime (수신지연 무관)
    primary: Stim | None            # 가장 끌린 자극
    secondary: Stim | None          # 곁들임 자극 (없으면 None)
    path: str                       # AND/OR 룰 결과 = 표현 모드
    route: dict                     # {"chat": bool} — 분기값 (Affect 가 통과시킴)
    deferred_ids: list[str]         # Backlog 로 넘긴 자극 id
    state: ReqState


# ===========================================================================
# Appraisal — 자극종류 → (valence, importance)
#   원래 affect_engine 에 있던 APPRAISAL_TABLE 을 Arbiter 가 흡수했다.
#   "이벤트종류 → importance / valence" 는 변환(Arbiter)의 책임이기 때문.
# ===========================================================================
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
DATA_KINDS = {"game_positive", "game_negative"}


def stim_type(c: Candidate) -> str:
    """social | data | goal | system."""
    if c.is_goal:
        return "goal"
    if c.kind in DATA_KINDS:
        return "data"
    if c.kind in SOCIAL_KINDS:
        return "social"
    return "system"


def valence_of(c: Candidate) -> float:
    """-1~1. data 는 map_event 가 이미 팀→부호를 풀어 kind 로 넘겨준다."""
    return APPRAISAL_TABLE.get(c.kind, (0.0, 0.3))[0]


def importance_of(c: Candidate) -> float:
    """이벤트종류 importance × 자극 세기."""
    base_imp = APPRAISAL_TABLE.get(c.kind, (0.0, 0.3))[1]
    return base_imp * c.intensity


def booster(c: Candidate) -> float:
    """bounty·멀티킬 등 원본 필드 기반 salience 부스터 (원본은 여기서만 소비)."""
    b = 1.0
    p = c.payload
    if p.get("bounty", 0) and p["bounty"] >= 300:
        b *= 1.15
    if p.get("multikill"):
        b *= 1.0 + 0.1 * min(4, int(p["multikill"]))
    return b


def tags_for(c: Candidate) -> list[str]:
    """템플릿 선택용 라벨. 실제 문구는 3층(컨텍스트 조립)이 채운다."""
    tags = [c.kind]
    for k in ("event", "team", "killType", "monsterType"):
        if c.payload.get(k):
            tags.append(str(c.payload[k]))
    return tags


# ===========================================================================
# salience — 두 종류를 구분한다 (노션 문서대로)
#   appraisal_salience : primary.salience 로 Affect 에 넘김 = importance × 부스터
#   select_score       : Arbiter 내부 줄세우기 = 세기 × 성격가중 × recency × 되먹임
# ===========================================================================
def appraisal_salience(c: Candidate) -> float:
    """Affect 로 넘기는 주목도 = importance × 부스터."""
    return importance_of(c) * booster(c)


# ===========================================================================
# 팬심(Fan심) — 유저↔팔로우팀 관여 깊이 → 팔로우팀 자극 salience 증폭기
#   노션 '팬심' 문서: 누적 Fan심(상승만)을 등급으로 변환하고, 등급이 개인화
#   ("Companion 이 더 뜨겁게 반응")를 좌우한다. 여기선 P0 범위 = 누적 등급만
#   반영한다(모멘텀/열기는 후속). valence 는 건드리지 않고 '팔로우팀 관련
#   자극(data)'의 salience(주목도)만 키운다 → 광팬일수록 우리 팀 이벤트에 더
#   크게 반응(좋을 땐 더 기쁘게, 나쁠 땐 더 속상하게). 선택 순위(select_score)
#   에는 관여하지 않는다.
# ===========================================================================
FAN_TIERS = [(8000, "die_hard"), (4000, "core"), (1500, "devoted"), (500, "follower")]
FAN_FACTOR = {"rookie": 1.0, "follower": 1.15, "devoted": 1.30, "core": 1.50, "die_hard": 1.80}


def fan_tier(cumulative: float) -> str:
    """누적 Fan심 → 등급 코드 (팬심 문서 7절 경계값 초안)."""
    for thr, name in FAN_TIERS:
        if cumulative >= thr:
            return name
    return "rookie"


def fan_factor(cumulative: float) -> float:
    """등급 → 팔로우팀(data) 자극 salience 배율 (rookie 1.0 → die_hard 1.8)."""
    return FAN_FACTOR[fan_tier(cumulative)]


def affect_mod(kind: str, prev: dict) -> float:
    """직전 감정(prev_affect)이 '무엇에 끌리는가'를 변조 = 되먹임 통로.
    Affect 안이 아니라 Arbiter 에서, 지난 턴 결과로 계산한다(턴 내 순환 없음)."""
    E = prev.get("E", 0.0)
    warmth = prev.get("warmth", 0.0)        # 친밀도 기반 친밀감(0~1)
    m = 1.0
    # 아직 서먹하면(친밀도 낮음) 사교적 자극에 덜 끌림 (0.4 ~ 1.0)
    if kind in SOCIAL_KINDS:
        m *= 0.4 + 0.6 * warmth
    # mood-congruent: 기분 좋을 땐 긍정 자극, 나쁠 땐 부정 자극에 더 끌림
    if kind in ("game_positive", "compliment", "greeting"):
        m *= 1.0 + 0.3 * max(0.0, E)
    if kind in ("game_negative", "insult"):
        m *= 1.0 + 0.3 * max(0.0, -E)
    return m


def select_score(c: Candidate, prev: dict, cfg: PersonalityConfig) -> float:
    """후보 1개 → 줄세우기 점수 1개 (되먹임 포함)."""
    base = c.intensity                               # 목표/자극 공통 척도
    weight = cfg.weight(c.kind)                      # 성격 주목 가중치
    recency = math.exp(-cfg.backlog_decay * c.age)   # 묵을수록 끌림 ↓
    mood = affect_mod(c.kind, prev)                  # 직전 감정 되먹임
    return base * weight * recency * mood


# ===========================================================================
# 선택 — 점수 받아 승자만 결정 (정책은 성격이 정함)
# ===========================================================================
def select(
    candidates: list[Candidate], prev: dict, cfg: PersonalityConfig
) -> tuple[list[Candidate], list[Candidate]]:
    if not candidates:
        return [], []
    scored = sorted(
        ((select_score(c, prev, cfg), c) for c in candidates),
        key=lambda x: x[0], reverse=True,
    )
    pol = cfg.select_policy
    if pol.type == "argmax":
        winners = [scored[0][1]]
    else:  # top_k: 임계 이상을 max_winners 까지 ("둘 다" 가능)
        winners = [c for s, c in scored if s >= pol.threshold][: pol.max_winners]
        if not winners:
            winners = [scored[0][1]]                 # 전부 임계 미만이면 최소 1개
    win_ids = {id(c) for c in winners}
    losers = [c for _, c in scored if id(c) not in win_ids]
    return winners, losers


# ===========================================================================
# 분기(route.chat) & 표현 모드(path)
# ===========================================================================
SPEAK_THRESHOLD = 0.45  # primary.salience 가 이 이상이면 먼저 말 검


def route_for(primary: Stim | None, user_spoke: bool) -> dict:
    """이번에 말까지 할지(chat) 판단. 재료(자극종류·salience·유저발화)는 Arbiter 만 가짐.
      - 유저가 직접 말 검            → chat: true
      - 목표(먼저 말 걸기)           → chat: true
      - 자극 salience ≥ 발화 임계    → chat: true
      - 그 외(미세 반응)             → chat: false (표정·이펙트만)"""
    if user_spoke:
        return {"chat": True}
    if primary is None:
        return {"chat": False}
    if primary.type == "goal":
        return {"chat": True}
    return {"chat": primary.salience >= SPEAK_THRESHOLD}


def path_for(winners: list[Candidate]) -> str:
    """AND/OR 룰 결과 = 표현 모드."""
    if not winners:
        return "idle"
    types = [stim_type(w) for w in winners]
    if len(set(types)) > 1:
        return "mixed_react"
    return {"social": "social_react", "data": "data_react",
            "goal": "goal_nudge", "system": "system_react"}[types[0]]


# ===========================================================================
# 상태 수집 헬퍼
# ===========================================================================
def make_seed(char_id: str, turn_id: str) -> int:
    """char_id + turn_id 해시 → 결정론 시드 (PYTHONHASHSEED 무관)."""
    h = hashlib.sha256(f"{char_id}:{turn_id}".encode("utf-8")).hexdigest()
    return int(h[:8], 16)


def match_heat_from(candidates: list[Candidate]) -> float:
    """경기 열기 스텁: 이번 턴 data 자극 세기의 평균 (실데이터는 stats_update)."""
    data = [c for c in candidates if c.kind in DATA_KINDS]
    if not data:
        return 0.0
    return round(min(1.0, sum(c.intensity for c in data) / len(data)), 3)


def _to_stim(c: Candidate, cfg: PersonalityConfig, fan_factor: float = 1.0) -> Stim:
    sal = appraisal_salience(c)
    if stim_type(c) == "data":          # 팔로우팀 경기 이벤트에만 팬심 배율 적용
        sal *= fan_factor
    return Stim(
        moment_id=c.moment_id or f"m_{c.kind}",
        type=stim_type(c),
        valence=round(valence_of(c), 3),
        salience=round(sal, 4),
        source=c.source,
        tags=tags_for(c),
    )


# ===========================================================================
# build_request — 한 턴의 Arbiter 출력 = AffectRequest
# ===========================================================================
def build_request(
    *,
    char_id: str,
    turn_id: str,
    tick: int,
    candidates: list[Candidate],
    intimacy: float,
    prev_affect: dict,          # {"E":, "A":}
    cfg: PersonalityConfig,
    user_spoke: bool,
    match_heat: float | None = None,
    fan: float = 0.0,               # 누적 Fan심 (유저↔팔로우팀)
    fan_target: bool = True,        # 팔로우팀이 있는가 (없으면 팬심 배율 무효)
    seed: int | None = None,
) -> tuple[AffectRequest, list[Candidate], list[Candidate]]:
    """후보 풀 → (AffectRequest, winners, losers).
    winners/losers 는 오케스트레이터(engine)가 표현·발화·Backlog 에 쓰도록 함께 반환."""
    prev = {"E": prev_affect.get("E", 0.0),
            "A": prev_affect.get("A", 0.15),
            "warmth": min(1.0, max(0.0, intimacy / 6.0))}  # 친밀도→친밀감(절친 6≈1.0)
    winners, losers = select(candidates, prev, cfg)

    tier = fan_tier(fan)
    ffac = fan_factor(fan) if fan_target else 1.0   # 팔로우팀 없으면 증폭 안 함
    primary = _to_stim(winners[0], cfg, ffac) if winners else None
    secondary = _to_stim(winners[1], cfg, ffac) if len(winners) > 1 else None

    req = AffectRequest(
        char_id=char_id,
        turn_id=turn_id,
        seed=seed if seed is not None else make_seed(char_id, turn_id),
        tick=tick,
        primary=primary,
        secondary=secondary,
        path=path_for(winners),
        route=route_for(primary, user_spoke),
        deferred_ids=[(c.moment_id or f"m_{c.kind}") for c in losers if not c.is_goal],
        state=ReqState(
            intimacy=round(intimacy, 3),
            match_heat=match_heat if match_heat is not None else match_heat_from(candidates),
            fan_tier=tier,
            fan_factor=round(ffac, 3),
            prev_affect={"E": round(prev["E"], 3), "A": round(prev["A"], 3)},
        ),
    )
    return req, winners, losers


# ===========================================================================
# Riot 어댑터 — 원본 이벤트 payload → 자극(kind·intensity·라벨)
#   팀(killerTeamID/teamID)을 풀어 valence 부호(game_positive/negative)로,
#   이벤트종류·등급을 intensity 로 환산한다. (원본 필드는 여기서만 소비)
# ===========================================================================
PERSPECTIVE_TEAM = 100  # 관점 팀 = '우리/팔로우 팀'
_MAJOR_MONSTER = {"baron": (1.0, "바론"), "dragon": (0.9, "드래곤"),
                  "elderDragon": (1.0, "장로 드래곤"), "riftHerald": (0.85, "전령"),
                  "voidGrub": (0.55, "공허충"), "horde": (0.55, "공허충")}
_TURRET_TIER = {"outer": 0.6, "inner": 0.7, "base": 0.85, "nexus": 1.0}


def _pteam(pid):
    return 100 if isinstance(pid, int) and 1 <= pid <= 5 else 200


def map_event(p: dict, team: int = PERSPECTIVE_TEAM):
    """게임 이벤트 payload → (kind, intensity, 설명) 또는 None(델타 아님).
    건물/포탑 teamID 는 '잃은(소유) 팀'으로 가정한다.
    team=0(팔로우 없음) → 중립 시청: 큰 플레이를 가벼운 흥분(positive·강도↓)으로."""
    if team == 0:
        base = map_event(p, 100)
        if base is None:
            return None
        _, inten, desc = base
        return "game_positive", round(inten * 0.7, 2), desc
    s = p.get("rfc461Schema")

    def kind(is_pos):
        return "game_positive" if is_pos else "game_negative"

    if s == "epic_monster_kill":
        mt = p.get("monsterType")
        if mt not in _MAJOR_MONSTER:
            return None              # 정글 잡몹(raptor 등)은 델타로 안 침
        inten, nm = _MAJOR_MONSTER[mt]
        return kind(p.get("killerTeamID") == team), inten, f"{nm} 처치"
    if s == "champion_kill":
        b = p.get("bounty") or 0
        return kind(p.get("killerTeamID") == team), min(0.95, 0.65 + (0.1 if b >= 300 else 0)), "챔피언 킬"
    if s == "champion_kill_special":
        kt = p.get("killType", "")
        return kind(_pteam(p.get("killer")) == team), 0.85 if kt == "firstBlood" else 0.8, kt or "특수 킬"
    if s == "building_destroyed":
        bt, tier = p.get("buildingType"), p.get("turretTier")
        inten = 0.8 if bt == "inhibitor" else _TURRET_TIER.get(tier, 0.7)
        nm = f"{tier} 타워" if bt == "turret" else (bt or "건물")
        return kind(p.get("teamID") != team), inten, f"{nm} 파괴"
    if s == "turret_plate_destroyed":
        return kind(p.get("teamID") != team), 0.45, "포탑 방패"
    if s == "game_end":
        win = p.get("winningTeam") == team
        return kind(win), 1.0, "게임 종료(승)" if win else "게임 종료(패)"
    return None


# ===========================================================================
# 감정 적분 (구 affect_engine) — valence·salience 숫자 → 기분 상태 E·A·열기
#   위에서 만든 AffectRequest 를 받아 감정 상태를 갱신한다. 게임 룰은 이미 위에서
#   숫자로 풀렸으므로 여기부턴 순수 계산이다. prev_affect 동봉으로 무상태 유지
#   (같은 요청이면 같은 출력).
# ===========================================================================
@dataclass
class AffectState:
    E: float = 0.0         # 정서   -1~+1
    A: float = 0.2         # 세기    0~1
    heat: float = 0.0      # 열기(경기 열기, match heat) 0~1 — 뜨거운 경기로 오르고 식음
    intimacy: float = 0.0  # 친밀도 누적치


@dataclass
class AffectOutput:
    """감정 출력. 앞(표현/채팅)으로만 흐른다."""
    E: float
    A: float
    intensity: float
    expression_intent: dict          # 표현층 직행 (표정·이펙트·톤). 항상.
    route: dict                      # 분기값 그대로 통과
    path: str
    tags: list[str] = field(default_factory=list)
    moment_id: str | None = None


def _decay_toward(value, baseline, rate, dt):
    return baseline + (value - baseline) * math.exp(-rate * dt)


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _integrate(E, A, heat, valence, salience, is_data, cfg):
    """valence·salience 한 쌍 → 기분 변화. data(경기)면 열기도 함께 달아오른다."""
    strength = salience                  # 주목도(importance×부스터)가 곧 자극 세기
    dE = valence * strength * cfg.reactivity
    dA = strength * cfg.arousal_gain
    E = _clamp(E + dE, -1.0, 1.0)
    A = _clamp(A + dA, 0.0, 1.0)
    if is_data:                          # 경기 이벤트만 열기를 올린다
        heat = _clamp(heat + strength * cfg.heat_gain, 0.0, 1.0)
    return E, A, heat


def affect(req: AffectRequest, prev_state: AffectState,
           cfg: PersonalityConfig, dt: float = 1.0):
    """AffectRequest → (new_state, AffectOutput, Expression)."""
    # (a) 감쇠 — 동봉된 prev_affect 기준으로 기저로 식음. 열기는 조용해지면 0 으로.
    base_E = req.state.prev_affect.get("E", prev_state.E)
    base_A = req.state.prev_affect.get("A", prev_state.A)
    E = _decay_toward(base_E, cfg.valence_bias, cfg.decay_E, dt)
    A = _decay_toward(base_A, 0.15, cfg.decay_A, dt)
    heat = _decay_toward(prev_state.heat, 0.0, cfg.decay_heat, dt)

    # (b) 적분 — primary(+secondary) 의 valence·salience 로 기분 갱신.
    #     data(경기) 자극이면 열기도 함께 달아오른다.
    for stim in (req.primary, req.secondary):
        if stim is None:
            continue
        E, A, heat = _integrate(E, A, heat, stim.valence, stim.salience,
                                stim.type == "data", cfg)

    # (c) 종합 세기 — A 와 |E| 의 혼합 (노션 예: 0.5·A + 0.5·|E|)
    intensity = round(_clamp(0.5 * A + 0.5 * abs(E), 0.0, 1.0), 3)

    new_state = replace(prev_state, E=E, A=A, heat=heat)
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
