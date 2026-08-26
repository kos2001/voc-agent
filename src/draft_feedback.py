"""초안 거부/수정 원인 분류 저장소 — VOC 답변 품질의 **결함 원인**을 축적한다.

배경(왜 만들었나):
  기존에 초안(RCA 댓글)에 대한 사람의 판단은 두 곳에서 **버려지고** 있었다.
    · `/rca/reject` — 상태만 rejected 로 바꾸고 끝. 왜 거부했는지 어디에도 안 남았다.
    · `/rca/approve` — `edited: bool` 과 (원본, 최종) 본문만 남았다. "고쳐졌다"는
      알지만 **무엇이 왜 틀렸는지**는 알 수 없어, 다음 초안이 같은 실수를 반복했다.
  그 결과 자기개선 loop(`self_improve`)의 신호원 4개(군집·모순·공백·비유용) 중
  정작 **최종 산출물의 실패**를 보는 신호가 하나도 없었다.

이 모듈의 계약:
  1) 원인은 **닫힌 분류 체계**(CAUSES)로만 기록한다. 자유 서술은 note 로 따로 둔다 —
     자유 서술만 쌓이면 집계가 안 되고, 집계가 안 되면 loop 가 돌지 않는다.
  2) 모든 원인은 **레버**(lever)에 매핑된다: retrieval | generation | knowledge |
     presentation. 이게 이 모듈의 핵심이다. "무엇이 틀렸나"만 세면 대시보드에서
     끝나지만, "어느 손잡이를 돌려야 하나"까지 알면 loop 가 액션을 만들 수 있다.
  3) 사람이 라벨을 안 달아도 신호를 잃지 않는다 — `classify_diff()` 가 (원본→최종)
     차이에서 원인을 **추정**한다. 추정은 `origin: "auto"` 로 표시해 사람 라벨
     (`origin: "human"`)과 절대 섞지 않는다. 섞으면 근거의 신뢰도를 알 수 없다.

저장: git 추적 data/draft_feedback.json (다른 지식 스토어와 동일 정책 — 버전·공유).
"""
from __future__ import annotations

import os
import re
import threading
from collections import Counter, defaultdict
from pathlib import Path

from json_store import read_json, write_json_atomic, now_iso

ROOT = Path(__file__).resolve().parent.parent
STORE_FILE = ROOT / "data" / "draft_feedback.json"
MAX_EVENTS = int(os.getenv("RVP_DRAFT_FB_MAX_EVENTS", "5000"))

# --------------------------------------------------------------------------- #
# 원인 분류 체계 (닫힌 목록)
# --------------------------------------------------------------------------- #
# lever = 이 원인을 고칠 수 있는 유일한 손잡이. loop 는 lever 별로 액션을 만든다.
#   retrieval    — 근거 검색이 틀림 → 게이트/랭킹 파라미터(L2), 평가셋
#   generation   — 근거는 맞는데 글이 틀림 → 프롬프트 규칙(guidance), few-shot
#   knowledge    — KB 에 답이 없거나 썩음 → RCA 작성·폐기·승격(L3, 사람)
#   presentation — 내용은 맞는데 형식 → 포매터/검증기
LEVERS = ("retrieval", "generation", "knowledge", "presentation", "other")

CAUSES: dict[str, dict] = {
    "wrong_root_cause": {
        "label": "근본원인이 틀림",
        "lever": "retrieval",
        "hint": "제시한 근본원인이 실제와 다름 — 근거 사례 선택이 잘못됨",
        # guidance: 이 원인이 반복될 때 생성 프롬프트에 주입할 규칙. lever 가
        # retrieval 이어도 생성 쪽에서 완화할 수 있는 것만 적는다(단정 금지 등).
        "guidance": "근본원인을 단정하지 말고, 근거 사례가 뒷받침하는 만큼만 주장하세요. "
                    "경쟁 가설이 있으면 '⚠ 불확실성' 에 반드시 함께 적습니다.",
    },
    "missing_evidence": {
        "label": "근거 사례 누락",
        "lever": "retrieval",
        "hint": "사람이 검토 중 더 적합한 과거 사례를 직접 추가함 — 검색이 놓친 것",
        "guidance": "",
    },
    "irrelevant_evidence": {
        "label": "근거 사례가 무관",
        "lever": "retrieval",
        "hint": "인용된 과거 사례가 이 이슈와 상관없음",
        "guidance": "인용 사례가 이 이슈와 어떤 점에서 같은지(칩·증상·조건) 한 줄로 "
                    "밝히세요. 밝힐 수 없으면 인용하지 마세요.",
    },
    "wrong_scope": {
        "label": "질문 요지를 빗나감",
        "lever": "retrieval",
        "hint": "고객이 물은 것과 다른 것에 답함(VOC 요지 불일치)",
        "guidance": "고객이 실제로 물은 질문을 첫 문단에서 한 문장으로 되짚고, "
                    "그 질문에 직접 답하는 것부터 쓰세요.",
    },
    "unsupported_claim": {
        "label": "근거 없는 주장(환각)",
        "lever": "generation",
        "hint": "제공된 사례에 없는 사실을 단정함",
        "guidance": "제공된 사례에서 확인되지 않는 문장은 반드시 `(배경)` 또는 `(추정)` "
                    "표시를 붙이세요. 표시 없는 문장은 인용으로 뒷받침돼야 합니다.",
    },
    "bad_citation": {
        "label": "인용 키 오류",
        "lever": "generation",
        "hint": "존재하지 않거나 근거 목록에 없는 사례 키를 인용",
        "guidance": "제공된 근거 목록에 있는 키만 인용하세요. 목록 밖 키는 본문 어디에도 "
                    "쓰지 않습니다.",
    },
    "too_shallow": {
        "label": "내용이 얕음·일반론",
        "lever": "generation",
        "hint": "일반적인 이야기만 하고 이 이슈 고유의 분석이 없음",
        "guidance": "일반론 대신 이 이슈의 구체 수치·로그·조건을 인용해 분석하세요. "
                    "어떤 이슈에도 붙일 수 있는 문장은 삭제합니다.",
    },
    "missing_action": {
        "label": "실행 가능한 조치 없음",
        "lever": "generation",
        "hint": "무엇을 해야 하는지 구체적 단계가 없음",
        "guidance": "'권장 해결 단계' 는 번호가 붙은 실행 가능한 절차로 쓰세요 — "
                    "누가·무엇을·어떤 순서로 확인/변경하는지가 드러나야 합니다.",
    },
    "no_case_exists": {
        "label": "해당 고장군 사례 자체가 없음",
        "lever": "knowledge",
        "hint": "KB 에 답이 없어 초안이 성립 불가 — 지식 공백",
        "guidance": "",
    },
    "outdated_knowledge": {
        "label": "폐기·구버전 지식 참조",
        "lever": "knowledge",
        "hint": "이미 대체되었거나 더 이상 유효하지 않은 사례를 인용",
        "guidance": "",
    },
    "duplicate_answer": {
        "label": "이미 답변된 내용 반복",
        "lever": "knowledge",
        "hint": "같은 이슈에 사실상 동일한 답이 이미 게시됨",
        "guidance": "",
    },
    "tone_format": {
        "label": "문체·형식·용어",
        "lever": "presentation",
        "hint": "내용은 맞으나 표현/구조/용어(한자 등)가 부적절",
        "guidance": "한국어 존댓말, 한자 없이 씁니다. 지정된 섹션 구조와 제목을 "
                    "그대로 지키세요.",
    },
    "other": {
        "label": "기타",
        "lever": "other",
        "hint": "위 분류에 해당하지 않음 — note 에 서술",
        "guidance": "",
    },
}

OUTCOMES = ("rejected", "edited", "accepted_clean")
ORIGINS = ("human", "auto")


def taxonomy() -> list[dict]:
    """UI/문서용 분류 체계 — 코드·라벨·레버·힌트. (닫힌 목록의 단일 소스)"""
    return [{"code": c, **{k: v for k, v in d.items() if k != "guidance"}}
            for c, d in CAUSES.items()]


def lever_of(cause: str) -> str:
    return CAUSES.get(cause, CAUSES["other"])["lever"]


def normalize_causes(causes) -> list[str]:
    """알 수 없는 코드는 'other' 로 접는다 — 저장소에 오타 코드가 새면 집계가 조용히 갈라진다."""
    if isinstance(causes, str):
        causes = [causes]
    out: list[str] = []
    for c in (causes or []):
        c = (str(c) or "").strip()
        if not c:
            continue
        code = c if c in CAUSES else "other"
        if code not in out:
            out.append(code)
    return out


# --------------------------------------------------------------------------- #
# 저장
# --------------------------------------------------------------------------- #
_LOCK = threading.Lock()


def _load() -> list:
    d = read_json(STORE_FILE, [])
    return d if isinstance(d, list) else []


def _save(events: list) -> None:
    write_json_atomic(STORE_FILE, events[-MAX_EVENTS:])


def record(*, key: str, outcome: str, causes=None, origin: str = "human",
           note: str = "", category: str = "", template: str = "", chip: str = "",
           source: str = "", citations: list | None = None,
           confidence: float | None = None, reviewer: str = "",
           diff: dict | None = None) -> dict:
    """초안 판정 1건 기록.

    같은 key 의 이전 이벤트를 지우지 않는다 — 초안은 여러 번 만들어질 수 있고,
    "두 번째 초안에서 같은 원인이 또 나왔나" 가 loop 에 가장 중요한 신호다.
    """
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome은 {OUTCOMES} 중 하나여야 함: {outcome!r}")
    if origin not in ORIGINS:
        raise ValueError(f"origin은 {ORIGINS} 중 하나여야 함: {origin!r}")
    ev = {
        "key": key or "", "outcome": outcome,
        "causes": normalize_causes(causes),
        "levers": sorted({lever_of(c) for c in normalize_causes(causes)}),
        "origin": origin, "note": (note or "").strip()[:1000],
        "category": category or "", "template": template or "", "chip": chip or "",
        "source": source or "", "citations": list(citations or []),
        "confidence": confidence, "reviewer": (reviewer or "").strip()[:80],
        "diff": diff or {}, "created_at": now_iso(),
    }
    with _LOCK:
        events = _load()
        events.append(ev)
        _save(events)
    return ev


def events(*, outcome: str = "", cause: str = "", template: str = "",
           limit: int = 0) -> list[dict]:
    out = [e for e in _load()
           if (not outcome or e.get("outcome") == outcome)
           and (not cause or cause in (e.get("causes") or []))
           and (not template or e.get("template") == template)]
    out.sort(key=lambda e: e.get("created_at", ""), reverse=True)
    return out[:limit] if limit else out


# --------------------------------------------------------------------------- #
# 자동 분류 — 사람이 라벨을 안 달아도 신호를 잃지 않는다
# --------------------------------------------------------------------------- #
# 섹션 제목은 이모지가 붙는다(### 🎯 예상 근본원인). 제목 텍스트로만 매칭한다.
_SECTIONS = {
    "root_cause": ("예상 근본원인", "근본 원인", "근본원인"),
    "resolution": ("권장 해결", "적용 해결", "해결책", "해결 단계"),
    "workaround": ("임시 우회책", "우회책"),
    "uncertainty": ("불확실성",),
}
_CITE_RE = re.compile(r"\b[A-Z]{2,}[A-Z0-9]*-\d+\b")
_HANJA_RE = re.compile(r"[一-鿿]")
# '(배경)'/'(추정)' 처럼 근거 구분 표시 — 사람이 이걸 추가했다면 단정을 완화한 것이다.
_HEDGE_RE = re.compile(r"\((?:배경|추정)\)")


def _section(md: str, titles: tuple[str, ...]) -> str:
    for t in titles:
        m = re.search(rf"#{{2,4}}[^\n]*{re.escape(t)}[^\n]*\n+(.+?)(?=\n#{{2,4}} |\Z)",
                      md or "", flags=re.S)
        if m:
            return m.group(1).strip()
    return ""


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def _similar(a: str, b: str) -> float:
    """토큰 자카드 — difflib 보다 재배열에 둔감해서 '문장 순서만 바꾼 수정'을 과대평가하지 않는다."""
    ta, tb = set(re.findall(r"\w+", (a or "").lower())), set(re.findall(r"\w+", (b or "").lower()))
    if not ta and not tb:
        return 1.0
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def diff_summary(original: str, final: str) -> dict:
    """(원본→최종) 구조적 차이 — 자동 분류의 입력이자 사람이 검증할 수 있는 근거."""
    o_c, f_c = set(_CITE_RE.findall(original or "")), set(_CITE_RE.findall(final or ""))
    sec = {}
    for name, titles in _SECTIONS.items():
        so, sf = _section(original, titles), _section(final, titles)
        # present: 양쪽에 그 섹션이 있었나. **한쪽에만 있으면 내용 비교가 무의미하다** —
        # 제목의 한자만 고쳐도(예상 근본원인 → 예상 根本原因) 한쪽 추출이 실패해
        # similarity 0.0 이 나오고, 그러면 형식 손질이 '근본원인이 틀림'(P1 retrieval)
        # 으로 오진된다. 실제로 검증 하네스에서 3건이 그렇게 잡혔다.
        sec[name] = {"changed": _norm(so) != _norm(sf),
                     "similarity": round(_similar(so, sf), 3),
                     "len_delta": len(sf) - len(so),
                     "present": [bool(so.strip()), bool(sf.strip())]}
    return {
        "len_delta": len(final or "") - len(original or ""),
        "citations_added": sorted(f_c - o_c),
        "citations_removed": sorted(o_c - f_c),
        "hanja_removed": bool(_HANJA_RE.search(original or "")) and not _HANJA_RE.search(final or ""),
        "hedges_added": max(0, len(_HEDGE_RE.findall(final or "")) - len(_HEDGE_RE.findall(original or ""))),
        "numbered_steps_added": max(0, len(re.findall(r"^\s*\d+[.)]\s", final or "", flags=re.M))
                                    - len(re.findall(r"^\s*\d+[.)]\s", original or "", flags=re.M))),
        "overall_similarity": round(_similar(original, final), 3),
        "sections": sec,
    }


# 자동 분류 임계값 — 근거 없는 라벨을 만들지 않기 위해 보수적으로 잡는다.
_SEC_CHANGED_SIM = 0.75   # 섹션이 이 이하로 닮았으면 '실질적으로 다시 씀'
_TRIVIAL_LEN = 40         # 이보다 작은 변화 + 섹션 무변경이면 형식 손질로 본다


def classify_diff(original: str, final: str) -> list[str]:
    """(원본→최종) 차이에서 원인을 **추정**한다. 확정이 아니다 — origin='auto' 로 기록.

    추정 규칙은 전부 "사람이 실제로 한 편집 행위"에 대응한다:
      · 인용 삭제        → 그 인용이 틀렸다(bad_citation)
      · 인용 추가        → 검색이 놓친 근거를 사람이 채웠다(missing_evidence)
      · 근본원인 섹션 재작성 → 근본원인이 틀렸다(wrong_root_cause)
      · 번호 단계 추가   → 실행 가능한 조치가 없었다(missing_action)
      · (배경)/(추정) 추가 → 단정이 과했다(unsupported_claim)
      · 한자 제거·소폭 손질 → 형식 문제(tone_format)
    겹치면 여러 개가 나온다 — 실제 수정은 보통 한 가지 이유가 아니다.
    """
    if _norm(original) == _norm(final):
        return []
    d = diff_summary(original, final)
    out: list[str] = []
    if d["citations_removed"]:
        out.append("bad_citation")
    if d["citations_added"]:
        out.append("missing_evidence")
    # 섹션이 한쪽에만 있으면 = 제목/구조가 바뀐 것. 내용 원인을 추정하지 않는다.
    def _both(sec: dict) -> bool:
        return all(sec.get("present", [True, True]))
    structural = any(not _both(v) for v in d["sections"].values())
    rc = d["sections"]["root_cause"]
    if _both(rc) and rc["changed"] and rc["similarity"] < _SEC_CHANGED_SIM:
        out.append("wrong_root_cause")
    res = d["sections"]["resolution"]
    if d["numbered_steps_added"] >= 2 or (_both(res) and res["changed"] and res["len_delta"] > 120):
        out.append("missing_action")
    if d["hedges_added"] > 0:
        out.append("unsupported_claim")
    if d["hanja_removed"] or structural:
        out.append("tone_format")
    # 본문이 크게 늘었는데 특정 섹션 문제로 설명되지 않으면 '얕았다'로 본다.
    if not out and d["len_delta"] > 300:
        out.append("too_shallow")
    # 어떤 규칙에도 안 걸리는 작은 변화 = 표현 손질.
    if not out and abs(d["len_delta"]) <= _TRIVIAL_LEN:
        out.append("tone_format")
    return out or ["other"]


def record_edit(*, key: str, original: str, final: str, causes=None, **meta) -> dict | None:
    """승인 경로 훅 — 수정이 있으면 원인과 함께 기록, 무수정이면 accepted_clean.

    사람이 라벨(causes)을 주면 그것이 정본(origin='human')이고, 안 주면 diff 에서
    추정한다(origin='auto'). 무수정 승인도 기록한다 — **분모가 없으면 개선율을
    계산할 수 없다.** (원인 분포만 쌓으면 "나빠지는 중"과 "표본이 늘어나는 중"을
    구분할 수 없다.)
    """
    edited = _norm(original) != _norm(final)
    if not edited:
        return record(key=key, outcome="accepted_clean", causes=[], origin="human", **meta)
    human = normalize_causes(causes)
    d = diff_summary(original, final)
    return record(key=key, outcome="edited",
                  causes=human or classify_diff(original, final),
                  origin="human" if human else "auto", diff=d, **meta)


# --------------------------------------------------------------------------- #
# 집계 — loop 의 측정 입력
# --------------------------------------------------------------------------- #
def _rel_store() -> str:
    """저장소 경로(가능하면 ROOT 상대). 테스트가 임시 경로로 갈아끼우므로 절대 죽으면 안 된다."""
    try:
        return str(STORE_FILE.relative_to(ROOT))
    except ValueError:
        return str(STORE_FILE)


def stats() -> dict:
    """헤드라인 KPI + 원인/레버 분포. VOC 답변 성능의 1차 지표는 accept/clean rate 다."""
    evs = _load()
    oc = Counter(e.get("outcome") for e in evs)
    rejected, edited, clean = oc.get("rejected", 0), oc.get("edited", 0), oc.get("accepted_clean", 0)
    judged = rejected + edited + clean
    approved = edited + clean
    cause_c: Counter = Counter()
    lever_c: Counter = Counter()
    for e in evs:
        for c in (e.get("causes") or []):
            cause_c[c] += 1
        for lv in (e.get("levers") or []):
            lever_c[lv] += 1
    return {
        "total": len(evs),
        "judged": judged, "rejected": rejected, "edited": edited, "accepted_clean": clean,
        # accept_rate: 초안이 게시까지 간 비율 (거부되지 않음)
        "accept_rate": round(approved / judged, 3) if judged else None,
        # clean_rate: 손대지 않고 그대로 게시된 비율 — 진짜 품질 지표
        "clean_rate": round(clean / judged, 3) if judged else None,
        "edit_rate": round(edited / judged, 3) if judged else None,
        "by_cause": [{"cause": c, "label": CAUSES.get(c, CAUSES["other"])["label"],
                      "lever": lever_of(c), "count": n}
                     for c, n in cause_c.most_common()],
        "by_lever": dict(lever_c),
        "human_labeled": sum(1 for e in evs if e.get("origin") == "human" and e.get("causes")),
        "auto_labeled": sum(1 for e in evs if e.get("origin") == "auto"),
        "store_path": _rel_store(),
    }


def by_class(min_count: int = 1) -> list[dict]:
    """고장 클래스(template)별 원인 분포 — "어느 영역이 왜 실패하나".

    loop 가 클래스 단위로 액션을 만들 수 있어야 한다. 전역 집계만 있으면
    "생성 품질이 나쁨" 같은 손댈 데가 없는 진단밖에 안 나온다.
    """
    agg: dict[str, dict] = defaultdict(lambda: {"judged": 0, "rejected": 0, "edited": 0,
                                                "accepted_clean": 0, "causes": Counter()})
    for e in _load():
        t = e.get("template") or e.get("category") or "(미분류)"
        a = agg[t]
        a["judged"] += 1
        a[e.get("outcome", "edited")] = a.get(e.get("outcome", "edited"), 0) + 1
        for c in (e.get("causes") or []):
            a["causes"][c] += 1
    out = []
    for t, a in agg.items():
        if a["judged"] < min_count:
            continue
        bad = a["rejected"] + a["edited"]
        out.append({
            "template": t, "judged": a["judged"], "rejected": a["rejected"],
            "edited": a["edited"], "accepted_clean": a["accepted_clean"],
            "failure_rate": round(bad / a["judged"], 3) if a["judged"] else None,
            "top_causes": [{"cause": c, "count": n} for c, n in a["causes"].most_common(3)],
            "top_levers": [lv for lv, _ in Counter(
                lever_of(c) for c, n in a["causes"].items() for _ in range(n)).most_common(2)],
        })
    out.sort(key=lambda x: (-(x["rejected"] + x["edited"]), -x["judged"]))
    return out


def trend(window: int = 20) -> dict:
    """최근 window 건 vs 그 이전 — clean_rate 가 오르고 있나(=loop 가 먹히나)."""
    evs = sorted(_load(), key=lambda e: e.get("created_at", ""))
    judged = [e for e in evs if e.get("outcome") in OUTCOMES]
    if len(judged) < 2 * window:
        return {"enough_data": False, "judged": len(judged), "window": window}

    def clean(chunk):
        return round(sum(1 for e in chunk if e["outcome"] == "accepted_clean") / len(chunk), 3)
    prev, curr = judged[-2 * window:-window], judged[-window:]
    return {"enough_data": True, "window": window,
            "clean_rate_prev": clean(prev), "clean_rate_curr": clean(curr),
            "delta": round(clean(curr) - clean(prev), 3)}


# --------------------------------------------------------------------------- #
# 환류 ①: 생성 프롬프트 가이던스 — 축적된 원인이 다음 초안을 직접 바꾼다
# --------------------------------------------------------------------------- #
GUIDANCE_MIN_COUNT = int(os.getenv("RVP_DRAFT_FB_GUIDANCE_MIN", "2"))
GUIDANCE_MAX_RULES = 4


def prompt_guidance(category: str = "", template: str = "",
                    min_count: int = 0, max_rules: int = GUIDANCE_MAX_RULES) -> str:
    """같은 고장 클래스에서 반복된 지적 → 생성 프롬프트에 넣을 규칙 블록.

    **같은 클래스만 본다.** 전역 폴백을 하지 않는 이유는 `rca_feedback.relevant_edits`
    와 같다 — 무관한 클래스의 지적을 주입하면 이번 이슈에 없는 문제를 고치려다
    있는 문제를 놓친다.

    min_count 미만으로 나온 원인은 무시한다. 1회 지적으로 프롬프트를 바꾸면
    노이즈가 규칙이 된다.
    """
    min_count = min_count or GUIDANCE_MIN_COUNT
    evs = _load()
    matched = [e for e in evs if template and e.get("template") == template]
    if not matched and category:
        matched = [e for e in evs if e.get("category") == category]
    if not matched:
        return ""
    c: Counter = Counter()
    for e in matched:
        for cause in (e.get("causes") or []):
            c[cause] += 1
    rules = []
    for cause, n in c.most_common():
        if n < min_count:
            continue
        g = (CAUSES.get(cause) or {}).get("guidance", "")
        if g:
            rules.append(f"- ({CAUSES[cause]['label']}, {n}회 지적) {g}")
        if len(rules) >= max_rules:
            break
    if not rules:
        return ""
    return ("\n\n## 이 유형에서 사람이 반복 지적한 사항 (반드시 지킬 것)\n"
            + "\n".join(rules) + "\n")


# --------------------------------------------------------------------------- #
# 환류 ②: 자기개선 loop 제안 (L3 — 사람 검토 큐로)
# --------------------------------------------------------------------------- #
SUGGEST_MIN = int(os.getenv("RVP_DRAFT_FB_SUGGEST_MIN", "3"))
SUGGEST_MAX = 6


def suggestions(min_count: int = 0) -> list[dict]:
    """축적된 원인 → actionable 개선 제안. `improve_queue` 스키마를 그대로 따른다.

    레버별로 액션이 다르다는 것이 요점이다:
      retrieval    → 파라미터 shadow 평가(L2)로 검증 후 적용
      generation   → 프롬프트 규칙은 이미 자동 주입되므로, 남는 건 평가셋 추가
      knowledge    → 사람이 RCA 작성/폐기 (기존 HITL 엔드포인트)
      presentation → 검증기/포매터 규칙
    """
    min_count = min_count or SUGGEST_MIN
    out: list[dict] = []
    classes = by_class()

    # 1) 클래스 단위 — 어느 고장군에서 어떤 레버가 반복해서 실패하나
    for cl in classes:
        bad = cl["rejected"] + cl["edited"]
        if bad < min_count:
            continue
        top = cl["top_causes"][0] if cl["top_causes"] else None
        if not top:
            continue
        # **최다 원인이 1회뿐이면 레버별 액션을 만들지 않는다.** 클래스가 실패한 건
        # 맞지만 이유가 흩어져 있다는 뜻이고, 그때 특정 레버를 지목하면 근거 없는
        # 지시가 된다("'인용 키 오류' 가 1회 — 검색 파라미터를 튜닝하세요"). 원인이
        # 모일 때까지 기다리는 편이 낫다.
        if top["count"] < 2:
            out.append({
                "type": "triage_scattered_failures", "priority": "P2",
                "target": cl["template"],
                "rationale": (f"'{cl['template'][:40]}' 초안 {cl['judged']}건 중 {bad}건이 "
                              f"거부/수정됐지만 원인이 흩어져 있다"
                              f"({', '.join(c['cause'] for c in cl['top_causes'])}) — "
                              f"레버를 특정할 수 없다. 사람이 사례를 직접 읽고 라벨을 "
                              f"달아야 원인이 모인다."),
                "evidence": {"template": cl["template"], "judged": cl["judged"],
                             "rejected": cl["rejected"], "edited": cl["edited"],
                             "top_causes": cl["top_causes"]},
                "action_hint": "GET /rca/draft-feedback?template=... 로 사례 확인 후 라벨링",
            })
            continue
        cause, lever = top["cause"], lever_of(top["cause"])
        label = CAUSES.get(cause, CAUSES["other"])["label"]
        base = {
            "target": f"{cl['template'][:60]}|{cause}",
            "evidence": {"template": cl["template"], "judged": cl["judged"],
                         "rejected": cl["rejected"], "edited": cl["edited"],
                         "failure_rate": cl["failure_rate"], "top_causes": cl["top_causes"]},
        }
        if lever == "retrieval":
            out.append({**base, "type": "tune_retrieval", "priority": "P1",
                        "rationale": (f"'{cl['template'][:40]}' 초안 {cl['judged']}건 중 {bad}건이 "
                                      f"거부/수정됐고 최다 원인은 '{label}'({top['count']}회) — "
                                      f"검색 근거가 틀린 것이므로 게이트/랭킹 파라미터를 "
                                      f"동결 평가셋에 shadow 평가 후 적용 검토."),
                        "action_hint": "POST /selfimprove/param/evaluate (gate_cos·boost 무회귀 확인 후 적용)"})
        elif lever == "knowledge":
            out.append({**base, "type": "author_rca", "priority": "P1",
                        "target": cl["template"],
                        "rationale": (f"'{cl['template'][:40]}' 에서 '{label}' 가 {top['count']}회 — "
                                      f"KB 에 쓸 만한 사례가 없거나 낡았다. 사례 작성/폐기가 필요."),
                        "action_hint": "해당 고장군 RCA 작성·시드 또는 POST /knowledge/lifecycle(deprecated)"})
        elif lever == "generation":
            out.append({**base, "type": "harden_generation", "priority": "P2",
                        "rationale": (f"'{cl['template'][:40]}' 에서 '{label}' 가 {top['count']}회 — "
                                      f"프롬프트 규칙은 자동 주입 중이므로, 이 클래스를 "
                                      f"평가셋에 추가해 회귀를 잡아야 한다."),
                        "action_hint": "data/eval_paraphrase.json 에 이 클래스 질의 추가 후 재평가"})
        else:
            out.append({**base, "type": "fix_presentation", "priority": "P3",
                        "rationale": (f"'{cl['template'][:40]}' 에서 '{label}' 가 {top['count']}회 — "
                                      f"내용이 아니라 형식 문제. 검증기 규칙으로 승인 전 차단 가능."),
                        "action_hint": "POST /rca/validate 규칙 보강(lang_validator)"})
        if len(out) >= SUGGEST_MAX:
            break

    # 2) 특정 사례가 반복해서 잘못 인용됨 → 그 사례 자체를 손봐야 한다
    bad_cites: Counter = Counter()
    for e in _load():
        if "bad_citation" in (e.get("causes") or []) or "outdated_knowledge" in (e.get("causes") or []):
            for k in (e.get("diff") or {}).get("citations_removed", []) or e.get("citations", []):
                bad_cites[k] += 1
    for k, n in bad_cites.most_common(3):
        if n < min_count:
            continue
        out.append({"type": "review_unhelpful", "priority": "P2", "target": k,
                    "rationale": f"{k} 가 초안에서 {n}회 잘못 인용되어 사람이 삭제 — 폐기/대체 검토",
                    "evidence": {"removed_count": n},
                    "action_hint": "POST /knowledge/lifecycle (deprecated/superseded) 검토"})
    return out
