"""
engine.py — 한 턴 오케스트레이션
=============================================
노션 단방향 파이프라인:  자극 ─> Arbiter ─(AffectRequest)─> Affect ─> 표현/채팅

turn() 흐름:
  1) 목표 엔진(스텁): 상태 보고 내부 목표 생성 (예: 신규 -> 팔로우 유도)
  2) 후보 풀 = 새 자극 + 목표
  3) Arbiter: 후보 → 선택 + valence·salience + route·path → AffectRequest
  4) Affect:  숫자로 E·A·intensity 계산 (route/path/tags passthrough)
  5) 표현(항상) + 채팅(route.chat==true 일 때만, 스킵 게이트로 T1/LLM)

캐릭터가 답하면 그 턴 로직 끝. 친밀도·관계 상태를 스스로 올리는 되먹임은 없다
— 친밀도·팔로잉·팬심은 전부 외부에서 주어지는 입력이다.
"""
from __future__ import annotations
import os
from dataclasses import dataclass, field
from typing import Callable, Protocol

from config import PersonalityConfig, load_config
from affect_engine import AffectState, affect, decay_state
from arbiter import Candidate, build_request, match_heat_from
from expression import express
import chat


@dataclass
class Session:
    """한 캐릭터를 '지금 보는' 한 세션.
    성격(cfg)·응원팀은 캐릭터 정의(고정)에서, 기분·친밀도·팔로잉은 서버에 저장된
    관계 상태에서 온다. backlog/history 는 이 세션 동안만 사는 휘발 상태."""
    cfg: PersonalityConfig
    perspective_team: int = 100
    state: AffectState = field(default_factory=AffectState)
    history: list[dict] = field(default_factory=list)
    following: list[str] = field(default_factory=list)
    last_tick: int = 0                     # 마지막 처리 tick (스왑 복귀 시 공백 감쇠용)


def goal_engine(session: Session) -> list[Candidate]:
    """내부 목표 생성 스텁. 신규(친밀도 낮음) + 팔로잉 없음 -> 팔로우 유도."""
    if session.state.intimacy < 0.5 and not session.following:
        return [Candidate("goal_follow_nudge", intensity=0.5, source="goal", is_goal=True)]
    return []


def speech_primary(winners):
    """발화 대상 선택. 자유 발화(유저가 직접 건 대화)를 실시간 델타·정형 자극보다
    우선한다 — 살리언스 1등이 게임 알림이라도, 유저 발화엔 먼저 답한다."""
    for w in winners:
        if w.kind in chat.FREEFORM_KINDS:
            return w
    return winners[0] if winners else None


def turn(session, stimuli, cfg=None, dt=1.0, turn_id="t_0", tick=0, fan=0.0):
    cfg = cfg or session.cfg                 # 세션이 자기 성격을 소유 (없으면 인자로 받음)
    # 2) 후보 풀: 새 자극 + 목표 (Backlog 없음 — 답하면 그 턴 끝)
    candidates = list(stimuli) + goal_engine(session)
    user_spoke = any(c.source == "user" for c in candidates)

    # 3) Arbiter: 후보 → AffectRequest (+ winners/losers 는 오케스트레이션용)
    #    팬심(fan)은 팔로우팀이 있을 때만 팔로우팀 자극 salience 를 키운다.
    req, winners, losers = build_request(
        char_id=cfg.name, turn_id=turn_id, tick=tick,
        candidates=candidates,
        intimacy=session.state.intimacy,
        prev_affect={"E": session.state.E, "A": session.state.A},
        match_heat=match_heat_from(candidates),
        fan=fan, fan_target=bool(session.following),
        prev_heat=session.state.heat, dt=dt,
        cfg=cfg, user_spoke=user_spoke,
    )

    # 4) Affect: 숫자만 받아 기분 계산 (순수 함수)
    new_state, output, expr = affect(req, session.state, cfg, dt)
    session.state = new_state

    # 5) 표현(항상) + 채팅(route.chat 일 때만) — 답하면 이 턴 끝
    primary = speech_primary(winners)
    if output.route.get("chat") and primary is not None:
        text, route = chat.respond(primary, session.state, expr, cfg, session.history)
    else:
        text, route = None, "expr_only"   # 표정·이펙트만, 채팅 단계 진입 안 함

    # 대화 기록만 남긴다 (반복 회피용). 친밀도·팔로잉을 되먹이는 피드백은 없음.
    session.history.append({"kind": primary.kind if primary else None, "route": route, "text": text})
    session.last_tick = tick                              # 스왑 후 공백 감쇠 계산용
    return {
        "reacted": [w.kind for w in winners],
        "route": route,
        "chat": output.route.get("chat"),
        "path": output.path,
        "E": output.E, "A": output.A, "intensity": output.intensity,
        "heat": session.state.heat, "intimacy": session.state.intimacy,
        "expr": expr, "text": text,
        "dropped": list(req.deferred_ids),   # 이번 턴에 안 뽑혀 버려진 자극
    }


# ===========================================================================
# 다중 캐릭터 — 유저는 한 캐릭터를 골라 경기를 본다 (활성 1명).
#   성격(cfg)      : 캐릭터 정의, 고정·유저 공통  → characters/{char_id}.json
#   기분·친밀도    : user × char_id 관계 상태, 서버 저장  → RelationStore
# 교체하면 떠나는 캐릭터를 save, 새 캐릭터의 관계를 load 해 세션을 재구성한다.
# ===========================================================================
CHAR_DIR = os.path.join(os.path.dirname(__file__), "characters")

# 관계 상태 중 서버에 저장할 필드 (성격·히스토리는 저장 안 함)
_RELATION_FIELDS = ("E", "A", "heat", "intimacy")

# 논리시계(tick=gameTime ms) → 감쇠 dt 단위 변환 (기본: 1000ms ≈ 1턴 감쇠분)
TICKS_PER_DT = 1000.0


def load_character(char_id: str) -> PersonalityConfig:
    """캐릭터 정의(성격) 로드 — 유저와 무관한 고정값."""
    return load_config(os.path.join(CHAR_DIR, f"{char_id}.json"))


def dump(session: Session) -> dict:
    """서버 저장용 관계 상태 = 기분(E·A·열기) + 친밀도 + 팔로잉 (+ last_tick)."""
    s = session.state
    data = {k: getattr(s, k) for k in _RELATION_FIELDS}
    data["following"] = list(session.following)
    data["last_tick"] = session.last_tick
    return data


def restore(cfg: PersonalityConfig, data: dict | None) -> Session:
    """성격(cfg, 고정) + 저장된 관계(data) → 세션. data=None 이면 신규 관계(중립)."""
    data = data or {}
    base = AffectState()   # 신규 기본값(E=0, A=0.2, heat=0, intimacy=0)
    return Session(
        cfg=cfg,
        perspective_team=cfg.perspective_team,
        state=AffectState(**{k: data.get(k, getattr(base, k)) for k in _RELATION_FIELDS}),
        following=list(data.get("following", [])),
        last_tick=data.get("last_tick", 0),
    )


class RelationStore(Protocol):
    """유저×캐릭터 관계 상태 저장소. 서버 DB 로 교체될 자리 — 인터페이스만 고정한다."""
    def load(self, user_id: str, char_id: str) -> dict | None: ...
    def save(self, user_id: str, char_id: str, data: dict) -> None: ...


class MemoryStore:
    """MVP 스텁 — 프로세스 메모리 dict. 서버 연동 시 이 클래스만 갈아끼우면 된다."""
    def __init__(self) -> None:
        self._db: dict[tuple[str, str], dict] = {}

    def load(self, user_id: str, char_id: str) -> dict | None:
        return self._db.get((user_id, char_id))

    def save(self, user_id: str, char_id: str, data: dict) -> None:
        self._db[(user_id, char_id)] = dict(data)


def switch_character(
    store: RelationStore,
    user_id: str,
    next_char_id: str,
    *,
    now_tick: int = 0,
    prev_session: Session | None = None,
    prev_char_id: str | None = None,
    cfg_loader: Callable[[str], PersonalityConfig] = load_character,
) -> Session:
    """경기 도중 캐릭터 교체(첫 진입이면 prev_* 생략).
      1) 떠나는 캐릭터의 관계 상태 저장(동결)
      2) 새 캐릭터 성격(고정) 로드
      3) 이 유저와 새 캐릭터의 관계 로드 (없으면 신규)
      4) 성격+관계 합쳐 세션 재구성
      5) 공백 감쇠 — 이 캐릭터를 안 보던 동안(now_tick-last_tick) 기분이 기저로 식음.
         신규 캐릭터(last_tick=0)는 감쇠 없이 중립에서 시작."""
    if prev_session is not None and prev_char_id is not None:
        store.save(user_id, prev_char_id, dump(prev_session))
    cfg = cfg_loader(next_char_id)
    session = restore(cfg, store.load(user_id, next_char_id))

    gap = now_tick - session.last_tick
    if session.last_tick and gap > 0:
        session.state = decay_state(session.state, cfg, gap / TICKS_PER_DT)
        session.last_tick = now_tick
    return session


# ---------------------------------------------------------------------------
# 데모 — 한 캐릭터, 여러 턴. 반복 -> LLM 변주, 게임 이벤트 -> 열기↑ 확인
# ---------------------------------------------------------------------------
def _run(session, script, *, base_tick=0):
    for i, stim in enumerate(script, 1):
        r = turn(session, stim, turn_id=f"t_{i:04d}", tick=base_tick + i * 1000)
        e = r["expr"]
        print(f"[T{i}] in={[c.kind for c in stim]}")
        print(f"     반응={r['reacted']} path={r['path']} chat={r['chat']} route={r['route']} dropped={r['dropped']}")
        print(f"     E={r['E']:+.2f} A={r['A']:.2f} intensity={r['intensity']:.2f} 열기={r['heat']:.2f} 친밀도={r['intimacy']:.2f}")
        print(f"     표현={e.face} {e.energy}/{e.tone} 파티클x{e.particles}")
        print(f"     말: {r['text']}\n")


if __name__ == "__main__":
    # 유저 한 명이 캐릭터를 골라 경기를 보다가, 도중에 다른 캐릭터로 교체하는 시나리오.
    # 관계(친밀도·기분)는 MemoryStore(=서버 자리)에 char_id 별로 저장·복원된다.
    store = MemoryStore()
    USER = "noah"

    greet = [Candidate("greeting", 0.6, source="user")]
    script_a = [greet, greet,
                [Candidate("game_positive", 0.9, source="delta", payload={"team": "T1", "event": "바론"})]]

    # 1) 루나 선택 → 몇 턴 시청 (친밀도가 쌓인다)
    luna = switch_character(store, USER, "luna")
    print(f"=== {luna.cfg.name} ({luna.cfg.archetype}) 선택, 응원팀={luna.perspective_team} ===\n")
    _run(luna, script_a)

    # 2) 경기 도중 리안으로 교체 → 루나 관계는 서버에 동결 저장, 리안은 신규(친밀도 0)에서 시작
    rian = switch_character(store, USER, "rian", now_tick=3000,
                            prev_session=luna, prev_char_id="luna")
    print(f"=== {rian.cfg.name} ({rian.cfg.archetype}) 로 교체, 응원팀={rian.perspective_team} ===\n")
    _run(rian, [greet, greet], base_tick=3000)

    # 3) 다시 루나로 복귀 → now_tick=6000, 떠난 tick=3000 → 3.0dt 만큼 기분이 식은 채 재개
    luna2 = switch_character(store, USER, "luna", now_tick=6000,
                             prev_session=rian, prev_char_id="rian")
    print(f"=== {luna2.cfg.name} 복귀 — 공백감쇠 후 기분이 식은 채 재개 "
          f"(E={luna2.state.E:+.2f}, 열기={luna2.state.heat:.2f}, 친밀도={luna2.state.intimacy:.2f}) ===\n")
    _run(luna2, [greet], base_tick=6000)
