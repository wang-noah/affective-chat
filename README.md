# affective-chat — AI 캐릭터 채팅 엔진

성격(Config) → 감정(Affect) → 표현(Expression) → 발화(Chat)로 이어지는
한 턴 오케스트레이션 엔진. 결정론 코어 위에 필요할 때만 LLM 1티어를 얹는다.

## 구조

| 파일 | 역할 |
|------|------|
| `config.py` | SSOT 로더. `character.json` → 타입 있는 `PersonalityConfig` |
| `affect_engine.py` | 결정론 코어(LLM 0). salience → Arbiter → E/A/열기 갱신 |
| `expression.py` | 3숫자(E/A/열기) → 표정·에너지·톤 프리셋 매핑 |
| `chat.py` | 스킵 게이트. 정형은 T1 템플릿(과금 0), 자유 발화만 LLM |
| `engine.py` | 한 턴 오케스트레이션 + 피드백 루프 + 데모 진입점 |
| `playground.py` | 브라우저 컨트롤 패널 — 소스층 수치 조절 + 엔진 트레이스 |
| `character.json` | 캐릭터 성격 설정(루나) |

## 셋업

요구사항: **Python 3.9+** (데모는 추가 의존성 없음).

```bash
# (선택) 가상환경
python3 -m venv .venv
source .venv/bin/activate

# 데모 실행 — 키 없이 mock LLM으로 전체 루프 동작
python3 engine.py

# 브라우저 컨트롤 패널 — http://localhost:8765
python3 playground.py
```

## 라이브 LLM 모드

실제 Haiku 호출을 쓰려면:

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...
```

`engine.py`의 `chat.respond(...)` 호출에 `use_llm=chat.call_haiku`를 주입하면
자유 발화가 mock 대신 실제 모델로 응답한다. 컨텍스트 조립은 `chat.build_context()`가
어펙트 엔진의 종합 수치(친밀도·정서·열기)를 행동 지시로 번역해 담당한다.
