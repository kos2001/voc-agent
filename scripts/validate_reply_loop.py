"""고객 대응 답변 엔드투엔드 검증 — **고객에게 나가도 되는 글인가**를 잰다.

무엇을 재는가:
  1) **정책 검출 복원율(적대적)** — 깨끗한 답변에 *알려진 위반*을 하나씩 주입하고,
     정책 검사가 그것을 되찾는지 본다. 동시에 **오탐**도 잰다 — 정상 문장이 걸리면
     경고가 소음이 되고, 소음이 된 경고는 아무도 안 본다.
  2) **전수 생성** — 미해결 VOC 전건에 대해 실제 서버 경로(`/voc/reply/draft`)로
     초안을 만들고, 차단 잔존율·내부 키 유출·골격 준수·요청 반영률을 잰다.
  3) **요청 반영률** — 고객이 물은 것(`h2. 고객 요청 (Ask)`)의 핵심어가 답변에
     들어갔는가. 어휘 일치라 상한이 아니라 하한이다(뜻은 맞는데 말이 다르면 놓친다).
     `--judge` 를 주면 LLM 이 '요청에 답했는가 / 고객에게 보내도 되는가'를 판정한다.

왜 이런 형태인가:
  "돌아간다"만 확인하는 검증은 무의미하다. 위반을 **알고** 주입해야 검출률을 숫자로
  말할 수 있고, 정상 문장을 같이 넣어야 오탐률을 말할 수 있다. 둘 중 하나만 재면
  "전부 차단" 또는 "전부 통과" 하는 검사기가 만점을 받는다.

기본은 LLM 없이(결정적 템플릿) 돈다 — 비용 0, CI 가능. 실제 품질 수치는 `--llm`.
큐는 임시 경로로 격리한다 — 실제 발송 대기 큐를 오염시키지 않는다.

실행:
    .venv/bin/python scripts/validate_reply_loop.py            # 정책 + 템플릿 전수
    .venv/bin/python scripts/validate_reply_loop.py --llm      # 실제 생성 경로
    .venv/bin/python scripts/validate_reply_loop.py --llm --judge
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "backend"))

# .env 를 읽어야 KB 보조 원천(RVP_KB_EXTRA)과 LLM 자격증명이 잡힌다 — 안 읽으면
# VOC 목 데이터가 KB 에 없어 0건을 검증하고도 초록이 뜬다(실제로 한 번 그랬다).
from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

os.environ.setdefault("RVP_MCP", "0")
os.environ.setdefault("RVP_PREWARM", "0")
os.environ.setdefault("RVP_JIRA_POLL_SEC", "0")

import voc_agents as V          # noqa: E402
import reply_queue              # noqa: E402

# --------------------------------------------------------------------------- #
# 1) 정책 검출 — 주입한 위반을 되찾는가 / 정상 문장을 잘못 잡지 않는가
# --------------------------------------------------------------------------- #
CLEAN = ("안녕하세요, 문의 주신 내용 확인했습니다.\n\n"
         "### 확인한 내용\n말씀해 주신 증상을 확인했습니다.\n\n"
         "### 앞으로의 진행\n담당 엔지니어가 확인하고 있으며, 확인되는 대로 안내드리겠습니다.\n\n"
         "감사합니다.")

# (주입 문장, 기대 코드, 검사 인자) — 실제 검토에서 문제가 됐거나 될 수 있는 형태만.
INJECTIONS: list[tuple[str, str, dict]] = [
    ("LSI-42 사례와 동일한 증상입니다.", "internal_key", {}),
    ("VOC-13 에서도 같은 보고가 있었습니다.", "internal_key", {}),
    ("問題의 원인을 확인했습니다.", "han_char", {}),
    ("다음 주까지 수정해 드리겠습니다.", "date_promise", {}),
    ("3월 15일까지 배포하겠습니다.", "date_promise", {}),
    ("2주 내 완료해 드리겠습니다.", "date_promise", {}),
    ("확인 후 환불해 드리겠습니다.", "compensation_promise", {}),
    ("무상 교체 진행해 드리겠습니다.", "compensation_promise", {}),
    ("원인 확인 중임. 결과 나오면 연락하겠음.", "plain_speech", {}),
    ("지식베이스의 인용 사례로 확인했습니다.", "internal_jargon", {}),
    ("다른 고객사 Lumen Imaging 에서도 보고되었습니다.", "third_party",
     {"forbidden": ("Lumen Imaging",)}),
]

# 걸리면 안 되는 정상 문장 — 오탐 측정용.
NEGATIVES: list[str] = [
    "다음 주에 추가 확인을 진행하며, 결과가 나오는 대로 안내드리겠습니다.",
    "교환·환불 여부는 담당 부서에서 제품 확인 후 결정되며, 절차를 안내드리겠습니다.",
    "PM9C3-NVMe 제품의 HS-G4 링크 설정을 확인해 주시면 안내드리겠습니다.",
    "이번 주 중 담당자가 연락드릴 예정이며, 확인되는 대로 다시 안내드리겠습니다.",
    "펌웨어 3.2 버전 적용 여부를 확인해 주시면 안내드리겠습니다.",
]


CLEAN_EN = ("Hello, thank you for reaching out. We have reviewed your inquiry.\n\n"
            "### What we found so far\nWe are still confirming the cause.\n\n"
            "### Next steps\nOur engineers are looking into it and we will update you "
            "as soon as we confirm the cause.\n\nThank you.")

EN_INJECTIONS: list[tuple[str, str]] = [
    ("This matches case LSI-42.", "internal_key"),
    ("We will complete the fix by next week.", "date_promise"),
    ("We will deploy the firmware within 2 weeks.", "date_promise"),
    ("We will ship the fix in 3 days.", "date_promise"),
    ("We will issue a refund once we confirm this.", "compensation_promise"),
    ("확인 후 안내드리겠습니다.", "wrong_language"),
]

NEGATIVES_EN: list[str] = [
    "We are reviewing the refund process and will get back to you.",
    "Next week our engineer will run additional checks and we will update you.",
    "Please confirm whether firmware 3.2 is applied; we will update you afterwards.",
]


def check_policy_detection() -> tuple[int, int, int, int]:
    print("\n[1] 정책 검출 — 주입 위반 복원 / 정상 문장 오탐")
    hit = 0
    for sentence, expect, kw in INJECTIONS:
        body = CLEAN.replace("감사합니다.", sentence + "\n\n감사합니다.")
        codes = {v["code"] for v in V.check(body, **kw)["violations"]}
        ok = expect in codes
        hit += ok
        print(f"  {'✓' if ok else '✗'} {expect:<22} ← {sentence[:36]}"
              + ("" if ok else f"   (검출: {sorted(codes)})"))
    fp = 0
    for sentence in NEGATIVES:
        body = CLEAN.replace("감사합니다.", sentence + "\n\n감사합니다.")
        bad = [v for v in V.check(body)["violations"] if v["severity"] == "block"]
        fp += bool(bad)
        print(f"  {'✓' if not bad else '✗'} 오탐 없음            ← {sentence[:36]}"
              + ("" if not bad else f"   (오검출: {[v['code'] for v in bad]})"))
    # 영어 답변에도 같은 규칙이 살아 있는가 — 규칙이 언어에 따라 사라지면 없는 것과 같다.
    for sentence, expect in EN_INJECTIONS:
        body = CLEAN_EN.replace("Thank you.", sentence + "\n\nThank you.")
        codes = {v["code"] for v in V.check(body, lang="en")["violations"]}
        ok = expect in codes
        hit += ok
        print(f"  {'✓' if ok else '✗'} {expect:<22} ← (en) {sentence[:32]}"
              + ("" if ok else f"   (검출: {sorted(codes)})"))
    for sentence in NEGATIVES_EN:
        body = CLEAN_EN.replace("Thank you.", sentence + "\n\nThank you.")
        bad = [v for v in V.check(body, lang="en")["violations"] if v["severity"] == "block"]
        fp += bool(bad)
        print(f"  {'✓' if not bad else '✗'} 오탐 없음(en)        ← {sentence[:34]}"
              + ("" if not bad else f"   (오검출: {[v['code'] for v in bad]})"))
    ben = V.check(CLEAN_EN, lang="en")
    print(f"  {'✓' if not ben['blocked'] else '✗'} 영어 기준 답변은 차단되지 않는다"
          + ("" if not ben["blocked"] else f"   {ben['violations']}"))
    base = V.check(CLEAN)
    print(f"  {'✓' if not base['blocked'] else '✗'} 기준 답변은 차단되지 않는다")
    return hit, len(INJECTIONS) + len(EN_INJECTIONS), fp, len(NEGATIVES) + len(NEGATIVES_EN)


# --------------------------------------------------------------------------- #
# 2) 전수 생성 — 실제 서버 경로로
# --------------------------------------------------------------------------- #
# 요청 반영 판정은 voc_agents.ask_terms 가 단일 소스다 — 하네스가 따로 구현하면
# 하네스 수치와 제품(경고 `unanswered_ask`)의 판정이 갈라진다.
ask_terms = V.ask_terms


def run_corpus(use_llm: bool, limit: int) -> dict:
    import server
    reply_queue._Q.path = Path(tempfile.mkdtemp()) / "reply.json"   # 실제 큐 격리
    # 지식 공백 기록도 막는다 — 검증 실행이 운영 신호를 만들면 대시보드의 '공백' 이
    # 실제 사용자 요청이 아니라 테스트 실행 횟수를 세게 된다.
    server._record_gap = lambda *a, **k: None
    st = server._reco_state()
    keys = [k for k, v in st["by_key"].items()
            if k.startswith("VOC-") and v.get("status") != "완료"][:limit]
    print(f"\n[2] 전수 생성 — 미해결 VOC {len(keys)}건 "
          f"({'LLM' if use_llm else '템플릿'} 경로)")
    rows = []
    for key in keys:
        res = server.voc_reply_draft(server.ReplyDraftBody(key=key, use_llm=use_llm))
        if not res.get("queued"):
            print(f"  ✗ {key} 초안 실패: {res.get('reason')}")
            continue
        it = res["item"]
        rec = st["by_key"][key]
        # 하네스는 제품 경고보다 **엄한** 임계를 쓴다(핵심어 2개 이상). 경고는 소음을
        # 피해야 하지만 품질 측정은 후하면 안 된다 — 목적이 다르다.
        terms = ask_terms(rec.get("customer_ask", ""))
        # 요청을 그대로 되짚은 줄은 빼고 센다. 템플릿은 요청 문장을 본문에 그대로
        # 옮기므로, 그 줄을 세면 "요청을 복사했다" 가 "요청에 답했다" 로 둔갑한다.
        measured = "\n".join(ln for ln in it["body"].split("\n")
                             if not ln.strip().startswith("요청하신 사항:"))
        hit = sum(1 for t in terms if t.lower() in measured.lower())
        sections = V.INTENTS[it["intent"]]["sections"]
        rows.append({
            "key": key, "intent": it["intent"], "engine": it["engine"],
            "blocked": it["policy"]["blocked"],
            "codes": [v["code"] for v in it["policy"]["violations"]],
            "leak": bool(re.search(r"[A-Z][A-Z0-9]*-\d+", it["body"])),
            "sections_ok": all(s in it["body"] for s in sections),
            "ask_terms": len(terms), "ask_hit": hit,
            "ask_covered": (hit >= min(2, len(terms))) if terms else None,
            "has_evidence": it["has_evidence"], "body": it["body"],
            "ask": rec.get("customer_ask", ""),
        })
    return {"rows": rows, "keys": keys}


# 목 데이터는 전부 한국어다. 영어 경로는 실제로 한 번도 실행되지 않은 채 배포될 수
# 있어, 합성 레코드를 KB 에 끼워 넣어 **같은 서버 경로로** 통과시킨다.
EN_RECORD = {
    "key": "VOC-EN-TEST",
    "summary": "Card reader intermittently stops responding while wireless charging",
    "symptom": ("When the phone sits on the wireless charging pad, the transit card "
                "reader fails to detect it. Taking it off the pad works fine."),
    "customer_ask": ("Is this expected behaviour when charging and tapping at the same "
                     "time? We need to decide whether to change our user guidance."),
    "status": "접수", "chip": "", "category": "NFC", "customer": "",
}


def run_english_path(use_llm: bool) -> bool:
    import server
    print("\n[2b] 영어 문의 경로 — 고객이 쓴 언어로 답하는가")
    st = server._reco_state()
    st["by_key"][EN_RECORD["key"]] = dict(EN_RECORD)
    try:
        res = server.voc_reply_draft(server.ReplyDraftBody(key=EN_RECORD["key"], use_llm=use_llm))
        if not res.get("queued"):
            print(f"  ✗ 초안 실패: {res.get('reason')}")
            return False
        it = res["item"]
        lang_ok = it.get("lang") == "en" and V.detect_lang(it["body"]) == "en"
        pol = it["policy"]
        print(f"  {'✓' if lang_ok else '✗'} 영어로 답한다 (판정 {it.get('lang')}, "
              f"본문 {V.detect_lang(it['body'])}, 엔진 {it['engine']})")
        print(f"  {'✓' if not pol['blocked'] else '✗'} 발송 차단 없음"
              + ("" if not pol["blocked"] else f"   {pol['violations']}"))
        if not lang_ok:
            print("    " + it["body"][:200].replace("\n", " "))
        return lang_ok and not pol["blocked"]
    finally:
        st["by_key"].pop(EN_RECORD["key"], None)


def report_corpus(rows: list[dict], use_llm: bool = False) -> None:
    n = len(rows) or 1
    blocked = sum(r["blocked"] for r in rows)
    leaks = sum(r["leak"] for r in rows)
    sec_ok = sum(r["sections_ok"] for r in rows)
    askable = [r for r in rows if r["ask_covered"] is not None]
    covered = sum(r["ask_covered"] for r in askable)
    warn = Counter(c for r in rows for c in r["codes"])
    print(f"\n  건수                 {len(rows)}")
    print(f"  발송 차단 잔존       {blocked}/{len(rows)}  (0 이어야 한다)")
    print(f"  내부 키 유출         {leaks}/{len(rows)}  (0 이어야 한다)")
    print(f"  유형 골격 준수       {sec_ok}/{len(rows)}  ({sec_ok / n:.3f})")
    if askable:
        print(f"  요청 반영(어휘)      {covered}/{len(askable)}  ({covered / len(askable):.3f})")
    print(f"  근거 있음            {sum(r['has_evidence'] for r in rows)}/{len(rows)}")
    engines = dict(Counter(r["engine"] for r in rows))
    print(f"  생성 엔진            {engines}"
          + ("   ← LLM 경로인데 전부 템플릿이다. 생성이 실패하고 있다"
             if use_llm and set(engines) == {"template"} else ""))
    print(f"  유형 분포            {dict(Counter(r['intent'] for r in rows))}")
    print(f"  정책 지적            {dict(warn) or '없음'}")
    for r in rows:
        if r["blocked"] or r["leak"]:
            print(f"    ✗ {r['key']} codes={r['codes']} leak={r['leak']}")
    # 어느 건이 요청을 놓쳤는지 보여준다 — 비율만 알면 프롬프트를 고칠 수 없다.
    misses = [r for r in rows if r["ask_covered"] is False]
    if misses:
        print("\n  요청을 놓친 것으로 보이는 건 (어휘 기준, 하한):")
        for r in misses:
            print(f"    · {r['key']} 요청: {r['ask'][:70]}")
            print(f"      핵심어 {r['ask_hit']}/{r['ask_terms']} 일치 — {r['body'][:90]}…")


# --------------------------------------------------------------------------- #
# 3) LLM 판정 — 요청에 답했는가 / 고객에게 보내도 되는가
# --------------------------------------------------------------------------- #
_VERDICT_RE = re.compile(r"^\s*(ANSWERS|SENDABLE)\s*:\s*(yes|no)", re.I | re.M)
_PROBLEM_RE = re.compile(r"^\s*PROBLEM\s*:\s*(.*)$", re.I | re.M)


def _parse_verdict(text: str) -> tuple[bool, bool, str] | None:
    """평문 판정 파싱.

    구조화 출력(pydantic 스키마)을 쓰다가 30건 중 5건이 '문자열이 아님' 으로 깨졌다.
    판정이 깨지면 그 건은 집계에서 빠지고, 많이 빠지면 수치 자체를 믿을 수 없다.
    두 줄짜리 평문은 어떤 모델에서도 나온다.
    """
    got = {k.upper(): v.lower() == "yes" for k, v in _VERDICT_RE.findall(text or "")}
    if "ANSWERS" not in got or "SENDABLE" not in got:
        return None
    m = _PROBLEM_RE.search(text or "")
    return got["ANSWERS"], got["SENDABLE"], (m.group(1).strip() if m else "")


JUDGE_PROMPT = (
    "아래는 고객이 요청한 내용과, 그 고객에게 보낼 답변 초안입니다.\n\n"
    "1) 답변이 고객의 요청에 실제로 답하고 있습니까? 지금 확답할 수 없다는 사실을 "
    "분명히 밝히고 언제 답하겠다고 했다면 '답한 것'으로 봅니다. 질문을 그냥 건너뛴 "
    "경우만 '아니오' 입니다.\n"
    "2) 내부 이슈 번호 노출·확정 일정 약속·보상 확약·반말·다른 고객사 정보 없이, "
    "이대로 고객에게 보내도 됩니까?\n\n"
    "정확히 아래 세 줄로만 답하세요. 다른 말은 쓰지 마세요.\n"
    "ANSWERS: yes 또는 no\n"
    "SENDABLE: yes 또는 no\n"
    "PROBLEM: 문제가 있으면 한 문장, 없으면 없음\n\n"
    "## 고객 요청\n{ask}\n\n## 답변 초안\n{body}")


def judge(rows: list[dict]) -> None:
    """어휘 일치가 놓치는 것을 본다 — 뜻은 맞는데 말이 다른 경우."""
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        print("\n[3] LLM 판정 — OPENROUTER_API_KEY 없음, 건너뜀")
        return
    from agno.agent import Agent
    from agno.models.openrouter import OpenRouter

    model = os.getenv("RVP_JUDGE_MODEL") or os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini")
    agent = Agent(model=OpenRouter(id=model, api_key=api_key,
                                   base_url=os.getenv("OPENROUTER_BASE_URL",
                                                      "https://openrouter.ai/api/v1")),
                  markdown=False)
    print(f"\n[3] LLM 판정 — 모델 {model}")
    ok_ask = ok_send = judged = skipped = 0
    for r in rows:
        try:
            out = agent.run(input=JUDGE_PROMPT.format(ask=r["ask"], body=r["body"]))
            parsed = _parse_verdict(getattr(out, "content", "") or "")
            if parsed is None:
                raise ValueError("판정 형식 아님")
        except Exception as e:
            # 판정 실패는 '통과' 도 '실패' 도 아니므로 **분모에서 뺀다** — 실패를
            # 통과로 세면 수치가 조용히 좋아진다.
            skipped += 1
            print(f"  · {r['key']} 판정 실패(집계 제외): {str(e)[:80]}")
            continue
        answers, sendable, problem = parsed
        judged += 1
        ok_ask += answers
        ok_send += sendable
        if not (answers and sendable):
            print(f"  ✗ {r['key']} ask={answers} send={sendable} — {problem[:110]}")
    if judged:
        print(f"\n  요청에 답함           {ok_ask}/{judged}  ({ok_ask / judged:.3f})")
        print(f"  발송 가능             {ok_send}/{judged}  ({ok_send / judged:.3f})")
    if skipped:
        print(f"  판정 실패(집계 제외)  {skipped}건")


ROWS_CACHE = ROOT / "tmp_db" / "reply_validation_rows.json"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--llm", action="store_true", help="실제 LLM 생성 경로로 돌린다")
    ap.add_argument("--judge", action="store_true", help="LLM 이 답변을 판정한다")
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--reuse", action="store_true",
                    help="직전 생성 결과를 재사용한다(판정만 다시 돌릴 때)")
    a = ap.parse_args()

    hit, total, fp, negs = check_policy_detection()
    print(f"\n  검출 복원율          {hit}/{total}  ({hit / total:.3f})")
    print(f"  오탐(정상 문장 차단)  {fp}/{negs}")

    if a.reuse and ROWS_CACHE.exists():
        out = {"rows": json.loads(ROWS_CACHE.read_text(encoding="utf-8"))}
        print(f"\n[2] 전수 생성 — 직전 결과 재사용 ({len(out['rows'])}건, {ROWS_CACHE})")
        report_corpus(out["rows"], a.llm)
        en_ok = True          # 재사용 모드는 생성을 돌리지 않으므로 영어 경로도 건너뛴다
    else:
        out = run_corpus(a.llm, a.limit)
        # 생성은 비싸다(LLM 32회). 판정만 다시 돌릴 수 있게 남긴다.
        ROWS_CACHE.parent.mkdir(exist_ok=True)
        ROWS_CACHE.write_text(json.dumps(out["rows"], ensure_ascii=False, indent=1),
                              encoding="utf-8")
        report_corpus(out["rows"], a.llm)
        en_ok = run_english_path(a.llm)
    if a.judge:
        judge(out["rows"])

    # 0건 검증은 통과가 아니라 실패다 — 아무것도 안 재고 초록을 내는 것이
    # 검증 하네스가 할 수 있는 가장 나쁜 일이다.
    if not out["rows"]:
        print("\n실패 — 미해결 VOC 를 하나도 찾지 못했다. "
              "RVP_KB_EXTRA(.env) 와 data/voc_mock_issues.json 을 확인하라 "
              "(생성: .venv/bin/python scripts/build_voc_mock.py).")
        return 1
    bad = ((hit < total) or fp or not en_ok
           or any(r["blocked"] or r["leak"] for r in out["rows"]))
    print("\n" + ("실패 — 위 ✗ 항목 확인" if bad else "모두 통과"))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
