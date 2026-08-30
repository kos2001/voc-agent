"""가이던스 **효과 검증** — 프롬프트에 주입한 규칙이 실제로 결함을 줄였는가.

왜 필요한가:
  `prompt_guidance()` 는 같은 클래스에서 2회 이상 지적된 원인의 규칙을 생성
  프롬프트에 **자동 주입**한다. 그런데 그 개입이 효과가 있는지는 아무도 재지
  않았다. loop 가 측정 → 진단 → 개입까지만 있고 **검증**이 없으면, 효과 없는
  규칙이 영원히 프롬프트에 남아 컨텍스트만 먹고, 더 나쁘게는 "조치했다"는
  착각 때문에 진짜 원인(검색·지식 레버)을 손대지 않게 된다.

여기서 지키는 계약:
  · 단순 전후 비교를 쓰지 않는다. 활성 시점 자체가 "그 원인이 많이 나온 시점"으로
    정의되므로, 아무 효과가 없어도 이후 발생률은 평균으로 되돌아가며 떨어진다
    (평균회귀). 그래서 **같은 기간 같은 클래스의 다른 원인**을 대조군으로 두고
    이중차분(difference-in-differences)으로 본다.
  · 표본이 부족하면 판정하지 않는다 — "효과 있음" 을 3건으로 선언하면 loop 가
    노이즈를 근거로 개입을 유지한다.
  · 효과 없음·악화는 **다른 레버로 올리라는 제안**이 되어야 한다. 판정만 하고
    아무 일도 안 일어나면 검증을 붙인 의미가 없다.

실행:
    .venv/bin/python tests/test_guidance_effect.py
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
    D.STORE_FILE = Path(tempfile.mkdtemp()) / "draft_feedback.json"


def feed(template: str, seq: list) -> None:
    """seq: 원인 리스트(빈 리스트 = 무수정 승인)를 시간 순서대로 기록."""
    for i, causes in enumerate(seq):
        D.record(key=f"K-{template[:3]}-{i}", template=template,
                 outcome="accepted_clean" if not causes else "edited",
                 causes=causes, origin="human")


def test_needs_enough_data() -> None:
    """활성 이후 표본이 적으면 판정하지 않는다."""
    fresh()
    feed("T-소량", [["missing_action"], ["missing_action"], ["missing_action"]])
    rows = D.guidance_effect()
    row = [r for r in rows if r["template"] == "T-소량"]
    check("표본 부족 시 판정 보류",
          bool(row) and row[0]["verdict"] == "표본 부족", str(row))


def test_effective_guidance() -> None:
    """활성 이후 해당 원인만 사라지고 다른 원인은 그대로 → '효과 있음'."""
    fresh()
    feed("T-효과", [["missing_action"], ["missing_action"],      # 여기서 활성
                   ["tone_format"], ["tone_format"], ["tone_format"],
                   ["tone_format"], []])
    row = [r for r in D.guidance_effect() if r["cause"] == "missing_action"][0]
    check("효과 있음 판정", row["verdict"] == "효과 있음", str(row))
    check("이중차분이 음수", row["adjusted_delta"] < 0, str(row))


def test_regression_to_mean_is_not_called_effective() -> None:
    """모든 원인이 함께 줄면(=표본이 좋아진 것) 가이던스 공로로 돌리지 않는다."""
    fresh()
    # 활성 전: 두 원인이 함께 빈발 / 활성 후: 두 원인이 함께 사라짐(무수정 승인)
    feed("T-평균회귀", [["missing_action", "tone_format"], ["missing_action", "tone_format"],
                     [], [], [], [], []])
    row = [r for r in D.guidance_effect() if r["cause"] == "missing_action"][0]
    check("전후 단순 델타는 개선으로 보인다", row["delta"] < 0, str(row))
    check("대조군 보정 후에는 효과 없음", row["verdict"] != "효과 있음", str(row))


def test_worsening_guidance() -> None:
    """활성 이후에도 그 원인만 계속 나오면 '악화 또는 무효'."""
    fresh()
    feed("T-무효", [["wrong_root_cause"], ["wrong_root_cause"],
                   ["wrong_root_cause"], ["wrong_root_cause"],
                   ["wrong_root_cause"], ["wrong_root_cause"]])
    row = [r for r in D.guidance_effect() if r["cause"] == "wrong_root_cause"][0]
    check("효과 없음 판정", row["verdict"] in ("효과 없음", "악화"), str(row))


def test_ineffective_guidance_becomes_a_suggestion() -> None:
    """효과 없는 가이던스는 **다른 레버로 올리라는 제안**이 되어야 한다."""
    fresh()
    feed("T-무효2", [["wrong_root_cause"]] * 8)
    sug = D.suggestions()
    esc = [s for s in sug if s["type"] == "escalate_lever"]
    check("레버 상향 제안 생성", bool(esc), str([s["type"] for s in sug]))
    check("제안이 근거를 말한다",
          bool(esc) and "wrong_root_cause" in str(esc[0]["evidence"]), str(esc[:1]))


def test_no_events_is_safe() -> None:
    """데이터가 없어도 죽지 않는다 — loop 가 매일 도는 경로다."""
    fresh()
    check("빈 저장소에서 안전", D.guidance_effect() == [] and isinstance(D.suggestions(), list))


def main() -> int:
    for fn in (test_needs_enough_data, test_effective_guidance,
               test_regression_to_mean_is_not_called_effective,
               test_worsening_guidance, test_ineffective_guidance_becomes_a_suggestion,
               test_no_events_is_safe):
        fn()
    print(f"\n{'✅ 통과' if not FAILS else '❌ 실패: ' + ', '.join(FAILS)}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
