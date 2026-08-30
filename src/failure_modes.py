"""고장모드(Known-Issue) 기사 계층 — 중복 사례를 정규 지식 기사로 묶는다.

배경(지식자산화 갭 P2-4):
  동일 근본 고장의 중복 티켓이 평면 레코드로만 존재했다. 성숙한 지식자산은
  incident(사례) → 큐레이션 기사(Known-Issue)로 승격되며, 사례들은 그 기사에
  instances-of로 링크된다.

기능:
  - cluster_*: 해결 KB 임베딩을 코사인 임계로 군집화해 '고장모드 후보'를 도출
    (recommender의 _kb_emb 자산 재사용 — 재계산 없음, 의존성은 numpy만).
  - 기사 저장소(data/known_issues.json, git 추적 — P1-1과 동일하게 버전·백업·공유):
    사람이 후보 군집을 정규 기사로 승격(promote), 사례를 members로 링크.
  - annotate/ member_index: 추천 매치에 소속 기사를 주석 → UI가 기사 단위로 묶어 노출.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from json_store import read_json, write_json_atomic, now_iso as _now

ROOT = Path(__file__).resolve().parent.parent
STORE_FILE = ROOT / "data" / "known_issues.json"
SCHEMA_VERSION = 1


# --------------------------------------------------------------------------- #
# 클러스터링 — 고장모드 후보 도출
# --------------------------------------------------------------------------- #
def _connected_components(adj: list[set]) -> list[list[int]]:
    """임계 초과 간선 그래프의 연결요소(고장모드 후보 군집). 반복 DFS."""
    n = len(adj)
    seen = [False] * n
    comps = []
    for s in range(n):
        if seen[s]:
            continue
        stack, comp = [s], []
        seen[s] = True
        while stack:
            u = stack.pop()
            comp.append(u)
            for v in adj[u]:
                if not seen[v]:
                    seen[v] = True
                    stack.append(v)
        comps.append(comp)
    return comps


def cluster(emb, keys: list[str], records: list[dict], *,
            threshold: float = 0.80, min_size: int = 2) -> list[dict]:
    """임베딩 코사인 ≥ threshold 간선의 연결요소로 군집. min_size 이상만 반환.

    각 군집: members(키), size, representative(검증 우선·medoid), chips/categories,
    sample_summaries. 의존성은 numpy만(소규모 KB 기준 O(N^2) 충분).
    """
    if emb is None or len(keys) < min_size:
        return []
    X = np.asarray(emb, dtype=np.float32)
    norm = np.linalg.norm(X, axis=1, keepdims=True) + 1e-9
    Xn = X / norm
    sim = Xn @ Xn.T                                   # 코사인 유사도 행렬
    n = len(keys)
    adj = [set() for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            if sim[i, j] >= threshold:
                adj[i].add(j)
                adj[j].add(i)

    by_key = {r["key"]: r for r in records}
    out = []
    for comp in _connected_components(adj):
        if len(comp) < min_size:
            continue
        members = [keys[i] for i in comp]
        recs = [by_key.get(k, {}) for k in members]
        # 대표(representative): 검증된 사례 우선, 그다음 군집 내 평균 유사도 최대(medoid)
        idx = comp
        sub = sim[np.ix_(idx, idx)]
        medoid_local = int(np.argmax(sub.sum(axis=1)))
        verified = [k for k, r in zip(members, recs) if r.get("verified")]
        representative = verified[0] if verified else members[medoid_local]
        out.append({
            "members": members,
            "size": len(members),
            "representative": representative,
            "chips": sorted({r.get("chip", "") for r in recs if r.get("chip")}),
            "categories": sorted({r.get("category", "") for r in recs if r.get("category")}),
            "verified_count": len(verified),
            "avg_similarity": round(float((sub.sum() - len(idx)) / max(len(idx) * (len(idx) - 1), 1)), 3),
            "sample_summaries": [{"key": k, "summary": r.get("summary", "")}
                                 for k, r in list(zip(members, recs))[:5]],
        })
    out.sort(key=lambda c: -c["size"])
    return out


def cluster_from_recommender(reco, *, threshold: float = 0.80, min_size: int = 2) -> list[dict]:
    """recommender의 임베딩 자산(_kb_emb/_keys/kb)으로 군집화. 임베딩 없으면 빈 리스트."""
    emb = getattr(reco, "_kb_emb", None)
    keys = getattr(reco, "_keys", None)
    kb = getattr(reco, "kb", None)
    if emb is None or not keys:
        return []
    clusters = cluster(emb, keys, kb, threshold=threshold, min_size=min_size)
    midx = member_index()
    for c in clusters:                                # 이미 승격된 기사 표시
        ids = {midx[k] for k in c["members"] if k in midx}
        c["known_issue_ids"] = sorted(ids)
        c["promoted"] = bool(ids)
    return clusters


# --------------------------------------------------------------------------- #
# Known-Issue 기사 저장소 (git 추적, 영속)
# --------------------------------------------------------------------------- #
def _load_envelope() -> dict:
    d = read_json(STORE_FILE, None)
    if isinstance(d, dict) and isinstance(d.get("articles"), list):
        return d
    return {"schema_version": SCHEMA_VERSION, "articles": []}


def _save_envelope(env: dict) -> None:
    write_json_atomic(STORE_FILE, env)


def articles() -> list[dict]:
    return _load_envelope()["articles"]


def member_index() -> dict:
    """issue_key → article_id (추천 매치 주석용 역색인)."""
    idx = {}
    for a in articles():
        for m in a.get("members", []):
            idx[m] = a["id"]
    return idx


def _next_id(env: dict) -> str:
    nums = [int(a["id"].split("-")[-1]) for a in env["articles"]
            if a.get("id", "").startswith("KI-") and a["id"].split("-")[-1].isdigit()]
    return f"KI-{(max(nums) + 1) if nums else 1}"


def promote(*, title: str, members: list[str], failure_summary: str = "",
            root_cause: str = "", resolution: str = "", workaround: str = "",
            chips: list | None = None, categories: list | None = None,
            author: str = "", article_id: str = "") -> dict:
    """후보 군집(또는 선택 사례)을 정규 Known-Issue 기사로 승격/갱신.

    article_id 지정 시 갱신(멤버 합집합), 아니면 신규 생성. title·members 필수.
    """
    if not (title or "").strip():
        raise ValueError("title 필수")
    members = sorted(set(m for m in (members or []) if m))
    if not members:
        raise ValueError("members 최소 1건 필요")
    env = _load_envelope()
    arts = env["articles"]
    existing = next((a for a in arts if a.get("id") == article_id), None) if article_id else None
    now = _now()
    if existing:
        existing.update({
            "title": title, "failure_summary": failure_summary or existing.get("failure_summary", ""),
            "root_cause": root_cause or existing.get("root_cause", ""),
            "resolution": resolution or existing.get("resolution", ""),
            "workaround": workaround or existing.get("workaround", ""),
            "members": sorted(set(existing.get("members", []) + members)),
            "chips": sorted(set((existing.get("chips") or []) + (chips or []))),
            "categories": sorted(set((existing.get("categories") or []) + (categories or []))),
            "updated_at": now,
        })
        _save_envelope(env)
        return existing
    art = {
        "id": _next_id(env), "title": title.strip(),
        "failure_summary": failure_summary or "", "root_cause": root_cause or "",
        "resolution": resolution or "", "workaround": workaround or "",
        "members": members, "chips": sorted(chips or []), "categories": sorted(categories or []),
        "author": author or "", "created_at": now, "updated_at": now,
        "schema_version": SCHEMA_VERSION,
    }
    arts.append(art)
    _save_envelope(env)
    return art


def set_canonical_reply(article_id: str, body: str, *, from_key: str = "",
                        author: str = "") -> dict | None:
    """이 유형의 **정본 고객 답변**을 기록한다 — 사람이 검토·발송한 그 글.

    왜 기사에 붙이나: 같은 유형의 문의가 반복되면 답변도 같아야 한다. 매번 새로
    지어내면 고객사마다 다른 말을 듣게 되고, 사람이 고쳐 놓은 표현이 다음 답변에
    남지 않는다. 기사는 '같은 고장' 을 묶는 유일한 축이므로 정본이 붙을 자리다.

    덮어쓴다 — 가장 최근에 **발송된** 것이 정본이다. 옛 판본은 발송 큐의 이력에
    남아 있으므로 여기서 또 쌓지 않는다.
    """
    if not (body or "").strip():
        return None
    env = _load_envelope()
    art = next((a for a in env["articles"] if a.get("id") == article_id), None)
    if art is None:
        return None
    art["canonical_reply"] = {"body": body.strip(), "from_key": from_key,
                              "author": author, "updated_at": _now()}
    art["updated_at"] = _now()
    _save_envelope(env)
    return art


def canonical_reply_for(keys) -> tuple[str, str]:
    """근거 키들이 속한 기사의 정본 답변. 반환 (본문, 기사 id). 없으면 ("", "").

    첫 번째로 찾은 것을 쓴다 — 근거가 여러 기사에 걸치면 최상위 근거의 유형을
    따르는 것이 맞다(정렬은 호출부가 관련도 순으로 준다).
    """
    idx = member_index()
    for k in keys or []:
        aid = idx.get(k)
        if not aid:
            continue
        art = get(aid) or {}
        body = (art.get("canonical_reply") or {}).get("body", "")
        if body:
            return body, aid
    return "", ""


def get(article_id: str) -> dict | None:
    return next((a for a in articles() if a.get("id") == article_id), None)


def annotate(matches: list[dict]) -> list[dict]:
    """추천 매치에 소속 Known-Issue 기사(id/title) 주석을 단다(원본 변형, 반환도 동일)."""
    if not matches:
        return matches
    idx = member_index()
    by_id = {a["id"]: a for a in articles()}
    for m in matches:
        aid = idx.get(m.get("key"))
        if aid and aid in by_id:
            m["known_issue"] = {"id": aid, "title": by_id[aid].get("title", "")}
    return matches


def stats() -> dict:
    arts = articles()
    return {
        "total_articles": len(arts),
        "total_linked_cases": sum(len(a.get("members", [])) for a in arts),
        "store_path": str(STORE_FILE.relative_to(ROOT)),
    }
