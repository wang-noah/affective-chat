"""
engine.py — 한 턴 오케스트레이션 + 피드백 루프
=============================================
turn() 흐름:
  1) 목표 엔진(스텁): 상태 보고 내부 목표 생성 (예: 신규 -> 팔로우 유도)
  2) 후보 풀 = 새 자극 + Backlog(묵은 것) + 목표
  3) Affect update: salience -> Arbiter -> E/A/열기 갱신
  4) 진 자극 -> Backlog (age++ , 너무 식으면 폐기)
  5) 표현 매핑 + 채팅(스킵 게이트)
  6) 피드백: 상호작용 결과 -> 친밀도↑(+시간감소) -> 다음 턴 열기 기저↑ -> 더 따뜻
"""
from __future__ import annotations
from dataclasses import dataclass, field

from config import PersonalityConfig, load_config
from affect_engine import AffectState, Candidate, update, SOCIAL_KINDS
from expression import express
import chat


@dataclass
class Session:
    state: AffectState = field(default_factory=AffectState)
    backlog: list[Candidate] = field(default_factory=list)
    history: list[dict] = field(default_factory=list)
    following: list[str] = field(default_factory=list)


def goal_engine(session: Session) -> list[Candidate]:
    """내부 목표 생성 스텁. 신규(친밀도 0) + 팔로잉 없음 -> 팔로우 유도."""
    if session.state.intimacy < 0.5 and not session.following:
        return [Candidate("goal_follow_nudge", intensity=0.5, is_goal=True)]
    return []


BACKLOG_DROP = 0.05   # salience 가 이 밑이면 폐기되는 임계(나이로 감쇠시켜 처리)


def turn(session, stimuli, cfg, dt=1.0):
    # 2) 후보 풀: 새 자극 + Backlog + 목표
    candidates = list(stimuli) + session.backlog + goal_engine(session)

    # 3) Affect
    new_state, winners, losers = update(session.state, candidates, cfg, dt)
    session.state = new_state

    # 4) Backlog 갱신: 진 자극은 나이 먹고 보류, 너무 묵으면 폐기
    refreshed = []
    for c in losers:
        aged = Candidate(c.kind, c.intensity, c.is_goal, c.age + 1, c.payload)
        if aged.intensity * (cfg.backlog_decay ** aged.age) >= BACKLOG_DROP and not aged.is_goal:
            refreshed.append(aged)
    session.backlog = refreshed

    # 5) 표현 + 채팅 (주목한 것 중 첫 번째를 발화 대상으로)
    expr = express(session.state)
    primary = winners[0]
    text, route = chat.respond(primary, session.state, expr, cfg, session.history)

    # 6) 피드백 루프 — 결과를 친밀도/팔로잉으로 되먹임
    session.state.intimacy *= cfg.intimacy_decay          # 시간 감소
    if primary.kind in SOCIAL_KINDS and primary.kind != "insult":
        session.state.intimacy += cfg.intimacy_gain       # 우호적 상호작용 누적
    if primary.kind == "goal_follow_nudge":
        session.following.append("T1")                    # (데모) 유도 성공 가정

    session.history.append({"kind": primary.kind, "route": route, "text": text})
    return {
        "reacted": [w.kind for w in winners],
        "route": route,
        "E": session.state.E, "A": session.state.A,
        "openness": session.state.openness, "intimacy": session.state.intimacy,
        "expr": expr, "text": text,
        "backlog": [b.kind for b in session.backlog],
    }


# ---------------------------------------------------------------------------
# 데모 — 한 캐릭터, 여러 턴. 친밀도↑ -> 톤 warm 화, 반복 -> LLM 변주 확인
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import os
    cfg = load_config(os.path.join(os.path.dirname(__file__), "character.json"))
    s = Session()

    script = [
        [Candidate("greeting", 0.6)],
        [Candidate("greeting", 0.6)],
        [Candidate("greeting", 0.6), Candidate("game_positive", 0.9,
            payload={"team": "T1", "event": "바론"})],
        [Candidate("user_distress", 0.7, payload={"text": "오늘 회사에서 진짜 짜증났어"})],
        [Candidate("greeting", 0.6)],
    ]

    print(f"=== {cfg.name} ({cfg.archetype}) ===\n")
    for i, stim in enumerate(script, 1):
        r = turn(s, stim, cfg)
        e = r["expr"]
        print(f"[T{i}] in={[c.kind for c in stim]}")
        print(f"     반응={r['reacted']} route={r['route']} backlog={r['backlog']}")
        print(f"     E={r['E']:+.2f} A={r['A']:.2f} 열기={r['openness']:.2f} 친밀도={r['intimacy']:.2f}")
        print(f"     표현={e.face} {e.energy}/{e.tone} 파티클x{e.particles}")
        print(f"     말: {r['text']}\n")
