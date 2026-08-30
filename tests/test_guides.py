"""답변 지침 원천(Confluence·FAQ·로컬 문서) 검증.

여기서 지키는 계약:
  · 문서를 **섹션 단위**로 자른다 — 긴 FAQ 한 장이 프롬프트를 다 먹으면 안 되고,
    어느 문단이 근거인지 말할 수 있어야 한다
  · 파서는 **내용**으로 고른다 — HTML 을 마크다운으로 읽으면 태그가 본문이 된다
  · 한국어 어미를 떼고 찾는다 — "환불해 주세요" 가 "환불은 …" 지침을 찾아야 한다
  · 무관한 질의에는 **아무 지침도 주지 않는다** — 아무거나 끌어다 붙이면 규칙이 아니라 소음이다
  · 원천 하나가 실패해도 나머지는 수집된다 — 위키 한 장이 막혔다고 지침 전체가 비면 안 된다
  · 지침은 프롬프트에서 **사례보다 우선**한다고 명시된다

실행:
    .venv/bin/python tests/test_guides.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import guides as G  # noqa: E402
import voc_agents as V  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'✓' if cond else '✗'} {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


HTML = ("<html><head><title>응대 지침</title></head><body>"
        "<h2>교환·환불 문의 응대</h2><p>승인 권한은 CS 운영팀에 있습니다. 담당자는 절차만 안내합니다.</p>"
        "<h2>권한 신청 절차</h2><p>포털에서 신청하고 팀장 승인 후 반영됩니다. 처리는 영업일 1일입니다.</p>"
        "<script>var x = '무시되어야 하는 스크립트 본문입니다';</script>"
        "</body></html>")


def test_sectioning() -> None:
    print("\n[섹션 분해]")
    secs = G.html_to_sections(HTML, title="응대 지침")
    titles = [t for t, _ in secs]
    check("제목마다 섹션이 나뉜다", "교환·환불 문의 응대" in titles and "권한 신청 절차" in titles,
          str(titles))
    check("본문에 태그가 남지 않는다", all("<" not in b for _, b in secs))
    check("script 는 버린다", all("스크립트 본문" not in b for _, b in secs))
    check("너무 짧은 조각은 섹션이 아니다",
          G.html_to_sections("<h2>제목</h2><p>짧음</p>") == [])
    md = G.md_to_sections("# 개요\n" + "충분히 긴 본문입니다. " * 4 + "\n## 절차\n" + "절차 본문입니다. " * 4)
    check("마크다운도 제목에서 자른다", [t for t, _ in md] == ["개요", "절차"], str(md))


def test_parser_choice() -> None:
    """확장자/내용으로 파서를 고른다 — 전부 마크다운으로 읽으면 HTML 이 통째로
    한 섹션이 되고 본문에 태그가 남는다(실제로 그렇게 나왔다)."""
    print("\n[파서 선택]")
    tmp = Path(tempfile.mkdtemp())
    (tmp / "faq.html").write_text(HTML, encoding="utf-8")
    (tmp / "guide.md").write_text("# 개요\n" + "본문입니다. " * 8, encoding="utf-8")
    G.ROOT = tmp
    out = G.collect([f"file:faq.html", f"file:guide.md"])
    check("오류 없이 수집", out["errors"] == [], str(out["errors"]))
    kinds = {s["doc_title"] for s in out["sections"]}
    check("HTML 은 태그가 벗겨진다",
          all("<" not in s["text"] for s in out["sections"]), str(kinds))
    check("HTML 제목은 <title> 을 쓴다", "응대 지침" in kinds, str(kinds))
    check("두 문서 모두 섹션이 나온다", len(out["docs"]) == 2 and len(out["sections"]) >= 3)


def test_search() -> None:
    print("\n[지침 검색]")
    secs = G.html_to_sections(HTML, title="응대 지침")
    secs = [{"doc_title": "응대 지침", "section": t, "text": b, "url": "", "kind": "url"}
            for t, b in secs]
    check("어미가 달라도 찾는다 — '환불해' vs '환불은'",
          G.search("환불해 주세요", secs=secs)[0]["section"] == "교환·환불 문의 응대",
          str(G.search("환불해 주세요", secs=secs)))
    check("권한 질의", G.search("스테이징 권한 주세요", secs=secs)[0]["section"] == "권한 신청 절차")
    check("무관한 질의에는 아무것도 주지 않는다", G.search("오늘 점심 메뉴", secs=secs) == [])
    check("문서가 적어도 찾는다 — BM25 는 소규모 코퍼스에서 전부 0 을 준다",
          G.search("권한", secs=secs[:2]) != [], "어휘 겹침 폴백이 있어야 한다")
    check("빈 질의도 안전", G.search("", secs=secs) == [])
    blk, hits = G.prompt_block("환불 절차가 어떻게 돼요?", secs=secs)
    check("프롬프트 블록에 출처 제목이 붙는다", "교환·환불 문의 응대" in blk, blk[:80])
    check("출처를 함께 돌려준다", hits and hits[0]["doc_title"] == "응대 지침")
    check("지침이 없으면 빈 블록 — 없는 규칙을 있는 척하지 않는다",
          G.prompt_block("오늘 점심", secs=secs) == ("", []))


def test_failure_isolation() -> None:
    print("\n[원천 격리]")
    tmp = Path(tempfile.mkdtemp())
    (tmp / "ok.md").write_text("# 개요\n" + "정상 문서 본문입니다. " * 5, encoding="utf-8")
    G.ROOT = tmp
    out = G.collect(["file:ok.md", "file:없는파일.md", "이상한형식"])
    check("실패해도 나머지는 수집된다", len(out["docs"]) == 1 and out["sections"], str(out))
    check("실패는 조용히 넘어가지 않는다", len(out["errors"]) == 2, str(out["errors"]))
    check("알 수 없는 형식도 오류로 남는다",
          any("알 수 없는" in e["error"] for e in out["errors"]), str(out["errors"]))


def test_prompt_precedence() -> None:
    print("\n[지침이 사례를 이긴다]")
    intent = V.classify("환불해 주세요")
    p = V.reply_prompt({"summary": "환불 요청"}, [], None, intent,
                       policy_docs="[응대 지침 — 교환·환불]\n승인 권한은 CS 운영팀에 있습니다.")
    check("지침 절이 들어간다", "답변 지침" in p)
    check("사례보다 우선한다고 못박는다", "사례보다 우선" in p, p[:200])
    check("지침이 없으면 그 절도 없다",
          "답변 지침" not in V.reply_prompt({"summary": "x"}, [], None, intent))
    en = V.reply_prompt({"summary": "refund"}, [], None, intent, lang="en",
                        policy_docs="[Policy] CS team approves refunds.")
    check("영어 경로에도 같은 규칙", "override past" in en and "CS team approves" in en)

    import issue_chat as C
    cp = C.build_prompt("환불 규정이 어떻게 돼?", [], policy_docs="[응대 지침] CS 운영팀 승인")
    check("챗봇도 지침을 근거로 쓴다", "사내 지침" in cp and "CS 운영팀 승인" in cp)


def test_manual_store() -> None:
    """직접 작성 지침 — 위키가 없어도 규칙을 넣을 수 있어야 한다."""
    print("\n[직접 작성 지침]")
    G.MANUAL_FILE = Path(tempfile.mkdtemp()) / "manual.json"
    a = G.manual_upsert("교환·환불 응대", "환불 승인은 CS 운영팀이 합니다. 담당자는 절차만 안내합니다.")
    b = G.manual_upsert("배포 공지", "배포 30분 전에 공지 채널에 알립니다. 롤백 기준도 함께 적습니다.")
    check("추가되고 id 가 붙는다", a["id"] == "G-1" and b["id"] == "G-2", f"{a['id']} {b['id']}")
    G.manual_upsert("교환·환불 응대", "수정된 규칙입니다. 접수 절차만 안내합니다.", item_id=a["id"])
    check("같은 id 는 수정된다",
          len(G.manual_items()) == 2 and "수정된 규칙" in G.manual_items()[0]["text"])
    check("제목·본문이 비면 거절", _raises(lambda: G.manual_upsert("", "본문")))
    secs = G._manual_sections()
    check("검색 대상이 된다",
          G.search("환불 절차", secs=secs)[0]["section"] == "교환·환불 응대", str(secs))
    check("kind 로 출처를 구분한다", all(s["kind"] == "manual" for s in secs))
    check("삭제된다", G.manual_delete(b["id"]) and len(G.manual_items()) == 1)
    check("없는 id 삭제는 False", G.manual_delete("G-999") is False)


def test_manual_precedence() -> None:
    """직접 작성한 규칙이 수집본보다 앞에 온다 — 사람이 방금 쓴 것이 먼저 걸려야 한다."""
    print("\n[직접 작성 우선]")
    G.MANUAL_FILE = Path(tempfile.mkdtemp()) / "manual.json"
    G.STORE_FILE = Path(tempfile.mkdtemp()) / "guides.json"
    G.manual_upsert("환불 규정", "환불은 이번 분기부터 CS 운영팀이 단독 승인합니다.")
    G.save({"sections": [{"doc_title": "옛 위키", "section": "환불 규정",
                          "text": "환불은 담당 엔지니어가 승인합니다.", "url": "", "kind": "url"}],
            "docs": [{"source": "x", "title": "옛 위키", "url": ""}], "errors": []})
    hits = G.search("환불 승인", k=2)
    check("직접 작성이 먼저", hits and hits[0]["kind"] == "manual",
          str([(h["kind"], h["score"]) for h in hits]))
    st = G.stats()
    check("현황이 둘을 함께 센다", st["manual"] == 1 and st["sections"] == 2, str(st))


def _raises(fn) -> bool:
    try:
        fn()
        return False
    except Exception:
        return True


def main() -> int:
    for fn in (test_sectioning, test_parser_choice, test_search,
               test_failure_isolation, test_manual_store, test_manual_precedence,
               test_prompt_precedence):
        fn()
    print()
    if FAILS:
        print(f"실패 {len(FAILS)}건: {FAILS}")
        return 1
    print("모두 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
