"""초안 거부/수정 원인 분류 검증 — 신호가 실제로 축적되고 액션으로 바뀌는가.

여기서 지키는 계약:
  · 사람 라벨(human)과 자동 추정(auto)은 절대 섞이지 않는다 — 근거의 신뢰도가 달라서다
  · 사람이 라벨을 안 달아도 (원본→최종) diff 에서 원인이 추정된다
  · 무수정 승인도 기록된다 — 분모가 없으면 개선율(clean_rate)을 계산할 수 없다
  · 알 수 없는 원인 코드는 'other' 로 접힌다 — 오타 코드가 새면 집계가 조용히 갈라진다
  · 축적된 원인이 ① 생성 프롬프트 규칙과 ② 개선 큐 제안으로 환류된다
  · 1회짜리 지적은 프롬프트 규칙이 되지 않는다 (노이즈가 규칙이 되면 안 된다)

실행:
    .venv/bin/python tests/test_draft_feedback.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import draft_feedback as D  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'✓' if cond else '✗'} {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def fresh() -> None:
    """실제 data/draft_feedback.json 을 건드리지 않도록 임시 파일로 격리."""
    D.STORE_FILE = Path(tempfile.mkdtemp()) / "draft_feedback.json"


DRAFT = """🤖 **RCA-bot**

### 🎯 예상 근본원인
UFS bkops 가 critical 레벨로 올라가며 write latency 가 튀는 것으로 보입니다 (LSI-10).
펌웨어 스케줄러가 GC 를 지연시킨 것이 原因입니다.

### ✅ 권장 해결 단계
펌웨어를 올리세요.

### ↪ 임시 우회책
—
"""

FINAL_ROOT_CAUSE_REWRITTEN = """🤖 **RCA-bot**

### 🎯 예상 근본원인
호스트가 bkops 를 허용하지 않아 device 내부 GC 가 지연되고, 임계 이후 강제 GC 가
전면화되며 latency spike 가 발생합니다 (LSI-10).

### ✅ 권장 해결 단계
펌웨어를 올리세요.

### ↪ 임시 우회책
—
"""

FINAL_STEPS_ADDED = DRAFT.replace(
    "펌웨어를 올리세요.",
    "1. bkops_en 설정값을 확인합니다.\n2. UF40.3.4.150 이상으로 펌웨어를 올립니다.\n"
    "3. 버스트 시나리오로 24시간 재현 테스트를 돌립니다.")

FINAL_HANJA_FIXED = DRAFT.replace("原因", "원인")


def test_taxonomy() -> None:
    fresh()
    codes = {t["code"] for t in D.taxonomy()}
    check("분류 체계가 닫힌 목록으로 노출됨", codes == set(D.CAUSES))
    check("모든 원인에 레버가 매핑됨",
          all(t["lever"] in D.LEVERS for t in D.taxonomy()))
    check("알 수 없는 코드는 other 로 접힘",
          D.normalize_causes(["wrong_root_cause", "typo_code"]) == ["wrong_root_cause", "other"])
    check("중복 코드는 한 번만", D.normalize_causes(["tone_format", "tone_format"]) == ["tone_format"])


def test_auto_classify() -> None:
    fresh()
    check("근본원인 재작성 → wrong_root_cause",
          "wrong_root_cause" in D.classify_diff(DRAFT, FINAL_ROOT_CAUSE_REWRITTEN))
    check("번호 단계 추가 → missing_action",
          "missing_action" in D.classify_diff(DRAFT, FINAL_STEPS_ADDED))
    check("한자 제거 → tone_format",
          "tone_format" in D.classify_diff(DRAFT, FINAL_HANJA_FIXED))
    removed = D.classify_diff(DRAFT, DRAFT.replace(" (LSI-10)", ""))
    check("인용 삭제 → bad_citation", "bad_citation" in removed, str(removed))
    added = D.classify_diff(DRAFT, DRAFT.replace("(LSI-10)", "(LSI-10, LSI-77)"))
    check("인용 추가 → missing_evidence", "missing_evidence" in added, str(added))
    check("변화 없으면 원인 없음", D.classify_diff(DRAFT, DRAFT) == [])


def test_heading_change_is_not_a_content_defect() -> None:
    """제목만 고친 수정이 '근본원인이 틀림' 으로 오진되면 안 된다.

    실제로 났던 문제: 제목의 한자를 고치면(예상 근본원인 → 예상 根本原因) 섹션 추출이
    한쪽에서만 실패해 similarity 0.0 이 나왔고, 형식 손질이 P1 retrieval 신호로
    분류됐다. 그대로 두면 loop 가 오타 하나 때문에 검색 파라미터를 튜닝하러 간다.
    """
    fresh()
    broken = DRAFT.replace("예상 근본원인", "예상 根本原因")
    got = D.classify_diff(broken, DRAFT)
    check("제목 수정은 형식 문제로 분류", "tone_format" in got, str(got))
    check("제목 수정이 근본원인 오류로 오진되지 않음", "wrong_root_cause" not in got, str(got))
    # 반대로, 제목은 그대로인데 본문이 다시 쓰이면 여전히 잡아야 한다(과교정 방지).
    check("본문 재작성은 여전히 wrong_root_cause",
          "wrong_root_cause" in D.classify_diff(DRAFT, FINAL_ROOT_CAUSE_REWRITTEN))


def test_record_edit_origin() -> None:
    fresh()
    ev = D.record_edit(key="LSI-1", original=DRAFT, final=FINAL_ROOT_CAUSE_REWRITTEN,
                       template="ufs-latency")
    check("사람 라벨 없으면 origin=auto", ev["origin"] == "auto")
    check("auto 라벨도 원인을 남긴다", bool(ev["causes"]))
    ev2 = D.record_edit(key="LSI-2", original=DRAFT, final=FINAL_STEPS_ADDED,
                        causes=["too_shallow"], template="ufs-latency")
    check("사람 라벨이 있으면 origin=human", ev2["origin"] == "human")
    check("사람 라벨이 정본 — 자동 추정으로 덮이지 않음", ev2["causes"] == ["too_shallow"])
    ev3 = D.record_edit(key="LSI-3", original=DRAFT, final=DRAFT, template="ufs-latency")
    check("무수정 승인은 accepted_clean 으로 기록(분모 확보)",
          ev3["outcome"] == "accepted_clean" and ev3["causes"] == [])
    check("레버가 함께 저장됨", ev["levers"] == sorted({D.lever_of(c) for c in ev["causes"]}))


def test_stats_and_rates() -> None:
    fresh()
    D.record_edit(key="A", original=DRAFT, final=DRAFT, template="t1")            # clean
    D.record_edit(key="B", original=DRAFT, final=FINAL_STEPS_ADDED, template="t1")  # edited
    D.record(key="C", outcome="rejected", causes=["no_case_exists"], template="t1")
    s = D.stats()
    check("judged = clean+edited+rejected", s["judged"] == 3, str(s))
    check("accept_rate = 게시된 비율", s["accept_rate"] == round(2 / 3, 3), str(s["accept_rate"]))
    check("clean_rate = 무수정 게시 비율", s["clean_rate"] == round(1 / 3, 3), str(s["clean_rate"]))
    check("원인 분포가 집계됨", any(c["cause"] == "no_case_exists" for c in s["by_cause"]))
    check("레버 분포가 집계됨", s["by_lever"].get("knowledge", 0) >= 1, str(s["by_lever"]))


def test_by_class() -> None:
    fresh()
    for i in range(3):
        D.record(key=f"X{i}", outcome="rejected", causes=["wrong_root_cause"], template="thermal")
    D.record(key="Y", outcome="accepted_clean", causes=[], template="nfc")
    cls = {c["template"]: c for c in D.by_class()}
    check("클래스별 실패율 집계", cls["thermal"]["failure_rate"] == 1.0, str(cls["thermal"]))
    check("클래스별 최다 원인 노출",
          cls["thermal"]["top_causes"][0]["cause"] == "wrong_root_cause")
    check("정상 클래스는 실패율 0", cls["nfc"]["failure_rate"] == 0.0)
    check("실패 많은 클래스가 먼저", D.by_class()[0]["template"] == "thermal")


def test_prompt_guidance() -> None:
    fresh()
    D.record(key="A", outcome="edited", causes=["missing_action"], template="thermal")
    g1 = D.prompt_guidance(template="thermal")
    check("1회 지적은 규칙이 되지 않음(노이즈 차단)", g1 == "", repr(g1[:60]))
    D.record(key="B", outcome="edited", causes=["missing_action"], template="thermal")
    g2 = D.prompt_guidance(template="thermal")
    check("2회 반복되면 규칙으로 주입됨", "권장 해결 단계" in g2, repr(g2[:120]))
    check("지적 횟수가 규칙에 드러남", "2회 지적" in g2)
    check("다른 클래스에는 새지 않음(전역 폴백 없음)",
          D.prompt_guidance(template="nfc") == "")
    D.record(key="C", outcome="edited", causes=["tone_format"], category="Firmware")
    D.record(key="D", outcome="edited", causes=["tone_format"], category="Firmware")
    check("템플릿 매칭 없으면 분류로 폴백",
          "한자" in D.prompt_guidance(category="Firmware", template="unknown-template"))


def test_suggestions() -> None:
    fresh()
    for i in range(3):
        D.record(key=f"R{i}", outcome="rejected", causes=["wrong_root_cause"], template="thermal")
    sg = {s["type"]: s for s in D.suggestions()}
    check("retrieval 레버 → 파라미터 튜닝 제안", "tune_retrieval" in sg, str(list(sg)))
    check("제안이 improve_queue 스키마를 따름",
          all(k in sg["tune_retrieval"] for k in ("type", "priority", "target", "rationale",
                                                  "evidence", "action_hint")))
    fresh()
    for i in range(3):
        D.record(key=f"K{i}", outcome="rejected", causes=["no_case_exists"], template="nfc-wlc")
    check("knowledge 레버 → RCA 작성 제안",
          any(s["type"] == "author_rca" for s in D.suggestions()))
    fresh()
    for i in range(3):
        D.record(key=f"G{i}", outcome="edited", causes=["unsupported_claim"], template="isp")
    check("generation 레버 → 평가셋 보강 제안",
          any(s["type"] == "harden_generation" for s in D.suggestions()))
    fresh()
    D.record(key="Z", outcome="edited", causes=["wrong_root_cause"], template="thermal")
    check("표본이 적으면 제안하지 않음(성급한 액션 차단)", D.suggestions() == [])


def test_scattered_causes_do_not_pick_a_lever() -> None:
    """실패는 많은데 원인이 흩어져 있으면 레버를 지목하지 않는다.

    n=1 짜리 최다 원인으로 "검색 파라미터를 튜닝하세요" 를 만들면, 근거 한 건으로
    시스템을 건드리라는 지시가 된다. 원인이 모일 때까지 기다리는 편이 낫다.
    """
    fresh()
    for i, c in enumerate(["bad_citation", "missing_action", "tone_format"]):
        D.record(key=f"S{i}", outcome="edited", causes=[c], template="scattered")
    types = {s["type"] for s in D.suggestions()}
    check("흩어진 원인은 레버 액션이 아니라 라벨링 요청으로",
          types == {"triage_scattered_failures"}, str(types))
    D.record(key="S9", outcome="edited", causes=["bad_citation"], template="scattered")
    types2 = {s["type"] for s in D.suggestions()}
    check("원인이 2회로 모이면 레버 액션이 나온다",
          "harden_generation" in types2, str(types2))


def test_repeat_bad_citation() -> None:
    fresh()
    body = DRAFT
    stripped = DRAFT.replace(" (LSI-10)", "")
    for i in range(3):
        D.record_edit(key=f"E{i}", original=body, final=stripped, template=f"t{i}")
    sug = [s for s in D.suggestions() if s["type"] == "review_unhelpful"]
    check("반복 오인용된 사례가 폐기 검토 대상으로 올라옴",
          any(s["target"] == "LSI-10" for s in sug), str(sug))


def main() -> int:
    for fn in (test_taxonomy, test_auto_classify,
               test_heading_change_is_not_a_content_defect, test_record_edit_origin,
               test_stats_and_rates, test_by_class, test_prompt_guidance,
               test_suggestions, test_scattered_causes_do_not_pick_a_lever,
               test_repeat_bad_citation):
        print(f"\n— {fn.__name__}")
        fn()
    print()
    if FAILS:
        print(f"실패 {len(FAILS)}건: {FAILS}")
        return 1
    print("모두 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
