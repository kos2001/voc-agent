"""고객 대응 VOC 에이전트 검증 — 고객에게 나가는 글이 지켜야 할 계약.

여기서 지키는 계약:
  · 요청 유형이 답의 골격을 정한다 — "언제 고쳐지나요" 에 증상 접수로 답하지 않는다
  · 고객이 물은 문장을 놓치지 않는다 (요청 문장 추출)
  · 내부 이슈 키·한자·확정 일정 약속·보상 확약·반말은 **차단**된다 (발송 불가)
  · 다음 단계 누락·내부 용어는 경고다 — 전부 차단하면 검토자가 검사를 무시한다
  · LLM 이 없어도 초안이 나온다 — 고객 대응에서 무응답은 가장 나쁜 실패다
  · 근거가 없으면 원인을 단정하지 않는다
  · 발송은 사람 승인에서만 일어나고, 정책 차단이 남으면 승인해도 나가지 않는다
  · 고객 답변 큐와 RCA 큐는 서로를 덮어쓰지 않는다 (같은 이슈에 둘 다 존재 가능)

실행:
    .venv/bin/python tests/test_voc_agents.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "backend"))

os.environ.update({
    "RVP_JIRA_POLL_SEC": "0", "RVP_PREWARM": "0", "RVP_MCP": "0",
    "RVP_AUTH_DEV_LOGIN": "1", "RVP_SESSION_SECRET": "test-secret-fixed",
    "RVP_USERS_FILE": str(ROOT / "tests" / "_users_test.yaml"),
})

import voc_agents as V  # noqa: E402
import reply_queue  # noqa: E402
import rca_queue  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'✓' if cond else '✗'} {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def codes(report: dict) -> set[str]:
    return {v["code"] for v in report["violations"]}


# ── 1) 의도 분류 ────────────────────────────────────────────────────────────
def test_intent() -> None:
    print("\n[요청 유형 분류]")
    check("일정 문의", V.classify("아직 안 됩니다. 언제 고쳐지나요?")["intent"] == "status_inquiry",
          "고장 서술이 섞여도 고객이 기다리는 답은 일정이다")
    check("고장 신고", V.classify("태그를 대면 인식이 안 됩니다")["intent"] == "defect_report")
    check("환불 요청", V.classify("계속 고장나서 환불해 주세요")["intent"] == "rma_request")
    check("우회책 요청", V.classify("급합니다. 임시로 쓸 방법 없나요?")["intent"] == "workaround_request")
    check("사용법 문의", V.classify("절전 모드 설정은 어떻게 하나요?")["intent"] == "howto")
    check("규격 문의", V.classify("이 칩이 NCI 2.3 규격을 지원하나요?")["intent"] == "spec_inquiry")
    check("분류 불가는 other", V.classify("안녕하세요")["intent"] == "other")
    check("유형마다 골격이 다르다",
          V.INTENTS["rma_request"]["sections"] != V.INTENTS["defect_report"]["sections"])


def test_asks() -> None:
    print("\n[요청 문장 추출]")
    asks = V.extract_asks("충전 중에 카드가 안 찍힙니다. 왜 그런가요? 임시 방법이라도 알려주세요.")
    check("질문 2건 추출", len(asks) == 2, str(asks))
    check("물음표 문장 포함", any("왜 그런가요" in a for a in asks), str(asks))
    check("요청형 어미 문장 포함", any("알려주세요" in a for a in asks), str(asks))
    # '해주세요' 형태만 보면 "확답을 주세요"·"근거가 필요합니다" 를 놓친다 —
    # 실측에서 이 두 형태가 목 VOC 의 주된 요청 표현이었다.
    check("'확답을 주세요' 도 요청",
          V.extract_asks("하드웨어 교체가 필요한지 확답을 주세요.") != [])
    check("'근거가 필요합니다' 도 요청",
          V.extract_asks("기지국 문제인지 단말 문제인지 판단 근거가 필요합니다.") != [])
    check("요청이 없으면 빈 목록", V.extract_asks("어제부터 발열이 심합니다.") == [])


# ── 2) 정책 검사 ────────────────────────────────────────────────────────────
def test_ask_terms_stem() -> None:
    """어미까지 붙은 채로 비교하면 제대로 답한 문장이 '미응답' 으로 잡힌다."""
    print("\n[요청 핵심어 추출]")
    check("어미를 벗긴다", V._stem("롤백하면") == "롤백" and V._stem("발생합니다") == "발생")
    check("어미 → 조사 순서", V._stem("임시로라도") == "임시",
          "조사를 먼저 떼면 '임시로라' 같은 없는 말이 남는다")
    check("2자 미만으로 줄이지 않는다", V._stem("확인") == "확인")
    terms = V.ask_terms("롤백하면 되는지, 아니면 앱 쪽에서 대응해야 하는지 알려주세요.")
    check("기능어는 핵심어가 아니다", "되는지" not in terms and "아니면" not in terms, str(terms))
    check("내용어가 남는다", "롤백" in terms and "대응" in terms, str(terms))
    body = "이전 버전으로 롤백하시면 정상화됩니다. 앱 쪽 대응은 필요하지 않습니다. 안내드리겠습니다."
    check("어미가 달라도 답한 것으로 센다",
          not V.unanswered_asks(["롤백하면 되는지, 아니면 앱 쪽에서 대응해야 하는지 알려주세요."], body))


def test_internal_profile() -> None:
    """사내 VOC 는 외부 고객 응대와 **정책이 정반대인 지점**이 있다.

    문체만 다른 게 아니다. 사내에서 이슈 키는 지우면 안 되는 정보이고, 환불 확약은
    개념 자체가 없으며, 확정 일정은 정상 업무다. 파이프라인은 하나로 두고 프로파일만
    가른다 — 두 벌이 되면 한쪽만 갱신되고, 갈라진 규칙은 없는 규칙과 같다.
    """
    print("\n[사내 VOC 프로파일]")
    check("유형 체계가 다르다",
          "access_request" in V.intents_of("internal") and "rma_request" not in V.intents_of("internal"))
    check("사내에 없는 유형으로 분류되지 않는다",
          V.classify("환불해 주세요", "internal")["intent"] == "other"
          and V.classify("환불해 주세요")["intent"] == "rma_request")
    check("장애가 최우선", V.classify("접속이 안 됩니다. 권한도 주세요.", "internal")["intent"] == "outage")
    check("권한 요청 분류", V.classify("스테이징 접근 권한 주세요", "internal")["intent"] == "access_request")

    body = "LSI-7 에서 추적 중입니다. 다음 주까지 배포하겠습니다. 확인 후 안내드리겠습니다."
    ext = {v["code"]: v["severity"] for v in V.check(body)["violations"]}
    intl = {v["code"]: v["severity"] for v in V.check(body, prof="internal")["violations"]}
    check("외부는 이슈 키를 차단", ext.get("internal_key") == "block")
    check("사내는 이슈 키가 위반이 아니다", "internal_key" not in intl, str(intl))
    check("외부는 확정 일정을 차단", ext.get("date_promise") == "block")
    check("사내는 확정 일정이 경고 — 커밋먼트는 정상 업무다",
          intl.get("date_promise") == "warn", str(intl))
    check("사내 답변은 차단되지 않는다", V.check(body, prof="internal")["blocked"] is False)
    check("사내에서는 이슈 키를 지우지 않는다", V.redact("LSI-7 참고", "internal") == "LSI-7 참고")
    check("외부에서는 지운다", "LSI-7" not in V.redact("LSI-7 참고"))
    check("차단 목록도 프로파일을 따른다",
          "internal_key" not in V.blocking_for("internal") and "internal_key" in V.blocking_for())
    p = V.reply_prompt({"summary": "배포 후 500"}, [], None,
                       V.classify("배포 후 500 에러", "internal"), prof="internal")
    check("사내 페르소나", "SW 엔지니어" in p)
    check("이슈 키 인용을 권장한다", "적극적으로" in p, p[:200])
    check("알 수 없는 프로파일은 기본값으로 접힌다",
          V.profile("오타")["label"] == V.profile("external")["label"])


def test_secret_leak() -> None:
    """자격증명은 어느 프로파일에서도 차단한다 — 사내 채널 유출이 가장 흔한 경로다."""
    print("\n[자격증명 유출]")
    for label, sample in [
        ("GitHub 토큰", "토큰은 ghp_abcdefghij1234567890abcdefgh 입니다."),
        ("AWS 키", "AKIAIOSFODNN7EXAMPLE 로 접근하세요."),
        ("비밀번호 평문", "password: hunter2plus 로 로그인하세요."),
        ("Bearer 토큰", "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9abcdef"),
        ("개인 키", "-----BEGIN RSA PRIVATE KEY-----"),
        ("접속 문자열", "postgres://svc:pw1234@db.internal:5432/app 로 붙으세요."),
    ]:
        r = V.check(sample + " 안내드리겠습니다.", prof="internal")
        check(f"{label} 차단", "secret_leak" in codes(r) and r["blocked"] is True, str(codes(r)))
    check("외부 프로파일에서도 차단",
          "secret_leak" in codes(V.check("ghp_abcdefghij1234567890abcdefgh 안내드리겠습니다.")))
    check("비밀 값 자체를 위반 내용에 싣지 않는다",
          all("ghp_" not in v["detail"] for v in
              V.check("ghp_abcdefghij1234567890abcdefgh 안내드리겠습니다.")["violations"]))
    check("평범한 문장은 걸리지 않는다",
          "secret_leak" not in codes(V.check("설정 파일 경로를 확인 후 안내드리겠습니다.")))


def test_policy_blocks() -> None:
    print("\n[정책 — 차단]")
    check("내부 이슈 키", "internal_key" in codes(V.check("LSI-42 사례와 동일합니다.")))
    check("한자", "han_char" in codes(V.check("問題를 확인했습니다.")))
    check("확정 일정 약속",
          "date_promise" in codes(V.check("다음 주까지 수정해 드리겠습니다.")))
    check("보상 확약",
          "compensation_promise" in codes(V.check("확인 후 환불해 드리겠습니다.")))
    check("반말·개조식", "plain_speech" in codes(V.check("원인을 확인 중임. 곧 연락하겠음.")))
    check("차단이 하나라도 있으면 blocked", V.check("LSI-42 참조").get("blocked") is True)


def test_policy_does_not_overreach() -> None:
    print("\n[정책 — 오탐 방지]")
    ok = ("안녕하세요. 문의 주신 내용 확인했습니다. 다음 주에 추가 확인을 진행하며, "
          "결과가 나오는 대로 안내드리겠습니다. 감사합니다.")
    r = V.check(ok)
    check("시점만 있고 약속이 없으면 통과", "date_promise" not in codes(r), str(codes(r)))
    check("차단 없음", r["blocked"] is False, str(r["violations"]))
    check("환불 '절차 안내' 는 확약이 아니다",
          "compensation_promise" not in codes(V.check(
              "교환·환불 여부는 담당 부서에서 제품 확인 후 결정됩니다. 안내드리겠습니다.")))
    check("제목·글머리표는 문체 검사 대상이 아니다",
          "plain_speech" not in codes(V.check(
              "### 확인한 내용\n- 무선 충전 중 인식 실패\n확인 후 안내드리겠습니다.")))


def test_policy_warns() -> None:
    print("\n[정책 — 경고]")
    r = V.check("지식베이스의 유사 인용 사례로 확인했습니다.")
    check("내부 용어 경고", "internal_jargon" in codes(r))
    check("다음 단계 누락 경고", "missing_next_step" in codes(r))
    check("경고만 있으면 차단되지 않는다", r["blocked"] is False)
    check("근거 없는 단정은 경고",
          "unsupported_certainty" in codes(V.check(
              "확인해 보니 원인은 전원부 불량입니다.", has_evidence=False)))


def test_unknown_key() -> None:
    """사내 프로파일은 이슈 키를 허용하지만 **근거에 없는 키는 여전히 환각**이다.

    실측에서 모델이 "본 건은 SPT-1042 로 추적합니다" 처럼 없는 티켓 번호를 지어냈다.
    받는 사람은 그 번호를 찾으러 갔다가 없다는 것을 알기까지 시간을 쓴다.
    """
    print("\n[근거에 없는 이슈 키]")
    t = "본 건은 SPT-1042 로 추적합니다. LSI-7 사례와 같습니다. 안내드리겠습니다."
    r = V.check(t, prof="internal", known_keys=["LSI-7", "IVOC-44"])
    check("지어낸 키를 잡는다", "unknown_key" in codes(r), str(codes(r)))
    check("차단이 아니라 경고 — 사내에서 키 자체는 정상이다", r["blocked"] is False)
    check("근거 키만 있으면 조용하다",
          "unknown_key" not in codes(V.check("LSI-7 사례와 같습니다. 안내드리겠습니다.",
                                             prof="internal", known_keys=["LSI-7"])))
    check("질의 이슈 자신도 허용",
          "unknown_key" not in codes(V.check("IVOC-44 진행 상황입니다. 안내드리겠습니다.",
                                             prof="internal", known_keys=["LSI-7", "IVOC-44"])))
    check("known_keys 를 안 주면 검사하지 않는다 — 모르는 것을 틀렸다고 하지 않는다",
          "unknown_key" not in codes(V.check(t, prof="internal")))
    check("파생 키도 원본으로 인정",
          "unknown_key" not in codes(V.check("LSI-7 참고. 안내드리겠습니다.",
                                             prof="internal", known_keys=["LSI-7-rca"])))


def test_third_party() -> None:
    print("\n[제3자 정보 노출]")
    r = V.check("다른 고객사 Lumen Imaging 에서도 같은 증상이 있었습니다. 안내드리겠습니다.",
                forbidden=("Lumen Imaging", "Vertex Cloud"))
    check("다른 고객사명 차단", "third_party" in codes(r) and r["blocked"] is True)
    check("금지 목록에 없으면 통과",
          "third_party" not in codes(V.check("유사한 사례가 있었습니다. 안내드리겠습니다.",
                                             forbidden=("Lumen Imaging",))))
    check("자동으로 지우지 않는다 — 사람이 고쳐야 한다",
          "Lumen Imaging" in V.redact("Lumen Imaging 사례"))


def test_unanswered_ask() -> None:
    """답변이 요지를 빗나가는 것은 거부 사유 1순위다 — 형식 검사만으로는 안 보인다."""
    print("\n[요청 반영]")
    asks = ["임시로라도 넘길 방법이 있으면 알려주세요.", "스펙 미달인지 정상 동작인지 답변 부탁드립니다."]
    off = V.check("안녕하세요. 확인 후 안내드리겠습니다.", asks=asks)
    check("빗나간 답변에 경고", "unanswered_ask" in codes(off))
    check("차단은 아니다 — 어휘 일치는 하한이라 오탐이 있다", off["blocked"] is False)
    on = V.check("임시로 적용할 우회 방법과 스펙 기준을 안내드리겠습니다.", asks=asks)
    check("요청을 건드리면 경고 없음", "unanswered_ask" not in codes(on), str(codes(on)))
    check("요청이 없으면 경고 없음",
          "unanswered_ask" not in codes(V.check("확인 후 안내드리겠습니다.", asks=[])))


def test_redact() -> None:
    print("\n[내부 키 제거]")
    out = V.redact("LSI-42 및 VOC-7 에서 동일 증상이 있었습니다.")
    check("키가 남지 않는다", "LSI-42" not in out and "VOC-7" not in out, out)
    check("문장은 유지된다", "동일 증상" in out, out)
    check("제거 후 정책 통과", "internal_key" not in codes(V.check(out)))


# ── 3) 답변 생성(템플릿) ────────────────────────────────────────────────────
REC = {"summary": "무선충전 거치대에 올려두면 교통카드가 안 찍힙니다", "symptom": "간헐적 인식 실패"}
MATCH = [{"key": "LSI-7", "summary": "WLC 충전 중 polling 지연",
          "root_cause": "충전 중 NFC polling 주기 지연", "resolution": "FW 3.2 적용",
          "workaround": "거치대에서 내린 뒤 태그"}]
PROP = {"root_cause": MATCH[0]["root_cause"], "resolution": MATCH[0]["resolution"],
        "workaround": MATCH[0]["workaround"]}


def test_render() -> None:
    print("\n[LLM 없는 결정적 초안]")
    intent = V.classify(REC["summary"] + " 언제 고쳐지나요?")
    body = V.render_reply(REC, MATCH, PROP, intent)
    r = V.check(body)
    check("정책 위반 없음", r["ok"], str(r["violations"]))
    check("유형 골격을 모두 담는다",
          all(f"### {s}" in body for s in intent["sections"]), body[:200])
    check("내부 키가 새지 않는다", "LSI-7" not in body)
    check("고객 요청을 본문에 되짚는다", "언제 고쳐지나요" in body)


def test_internal_render_and_mock() -> None:
    """사내 경로는 사내 입력으로 잰다 — 외부 고객 문의로 재면 엉뚱한 것을 재게 된다."""
    print("\n[사내 목 데이터 · 템플릿]")
    import json as _json
    from preprocess import parse_issue
    path = ROOT / "data" / "internal_voc_mock_issues.json"
    if not path.exists():
        check("사내 목 데이터 존재", False, "scripts/build_internal_voc_mock.py 로 생성")
        return
    raws = _json.loads(path.read_text(encoding="utf-8"))
    reqs = [parse_issue(r) for r in raws if r.get("status") != "완료"]
    check("요청 건이 있다", len(reqs) >= 10, str(len(reqs)))
    intents, no_ask = set(), 0
    for r in reqs:
        ctx = "\n".join([r["summary"], r["symptom"], r["customer_ask"]])
        it = V.classify(ctx, "internal")
        intents.add(it["intent"])
        no_ask += (not it["asks"])
    check("사내 유형으로 분류된다", intents <= set(V.intents_of("internal")), str(intents))
    check("'기타' 로 흘러가지 않는다", "other" not in intents, str(intents))
    check("모든 요청에서 요청 문장이 나온다", no_ask == 0, f"{no_ask}건 누락")

    r = reqs[0]
    it = V.classify("\n".join([r["summary"], r["symptom"], r["customer_ask"]]), "internal")
    body = V.render_reply(r, [{"key": "IVOC-1"}],
                          {"root_cause": "커넥션이 풀에 남았다", "workaround": "CLI 로 받을 수 있다"}, it)
    pol = V.check(body, prof="internal")
    check("템플릿이 사내 골격을 채운다",
          all(f"### {sec}" in body for sec in it["sections"]), body[:160])
    check("내부 서술의 개조식이 답변 문장 끝이 되지 않는다",
          "plain_speech" not in codes(pol), str(codes(pol)))
    check("사내 초안은 차단되지 않는다", pol["blocked"] is False, str(pol["violations"]))


def test_render_without_evidence() -> None:
    print("\n[근거 없을 때]")
    intent = V.classify("전원이 갑자기 꺼집니다")
    body = V.render_reply({"summary": "전원이 갑자기 꺼집니다"}, [], None, intent)
    check("초안은 여전히 나온다", len(body) > 100)
    check("원인을 단정하지 않는다", V._NO_EVIDENCE in body, body[:300])
    check("정책 통과", V.check(body, has_evidence=False)["blocked"] is False)


def test_rma_never_promises() -> None:
    print("\n[교환·환불 — 확약 금지]")
    intent = V.classify("불량이 반복됩니다. 환불해 주세요.")
    body = V.render_reply({"summary": "불량이 반복됩니다"}, MATCH, PROP, intent)
    check("유형은 교환·환불", intent["intent"] == "rma_request")
    check("확약 문구 없음", "compensation_promise" not in codes(V.check(body)), body)
    check("담당 부서 이관을 안내한다", "담당 부서" in body, body)


def test_evidence_scrubbing_and_canonical() -> None:
    """근거는 남의 고장 이력이다 — 프롬프트에 다른 고객사 이름을 **보여주지 않는다**.

    정책이 출력에서 막긴 하지만, 애초에 안 보여주는 것이 규칙으로 막는 것보다
    확실하다(이슈 키에 대해 이미 같은 결론을 냈다).
    """
    print("\n[근거 정화 · 정본 답변]")
    m = [{"key": "LSI-100", "summary": "[DDI-OLED-T7] 색온도 불일치 (Helios Automotive / MIPI host)",
          "root_cause": "Helios Automotive 보드에서 재현", "resolution": "y", "workaround": "z"}]
    intent = V.classify("화면 색이 이상합니다")
    p = V.reply_prompt({"summary": "화면 색이 이상합니다"}, m, None, intent,
                       forbidden=("Helios Automotive",))
    check("다른 고객사명이 프롬프트에 없다", "Helios Automotive" not in p)
    check("치환 흔적은 남는다 — 문장이 깨지지 않도록", "다른 고객사" in p)
    check("근거 키도 여전히 없다", "LSI-100" not in p)
    p2 = V.reply_prompt({"summary": "화면 색이 이상합니다"}, m, None, intent,
                        canonical="이미 발송한 정본 답변입니다.")
    check("정본 답변이 주입된다", "이미 발송한 정본 답변입니다." in p2 and "정본 답변" in p2)
    check("정본이 없으면 그 절은 없다", "정본 답변" not in V.reply_prompt(
        {"summary": "x"}, m, None, intent))
    en = V.reply_prompt({"summary": "screen colour is off"}, m, None, intent,
                        lang="en", forbidden=("Helios Automotive",), canonical="Approved reply.")
    check("영어 경로도 같다", "Helios Automotive" not in en and "Approved reply." in en)


def test_prompt_hides_internal_keys() -> None:
    print("\n[프롬프트에 내부 키를 넣지 않는다]")
    intent = V.classify(REC["summary"])
    p = V.reply_prompt(REC, MATCH, PROP, intent)
    check("근거 키가 프롬프트에 없다", "LSI-7" not in p,
          "보여주면 모델이 인용한다 — 규칙보다 미노출이 확실하다")
    check("요청 골격이 프롬프트에 있다", all(s in p for s in intent["sections"]))
    check("문체 규칙이 들어간다", "존댓말" in p)


# ── 4) 큐 · 엔드포인트 계약 ────────────────────────────────────────────────
def test_customer_ask_is_parsed() -> None:
    """고객 문의 본문의 '고객 요청 (Ask)' 절이 레코드로 올라와야 한다.

    이 절을 안 뽑으면 요청 문장이 증상 서술에 묻히고, 답변이 요지를 빗나간다 —
    초안 거부 사유 중 가장 흔한 종류다. 실제로 목 VOC 32건이 전부 그 상태였다.
    """
    print("\n[고객 요청 절 파싱]")
    from preprocess import parse_issue
    raw = {"key": "VOC-9", "summary": "[문의] 속도가 반토막 납니다", "status": "접수",
           "description": ("h2. 증상 (Symptom)\n\n대용량 쓰기에서 느려집니다.\n\n"
                           "h2. 고객 요청 (Ask)\n\n정상 동작인지 답변 부탁드립니다. "
                           "임시 방법이 있으면 알려주세요.\n"),
           "comments": []}
    rec = parse_issue(raw)
    check("고객 요청 절 추출", "답변 부탁드립니다" in rec["customer_ask"], rec["customer_ask"])
    check("증상과 분리된다", "답변 부탁드립니다" not in rec["symptom"])
    intent = V.classify(rec["summary"] + "\n" + rec["symptom"] + "\n" + rec["customer_ask"])
    check("요청 문장 2건", len(intent["asks"]) == 2, str(intent["asks"]))
    p = V.reply_prompt(rec, MATCH, PROP, intent)
    check("프롬프트가 요청을 싣는다", "답변 부탁드립니다" in p)


def test_language() -> None:
    """고객이 쓴 언어로 답한다 — 내용이 정확해도 언어가 다르면 대응 실패다."""
    print("\n[답변 언어]")
    check("영어 문의 판정", V.detect_lang("The device reboots randomly. Please advise.") == "en")
    check("한글+영문 로그 혼재는 한국어",
          V.detect_lang("PM9C3-NVMe link down 이 반복됩니다. thermal throttle engaged") == "ko")
    intent = V.classify("The card reader stops responding while charging")
    body = V.render_reply({"summary": "The card reader stops responding"}, MATCH, PROP,
                          intent, lang="en")
    check("영어 폴백 초안", V.detect_lang(body) == "en", body[:80])
    r = V.check(body, lang="en")
    check("영어 초안 정책 통과", r["blocked"] is False, str(r["violations"]))
    check("언어 불일치는 차단",
          "wrong_language" in codes(V.check("안녕하세요. 확인 후 안내드리겠습니다.", lang="en")))
    check("영어 확정 약속 차단 — 시점이 뒤에 와도",
          "date_promise" in codes(V.check(
              "We will complete the fix by next week. We will update you.", lang="en")))
    check("영어 보상 확약 차단",
          "compensation_promise" in codes(V.check(
              "We will issue a refund once confirmed. We will update you.", lang="en")))
    check("영어 정상 문장 오탐 없음",
          not codes(V.check("We are reviewing the refund process and will get back to you soon.",
                            lang="en")) - {"internal_jargon"},
          str(codes(V.check("We are reviewing the refund process and will get back to you soon.", lang="en"))))
    check("존댓말 규칙은 영어에 적용하지 않는다",
          "plain_speech" not in codes(V.check(
              "We have reviewed your report. We will get back to you shortly.", lang="en")))
    p = V.reply_prompt({"summary": "device reboots"}, MATCH, PROP, intent, lang="en")
    check("영어 프롬프트에도 약속 금지 규칙", "Never promise a completion date" in p)
    check("영어 프롬프트에도 내부 키 금지", "internal ticket keys" in p)


def test_canonical_reply_store() -> None:
    """같은 유형의 문의에는 같은 답변이 나가야 한다 — 발송한 글이 그 유형의 정본이 된다."""
    print("\n[반복 문의 유형 · 정본 답변]")
    import failure_modes as F
    F.STORE_FILE = Path(tempfile.mkdtemp()) / "ki.json"
    art = F.promote(title="충전 중 태그 인식 실패", members=["LSI-7", "LSI-9"])
    check("유형 생성", art["id"] == "KI-1" and art["members"] == ["LSI-7", "LSI-9"])
    check("정본이 없으면 빈 값", F.canonical_reply_for(["LSI-7"]) == ("", ""))
    F.set_canonical_reply(art["id"], "안녕하세요. 확인 후 안내드리겠습니다.", from_key="VOC-3")
    body, aid = F.canonical_reply_for(["LSI-9"])
    check("다른 멤버로도 같은 정본을 찾는다", aid == "KI-1" and "안내드리겠습니다" in body)
    check("유형 밖의 키는 정본이 없다", F.canonical_reply_for(["LSI-999"]) == ("", ""))
    check("빈 본문은 정본이 되지 않는다", F.set_canonical_reply(art["id"], "  ") is None)
    F.set_canonical_reply(art["id"], "새로 발송한 답변입니다.", from_key="VOC-8")
    check("가장 최근 발송본이 정본 — 옛 판본은 발송 이력에 남는다",
          F.canonical_reply_for(["LSI-7"])[0] == "새로 발송한 답변입니다.")


def test_queues_are_separate() -> None:
    print("\n[큐 분리]")
    tmp = Path(tempfile.mkdtemp())
    reply_queue._Q.path = tmp / "reply.json"
    rca_queue._Q.path = tmp / "rca.json"
    rca_queue.upsert({"key": "LSI-1", "body": "RCA 초안", "created_at": "1"})
    reply_queue.upsert({"key": "LSI-1", "body": "고객 답변 초안", "created_at": "1"})
    check("같은 이슈에 둘 다 존재", rca_queue.get("LSI-1")["body"] == "RCA 초안"
          and reply_queue.get("LSI-1")["body"] == "고객 답변 초안")
    reply_queue.set_state("LSI-1", "approved")
    reply_queue.upsert({"key": "LSI-1", "body": "새 초안", "created_at": "2"})
    check("발송된 답변은 덮어쓰지 않는다", reply_queue.get("LSI-1")["body"] == "고객 답변 초안")


class _StubReco:
    def __init__(self, coverage: bool) -> None:
        self.coverage = coverage

    def recommend(self, rec, k=4, exclude_key=None):
        if not self.coverage:
            return {"matches": [], "proposal": None, "coverage": False}
        return {"matches": MATCH, "proposal": PROP, "coverage": True}


def _client(coverage: bool = True):
    from fastapi.testclient import TestClient
    import server
    server._RECO_STATE = {"by_key": {"VOC-1": dict(REC, key="VOC-1", status="접수")},
                          "reco": _StubReco(coverage)}
    server._record_gap = lambda *a, **k: None          # 지식 공백 스토어 오염 방지
    c = TestClient(server.app)
    return server, c


def _login(c, who: str) -> None:
    r = c.post("/auth/dev-login", json={"email": who})
    assert r.status_code == 200, r.text


def test_endpoints() -> None:
    print("\n[엔드포인트 — 초안·검사]")
    tmp = Path(tempfile.mkdtemp())
    reply_queue._Q.path = tmp / "reply.json"
    server, c = _client()
    _login(c, "eng@example.com")
    r = c.post("/voc/reply/draft", json={"key": "VOC-1", "use_llm": False}).json()
    check("큐 적재", r.get("queued") is True, str(r)[:200])
    item = r["item"]
    check("유형이 기록된다", item["intent"] in V.INTENTS)
    check("근거 키는 내부 필드에만", "LSI-7" not in item["body"] and item["evidence"] == ["LSI-7"])
    check("정책 결과 동봉", item["policy"]["ok"] is True, str(item["policy"]))
    check("템플릿 초안은 항상 검토 대상", item["needs_review"] is True,
          "템플릿은 접수·계획만 말하고 고객 질문에 답하지 않는다")
    check("대기 목록에 보인다",
          c.get("/voc/reply/pending").json()["counts"]["pending"] == 1)
    chk = c.post("/voc/reply/check", json={"body": "LSI-9 참고 바랍니다."}).json()
    check("편집 중 재검사", chk["blocked"] is True and "internal_key" in {v["code"] for v in chk["violations"]})
    check("분류 체계 노출", len(c.get("/voc/reply/intents").json()["intents"]) == len(V.INTENTS))


def test_send_gate() -> None:
    print("\n[발송 게이트 — HITL]")
    tmp = Path(tempfile.mkdtemp())
    reply_queue._Q.path = tmp / "reply.json"
    server, c = _client()
    _login(c, "eng@example.com")
    c.post("/voc/reply/draft", json={"key": "VOC-1", "use_llm": False})
    r = c.post("/voc/reply/send", json={"key": "VOC-1"})
    check("사용자는 발송할 수 없다", r.status_code == 403, str(r.status_code))

    posted: list = []
    import jira_commenter
    jira_commenter.post_comment = lambda key, body: (posted.append((key, body)), {"id": "1"})[1]
    _login(c, "admin")
    bad = c.post("/voc/reply/send",
                 json={"key": "VOC-1", "body": "확인했습니다. 다음 달까지 완료해 드리겠습니다."}).json()
    check("정책 차단이 남으면 발송하지 않는다", bad["ok"] is False and not posted, str(bad)[:200])
    good = c.post("/voc/reply/send",
                  json={"key": "VOC-1",
                        "body": "안녕하세요. 확인 후 결과를 안내드리겠습니다. 감사합니다."}).json()
    check("승인 시에만 발송", good["ok"] is True and len(posted) == 1, str(good)[:200])
    check("고객 답변 표식이 붙는다", server.REPLY_MARKER in posted[0][1], posted[0][1][:80])
    check("사람 수정이 기록된다", good["edited"] is True)
    check("발송 지표", good["stats"]["sent"] == 1 and good["stats"]["clean_rate"] == 0.0,
          str(good["stats"]))


def test_followup_reply() -> None:
    """고객이 답장하면 같은 티켓에 다시 답해야 한다 — 다만 나간 판본은 남아야 한다."""
    print("\n[후속 답변]")
    tmp = Path(tempfile.mkdtemp())
    reply_queue._Q.path = tmp / "reply.json"
    server, c = _client()
    _login(c, "eng@example.com")
    c.post("/voc/reply/draft", json={"key": "VOC-1", "use_llm": False})
    import jira_commenter
    jira_commenter.post_comment = lambda key, body: {"id": "7"}
    _login(c, "admin")
    c.post("/voc/reply/send", json={"key": "VOC-1",
                                    "body": "안녕하세요. 확인 후 안내드리겠습니다. 감사합니다."})
    r = c.post("/voc/reply/draft", json={"key": "VOC-1", "use_llm": False}).json()
    check("그냥 다시 요청하면 덮지 않는다",
          r["queued"] is False and r["reason_code"] == "already_sent", str(r)[:160])
    r = c.post("/voc/reply/draft", json={"key": "VOC-1", "use_llm": False, "again": True}).json()
    check("again 이면 후속 초안", r["queued"] is True and r["reason_code"] == "queued_followup")
    check("이전 발송본이 이력으로 남는다",
          len(r["item"]["history"]) == 1 and "안내드리겠습니다" in r["item"]["history"][0]["body"],
          str(r["item"].get("history"))[:160])
    st = c.get("/voc/reply/stats").json()
    check("발송 이력이 지표에서 사라지지 않는다", st["sent"] == 1 and st["followups"] == 1, str(st))
    check("발송률이 1을 넘지 않는다", (st["send_rate"] or 0) <= 1.0, str(st))


def test_proofread_guard() -> None:
    """교정은 오타만 고쳐야 한다 — 내용을 바꾼 교정본은 버리고 원문을 쓴다.

    교정 모델에게 '고쳐라' 만 시키면 문장을 다시 쓰면서 수치·조건·약속을 조용히
    바꾼다. 고객 답변에서 그건 오타보다 훨씬 큰 사고다.
    """
    print("\n[교정 가드]")
    # 규칙을 하나씩 격리해서 본다 — 한 입력이 여러 규칙에 걸리면 어느 그물이 잡았는지
    # 알 수 없고, 나중에 그 그물을 지워도 테스트가 통과한다.
    base = ("### 확인한 내용\n온도 85도에서 PM9C3-NVMe 가 느려지는 증상을 확인했습니다. "
            "담당 엔지니어가 원인을 확인하고 있으며, 확인되는 대로 안내드리겠습니다.")
    ok, why = V.safe_replace(base, base.replace("PM9C3-NVMe 가", "PM9C3-NVMe가"))
    check("띄어쓰기 교정은 채택", "PM9C3-NVMe가" in ok and why == "")
    for label, bad, expect in [
        ("숫자 변경", base.replace("85", "90"), "숫자"),
        ("제품명 변경", base.replace("NVMe", "NVMev"), "제품명"),
        ("제목 변경", base.replace("### 확인한 내용", "### 확인 내용"), "제목"),
        # 숫자·제품명·제목은 그대로 두고 문장만 날린다 — 길이 규칙만 걸리게 격리.
        ("내용 증발", "### 확인한 내용\n온도 85도에서 PM9C3-NVMe 가 느려지는 증상을 확인했습니다.", "길이"),
    ]:
        out, why = V.safe_replace(base, bad)
        check(f"{label} 은 폐기", out == base and expect in why, f"{out[:40]} / {why}")
    out, why = V.safe_replace(base, base.replace("원인을", "LSI-9 원인을"))
    check("없던 이슈 키가 생기면 폐기", out == base and "이슈 키" in why, why)
    check("빈 교정본은 폐기", V.safe_replace(base, "   ")[0] == base)
    check("동일하면 원문 유지", V.safe_replace(base, base) == (base, ""))


def test_spelling_warn() -> None:
    print("\n[표기 이상 경고]")
    check("단독 자모", "suspect_spelling" in codes(V.check("안녕하세요 ㅋ 안내드리겠습니다.")))
    check("같은 글자 반복", "suspect_spelling" in codes(V.check("죄송합니다아아아아. 안내드리겠습니다.")))
    check("문장부호 앞 공백", "suspect_spelling" in codes(V.check("확인했습니다 . 안내드리겠습니다.")))
    check("정상 문장은 조용하다",
          "suspect_spelling" not in codes(V.check("확인 후 안내드리겠습니다. 감사합니다.")))
    check("차단이 아니라 경고다 — 사전 없이 오타를 다 잡을 수는 없다",
          V.check("안녕하세요 ㅋ 안내드리겠습니다.")["blocked"] is False)


def test_jira_markup_conversion() -> None:
    """게시되는 것은 마크다운이 아니라 **Jira wiki markup** 이다.

    번호 목록을 그대로 두면 목록이 아니라 평문으로 렌더된다 — 답변의 '권장 조치
    순서' 가 통째로 문단으로 뭉개진다(실제로 그렇게 나가고 있었다).
    """
    print("\n[Jira 게시 형식]")
    server, _ = _client()
    out = server._md_to_jira("### 조치\n\n1. 첫 단계 **강조**.\n2. 두 번째.\n\n- 글머리\n")
    check("헤딩 → h3.", "h3. 조치" in out, out)
    check("번호 목록 → #", "# 첫 단계" in out and "# 두 번째" in out, out)
    check("글머리 → *", "* 글머리" in out, out)
    check("굵게 표시는 평문화된다 — Jira 에서 조사가 붙으면 렌더가 깨진다",
          "**" not in out and "강조" in out, out)
    plain = server._md_to_jira("일반 문단 1. 문장 안의 번호는 그대로 둡니다.\n")
    check("문장 안의 번호는 목록이 아니다", "# 문장" not in plain, plain)
    check("이슈 키는 monospace 로 감싼다 — Jira 가 카드로 확장하지 않도록",
          "{{LSI-7}}" in server._md_to_jira("근거 LSI-7 참고"))


def test_deep_analysis_is_customer_reply() -> None:
    """화면의 'AI 심층 분석' 자리는 이제 **고객 응대 답변**을 낸다.

    예전에는 여기서 엔지니어용 RCA(근본원인·검증 절차·사례 키 인용)를 만들었다.
    그러면 사람이 그걸 읽고 고객 답변을 다시 쓰는 단계가 통째로 남는다 — 에이전트가
    하라고 만든 일을 사람이 하게 된다. 생성기 자체가 바뀌었는지 고정한다.
    """
    print("\n[심층 분석 = 고객 응대 답변]")
    tmp = Path(tempfile.mkdtemp())
    reply_queue._Q.path = tmp / "reply.json"
    server, c = _client()
    # 생성기는 LLM 을 부르므로 스트림을 대체한다 — 여기서 재는 것은 '무엇을 만드는가' 다.
    body = ("### 확인한 내용\n말씀해 주신 증상을 확인했습니다. LSI-7 사례와 같습니다.\n\n"
            "### 앞으로의 진행\n확인되는 대로 안내드리겠습니다.\n\n감사합니다.")
    prompts: list[str] = []
    server._llm_stream = lambda p, reasoning=False: (prompts.append(p), iter([body]))[1]
    rec = dict(REC, key="VOC-1", status="접수")
    out = "".join(server._generate_explain_md(rec, MATCH))
    check("스트리밍은 그대로 흘러간다", out == body)
    check("고객 답변 프롬프트를 쓴다",
          "고객에게 그대로 보낼 답변" in prompts[0] and "존댓말" in prompts[0], prompts[0][:120])
    check("근거 키를 프롬프트에 넣지 않는다", "LSI-7" not in prompts[0])

    hit = server._explain_md_cached(rec, MATCH) or {}
    check("캐시본에서 내부 키가 지워진다", "LSI-7" not in hit.get("markdown", ""),
          hit.get("markdown", "")[:120])
    check("정책 판정이 함께 저장된다", isinstance(hit.get("policy"), dict) and
          hit["policy"]["blocked"] is False, str(hit.get("policy"))[:160])
    check("요청 유형이 저장된다", hit.get("intent") in V.INTENTS, str(hit.get("intent")))

    # 화면에서 읽은 그 글이 그대로 큐로 간다 — 다시 만들면 검토의 의미가 사라진다.
    _login(c, "eng@example.com")
    r = c.post("/voc/reply/draft-from-text",
               json={"key": "VOC-1", "body": hit["markdown"]}).json()
    check("발송 대기로 그대로 들어간다",
          r["queued"] is True and r["item"]["body"] == hit["markdown"], str(r)[:200])
    check("검토는 여전히 필요하다", r["item"]["needs_review"] is True)
    empty = c.post("/voc/reply/draft-from-text", json={"key": "VOC-1", "body": "  "}).json()
    check("빈 본문은 거절", empty["queued"] is False and empty["reason_code"] == "empty_body")


def test_reply_review_axes() -> None:
    """고객 답변 검토는 **점수가 아니라 축**이고, **게이트가 아니다.**

    1~10 한 축으로 접으면 무엇이 왜 문제인지가 사라지고, "10/10 통과" 같은 문장이
    사람에게 근거 없는 확신을 준다. 막는 것은 재현되는 규칙(정책 검사)의 일이다.
    """
    print("\n[고객 답변 AI 검토]")
    tmp = Path(tempfile.mkdtemp())
    reply_queue._Q.path = tmp / "reply.json"
    server, c = _client()
    _login(c, "eng@example.com")
    c.post("/voc/reply/draft", json={"key": "VOC-1", "use_llm": False})

    server._llm_stream = lambda p, reasoning=False: iter([""])   # 쓰이지 않아야 한다
    import agno.agent as _agno

    class _Out:
        def __init__(self, t): self.content = t

    class _Stub:
        def __init__(self, text): self.text = text
        def run(self, input=""): return _Out(self.text)

    orig = _agno.Agent
    try:
        _agno.Agent = lambda **kw: _Stub(
            "ANSWERS: yes\nGUIDES: n/a\nSENDABLE: no\nPROBLEM: 확정 일정을 약속했습니다.")
        os.environ["OPENROUTER_API_KEY"] = "test-key"
        d = c.post("/voc/reply/review", json={"key": "VOC-1"}).json()
        rv = d["review"]
        check("축별로 판정한다", rv["answers"] is True and rv["sendable"] is False, str(rv))
        check("지침이 없으면 n/a 는 None", rv["guides"] is None, str(rv))
        check("사유가 함께 온다", "확정 일정" in rv["problem"])
        check("점수를 매기지 않는다", "score" not in rv and "passed" not in rv, str(rv.keys()))
        check("자기 채점이면 그렇게 밝힌다", rv["self_judged"] is True, str(rv))
        check("결정적 정책 검사를 함께 준다", "policy" in d and "violations" in d["policy"])

        _agno.Agent = lambda **kw: _Stub("무슨 말인지 모르겠습니다")
        bad = c.post("/voc/reply/review", json={"key": "VOC-1"}).json()["review"]
        check("형식이 깨지면 판정 없음 — 통과로 세지 않는다",
              bad["available"] is False and "형식" in bad["reason"], str(bad))
    finally:
        _agno.Agent = orig
        os.environ.pop("OPENROUTER_API_KEY", None)

    check("큐에 없으면 오류", "error" in c.post("/voc/reply/review", json={"key": "NOPE-1"}).json())


def test_reply_without_evidence_still_drafts() -> None:
    print("\n[근거 없어도 답은 나간다]")
    tmp = Path(tempfile.mkdtemp())
    reply_queue._Q.path = tmp / "reply.json"
    server, c = _client(coverage=False)
    _login(c, "eng@example.com")
    r = c.post("/voc/reply/draft", json={"key": "VOC-1", "use_llm": False}).json()
    check("초안이 만들어진다", r.get("queued") is True, str(r)[:200])
    check("근거 없음이 표시된다", r["item"]["has_evidence"] is False)
    check("사람 검토가 강제된다", r["item"]["needs_review"] is True)


def main() -> int:
    for fn in (test_intent, test_asks, test_customer_ask_is_parsed, test_ask_terms_stem, test_internal_profile, test_secret_leak, test_policy_blocks, test_policy_does_not_overreach,
               test_policy_warns, test_third_party, test_unknown_key, test_unanswered_ask, test_spelling_warn,
               test_proofread_guard, test_redact, test_render, test_internal_render_and_mock, test_render_without_evidence,
               test_rma_never_promises, test_prompt_hides_internal_keys, test_evidence_scrubbing_and_canonical,
               test_language,
               test_canonical_reply_store, test_queues_are_separate, test_endpoints, test_send_gate, test_followup_reply, test_jira_markup_conversion,
               test_deep_analysis_is_customer_reply, test_reply_review_axes,
               test_reply_without_evidence_still_drafts):
        fn()
    print()
    if FAILS:
        print(f"실패 {len(FAILS)}건: {FAILS}")
        return 1
    print("모두 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
