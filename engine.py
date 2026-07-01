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
from dataclasses import dataclass, field

from config import PersonalityConfig, load_config
from arbiter import Candidate, build_request, match_heat_from, AffectState, affect
from expression import express
import chat


@dataclass
class Session:
    state: AffectState = field(default_factory=AffectState)
    history: list[dict] = field(default_factory=list)
    following: list[str] = field(default_factory=list)


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


def turn(session, stimuli, cfg, dt=1.0, turn_id="t_0", tick=0, fan=0.0):
    # 2) 후보 풀: 새 자극 + 목표
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


# ---------------------------------------------------------------------------
# 데모 — 한 캐릭터, 여러 턴. 반복 -> LLM 변주, 게임 이벤트 -> 열기↑ 확인
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import os
    cfg = load_config(os.path.join(os.path.dirname(__file__), "character.json"))
    s = Session()

    script = [
        [Candidate("greeting", 0.6, source="user")],
        [Candidate("greeting", 0.6, source="user")],
        [Candidate("greeting", 0.6, source="user"),
         Candidate("game_positive", 0.9, source="delta", payload={"team": "T1", "event": "바론"})],
        [Candidate("user_distress", 0.7, source="user", payload={"text": "오늘 회사에서 진짜 짜증났어"})],
        [Candidate("greeting", 0.6, source="user")],
    ]

    print(f"=== {cfg.name} ({cfg.archetype}) ===\n")
    for i, stim in enumerate(script, 1):
        r = turn(s, stim, cfg, turn_id=f"t_{i:04d}", tick=i * 1000)
        e = r["expr"]
        print(f"[T{i}] in={[c.kind for c in stim]}")
        print(f"     반응={r['reacted']} path={r['path']} chat={r['chat']} route={r['route']} dropped={r['dropped']}")
        print(f"     E={r['E']:+.2f} A={r['A']:.2f} intensity={r['intensity']:.2f} 열기={r['heat']:.2f} 친밀도={r['intimacy']:.2f}")
        print(f"     표현={e.face} {e.energy}/{e.tone} 파티클x{e.particles}")
        print(f"     말: {r['text']}\n")
