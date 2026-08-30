"""이슈 질의응답(챗봇) 검증 — 근거 밖의 말을 하지 않는가.

여기서 지키는 계약:
  · 답변은 **제공된 이슈 발췌**에서만 나온다 — 근거가 없으면 없다고 답해야 한다
  · 근거에 없는 이슈 키를 지어내면 **사후에 잡아낸다** (사용자가 그 번호를 찾으러 간다)
  · 건수·비율은 검색 결과가 아니라 **전수 계산**으로 답한다 — top-k 발췌로는 셀 수 없다
  · 대명사만 남은 후속 질문("그건 왜?")도 직전 질문을 붙여 검색이 성립한다
  · 발췌는 해결 단계를 **포함한다** — 추천 경로와 규칙이 다르다(사용자가 그걸 묻는다)
  · 스트리밍은 근거를 **먼저** 보낸다 — 무엇을 보고 답하는지 먼저 알아야 한다

실행:
    .venv/bin/python tests/test_issue_chat.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "backend"))

os.environ.update({
    "RVP_JIRA_POLL_SEC": "0", "RVP_PREWARM": "0", "RVP_MCP": "0",
    "RVP_AUTH_DEV_LOGIN": "1", "RVP_SESSION_SECRET": "test-secret-fixed",
    "RVP_USERS_FILE": str(ROOT / "tests" / "_users_test.yaml"),
})

import issue_chat as C  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'✓' if cond else '✗'} {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


RECS = [
    {"key": "LSI-7", "summary": "WLC 충전 중 NFC polling 지연", "status": "완료",
     "category": "NFC", "chip": "S3NRN", "symptom": "충전 중 태그 인식 실패",
     "root_cause": "충전 중 polling 주기가 밀린다", "resolution": "FW 3.2 에서 우선순위 조정",
     "workaround": "거치대에서 내린 뒤 태그"},
    {"key": "VOC-2", "summary": "지속 쓰기 중 thermal throttle", "status": "해야 할 일",
     "category": "Thermal", "chip": "PM9C3", "symptom": "속도가 절반으로 떨어짐",
     "customer_ask": "스펙 미달인지 알려주세요"},
    {"key": "IVOC-3", "summary": "게이트웨이 504 급증", "status": "완료",
     "category": "Outage", "chip": "", "symptom": "커넥션 풀 고갈",
     "root_cause": "keep-alive 불일치", "resolution": "풀 상향"},
]


def test_facts() -> None:
    print("\n[집계 — 세어서 답한다]")
    f = C.kb_facts(RECS)
    check("전체·해결·미해결 건수", "전체 3건" in f and "해결 2건" in f and "미해결 1건" in f, f)
    check("프로젝트별 집계", "LSI 1건" in f and "IVOC 1건" in f, f)
    check("분류별 집계", "NFC 1건" in f and "Outage 1건" in f, f)
    check("미해결만 따로 센다 — '미해결 중 뭐가 많냐' 는 실제로 받는 질문이다",
          "분류별(미해결만): Thermal 1건" in f, f)
    check("해결만도 따로 센다", "분류별(해결만)" in f and "NFC 1건" in f)
    check("빈 KB 는 빈 문자열", C.kb_facts([]) == "")


def test_excerpt() -> None:
    print("\n[발췌]")
    e = C.excerpt(RECS[0])
    check("해결 단계를 포함한다 — 사용자가 그걸 묻는다",
          "FW 3.2" in e and "거치대에서 내린" in e, e)
    check("키와 상태가 들어간다", "[LSI-7]" in e and "완료" in e)
    check("빈 필드는 줄을 만들지 않는다", "고객 요청" not in e)
    check("고객 요청이 있으면 들어간다", "스펙 미달인지" in C.excerpt(RECS[1]))
    check("상한을 지킨다", len(C.excerpt(RECS[0], limit=40)) <= 40)


def test_retrieval_query() -> None:
    print("\n[후속 질문]")
    hist = [{"role": "user", "content": "PM9C3 thermal throttle 사례 알려줘"},
            {"role": "assistant", "content": "속도가 절반으로 떨어지는 사례가 있습니다"}]
    q = C.retrieval_query("그건 왜 그래?", hist)
    check("직전 질문을 붙인다", "PM9C3" in q and "그건 왜" in q, q)
    check("답변 어휘는 붙이지 않는다 — 검색이 답변 쪽으로 끌려간다", "절반" not in q, q)
    check("긴 질문은 그대로", C.retrieval_query("무선 충전 중 태그 인식 실패 원인이 뭐야", []) ==
          "무선 충전 중 태그 인식 실패 원인이 뭐야")
    check("이력이 없으면 질문만", C.retrieval_query("왜?", []) == "왜?")


def test_prompt() -> None:
    print("\n[프롬프트]")
    p = C.build_prompt("충전 중 인식 실패 원인이 뭐야?", RECS, facts=C.kb_facts(RECS),
                       history=[{"role": "user", "content": "이전 질문"}])
    check("근거 발췌가 들어간다", "LSI-7" in p and "FW 3.2" in p)
    check("집계가 들어간다", "전체 3건" in p)
    check("대화 이력이 들어간다", "이전 질문" in p)
    check("근거 밖 금지 규칙", "지어내지 마세요" in p)
    check("발췌를 세지 말라고 못박는다", "발췌를 세어서 답하지 마세요" in p)
    check("근거가 없으면 그렇게 말한다",
          "관련 이슈를 찾지 못했습니다" in C.build_prompt("x", [], facts=""))
    scoped = C.build_prompt("이거 왜 이래?", RECS, scope_key="LSI-7")
    check("범위 지정이 프롬프트에 반영된다", "**LSI-7**" in scoped)


def test_citations() -> None:
    print("\n[인용 검증]")
    ok, bad = C.verify_citations("원인은 (LSI-7) 이고 (LSI-999) 도 관련 있습니다.", {"LSI-7", "VOC-2"})
    check("근거에 있는 키는 유효", ok == ["LSI-7"], str(ok))
    check("근거에 없는 키는 잡아낸다", bad == ["LSI-999"], str(bad))
    check("인용이 없으면 빈 목록", C.verify_citations("숫자가 없습니다.", {"LSI-7"}) == ([], []))
    check("파생 키도 원본으로 인정", C.verify_citations("(LSI-7) 참고", {"LSI-7-rca"})[1] == [])


# ── 엔드포인트 ──────────────────────────────────────────────────────────────
class _StubReco:
    def recommend(self, rec, k=6, exclude_key=None):
        return {"matches": [{"key": r["key"]} for r in RECS if r["key"] != exclude_key],
                "proposal": None, "coverage": True}


def _client():
    from fastapi.testclient import TestClient
    import server
    server._RECO_STATE = {"by_key": {r["key"]: r for r in RECS}, "reco": _StubReco()}
    server._llm_stream = lambda p, reasoning=False: iter(
        ["원인은 ", "(LSI-7) 입니다. ", "그리고 (LSI-404) 도 있습니다."])
    c = TestClient(server.app)
    c.post("/auth/dev-login", json={"email": "eng@example.com"})
    return server, c


def test_endpoints() -> None:
    print("\n[엔드포인트]")
    server, c = _client()
    d = c.post("/chat", json={"question": "충전 중 인식 실패 원인은?"}).json()
    check("답변이 온다", "LSI-7" in d["answer"], str(d)[:160])
    check("근거 목록이 온다", {s["key"] for s in d["sources"]} == {"LSI-7", "VOC-2", "IVOC-3"})
    check("유효 인용", d["citations"] == ["LSI-7"], str(d["citations"]))
    check("환각 인용을 표시한다", d["unsupported_mentions"] == ["LSI-404"],
          str(d["unsupported_mentions"]))
    check("빈 질문은 거절", "error" in c.post("/chat", json={"question": "  "}).json())

    r = c.post("/chat/stream", json={"question": "원인은?"})
    evs = [json.loads(l[6:]) for l in r.text.splitlines() if l.startswith("data: ")]
    kinds = [e["type"] for e in evs]
    check("근거를 먼저 보낸다", kinds[0] == "sources", str(kinds[:3]))
    check("델타로 흘려보낸다", "delta" in kinds)
    done = [e for e in evs if e["type"] == "done"][-1]
    check("완료에 인용 검증이 실린다",
          done["citations"] == ["LSI-7"] and done["unsupported_mentions"] == ["LSI-404"], str(done))
    check("범위 지정 시 그 이슈가 근거에 포함된다",
          c.post("/chat", json={"question": "이건?", "key": "VOC-2"}).json()["sources"][0]["key"] == "VOC-2")


def main() -> int:
    for fn in (test_facts, test_excerpt, test_retrieval_query, test_prompt,
               test_citations, test_endpoints):
        fn()
    print()
    if FAILS:
        print(f"실패 {len(FAILS)}건: {FAILS}")
        return 1
    print("모두 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
