"""유사 사례가 없을 때의 1차 원리 조사 계획 — coverage=false 전용.

**왜 필요한가.** coverage 게이트는 무관 사례에 기댄 환각을 막으려고 존재한다. 그래서
"유사 사례 없음" 이면 제품이 아무것도 하지 않는다 — 정직하지만, **가장 어려운 이슈에서
도움이 0** 이다. 처음 보는 고장일수록 사람이 도움을 원한다.

**그런데 여기서 근본원인을 생성하면 게이트를 무력화하는 것이다.** 그래서 산출물을
바꾼다: 결론이 아니라 **조사 계획**이다. 근거가 없을 때 정직한 값어치는 "원인은 X다"
가 아니라 "무엇을 재면 X인지 아닌지 갈린다" 이다.

**도구는 결정적으로 계산되는 것만 쓴다**(2026-08-04 실측으로 확인):
  · 약한 매치(게이트 미달 후보) — 점수와 함께, **근거가 아님**을 명시해 제시
  · 같은 칩의 다른 고장 이력 — 칩 11종, 증상이 달라도 그 칩의 약점은 알려준다
  · 같은 분류의 고장모드 목록 — 분류별 1~19종
  · 질의 엔티티를 공유하는 이슈

**쓰지 않는 것**: `debug_approach`. 136건에 고유 표현이 **5종뿐**이고 분류와 상관이
없다(실측: 특정 분류 전용 방법 0개). "이 분류에서 자주 쓰는 조사법" 이라고 제시하면
없는 신호를 있는 것처럼 말하는 셈이다. 부정지식(0건)·온톨로지(2건)도 같은 이유로 제외.

**표시 규약** — 모든 문장은 출처가 드러나야 한다:
  (참고) 약한 매치·칩 이력에서 온 것. 근거는 아니고 참고다.
  (배경) 도메인 일반 지식. 이 KB 와 무관.
  (추정) 가설. 반드시 판별 방법과 함께.
근본원인을 단정하는 문장은 **금지**한다 — 검증기가 잡는다.
"""

from __future__ import annotations

import re

import issue_keys
from collections import Counter

from recommender import template_key

# 조사 계획이 갖춰야 할 절 — 표기가 바뀌면 검증기도 같이 바꾼다.
SECTIONS = [
    ("관찰정리", "### 🔎 관찰 정리"),
    ("검색실패", "### 📉 왜 사례 검색이 실패했나"),
    ("가설", "### 🧪 가설 후보"),
    ("즉시확인", "### 📐 즉시 확인할 것"),
    ("배제조건", "### ✂️ 배제 조건"),
    ("참고자료", "### 📎 참고 자료(근거 아님)"),
    ("에스컬레이션", "### 🚨 에스컬레이션 기준"),
]

# 근거 없이 원인을 단정하는 표현 — 이게 나오면 게이트를 우회한 것이다.
_ASSERTION = re.compile(
    r"(근본\s*원인은[^.\n]{0,40}(이다|입니다|이며))"
    r"|(원인은[^.\n]{0,40}(이다|입니다)(?!\s*\(추정\)))"
    r"|(확실히|틀림없이|명백히)\s*[^.\n]{0,30}(때문|원인)")


def gather(query_rec: dict, reco, matches: list[dict], *, k_weak: int = 5) -> dict:
    """조사에 쓸 재료를 **결정적으로** 모은다. LLM 호출 없음.

    matches 는 게이트를 통과하지 못한 후보들이다 — 버리지 않고 "약한 매치" 로
    넘긴다. 관련이 없을 수 있지만, 무엇이 왜 안 맞는지 보는 것도 조사의 일부다.
    """
    chip = (query_rec.get("chip") or "").strip()
    category = (query_rec.get("category") or "").strip()
    kb = getattr(reco, "kb", []) or []

    weak = [{
        "key": m.get("key", ""),
        "summary": m.get("summary", ""),
        "chip": m.get("chip", ""),
        "category": m.get("category", ""),
        "root_cause": (m.get("root_cause") or "")[:400],
        "resolution": (m.get("resolution") or "")[:400],
        "rerank_score": m.get("rerank_score"),
        "embed_cos": m.get("embed_cos"),
        "entity_overlap": m.get("entity_overlap", 0),
    } for m in (matches or [])[:k_weak]]

    # 같은 칩의 다른 고장 — 증상이 달라도 그 칩이 어디서 약한지는 알려준다.
    chip_hist = []
    if chip:
        seen: set[str] = set()
        for r in kb:
            if r.get("chip") != chip:
                continue
            t = template_key(r.get("summary", ""))
            if t in seen:
                continue
            seen.add(t)
            chip_hist.append({"key": r.get("key", ""), "template": t,
                              "category": r.get("category", ""),
                              "root_cause": (r.get("root_cause") or "")[:200]})
        chip_hist = chip_hist[:8]

    # 같은 분류에서 알려진 고장모드 — 다른 칩이어도 축을 잡는 데 쓴다.
    cat_modes = []
    if category:
        c = Counter(template_key(r.get("summary", "")) for r in kb
                    if r.get("category") == category)
        cat_modes = [{"template": t, "count": n} for t, n in c.most_common(8)]

    # 질의 엔티티를 공유하는 이슈 — 그래프 랭커가 쓰는 것과 같은 신호.
    ents, ent_hits = set(), []
    try:
        from preprocess import extract_entities
        blob = " ".join([query_rec.get("summary", ""), query_rec.get("symptom", ""),
                         chip, category])
        ents = extract_entities(blob)
        kb_ents = getattr(reco, "_kb_ents", None)
        if kb_ents:
            for i, e in enumerate(kb_ents):
                shared = ents & e
                if len(shared) >= 2 and i < len(kb):
                    ent_hits.append({"key": kb[i].get("key", ""),
                                     "shared": sorted(shared)[:6],
                                     "summary": kb[i].get("summary", "")[:90]})
            ent_hits.sort(key=lambda x: -len(x["shared"]))
            ent_hits = ent_hits[:6]
    except Exception:
        pass

    return {
        "query": {"key": query_rec.get("key", ""), "summary": query_rec.get("summary", ""),
                  "symptom": query_rec.get("symptom", ""), "chip": chip, "category": category},
        "weak_matches": weak,
        "chip_history": chip_hist,
        "category_modes": cat_modes,
        "query_entities": sorted(ents)[:20],
        "entity_neighbors": ent_hits,
        "kb_size": len(kb),
    }


def allowed_keys(bundle: dict) -> set[str]:
    """본문이 언급해도 되는 사례 키 — 번들에 실제로 실린 것 + 질의 자신."""
    ks = {bundle.get("query", {}).get("key", "")}
    for grp in ("weak_matches", "chip_history", "entity_neighbors"):
        for it in bundle.get(grp, []) or []:
            ks.add(it.get("key", ""))
    return {k for k in ks if k}


def build_prompt(bundle: dict, gate: dict | None) -> str:
    q = bundle["query"]
    g = gate or {}
    lines = [
        "당신은 LSI 칩/펌웨어 불량 분석 시니어 엔지니어다.",
        "",
        "**이 질의는 유사 과거 사례를 찾지 못했다.** 따라서 근본원인을 단정하지 마라.",
        "대신 **조사 계획**을 쓴다 — 무엇을 재면 어떤 가설이 죽는지가 핵심이다.",
        "",
        "표시 규약(반드시 지킬 것):",
        "  (참고) 아래 '약한 매치'·'칩 이력'에서 가져온 내용. 근거가 아니라 참고다.",
        "  (배경) 일반 도메인 지식. 이 KB 와 무관하다는 뜻이다.",
        "  (추정) 가설. **반드시 판별 방법과 함께** 쓴다.",
        "규칙: 사례 키(LSI-숫자)는 아래에 실제로 제시된 것만 쓴다. 창작 금지.",
        "'근본원인은 …이다' 같은 단정문은 쓰지 마라. 한국어, 한자 금지.",
        "",
        f"## 대상 이슈 {q.get('key','')}",
        f"- 요약: {q.get('summary','')}",
        f"- 증상: {q.get('symptom','')}",
        f"- 칩: {q.get('chip') or '(미지정)'} · 분류: {q.get('category') or '(미지정)'}",
        "",
        "## 검색이 실패한 근거(수치)",
        f"- 게이트 신호: {g.get('signal','?')} · 통과 임계 "
        f"{g.get('threshold', g.get('cos_threshold','?'))}",
        f"- 최고 점수: {g.get('rerank_top', g.get('max_cos','?'))} · "
        f"엔티티 겹침 {g.get('top_entity_overlap', 0)}",
        f"- KB 해결 사례 수: {bundle.get('kb_size', 0)}",
    ]

    if bundle["weak_matches"]:
        lines += ["", "## 약한 매치(게이트 미달 — 근거 아님)"]
        for m in bundle["weak_matches"]:
            sc = m.get("rerank_score") if m.get("rerank_score") is not None else m.get("embed_cos")
            lines.append(f"- {m['key']} (점수 {sc}, 엔티티겹침 {m['entity_overlap']}) "
                         f"[{m['chip']}/{m['category']}] {m['summary'][:90]}")
            if m["root_cause"]:
                lines.append(f"    근본원인: {m['root_cause'][:200]}")
    if bundle["chip_history"]:
        lines += ["", f"## 같은 칩({q.get('chip')})의 다른 고장 이력"]
        for h in bundle["chip_history"]:
            lines.append(f"- {h['key']} [{h['category']}] {h['template'][:80]}")
    if bundle["category_modes"]:
        lines += ["", f"## 같은 분류({q.get('category')})에서 알려진 고장모드"]
        for c in bundle["category_modes"]:
            lines.append(f"- ({c['count']}건) {c['template'][:80]}")
    if bundle["entity_neighbors"]:
        lines += ["", "## 기술 용어를 공유하는 이슈"]
        for e in bundle["entity_neighbors"]:
            lines.append(f"- {e['key']} 공유: {', '.join(e['shared'])} — {e['summary']}")

    lines += ["", "## 출력 형식 — 아래 7개 절을 이 순서·이 제목 그대로", ""]
    lines += [f"{marker}" for _, marker in SECTIONS]
    lines += [
        "",
        "각 절 지침:",
        "- 관찰 정리: 증상을 관측 가능한 축(시점·조건·빈도·범위)으로 분해한다.",
        "- 왜 실패했나: 위 수치를 사람 말로 옮긴다. 사례가 없는 것인지, 표현이 달라서인지.",
        "- 가설 후보: 3~5개. 각각 (추정) + '이걸 재면 갈린다' 를 한 줄로 붙인다.",
        "- 즉시 확인할 것: 로그·계측·재현 조건. 가능한 명령/레지스터/신호를 구체적으로.",
        "- 배제 조건: 무엇이 관측되면 어떤 가설이 죽는지.",
        "- 참고 자료: 위 약한 매치·칩 이력 중 볼 만한 것. **근거가 아님을 명시.**",
        "- 에스컬레이션: 어느 조건이면 시니어/설계팀으로 올릴지.",
    ]
    return "\n".join(lines)


def validate(md: str, bundle: dict) -> dict:
    """LLM 없이 결정적으로 검사 — 게이트를 우회하지 않았는가.

    이 기능은 "근거 없이 분석하지 않는다" 는 제품 계약을 일부러 여는 것이라,
    검증이 없으면 게이트를 무력화한 것과 같다.
    """
    present = {name: (marker in md) for name, marker in SECTIONS}
    mentioned = set(issue_keys.find_full(md))
    allow = allowed_keys(bundle)
    # 접미사(-rca) 표기 차이를 흡수 — 근거 키가 LSI-7-rca 인데 본문은 LSI-7 로 쓴다.
    allowed = issue_keys.expand(allow)
    unsupported = sorted({m for m in mentioned
                          if m not in allowed and issue_keys.stem(m) not in allowed})

    # 가설 절에 (추정) 이 붙었는가 — 가설을 단정으로 쓰면 이 기능의 취지가 무너진다.
    hyp = ""
    m = re.search(r"### 🧪 가설 후보(.*?)(?=\n### |\Z)", md, flags=re.S)
    if m:
        hyp = m.group(1)
    hyp_items = [ln for ln in hyp.split("\n") if ln.strip().startswith(("-", "*", "1.", "2.", "3.", "4.", "5."))]
    hyp_labeled = sum(1 for ln in hyp_items if "(추정)" in ln)

    return {
        "sections_present": sum(present.values()),
        "sections_total": len(SECTIONS),
        "missing_sections": [n for n, ok in present.items() if not ok],
        "unsupported_mentions": unsupported,
        "hypotheses": len(hyp_items),
        "hypotheses_labeled": hyp_labeled,
        "assertions": [a[0] if isinstance(a, tuple) else a
                       for a in _ASSERTION.findall(md)][:5],
        "labels": {"참고": md.count("(참고)"), "배경": md.count("(배경)"),
                   "추정": md.count("(추정)")},
        "chars": len(md),
    }


def is_acceptable(v: dict) -> tuple[bool, str]:
    """게시해도 되는가 — 하나라도 어기면 조사 계획을 내보내지 않는다."""
    if v["sections_present"] < v["sections_total"]:
        return False, f"절 누락: {', '.join(v['missing_sections'])}"
    if v["unsupported_mentions"]:
        return False, f"제시되지 않은 사례 언급: {', '.join(v['unsupported_mentions'])}"
    if v["assertions"]:
        return False, "근거 없이 원인을 단정하는 문장이 있음"
    if v["hypotheses"] and v["hypotheses_labeled"] < v["hypotheses"]:
        return False, (f"가설 {v['hypotheses']}개 중 {v['hypotheses_labeled']}개만 "
                       f"(추정) 표시")
    return True, ""
