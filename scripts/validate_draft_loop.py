"""초안 거부/수정 원인 분류 loop 엔드투엔드 검증 — VOC 목 데이터로 돈다.

무엇을 재는가:
  1) **검색**: 고객 말투 VOC 질의가 같은 고장의 해결 사례를 찾아내는가 (P@1, coverage).
  2) **자동 분류 복원율**: 초안에 *알려진 결함*을 주입하고, 사람이 할 법한 수정을
     적용한 뒤, `draft_feedback.classify_diff` 가 **주입한 원인을 되찾는가**.
     이게 이 검증의 핵심이다 — 사람이 라벨을 안 달아도 신호가 남는다는 주장의 근거.
  3) **환류**: 축적된 원인이 프롬프트 가이던스와 개선 큐 제안으로 실제로 바뀌는가.

왜 결함을 주입하나:
  실제 거부/수정 데이터는 아직 없다(기능이 방금 생겼다). 그런데 자동 분류가 맞는지
  모르면 축적된 통계 전체를 믿을 수 없다. 결함을 **알고** 주입하면 정답이 생기고,
  복원율을 숫자로 말할 수 있다. 주입 없이 "돌아간다"만 확인하는 검증은 무의미하다.

기본은 임시 저장소에서 돌아 실제 data/draft_feedback.json 을 건드리지 않는다.
--write 를 주면 실제 저장소·개선 큐에 적재한다(데모/시연용).

실행:
    .venv/bin/python scripts/build_voc_mock.py
    .venv/bin/python scripts/validate_draft_loop.py
    .venv/bin/python scripts/validate_draft_loop.py --write      # 실제 저장소에 적재
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import draft_feedback as D      # noqa: E402
import preprocess               # noqa: E402
from recommender import Recommender, template_key  # noqa: E402

MOCK = ROOT / "data" / "voc_mock_issues.json"
TPL_COUNT = 8   # build_voc_mock.TEMPLATES 길이 — 정답 템플릿 판정용


def _tpl_of(key: str) -> int:
    """VOC-n 은 라운드로빈으로 템플릿에 배정된다 → 정답 클래스를 키에서 복원."""
    return (int(key.split("-")[1]) - 1) % TPL_COUNT


# --------------------------------------------------------------------------- #
# 초안 생성 — 서버 `_rca_comment_body` 와 같은 형태(LLM 없이 결정론적, 비용 0)
# --------------------------------------------------------------------------- #
def make_draft(query: dict, matches: list[dict]) -> str:
    top = matches[0]
    cited = ", ".join(m["key"] for m in matches[:3])
    return (
        f"🤖 **자동 근본원인 분석** (RCA-bot)\n\n"
        f"### 🎯 예상 근본원인\n{top.get('root_cause', '')} ({top['key']})\n\n"
        f"### ✅ 권장 해결 단계\n{top.get('resolution', '')}\n\n"
        f"### ↪ 임시 우회책\n{top.get('workaround') or '—'}\n\n"
        f"### ⚠ 불확실성·주의\n증상 표현이 사례와 달라 추가 확인이 필요할 수 있습니다.\n\n"
        f"참고 사례: {cited}\n")


# --------------------------------------------------------------------------- #
# 결함 주입 + 사람이 할 법한 수정 — (주입 원인, 초안 변형, 수정) 세 쌍이 정답이다
# --------------------------------------------------------------------------- #
def _inject_bad_citation(draft: str, q: dict, ms: list[dict]) -> tuple[str, str]:
    """근거 목록에 없는 사례 키를 인용 → 사람이 지운다."""
    bad = "VOC-999"
    broken = draft.replace("참고 사례: ", f"참고 사례: {bad}, ")
    return broken, draft


def _inject_missing_action(draft: str, q: dict, ms: list[dict]) -> tuple[str, str]:
    """해결 단계를 한 줄짜리 일반론으로 뭉갠다 → 사람이 번호 절차를 쓴다."""
    top = ms[0]
    vague = "펌웨어를 최신 버전으로 업데이트하시기 바랍니다."
    broken = draft.replace(top.get("resolution", ""), vague)
    fixed = draft.replace(
        top.get("resolution", ""),
        "1. 현재 펌웨어 버전과 호스트 설정값을 먼저 확인합니다.\n"
        f"2. {top.get('resolution', '')}\n"
        "3. 재현 시나리오로 24시간 반복 테스트를 수행해 재발 여부를 확인합니다.")
    return broken, fixed


def _inject_wrong_root_cause(draft: str, q: dict, ms: list[dict]) -> tuple[str, str]:
    """다른 고장의 근본원인을 붙인다 → 사람이 올바른 원인으로 다시 쓴다."""
    top = ms[0]
    other = next((m for m in ms[1:] if _tpl_of(m["key"]) != _tpl_of(top["key"])), None)
    wrong = (other or {}).get("root_cause") or "전원 시퀀스 이상으로 컨트롤러가 리셋되는 것이 원인입니다."
    broken = draft.replace(top.get("root_cause", ""), wrong)
    return broken, draft


def _inject_unsupported_claim(draft: str, q: dict, ms: list[dict]) -> tuple[str, str]:
    """근거 없는 단정을 추가 → 사람이 (추정) 표시를 붙여 완화한다."""
    claim = "이 문제는 다음 펌웨어 릴리스에서 완전히 해결됩니다."
    broken = draft.replace("### ⚠ 불확실성·주의\n", f"### ⚠ 불확실성·주의\n{claim}\n")
    fixed = draft.replace("### ⚠ 불확실성·주의\n", f"### ⚠ 불확실성·주의\n(추정) {claim}\n")
    return broken, fixed


def _inject_tone_format(draft: str, q: dict, ms: list[dict]) -> tuple[str, str]:
    """한자 혼용 → 사람이 한글로 고친다."""
    broken = draft.replace("예상 근본원인", "예상 根本原因")
    return broken, draft


INJECTORS = [
    ("bad_citation", _inject_bad_citation),
    ("missing_action", _inject_missing_action),
    ("wrong_root_cause", _inject_wrong_root_cause),
    ("unsupported_claim", _inject_unsupported_claim),
    ("tone_format", _inject_tone_format),
]
# 사람이 명시 라벨과 함께 거부하는 경우 — 자동 분류 대상이 아니다(본문 자체가 없거나
# 내용 문제가 아니라 '답할 근거가 없다'는 판정이라 diff 로는 알 수 없다).
REJECT_CAUSES = ["no_case_exists", "wrong_scope"]


def run(*, write: bool, method: str, backend: str, model: str) -> int:
    if not MOCK.exists():
        print(f"목 데이터가 없습니다: {MOCK.relative_to(ROOT)}\n"
              f"  .venv/bin/python scripts/build_voc_mock.py 를 먼저 실행하세요.")
        return 2
    if not write:
        # 실제 저장소를 오염시키지 않는다 — 검증은 부작용이 없어야 반복 가능하다.
        D.STORE_FILE = Path(tempfile.mkdtemp()) / "draft_feedback.json"

    recs = [preprocess.parse_issue(r) for r in json.loads(MOCK.read_text(encoding="utf-8"))]
    kb = [r for r in recs if r["status"] == "완료"]
    queries = [r for r in recs if r["status"] != "완료"]
    print(f"[1/4] VOC 목 데이터 — KB(해결) {len(kb)}건 / 질의(미해결) {len(queries)}건")

    reco = Recommender(kb, method=method, embed_backend=backend, embed_model=model,
                       signals=True, rerank=False)

    # ---- ① 검색 ---------------------------------------------------------- #
    drafted, p1, cov = [], 0, 0
    for q in queries:
        out = reco.recommend(q, k=3)
        ms = out.get("matches") or []
        if ms and _tpl_of(ms[0]["key"]) == _tpl_of(q["key"]):
            p1 += 1
        if out.get("coverage"):
            cov += 1
        if ms and out.get("coverage"):
            drafted.append((q, ms, make_draft(q, ms)))
    n = len(queries)
    print(f"[2/4] 검색 — P@1 {p1}/{n} ({round(p1/n, 3)}) · coverage 통과 {cov}/{n} "
          f"· 초안 생성 {len(drafted)}건")
    if not drafted:
        print("  초안이 하나도 생성되지 않았습니다 — 검색/게이트를 먼저 점검하세요.")
        return 1

    # ---- ② 결함 주입 → 자동 분류 복원율 ---------------------------------- #
    # 결정론적 배정: i 번째 초안에 i % len(INJECTORS) 결함을 넣는다. 매 실행 동일.
    recovered: Counter = Counter()
    injected_n: Counter = Counter()
    extra: Counter = Counter()
    misses: list[str] = []
    for i, (q, ms, draft) in enumerate(drafted):
        tpl = template_key(q.get("summary", ""))
        meta = dict(category=q.get("category", ""), template=tpl, chip=q.get("chip", ""),
                    source="proposal", citations=[m["key"] for m in ms])
        if i % 4 == 3:
            # 4건 중 1건은 사람이 거부 — 사람 라벨이 정본(origin=human)
            cause = REJECT_CAUSES[i % len(REJECT_CAUSES)]
            D.record(key=q["key"], outcome="rejected", causes=[cause], origin="human",
                     note="검증 시뮬레이션", **meta)
            continue
        gt, injector = INJECTORS[i % len(INJECTORS)]
        broken, fixed = injector(draft, q, ms)
        if broken == fixed:
            # 주입이 실패했는데(치환 대상 문자열 없음) 통과시키면 복원율이 부풀려진다.
            misses.append(f"{q['key']} 주입실패({gt})")
            continue
        injected_n[gt] += 1
        got = D.classify_diff(broken, fixed)
        if gt in got:
            recovered[gt] += 1
        else:
            misses.append(f"{q['key']} {gt} → {got}")
        for c in got:
            if c != gt:
                extra[c] += 1
        # 사람 라벨 없이 기록 — 자동 추정 경로를 그대로 탄다
        D.record_edit(key=q["key"], original=broken, final=fixed, **meta)

    tot_inj = sum(injected_n.values())
    tot_rec = sum(recovered.values())
    print(f"[3/4] 자동 분류 복원율 — {tot_rec}/{tot_inj} "
          f"({round(tot_rec / tot_inj, 3) if tot_inj else '—'})")
    for cause, k in injected_n.most_common():
        print(f"    {cause:20s} {recovered[cause]}/{k}")
    if extra:
        print(f"    추가로 붙은 원인(동반 추정): {dict(extra)}")
    if misses:
        print(f"    복원 실패: {misses}")

    # ---- ③ 환류: 통계 → 가이던스 → 제안 ---------------------------------- #
    st = D.stats()
    print(f"[4/4] 축적·환류")
    print(f"    판정 {st['judged']}건 — 거부 {st['rejected']} / 수정 {st['edited']} / "
          f"무수정 {st['accepted_clean']}")
    print(f"    무수정 승인율(clean_rate) {st['clean_rate']} · 게시율(accept_rate) {st['accept_rate']}")
    print(f"    레버 분포 {st['by_lever']}")
    print(f"    상위 원인 {[(c['label'], c['count']) for c in st['by_cause'][:5]]}")

    classes = D.by_class()
    print(f"    클래스별 집계 {len(classes)}종 — 최악 "
          f"{[(c['template'][:22], c['failure_rate']) for c in classes[:3]]}")

    guided = 0
    for c in classes:
        g = D.prompt_guidance(template=c["template"])
        if g:
            guided += 1
            if guided == 1:
                print(f"    ▸ 프롬프트 가이던스 예시 ({c['template'][:30]}):")
                for line in g.strip().splitlines()[1:4]:
                    print(f"        {line}")
    print(f"    프롬프트 가이던스가 생성된 클래스: {guided}/{len(classes)}")

    sug = D.suggestions()
    print(f"    개선 제안 {len(sug)}건 — 유형 {dict(Counter(s['type'] for s in sug))}")
    for s in sug[:3]:
        print(f"      [{s['priority']}] {s['type']}: {s['rationale'][:88]}…")

    if write:
        import improve_queue
        # prune=False — 이건 draft_feedback 만 생성한 부분 목록이다. 전량 생성이 아닌데
        # 회수까지 하면 다른 신호원(군집·모순·공백)의 멀쩡한 제안이 쓸려나간다.
        synced = improve_queue.sync(sug, prune=False)
        print(f"    개선 큐 적재 — 신규 {synced['added']} / 큐 {synced['counts']}")
        print(f"    저장 위치: {D.STORE_FILE.relative_to(ROOT)}")
    else:
        print(f"    (임시 저장소에서 실행 — 실제 데이터 변경 없음. --write 로 적재)")

    ok = tot_inj > 0 and tot_rec == tot_inj and guided > 0 and len(sug) > 0
    print(f"\n{'✅ 검증 통과' if ok else '⚠ 검증 미달'} — "
          f"복원 {tot_rec}/{tot_inj} · 가이던스 {guided} · 제안 {len(sug)}")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="초안 개선 loop 엔드투엔드 검증(VOC 목 데이터)")
    ap.add_argument("--write", action="store_true", help="실제 저장소·개선 큐에 적재")
    ap.add_argument("--method", default="hybrid_embed")
    # 기본은 로컬 임베딩 — 검증이 API 키·비용·네트워크에 의존하면 아무도 안 돌린다.
    ap.add_argument("--embed-backend", default="fastembed")
    ap.add_argument("--embed-model", default="", help="빈 값 = 백엔드 기본 모델")
    a = ap.parse_args()
    return run(write=a.write, method=a.method, backend=a.embed_backend, model=a.embed_model)


if __name__ == "__main__":
    raise SystemExit(main())
