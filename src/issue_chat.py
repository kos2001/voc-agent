"""이슈 질의응답(챗봇) — Jira 이슈 내용을 자연어로 묻고 답한다.

이 파일이 하는 일은 **프롬프트와 근거 조립**이다. 검색은 recommender 가, 생성은
서버의 스트리밍 경로가 이미 갖고 있다 — 그것들을 다시 만들지 않는다.

세 가지를 지킨다.

1) **근거 밖의 말을 하지 않는다.** 답변은 제공된 이슈 발췌에서만 나온다. 없으면
   "찾지 못했다" 고 답한다. 사내 담당자가 이 답을 믿고 고객에게 나가는 답변을 쓰므로,
   그럴듯한 추측은 틀린 답보다 나쁘다(틀린 줄도 모르고 전달된다).

2) **이슈 키를 인용한다.** 고객 답변과 정반대다 — 여기 사용자는 사내 담당자이고,
   추적 가능한 번호가 가장 유용한 정보다. 대신 **근거에 없는 키를 지어내면** 사후에
   잡아낸다(`verify_citations`).

3) **셀 수 있는 것은 세어서 준다.** "미해결 몇 건이야" 같은 질문은 top-k 발췌로는
   절대 맞출 수 없다 — 모델은 발췌 개수를 세거나 지어낸다. 그래서 집계는 검색이
   아니라 **계산해서** 컨텍스트에 넣는다. 검색으로 못 푸는 질문을 검색으로 푸는 척하지
   않는다.
"""
from __future__ import annotations

import re
from collections import Counter

import issue_keys

MAX_HISTORY = 6          # 직전 3턴(질문+답변)까지만 — 길어지면 검색 질의가 흐려진다
MAX_EXCERPT = 1200       # 이슈 1건당 발췌 상한(자)


def kb_facts(records: list[dict]) -> str:
    """세어서 답해야 하는 것들. 검색 결과가 아니라 **전수 계산**이다."""
    if not records:
        return ""
    resolved = [r for r in records if r.get("status") == "완료"]
    unresolved = [r for r in records if r.get("status") != "완료"]
    top = lambda c, n=6: ", ".join(f"{k} {v}건" for k, v in c.most_common(n)) or "없음"  # noqa: E731
    cat = lambda rs: Counter(r.get("category") or "(미분류)" for r in rs)  # noqa: E731
    chip = lambda rs: Counter(r.get("chip") or "(미상)" for r in rs)       # noqa: E731
    proj = Counter(str(r.get("key", "")).split("-")[0] for r in records)
    # 전체 분포만 주면 "미해결 중 무엇이 많냐" 에 답할 수 없다 — 실제로 그 질문을 받고
    # 모델이 "교차 집계가 없다" 고 답했다. 물어볼 것을 미리 세어 둔다.
    return (
        "## 지식베이스 집계 (전수 계산 — 이 숫자는 정확합니다)\n"
        f"- 전체 {len(records)}건 · 해결 {len(resolved)}건 · 미해결 {len(unresolved)}건\n"
        f"- 프로젝트별(전체): {top(proj)}\n"
        f"- 분류별(전체): {top(cat(records))}\n"
        f"- 분류별(미해결만): {top(cat(unresolved))}\n"
        f"- 분류별(해결만): {top(cat(resolved))}\n"
        f"- 칩·서비스별(전체): {top(chip(records))}\n"
        f"- 칩·서비스별(미해결만): {top(chip(unresolved))}\n")


def excerpt(rec: dict, limit: int = MAX_EXCERPT) -> str:
    """이슈 1건의 발췌. 해결 단계까지 **포함한다** — 질의응답에서는 답이 곧 그 내용이다.

    (추천 경로는 단계 인지 매칭 때문에 해결 필드를 질의에서 제외하지만, 여기서는
     사용자가 그걸 물어보는 것이므로 규칙이 다르다.)
    """
    parts = [f"[{rec.get('key', '?')}] {rec.get('summary', '')}",
             f"상태: {rec.get('status', '')} / 분류: {rec.get('category', '')}"
             f" / 칩·서비스: {rec.get('chip', '')}"]
    for label, field in (("증상", "symptom"), ("고객 요청", "customer_ask"),
                         ("조사 단서", "investigation"), ("근본 원인", "root_cause"),
                         ("해결책", "resolution"), ("임시 우회책", "workaround")):
        v = (rec.get(field) or "").strip()
        if v:
            parts.append(f"{label}: {v}")
    return "\n".join(parts)[:limit]


def retrieval_query(question: str, history: list[dict]) -> str:
    """검색에 쓸 질의. 대명사만 남은 후속 질문("그건 왜 그래?")은 혼자서 아무것도
    못 찾으므로 **직전 사용자 질문을 붙인다**. 답변까지 붙이면 답변 어휘가 검색을
    지배해 엉뚱한 이슈로 흘러간다 — 사용자 발화만 쓴다."""
    q = (question or "").strip()
    if len(q) >= 12:
        return q
    prev = [h.get("content", "") for h in (history or []) if h.get("role") == "user"]
    return (prev[-1] + " " + q).strip() if prev else q


def build_prompt(question: str, records: list[dict], *, facts: str = "",
                 history: list[dict] | None = None, scope_key: str = "",
                 policy_docs: str = "") -> str:
    """질의응답 프롬프트. 근거는 이슈 발췌, 집계는 계산값, 규칙은 사내 지침.

    "규정이 어떻게 돼 있어?" 는 이슈만으로는 답할 수 없는 질문이다 — 답은 사례가
    아니라 지침 문서에 있다.
    """
    ctx = "\n\n".join(excerpt(r) for r in records) or "(관련 이슈를 찾지 못했습니다)"
    turns = ""
    for h in (history or [])[-MAX_HISTORY:]:
        role = "사용자" if h.get("role") == "user" else "당신"
        turns += f"{role}: {(h.get('content') or '').strip()[:600]}\n"
    scope = (f"\n이 질문은 **{scope_key}** 이슈에 대한 것입니다. 그 이슈를 중심으로 답하세요.\n"
             if scope_key else "")
    return (
        "당신은 사내 이슈 트래커의 내용을 잘 아는 분석 담당자입니다. 아래 **제공된 이슈 "
        "발췌와 집계**만 근거로 질문에 한국어로 답하세요.\n\n"
        "규칙:\n"
        "- 발췌에 없는 내용은 지어내지 마세요. 모르면 '제공된 이슈에서는 확인되지 "
        "않습니다' 라고 답하고, 무엇을 더 보면 알 수 있는지 알려주세요.\n"
        "- 근거가 되는 이슈 키를 (LSI-7) 처럼 문장 옆에 답니다. **제공된 키만** 쓰세요.\n"
        "- 건수·비율은 '지식베이스 집계' 값을 쓰세요. 발췌를 세어서 답하지 마세요 "
        "(발췌는 관련 상위 몇 건일 뿐입니다).\n"
        "- 짧고 구체적으로. 표나 목록이 더 명확하면 그렇게 쓰세요.\n"
        "- 한자/CJK 한자 금지.\n"
        + scope
        + (f"\n## 지금까지의 대화\n{turns}" if turns else "")
        + (f"\n## 사내 지침 (Confluence·FAQ — 규정을 묻는 질문의 답은 여기에 있습니다)\n"
           f"{policy_docs}\n" if policy_docs else "")
        + (f"\n{facts}" if facts else "")
        + f"\n## 관련 이슈 발췌\n{ctx}\n\n## 질문\n{question}")


_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")


def verify_citations(answer: str, allowed: set[str]) -> tuple[list[str], list[str]]:
    """본문이 인용한 키를 검증. 반환 (유효 인용, 근거에 없는 인용).

    근거에 없는 키가 나오면 그건 환각이다. 사내 담당자는 그 번호를 찾으러 가고,
    없다는 것을 알기까지 시간을 쓴다 — 조용히 두면 안 된다.
    """
    mentioned = issue_keys.find_set(answer)
    ok = sorted(mentioned & issue_keys.expand(allowed))
    bad = sorted(mentioned - issue_keys.expand(allowed))
    return ok, bad
