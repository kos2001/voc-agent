"""초안 결함 **사전 탐지** 검증 — 사람이 읽기 전에 잡히는가, 그리고 오탐이 없는가.

왜 사전 탐지인가:
  `draft_feedback` 는 사람이 초안을 **읽고 고친 뒤에야** 원인을 안다. 그 사이에
  검토 한 번이 통째로 낭비되고, 같은 결함이 다음 초안에도 그대로 나간다.
  분류 체계(CAUSES)를 이미 갖고 있다면, 그중 **결정적으로 확인 가능한 것들**은
  초안이 사람에게 가기 전에 잡을 수 있다.

여기서 지키는 계약:
  · 탐지 결과는 `draft_feedback.CAUSES` 의 코드로만 나온다 — 분류 체계가 둘이 되면
    사전 탐지와 사후 집계가 서로 다른 말을 한다.
  · **오탐 0 이 정탐보다 중요하다.** 게이트로 쓰이는 신호가 멀쩡한 초안을 잡으면
    사람이 곧 무시하기 시작하고, 그 순간 탐지기는 없는 것과 같다. 그래서 확실한
    규칙(certain)과 추정 규칙(likely)을 분리해 표시한다.
  · 근거 목록에 없는 키 인용은 certain 이다 — 대조할 정답이 있다.
  · 정상 초안에는 아무것도 나오지 않는다.

실행:
    .venv/bin/python tests/test_draft_defects.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import draft_defects as DD      # noqa: E402
import draft_feedback as D      # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'✓' if cond else '✗'} {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


EVIDENCE = ["LSI-10", "LSI-24", "LSI-31"]

# 결함이 없는 초안 — 섹션이 모두 있고, 제공된 키만 인용하고, 번호 절차가 있고,
# 사례 밖 서술에는 (배경) 표시가 붙어 있다.
CLEAN = """🤖 **자동 근본원인 분석** (RCA-bot)

### 🎯 예상 근본원인
UFS bkops 가 critical 로 올라가며 write latency 가 튀는 것으로 보입니다 (LSI-10).

### 🔍 증상→원인 인과 분석
호스트가 bkops 를 지연시키면 내부 GC 가 밀리고, 그 결과 쓰기 지연이 관측됩니다 (LSI-24).

### ✅ 권장 해결 단계
1. 현재 펌웨어 버전과 호스트 bkops 설정값을 확인합니다.
2. bkops 임계값을 사례와 동일하게 조정합니다 (LSI-10).
3. 재현 시나리오로 24시간 반복 테스트를 수행합니다.

### ↪ 임시 우회책
쓰기 부하를 분산해 임시로 완화할 수 있습니다 (LSI-24).

### 🔬 근본원인 검증 방법
bkops 레벨과 write latency 를 동시 로깅해 상관을 확인합니다 (LSI-31).

### 🧩 사례 종합 / 재발 방지
세 사례 모두 호스트 스케줄링이 원인이었습니다 (LSI-10, LSI-24).

### ⚠ 불확실성·주의
(배경) UFS 규격상 bkops 는 호스트가 허용해야 동작합니다. 증상 표현이 사례와 달라
추가 확인이 필요할 수 있습니다.
"""


def test_clean_draft_has_no_findings() -> None:
    """정상 초안에 오탐이 없어야 한다 — 이게 무너지면 탐지기는 쓰이지 않는다."""
    f = DD.detect(CLEAN, evidence_keys=EVIDENCE)
    check("정상 초안 = 결함 0", f == [], f"오탐: {[x['cause'] for x in f]} {[x['detail'] for x in f]}")


def test_bad_citation_is_certain() -> None:
    """근거 목록에 없는 키 → bad_citation, 확실(certain). 대조할 정답이 있는 유일한 축."""
    body = CLEAN.replace("(LSI-31)", "(LSI-999)")
    f = DD.detect(body, evidence_keys=EVIDENCE)
    hit = [x for x in f if x["cause"] == "bad_citation"]
    check("근거 밖 키 인용 탐지", bool(hit), str(f))
    check("bad_citation 은 certain", bool(hit) and hit[0]["certainty"] == "certain",
          str(hit))
    check("어떤 키가 문제인지 말한다", bool(hit) and "LSI-999" in hit[0]["detail"], str(hit))


def test_hanja_and_missing_section() -> None:
    """한자·섹션 누락 → tone_format. 둘 다 규격 대조라 certain."""
    f = DD.detect(CLEAN.replace("예상 근본원인", "예상 根本原因"), evidence_keys=EVIDENCE)
    check("한자 탐지", any(x["cause"] == "tone_format" for x in f), str(f))

    dropped = CLEAN.split("### ↪ 임시 우회책")[0]
    f2 = DD.detect(dropped, evidence_keys=EVIDENCE)
    check("필수 섹션 누락 탐지", any(x["cause"] == "tone_format" for x in f2), str(f2))


def test_missing_action() -> None:
    """해결 단계가 번호 없는 한 줄 일반론 → missing_action."""
    body = CLEAN.replace(
        "1. 현재 펌웨어 버전과 호스트 bkops 설정값을 확인합니다.\n"
        "2. bkops 임계값을 사례와 동일하게 조정합니다 (LSI-10).\n"
        "3. 재현 시나리오로 24시간 반복 테스트를 수행합니다.",
        "펌웨어를 최신 버전으로 업데이트하시기 바랍니다.")
    f = DD.detect(body, evidence_keys=EVIDENCE)
    check("번호 절차 없는 해결 단계 탐지",
          any(x["cause"] == "missing_action" for x in f), str(f))


def test_unsupported_claim() -> None:
    """근거도 표시도 없는 단정(완전히 해결됩니다) → unsupported_claim."""
    body = CLEAN.replace("### ⚠ 불확실성·주의\n",
                         "### ⚠ 불확실성·주의\n이 문제는 다음 펌웨어 릴리스에서 완전히 해결됩니다.\n")
    f = DD.detect(body, evidence_keys=EVIDENCE)
    check("근거 없는 단정 탐지", any(x["cause"] == "unsupported_claim" for x in f), str(f))

    # 같은 문장에 (추정) 표시가 붙으면 결함이 아니다 — 사람이 실제로 하는 수정이 이것이다.
    hedged = CLEAN.replace("### ⚠ 불확실성·주의\n",
                           "### ⚠ 불확실성·주의\n(추정) 이 문제는 다음 펌웨어 릴리스에서 완전히 해결됩니다.\n")
    f2 = DD.detect(hedged, evidence_keys=EVIDENCE)
    check("(추정) 표시가 붙으면 결함 아님",
          not any(x["cause"] == "unsupported_claim" for x in f2), str(f2))


def test_no_citation_at_all_is_too_shallow() -> None:
    """인용이 하나도 없으면 근거에 매인 글이 아니다 → too_shallow."""
    import re as _re
    # 괄호 인용을 통째로 지운다 — 키를 하나씩 빼면 "(LSI-10, LSI-24)" 같은 복수
    # 인용이 남아 인용 0건 조건이 성립하지 않는다.
    body = _re.sub(r"\s*\(LSI-[\d,\s LSI-]*\)", "", CLEAN)
    assert not _re.search(r"LSI-\d+", body), body
    f = DD.detect(body, evidence_keys=EVIDENCE)
    check("인용 0건 탐지", any(x["cause"] == "too_shallow" for x in f), str(f))


def test_causes_are_taxonomy_codes() -> None:
    """탐지 코드는 draft_feedback.CAUSES 안에만 있어야 한다 — 분류 체계는 하나다."""
    body = CLEAN.replace("(LSI-31)", "(LSI-999)").replace("예상 근본원인", "예상 根本原因")
    f = DD.detect(body, evidence_keys=EVIDENCE)
    unknown = [x["cause"] for x in f if x["cause"] not in D.CAUSES]
    check("탐지 코드 ⊆ CAUSES", not unknown, str(unknown))
    check("모든 결함에 레버가 붙는다",
          all(x.get("lever") == D.lever_of(x["cause"]) for x in f), str(f))


def test_gate_blocks_only_certain() -> None:
    """게이트는 certain 결함에만 걸린다 — 추정으로 발송을 막으면 근거가 재현되지 않는다."""
    certain = DD.detect(CLEAN.replace("(LSI-31)", "(LSI-999)"), evidence_keys=EVIDENCE)
    check("certain 결함은 blocking", DD.blocking(certain) != [], str(certain))

    # 추정 규칙만 걸린 경우 — 막지 않고 경고만.
    likely = [x for x in DD.detect(
        CLEAN.replace("### ⚠ 불확실성·주의\n",
                      "### ⚠ 불확실성·주의\n이 문제는 완전히 해결됩니다.\n"),
        evidence_keys=EVIDENCE) if x["cause"] == "unsupported_claim"]
    check("likely 결함은 non-blocking", DD.blocking(likely) == [], str(likely))


def test_empty_evidence_does_not_flag_every_key() -> None:
    """근거 목록을 모르면 인용 정합을 판정하지 않는다 — 모르는 것을 결함이라 하면 안 된다."""
    f = DD.detect(CLEAN, evidence_keys=[])
    check("근거 목록 미제공 시 bad_citation 없음",
          not any(x["cause"] == "bad_citation" for x in f), str(f))


# 제안 기반 초안 — 서버 `_rca_comment_body` 가 실제로 만드는 형식 그대로.
# 불확실성 섹션도 번호 절차도 **약속한 적이 없다**.
PROPOSAL = """🤖 **자동 근본원인 분석** (RCA-bot · 신뢰도 높음)

### 예상 근본원인
device 가 거의 가득 찬 상태에서 urgent GC 가 host write 와 같은 우선순위로 스케줄됩니다.

### 권장 해결책
urgent GC 를 host idle window 로 분산하고 SLC cache 크기를 동적 조정합니다.

### 임시 우회책
저장 공간을 20% 이상 확보하면 빈도가 급감합니다.

참고 사례: LSI-10, LSI-24, LSI-31

_과거 해결 이슈 기반 자동 분석 (사람 승인 후 게시)._
"""


def test_kind_contract() -> None:
    """초안 종류마다 약속이 다르다 — 없는 약속을 어겼다고 하면 오탐 홍수가 난다.

    실측 회귀: LLM 분석 기준(불확실성 섹션·번호 절차)을 제안 기반 초안에 들이대니
    운영 KB 의 멀쩡한 초안이 전부 결함으로 잡혔다.
    """
    f = DD.detect(PROPOSAL, evidence_keys=EVIDENCE, kind="proposal")
    check("제안 초안 = 결함 0", f == [], f"오탐: {[(x['cause'], x['detail']) for x in f]}")

    strict = DD.detect(PROPOSAL, evidence_keys=EVIDENCE, kind="analysis")
    check("같은 본문도 analysis 기준에선 걸린다", strict != [], str(strict))

    unknown = DD.detect(PROPOSAL, evidence_keys=EVIDENCE, kind="없는종류")
    check("모르는 종류는 엄격한 기본값으로", unknown == strict, str(unknown))


def test_jira_wiki_headings() -> None:
    """Jira 위키 형식(h3.) 본문도 읽는다 — 못 읽는 것과 없는 것은 다르다.

    실측 회귀: 큐에 남아 있던 Jira 형식 초안이 섹션을 하나도 못 읽혀
    '필수 섹션 없음' 으로 잡혔다. 파싱 실패를 결함이라 부르면 안 된다.
    """
    jira = (PROPOSAL.replace("### 예상 근본원인", "h3. 예상 근본원인")
                    .replace("### 권장 해결책", "h3. 권장 해결책")
                    .replace("### 임시 우회책", "h3. 임시 우회책"))
    f = DD.detect(jira, evidence_keys=EVIDENCE, kind="proposal")
    check("Jira 형식 제목도 섹션으로 인식", f == [],
          f"오탐: {[(x['cause'], x['detail']) for x in f]}")


def main() -> int:
    for fn in (test_jira_wiki_headings, test_kind_contract, test_clean_draft_has_no_findings, test_bad_citation_is_certain,
               test_hanja_and_missing_section, test_missing_action,
               test_unsupported_claim, test_no_citation_at_all_is_too_shallow,
               test_causes_are_taxonomy_codes, test_gate_blocks_only_certain,
               test_empty_evidence_does_not_flag_every_key):
        fn()
    print(f"\n{'✅ 통과' if not FAILS else '❌ 실패: ' + ', '.join(FAILS)}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
