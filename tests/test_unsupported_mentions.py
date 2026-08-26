"""본문의 근거 없는 사례 언급 표시 검증.

실측(2026-08-04, `scripts/eval_explanations.py --n 30`): 설명 30건 중 **2건**이
근거에 없는 사례 키를 본문에서 인용했다. LSI-112 는 모델이 스스로 이렇게 적었다:

    "제공된 사례 중 LSI-70과 LSI-133은 본문에 직접 키가 명시되지 않았지만
     LSI-7-rca에 포함된 것으로 간주됩니다"

시스템은 이미 이걸 감지해 `dropped` 로 분리하고 있었다. 문제는 **본문은 그대로 두고**
화면에 "매치 외 인용 제거됨" 이라고 적은 것이다 — 제거된 것이 없으니 거짓이고,
사용자는 그 말을 믿고 본문의 LSI-70 을 찾으러 간다.

LLM 재작성은 쓰지 않는다 — 문서 전체를 LLM 에 맡겼다가 무관한 문장의 "펌웨어"가
"펌 - 만웨어"로 깨진 적이 있다(tests/test_lang_validator.py). 여기서는 정확히 그
키 토큰만 결정적으로 치환한다.

실행:
    .venv/bin/python tests/test_unsupported_mentions.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "backend"))

os.environ.update({"RVP_JIRA_POLL_SEC": "0", "RVP_PREWARM": "0", "RVP_MCP": "0"})

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'✓' if cond else '✗'} {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


import server  # noqa: E402

mark = server.mark_unsupported


def test_marks_prose_only() -> None:
    print("\n[본문만 표시, 코드는 건드리지 않는다]")
    md = ("해결책 3 (LSI-6, LSI-90) 을 적용한다.\n"
          "```\nnvme get-log LSI-90\n```\n"
          "인라인 `LSI-90` 도 코드다. 본문의 LSI-90 은 표시된다.")
    out = mark(md, ["LSI-90"])
    check("본문 언급에 표시", out.count("LSI-90(미제공)") == 2, out)
    check("코드 블록은 원문", "nvme get-log LSI-90\n```" in out, out)
    check("인라인 코드는 원문", "`LSI-90`" in out, out)
    check("근거 키는 건드리지 않는다", "LSI-6," in out and "LSI-6(미제공)" not in out, out)


def test_no_false_positives() -> None:
    print("\n[오탐 없음]")
    md = "LSI-901 과 LSI-9 는 다른 이슈다. LSI-90-rca 도 별개다."
    out = mark(md, ["LSI-90"])
    check("접두 일치로 오탐 없음", out == md, out)
    check("빈 목록이면 무변경", mark(md, []) == md)
    # 키 판정은 issue_keys 가 단일 소스다. 예전에는 `LSI-\d+` 로 하드코딩돼 있어
    # 다른 프로젝트 키(VOC-1 등)가 전부 "형식 밖"으로 무시됐다 — KB 에 VOC 원천이
    # 들어오자 인용 검증 전체가 조용히 무력화되는 버그였다.
    check("다른 프로젝트 키도 정상 처리", mark("VOC-1 참조", ["VOC-1"]) == "VOC-1(미제공) 참조",
          mark("VOC-1 참조", ["VOC-1"]))
    # 가드의 취지는 유지: 키 형태가 아닌 문자열은 손대지 않는다.
    for junk in ("PM9C3-NVMe", "HS-G4", "ISO-DEP", "not-a-key"):
        check(f"키 형태가 아니면 무시 ({junk})",
              mark(f"{junk} 참조", [junk]) == f"{junk} 참조", mark(f"{junk} 참조", [junk]))


def test_idempotent() -> None:
    print("\n[멱등 — 캐시 읽기마다 덧붙지 않는다]")
    md = "본문의 LSI-70 참조."
    once = mark(md, ["LSI-70"])
    twice = mark(once, ["LSI-70"])
    check("두 번 걸어도 한 번만", once == twice, f"{once!r} vs {twice!r}")
    check("세 번도 동일", mark(twice, ["LSI-70"]) == once)


def test_multiple_keys() -> None:
    print("\n[여러 키]")
    md = "우회책 없음 (LSI-7-rca, LSI-70, LSI-133, LSI-154)."
    out = mark(md, ["LSI-70", "LSI-133"])
    check("둘 다 표시", "LSI-70(미제공)" in out and "LSI-133(미제공)" in out, out)
    check("근거 키는 그대로", "LSI-7-rca," in out and "LSI-154)" in out, out)


def test_compose_applies_marking() -> None:
    """구조화 생성 경로가 표시를 실제로 건다."""
    print("\n[생성 경로 연결]")

    class Exp:
        root_cause = "원인은 LSI-90 사례와 같다."
        resolution = "해결은 LSI-6 참조."
        workaround = ""
        cited_keys = ["LSI-6", "LSI-90"]

    md, cited, dropped = server._compose_explanation(Exp(), {"LSI-6"})
    check("근거 밖 키가 dropped 로", dropped == ["LSI-90"], str(dropped))
    check("검증된 인용만 cited", cited == ["LSI-6"], str(cited))
    check("본문에 표시가 걸린다", "LSI-90(미제공)" in md, md)
    check("근거 키는 표시 없음", "LSI-6(미제공)" not in md, md)


def test_cached_read_is_retroactive() -> None:
    """이 기능 이전에 저장된 캐시본에도 읽을 때 표시가 걸리는가."""
    print("\n[기존 캐시본 소급]")
    old_cached = {"markdown": "예전에 저장된 본문. LSI-70 참조.",
                  "citations": ["LSI-7"], "dropped": ["LSI-70"]}
    shown = mark(old_cached["markdown"], old_cached["dropped"])
    check("재생성 없이 표시된다", "LSI-70(미제공)" in shown, shown)
    check("원본은 그대로(저장본 불변)", "LSI-70(미제공)" not in old_cached["markdown"])


def test_evidence_serialization_strips_foreign_keys() -> None:
    """근거 본문에 박힌 **다른 사례 키**가 프롬프트로 새지 않는가.

    이것이 "인용 환각" 의 진짜 정체였다. 큐레이션 RCA 기사(`{원본}-rca`)는 사람이 쓴
    종합 분석이라 본문에 "참고 사례: LSI-36, LSI-183, LSI-205" 같은 줄이 들어간다
    (실측: 내용 있는 -rca 4건 전부). 모델은 프롬프트에 보이는 키를 충실히 옮겼을
    뿐인데 검증기가 환각으로 셌다.
    """
    print("\n[근거 본문의 외부 키 제거]")
    keep = {"LSI-120", "LSI-120-rca", "LSI-36"}
    rec = {"key": "LSI-120-rca", "summary": "종합 분석",
           "root_cause": "VRR 전환 시 보정 지연.",
           "resolution": "보간 적용.\n참고 사례: LSI-36, LSI-183, LSI-205\n",
           "workaround": "LSI-36 과 동일. LSI-205 도 참고."}
    block = server._case_block(rec, keep)
    check("'참고 사례:' 줄이 사라진다", "참고 사례" not in block, block)
    check("근거 밖 키가 남지 않는다", "LSI-205" not in block and "LSI-183" not in block, block)
    check("근거 안 키는 보존", "LSI-36" in block, block)
    check("지운 자리를 표시", "(다른 사례)" in block, block)

    raw = server._case_block(rec)
    check("keep 없이 부르면 원문 그대로(하위호환)", "LSI-205" in raw)


def test_example_key_does_not_leak() -> None:
    """인용 형식 예시가 근거 밖 키를 흘리지 않는가.

    예전 프롬프트는 "(LSI-49)처럼 인라인 인용하세요" 였다. 진짜처럼 보이는 키를
    예시로 두니 모델이 그대로 베꼈다 — LSI-141 설명의 LSI-49 가 여기서 나왔다.
    """
    print("\n[형식 예시 키]")
    recs = [{"key": "LSI-77"}, {"key": "LSI-88"}]
    check("근거의 첫 키를 예시로", server._example_key(recs) == "LSI-77",
          server._example_key(recs))
    # 자리표시자는 JIRA_PROJECT_KEY 를 따른다 — 여기서 "LSI-000" 을 기대하면
    # 다른 프로젝트를 쓰는 사람에게서 깨진다(테스트가 .env 에 묶인다).
    import issue_keys
    check("근거가 없으면 자리표시자", server._example_key([]) == issue_keys.placeholder(),
          server._example_key([]))
    check("자리표시자는 실제 키처럼 보이지 않는다(-000)",
          issue_keys.placeholder().endswith("-000"), issue_keys.placeholder())


if __name__ == "__main__":
    test_marks_prose_only()
    test_no_false_positives()
    test_idempotent()
    test_multiple_keys()
    test_compose_applies_marking()
    test_cached_read_is_retroactive()
    test_evidence_serialization_strips_foreign_keys()
    test_example_key_does_not_leak()
    print("\n" + "=" * 56)
    if FAILS:
        print(f"실패 {len(FAILS)}건: " + " / ".join(FAILS))
        raise SystemExit(1)
    print("전부 통과")
