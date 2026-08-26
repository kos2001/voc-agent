"""자기 개선 loop — L1(측정·진단·제안, 무변경).

설계(loop = 측정 → 진단 → 개선 → 검증 → 거버넌스):
  이번에 만든 신호들(P1-3 유용성, P1-2 품질게이트, P3-8 지식공백, 자산 통계)을
  한 번에 집계하고, 직전 회차 대비 드리프트를 계산하며, 신호에서 우선순위 개선
  액션을 도출한다. **부작용이 없는 L1**이라 안전하게 주기 실행 가능(cron/엔드포인트).

  L2(파라미터 자동 튜닝 + 회귀 게이트)·L3(지식 변경 HITL 제안)은 이 측정 토대 위에
  올린다. 여기서는 측정·진단·제안까지만.

산출:
  - snapshot(): 현재 지표 묶음.
  - run(): snapshot + 직전 대비 드리프트 + 개선 제안 → 이력 적재(데이터) +
    날짜별 리포트(claudedocs/self_improve/). 반환 dict.
"""
from __future__ import annotations

import json
from pathlib import Path

from json_store import read_json, write_json_atomic, now_iso

ROOT = Path(__file__).resolve().parent.parent
HISTORY_FILE = ROOT / "data" / "self_improve_history.json"
REPORT_DIR = ROOT / "claudedocs" / "self_improve"


def _safe(fn, default=None):
    try:
        return fn()
    except Exception as e:
        return {"_error": str(e)[:120]} if default is None else default


def snapshot(records: list[dict] | None = None) -> dict:
    """모든 측정 신호를 한 번에 수집. records 미지정 시 KB 스냅숏 로드."""
    import draft_feedback
    import quality_gate
    import reco_feedback
    import knowledge_gaps
    import knowledge_store
    import failure_modes
    import lifecycle
    import ontology
    import negative_knowledge
    import outcome_tracker

    if records is None:
        import preprocess
        import kb_source
        # 서버(`_build_reco_state`)와 **같은 원천**이어야 한다 — 갈라지면 대시보드와
        # 개선 큐가 서로 다른 KB 를 근거로 다른 결론을 낸다(이미 한 번 당한 실패).
        records = [preprocess.parse_issue(r) for r in kb_source.raw_issues()]
    base = [r for r in records if not r.get("curated")]

    quality = _safe(lambda: quality_gate.validate(base), default={})
    return {
        "ts": now_iso(),
        "reco_feedback": _safe(reco_feedback.stats, {}),
        # 초안 결함 — 최종 산출물(고객에게 나가는 답변)이 실패한 신호. 다른 신호가
        # 전부 '중간 과정'을 보는 반면 이것만 결과를 본다.
        "draft_feedback": _safe(draft_feedback.stats, {}),
        "draft_classes": _safe(lambda: draft_feedback.by_class()[:10], []),
        "draft_trend": _safe(draft_feedback.trend, {}),
        "kb_quality": {"ok": quality.get("ok"), "violations": quality.get("violations", []),
                       "fill": (quality.get("report") or {}).get("fill", {}),
                       "deficient": len((quality.get("report") or {}).get("deficient_resolved_keys", []))},
        "knowledge_gaps": _safe(lambda: knowledge_gaps.report(top=10), {}),
        "outcomes": _safe(outcome_tracker.report, {}),
        "assets": {
            "curated_knowledge": _safe(knowledge_store.stats, {}),
            "known_issues": _safe(failure_modes.stats, {}),
            "lifecycle": _safe(lifecycle.stats, {}),
            "ontology": _safe(ontology.stats, {}),
            "negative_knowledge": _safe(negative_knowledge.stats, {}),
        },
    }


def recommendations(snap: dict) -> list[dict]:
    """신호 → 우선순위 개선 액션(진단). L1은 제안만, 실행은 L2/L3."""
    out = []
    # 초안 품질이 최우선 진단이다 — 고객에게 나가는 답변의 성능을 직접 재는 유일한 지표.
    df = snap.get("draft_feedback", {})
    judged, clean = df.get("judged", 0), df.get("clean_rate")
    if judged >= 5 and clean is not None and clean < 0.5:
        # 레버별로 액션이 다르므로 최다 레버를 진단에 박아 둔다 — "품질이 나쁘다"만
        # 말하는 진단은 손댈 데가 없어 아무도 움직이지 않는다.
        lev = sorted((snap.get("draft_feedback", {}).get("by_lever") or {}).items(),
                     key=lambda x: -x[1])
        top_lev = lev[0][0] if lev else "?"
        top_cause = (df.get("by_cause") or [{}])[0]
        out.append({"priority": "P1", "area": "초안 품질",
                    "action": (f"무수정 승인율 {clean} < 0.5 (판정 {judged}건) — 최다 원인 "
                               f"'{top_cause.get('label', '?')}' ({top_cause.get('count', 0)}회), "
                               f"주 레버 '{top_lev}'. 개선 큐의 해당 제안부터 처리 권장")})
    if judged >= 5 and df.get("accept_rate") is not None and df["accept_rate"] < 0.7:
        out.append({"priority": "P1", "area": "초안 거부율",
                    "action": (f"초안 거부율 {round(1 - df['accept_rate'], 3)} — 게시조차 못 하는 "
                               f"초안이 많다. 거부 사유 상위 항목을 /rca/draft-feedback 에서 확인")})
    dt = snap.get("draft_trend", {})
    if dt.get("enough_data") and dt.get("delta", 0) < -0.1:
        out.append({"priority": "P1", "area": "초안 품질 추세",
                    "action": (f"무수정 승인율이 {dt['clean_rate_prev']} → {dt['clean_rate_curr']} "
                               f"로 하락({dt['delta']}) — 최근 변경(프롬프트/파라미터/KB)의 회귀 의심")})
    fb = snap.get("reco_feedback", {})
    rate = fb.get("helpful_rate")
    if rate is not None and rate < 0.7 and fb.get("total", 0) >= 5:
        out.append({"priority": "P1", "area": "랭킹",
                    "action": f"유용성 {rate} < 0.7 — helpfulness_prior로 재순위 보정 검토(회귀 게이트 후 적용)"})
    if not snap.get("kb_quality", {}).get("ok", True):
        out.append({"priority": "P1", "area": "KB 품질",
                    "action": f"품질 게이트 위반: {snap['kb_quality'].get('violations')} — 인입 마커/추출 점검"})
    oc = snap.get("outcomes", {})
    if oc.get("efficacy_rate") is not None and oc["efficacy_rate"] < 0.5 and oc.get("total_tracked", 0) >= 4:
        out.append({"priority": "P1", "area": "효능",
                    "action": f"게시 RCA 효능율 {oc['efficacy_rate']} < 0.5 — 미해결 잔존 사례 재검토/대체 필요"})
    gaps = snap.get("knowledge_gaps", {}).get("top_underserved_templates", [])
    if gaps:
        top = ", ".join(f"{g['template'][:24]}({g['count']})" for g in gaps[:3])
        out.append({"priority": "P2", "area": "지식 공백",
                    "action": f"사례 없는 빈출 영역 RCA 작성·시드 필요: {top}"})
    ki = snap.get("assets", {}).get("known_issues", {}).get("total_articles", 0)
    if ki == 0:
        out.append({"priority": "P3", "area": "고장모드",
                    "action": "승격된 Known-Issue 기사 0건 — 중복 군집을 기사로 묶기 권장(/knowledge/clusters)"})
    onto = snap.get("assets", {}).get("ontology", {})
    if onto.get("synonym_groups", 0) == 0:
        out.append({"priority": "P3", "area": "온톨로지",
                    "action": "동의어 그룹 0건 — /knowledge/ontology/review의 산재 표기 정규화 권장"})
    if not out:
        out.append({"priority": "—", "area": "전반", "action": "임계 신호 없음 — 안정적"})
    return out


def _load_history() -> list:
    d = read_json(HISTORY_FILE, [])
    return d if isinstance(d, list) else []


def _key_metrics(snap: dict) -> dict:
    fb = snap.get("reco_feedback", {})
    df = snap.get("draft_feedback", {})
    return {
        "ts": snap.get("ts"),
        # 초안 KPI — VOC 답변 성능의 1차 지표(사람이 손대지 않고 나갔는가)
        "draft_clean_rate": df.get("clean_rate"),
        "draft_accept_rate": df.get("accept_rate"),
        "draft_judged": df.get("judged", 0),
        "helpful_rate": fb.get("helpful_rate"),
        "feedback_total": fb.get("total", 0),
        "roi_queries": fb.get("actual_root_cause_queries", 0),
        "kb_quality_ok": snap.get("kb_quality", {}).get("ok"),
        "efficacy_rate": snap.get("outcomes", {}).get("efficacy_rate"),
        "gap_events": snap.get("knowledge_gaps", {}).get("total_gap_events", 0),
        "curated": snap.get("assets", {}).get("curated_knowledge", {}).get("total", 0),
        "known_issues": snap.get("assets", {}).get("known_issues", {}).get("total_articles", 0),
    }


def _drift(curr: dict, prev: dict | None) -> dict:
    if not prev:
        return {"first_run": True}
    d = {}
    for k in ("draft_clean_rate", "draft_accept_rate", "draft_judged",
              "helpful_rate", "feedback_total", "roi_queries", "gap_events", "curated", "known_issues"):
        a, b = curr.get(k), prev.get(k)
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            d[k] = round(a - b, 3)
    d["since"] = prev.get("ts")
    return d


def run(records: list[dict] | None = None, *, save: bool = True) -> dict:
    """측정 → 진단 → (이력 적재 + 리포트). 부작용은 데이터/리포트 쓰기뿐(지식 불변)."""
    snap = snapshot(records)
    recs = recommendations(snap)
    metrics = _key_metrics(snap)
    history = _load_history()
    drift = _drift(metrics, history[-1] if history else None)
    result = {"snapshot": snap, "recommendations": recs, "metrics": metrics, "drift": drift}
    if save:
        history.append(metrics)
        write_json_atomic(HISTORY_FILE, history[-200:])
        _write_report(result)
    return result


def _write_report(result: dict) -> Path:
    snap, metrics, drift = result["snapshot"], result["metrics"], result["drift"]
    ts = snap["ts"].replace(":", "").replace("-", "")
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / f"selfcheck_{ts}.md"
    lines = [f"# 자기 개선 점검 — {snap['ts']}", "",
             "## 핵심 지표", "| 지표 | 값 | 직전 대비 |", "|---|---|---|"]
    for k, label in (("draft_clean_rate", "초안 무수정 승인율"),
                     ("draft_accept_rate", "초안 게시율"),
                     ("draft_judged", "초안 판정 건수"),
                     ("helpful_rate", "추천 유용성"), ("roi_queries", "ROI(실제RC 질의)"),
                     ("gap_events", "지식 공백 이벤트"), ("curated", "큐레이션 지식"),
                     ("known_issues", "고장모드 기사")):
        dv = drift.get(k)
        lines.append(f"| {label} | {metrics.get(k)} | {('+' if isinstance(dv,(int,float)) and dv>0 else '')}{dv if dv is not None else ('초회' if drift.get('first_run') else '—')} |")
    lines += ["", f"- KB 품질 게이트: {'통과' if metrics.get('kb_quality_ok') else '위반'}", ""]
    lines += ["## 개선 제안 (L1 — 제안만, 실행은 L2/L3)", "", "| 우선 | 영역 | 액션 |", "|---|---|---|"]
    for r in result["recommendations"]:
        lines.append(f"| {r['priority']} | {r['area']} | {r['action']} |")
    gaps = snap.get("knowledge_gaps", {}).get("top_underserved_templates", [])
    if gaps:
        lines += ["", "## 지식 공백 상위(문서화 우선순위)", ""]
        lines += [f"- {g['template'][:50]} — {g['count']}회" for g in gaps[:10]]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# L2 — 파라미터 후보를 동결 평가셋에 shadow 평가 + 무회귀 게이트 (가장 안전)
# --------------------------------------------------------------------------- #
# 튜닝 가능 파라미터 화이트리스트: {param: (env_override, recommender_kwarg)}.
# 1차 검색 파라미터만(리랭커 불필요 → shadow 평가가 빠르고 비용 0). 미설정 시 현행 기본값.
TUNABLE = {
    "gate_cos": "RVP_GATE_COS",   # coverage 게이트 임베딩 임계
    "boost": "RVP_BOOST",         # 동일 칩/분류 가산
}
# 무회귀 게이트가 지키는 지표(모두 현행 이상이어야 '안전'). 허용 오차 0(엄격).
_GUARDED = ("P@1", "P@3", "MRR")
_GUARDED_PARA = ("P@1", "P@3", "MRR", "gate_pass", "junk_blocked")


def embed_kwargs() -> dict:
    """서버(`backend/server.py` `_reco_state`)와 **동일한** 임베딩 백엔드·모델 인자.

    오프라인에서 만드는 추천기는 사용자가 실제로 검색하는 것과 같은 유사도 공간이어야
    한다. 넘기지 않으면 Recommender 가 로컬 MiniLM 기본값으로 떨어지는데, 운영은
    bge-m3(API)다. 그 상태에서 군집·모순·파라미터 평가를 돌리면 **없는 문제를 만들고
    있는 문제를 놓친다** — 실제로 대시보드가 "모순 없음", 개선 큐가 "모순 1건" 을
    동시에 보여줬다. 서버 쪽 로직을 바꾸면 여기도 같이 바꿔야 한다.
    """
    from recommender import env_embed_kwargs
    return env_embed_kwargs()


def _resolved_kb():
    import preprocess
    import kb_source
    records = [preprocess.parse_issue(r) for r in kb_source.raw_issues()]
    return [r for r in records if r.get("status") == "완료" and not r.get("curated")]


def _eval_with(resolved, overrides: dict) -> dict:
    """주어진 파라미터 override로 Recommender를 만들어 LOO + 동결 paraphrase 평가.

    리랭커 없이(1차 검색 기준) 평가 — 빠르고 외부 비용 0. 동결셋: data/eval_paraphrase.json.
    """
    import eval_recommender as ev
    from recommender import Recommender
    import os as _os
    # 임베딩은 서버와 같은 것 — 다른 공간에서 튜닝한 임계값은 운영에서 의미가 없다.
    kw = {"method": _os.getenv("RVP_RECO_METHOD", "hybrid_embed"), "rerank": False,
          **embed_kwargs()}
    kw.update(overrides)
    rec = Recommender(resolved, **kw)
    kb_keys = {r["key"] for r in resolved}
    loo = ev.evaluate(rec, resolved, kb_keys, loo=True, k=3)
    out = {"loo": loo}
    para_path = ROOT / "data" / "eval_paraphrase.json"
    if para_path.exists():
        out["paraphrase"] = ev.evaluate_paraphrase(rec, json.loads(para_path.read_text(encoding="utf-8")))
    return out


def evaluate_param(param: str, value: float) -> dict:
    """후보 파라미터 값을 동결 평가셋에 shadow 평가하고 무회귀 여부를 판정(READ-ONLY).

    live 상태(_RECO_STATE)·설정을 일절 건드리지 않는다. safe=True는 모든 가드 지표가
    현행 이상일 때만(엄격). 안전하다고 자동 적용하지 않음 — 적용은 별도 명시 단계.
    """
    if param not in TUNABLE:
        raise ValueError(f"튜닝 가능 파라미터 아님: {param} (가능: {list(TUNABLE)})")
    import os as _os
    resolved = _resolved_kb()
    cur_val = _os.getenv(TUNABLE[param])
    cur_overrides = {param: float(cur_val)} if cur_val else {}
    base = _eval_with(resolved, cur_overrides)
    cand = _eval_with(resolved, {param: float(value)})

    regressions, deltas = [], {}
    for setname, guarded in (("loo", _GUARDED), ("paraphrase", _GUARDED_PARA)):
        b, c = base.get(setname), cand.get(setname)
        if not (b and c):
            continue
        for k in guarded:
            if k in b and k in c:
                d = round(c[k] - b[k], 4)
                deltas[f"{setname}.{k}"] = d
                if d < 0:
                    regressions.append(f"{setname}.{k} {b[k]}→{c[k]} ({d})")
    safe = not regressions
    return {
        "param": param, "current_value": cur_val, "candidate_value": value,
        "current": base, "candidate": cand, "deltas": deltas,
        "safe": safe, "regressions": regressions,
        "verdict": ("무회귀 — 적용 안전(수동 적용 필요)" if safe
                    else f"회귀 발생 — 적용 금지: {'; '.join(regressions)}"),
    }


# --------------------------------------------------------------------------- #
# L3 — 신호에서 '지식 변경' 제안 도출(사람 검토 큐). loop는 실행하지 않는다.
# --------------------------------------------------------------------------- #
# 승격 제안 개수 상한 — 큐가 한 유형으로 덮이면 다른 유형이 묻힌다.
PROMOTE_SUGGEST_MAX = 5


def suggest(reco=None, records: list[dict] | None = None) -> list[dict]:
    """측정 신호 → actionable 지식 변경 제안 목록. 실행은 사람이 HITL 엔드포인트로.

    각 제안: {type, priority, target, rationale, evidence, action_hint}.
    """
    out = []

    # 0) 초안 거부/수정 원인 → 레버별 개선 제안. **다른 어떤 신호보다 직접적이다** —
    #    군집·모순·공백은 "KB 가 이럴 것이다" 를 보지만, 이건 사람이 실제로 나가는
    #    답변을 보고 내린 판정이다.
    try:
        import draft_feedback
        out.extend(draft_feedback.suggestions())
    except Exception:
        pass

    # 1) 미승격 고장모드 군집 → Known-Issue 기사 승격 제안(중복 사례 정리)
    #
    # **개수를 제한한다.** 예전에는 군집마다 한 건씩 뽑아 큐가 같은 유형으로 51건까지
    # 쌓였다(전체 53건 중). 사람이 처리하는 목록인데 한 종류가 화면을 덮으면 다른
    # 유형(모순·온톨로지)이 묻히고, 결국 아무도 큐를 보지 않게 된다.
    #
    # 효과 실측(2026-08-02): 승격 3건을 실행해도 **검색 품질은 그대로**였고
    # (P@1 1.000 → 1.000, 게이트 1.000 유지), 바뀐 것은 매치에 붙는 known_issue
    # 주석이었다(1/48 → 9/48). 즉 이 제안의 값어치는 **검색 개선이 아니라 지식
    # 정리**다 — rationale 에 그대로 적어 우선순위를 오해하지 않게 한다.
    if reco is not None:
        try:
            import failure_modes
            clusters = [c for c in failure_modes.cluster_from_recommender(reco, threshold=0.80, min_size=3)
                        if not c.get("promoted") and c.get("avg_similarity", 0) >= 0.80]
            # 큰 군집부터 — 같은 노력으로 더 많은 사례가 정리된다.
            clusters.sort(key=lambda c: (-c.get("size", 0), -c.get("avg_similarity", 0)))
            for c in clusters[:PROMOTE_SUGGEST_MAX]:
                out.append({
                    "type": "promote_known_issue", "priority": "P3",
                    "target": c["representative"],
                    "rationale": (f"유사도 {c['avg_similarity']}로 묶인 {c['size']}건 중복 사례 — "
                                  f"정규 기사로 승격 권장. 검색 정확도는 바뀌지 않고(실측) "
                                  f"매치에 '알려진 고장모드' 주석이 붙어 읽는 사람이 "
                                  f"중복임을 알 수 있다."),
                    "evidence": {"members": c["members"], "size": c["size"],
                                 "avg_similarity": c["avg_similarity"], "chips": c.get("chips", [])},
                    "action_hint": "POST /knowledge/known-issue (members 승격)",
                })
            if len(clusters) > PROMOTE_SUGGEST_MAX:
                out.append({
                    "type": "promote_known_issue_bulk", "priority": "P3",
                    "target": "",
                    "rationale": (f"승격 대상 군집이 {len(clusters)}개 더 있다 — 상위 "
                                  f"{PROMOTE_SUGGEST_MAX}건만 개별 제안으로 올렸다. "
                                  f"나머지는 대시보드의 '중복 지식' 카드에서 한 번에 본다."),
                    "evidence": {"remaining": len(clusters) - PROMOTE_SUGGEST_MAX},
                    "action_hint": "GET /knowledge/clusters",
                })
        except Exception:
            pass

    # 1b) 지식 모순(같은 고장모드인데 근본원인 엇갈림) → 사람 중재 제안(#2)
    if reco is not None:
        try:
            import contradictions
            for c in contradictions.detect(reco)[:10]:
                out.append({
                    "type": "resolve_contradiction", "priority": "P2",
                    "target": f"{c['a']}|{c['b']}",
                    "rationale": f"유사 고장(doc {c['doc_similarity']})인데 근본원인 엇갈림(rc {c['root_cause_similarity']}) — 중재 필요",
                    "evidence": {"a": c["a"], "b": c["b"], "doc_sim": c["doc_similarity"],
                                 "rc_sim": c["root_cause_similarity"]},
                    "action_hint": "두 사례 비교 후 disputed/대체 결정(POST /knowledge/lifecycle)",
                })
        except Exception:
            pass

    # 2) 지식 공백(자주 묻지만 사례 없음) → RCA 작성/시드 제안
    try:
        import knowledge_gaps
        for g in knowledge_gaps.report(top=10).get("top_underserved_templates", []):
            if g.get("count", 0) < 2:
                continue
            out.append({
                "type": "author_rca", "priority": "P1",
                "target": g["template"],
                "rationale": f"'{g['template'][:40]}' 영역이 {g['count']}회 질의됐으나 유사 사례 없음(coverage 미통과)",
                "evidence": {"gap_count": g["count"]},
                "action_hint": "해당 고장군 해결 사례 시드/문서화",
            })
    except Exception:
        pass

    # 3) 반복적으로 '도움 안 됨'으로 평가된 사례 → 검토/폐기 제안
    try:
        import reco_feedback
        from collections import defaultdict
        net = defaultdict(int)
        for e in reco_feedback._load():
            net[e["match_key"]] += 1 if e.get("rating") == "helpful" else -1
        for k, v in net.items():
            if v <= -2:
                out.append({
                    "type": "review_unhelpful", "priority": "P2", "target": k,
                    "rationale": f"{k}가 추천에서 반복적으로 '도움 안 됨'(순효용 {v}) — 폐기/대체 검토",
                    "evidence": {"net_helpful": v},
                    "action_hint": "POST /knowledge/lifecycle (deprecated/superseded) 검토",
                })
    except Exception:
        pass

    # 4) 통제 어휘 밖 빈출 용어 → 온톨로지 정규화 검토(한 건으로 묶음, 노이즈 방지)
    if records is not None:
        try:
            import ontology
            rev = ontology.review(records, top=8)
            terms = [e["term"] for e in rev.get("uncontrolled_entities", []) if e["count"] >= 10]
            if len(terms) >= 3:
                out.append({
                    "type": "normalize_ontology", "priority": "P3", "target": "uncontrolled_terms",
                    "rationale": "통제 어휘 밖 빈출 용어가 다수 — 동의어 정규화로 검색성/집계 개선",
                    "evidence": {"top_terms": terms},
                    "action_hint": "GET /knowledge/ontology/review 후 POST /knowledge/ontology/synonym",
                })
        except Exception:
            pass
    return out


def _build_recommender(resolved):
    """cron/오프라인용 추천기 — 서버 없이 군집화(suggest)에 쓸 임베딩 보유. 리랭커 off(비용 0).

    **임베딩 백엔드·모델은 서버와 같아야 한다.** 예전에는 이 인자를 넘기지 않아 오프라인
    루프가 로컬 MiniLM 으로, 서버는 bge-m3(API)로 KB 를 봤다. 유사도 공간이 달라지니
    같은 임계값(0.85/0.60)에 같은 detect() 를 써도 결론이 갈렸다 — 대시보드는
    "모순 없음", 개선 큐는 "모순 1건(LSI-210|LSI-211)" 을 동시에 보여줬다.
    군집·모순·온톨로지 제안 전부가 **사용자가 실제로 검색하는 공간이 아닌 곳**에서
    계산되고 있었다. 리랭커만 끈다(비용) — 이건 순위 재배열이라 유사도 공간을 안 바꾼다.
    """
    import os as _os
    from recommender import Recommender
    kw = {kw_: float(_os.environ[env]) for kw_, env in
          (("gate_cos", "RVP_GATE_COS"), ("boost", "RVP_BOOST")) if _os.getenv(env)}
    return Recommender(resolved, method=_os.getenv("RVP_RECO_METHOD", "hybrid_embed"),
                       rerank=False, **embed_kwargs(), **kw)


def run_full(save: bool = True) -> dict:
    """cron 진입점 — 측정·진단(L1) + 지식 변경 제안(L3) 큐 병합. 서버 불필요, 지식 불변.

    L2(파라미터 적용)·L3 실행은 포함하지 않는다 — 사람이 검토 후 적용/실행한다.
    """
    import improve_queue
    resolved = _resolved_kb()
    out = run(save=save)                       # 측정·진단·리포트(L1)
    suggestions, synced = [], {"added": 0, "counts": improve_queue.counts()}
    try:
        reco = _build_recommender(resolved)
        suggestions = suggest(reco=reco, records=resolved)
        synced = improve_queue.sync(suggestions)
    except Exception as e:
        out["suggest_error"] = str(e)[:160]
    out["suggestions"] = {"generated": len(suggestions), **synced}
    return out


def main() -> int:
    out = run_full()
    m = out["metrics"]
    print(f"[self_improve] {m['ts']} — 유용성 {m['helpful_rate']} · 공백 {m['gap_events']} · "
          f"큐레이션 {m['curated']} · 기사 {m['known_issues']}")
    for r in out["recommendations"]:
        print(f"  진단 [{r['priority']}] {r['area']}: {r['action']}")
    s = out["suggestions"]
    print(f"[self_improve] 제안 생성 {s['generated']} · 신규 {s.get('added', 0)} · 큐 {s.get('counts')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
