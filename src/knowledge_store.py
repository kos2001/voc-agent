"""큐레이션 지식 영속 저장소 — 사람이 검증·승인한 RCA를 '지속·공유 가능한 자산'으로.

배경(지식자산화 갭 P1-1):
  기존엔 승인된 RCA의 큐레이션 지식이 `tmp_db/rca_feedback.json`(gitignore)에만
  존재 → 단일 머신, 백업·버전·공유 없음. 머신 분실 = 자산 소실.

해결:
  - 저장 경로를 **커밋 추적되는** `data/knowledge_store.json`으로 둔다.
    → git 이력 = 버전관리 + 원격 백업 + 팀 공유 + PR 디프로 지식 변경 리뷰.
  - 레코드마다 스키마 검증(품질 게이트) + 내용 해시(무결성) + 출처(comment_id,
    source_issue, author) + 타임스탬프(스키마/갱신).
  - Jira 환류: 승인 시 여기에 적재하고, 유실 시 Jira 봇 댓글에서 재구성 가능
    (rebuild_from_jira). Jira(조직 SoT) + git 두 곳에 이중 보존.

KB 형식 변환(kb_records)은 recommender가 바로 쓰도록 rca_feedback과 동일 shape.
"""
from __future__ import annotations

import hashlib
import os
import re

import issue_keys
from pathlib import Path

from json_store import read_json, write_json_atomic, now_iso as _now

ROOT = Path(__file__).resolve().parent.parent
STORE_FILE = ROOT / "data" / "knowledge_store.json"
SCHEMA_VERSION = 1

# 적재 품질 게이트 — 이 필드들이 비면 큐레이션 지식으로서 가치가 없어 거부한다.
# (summary는 Jira 재구성 시 댓글에 없을 수 있어 필수에서 제외 — 본문이 핵심 자산)
REQUIRED = ("key", "source_issue", "content_text")


# --------------------------------------------------------------------------- #
# 저장/적재 (원자적 쓰기)
# --------------------------------------------------------------------------- #
def _load_envelope() -> dict:
    d = read_json(STORE_FILE, None)
    if isinstance(d, dict) and isinstance(d.get("records"), list):
        return d
    return {"schema_version": SCHEMA_VERSION, "records": []}


def _save_envelope(env: dict) -> None:
    write_json_atomic(STORE_FILE, env)


def records() -> list[dict]:
    return _load_envelope()["records"]


def _hash(text: str) -> str:
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:12]


def _section(body: str, *titles: str) -> str:
    """본문에서 섹션 추출 — 마크다운('### 제목') / Jira wiki('h3. 제목') 모두 지원."""
    for t in titles:
        m = re.search(rf"(?:^|\n)(?:#{{1,6}}|h[1-6]\.)[^\n]*{re.escape(t)}[^\n]*\n+(.+?)"
                      rf"(?=\n(?:#{{1,6}}|h[1-6]\.)|\Z)", body, flags=re.S)
        if m:
            return m.group(1).strip()
    return ""


# --------------------------------------------------------------------------- #
# 쓰기 — 승인 시 호출
# --------------------------------------------------------------------------- #
def upsert(source_issue: str, summary: str, content_text: str, *,
           comment_id: str = "", citations: list | None = None,
           category: str = "", template: str = "", symptom: str = "", chip: str = "",
           author: str = "", approved_at: str = "") -> dict:
    """큐레이션 지식 1건 적재/갱신. key는 '{원본이슈}-rca'(원본 KB와 분리).

    품질 게이트: REQUIRED 결측 시 ValueError. 동일 key는 내용 해시가 다를 때만 갱신.
    """
    key = f"{source_issue}-rca"
    rec = {
        "key": key, "source_issue": source_issue, "comment_id": str(comment_id or ""),
        "summary": summary or "", "category": category or "", "template": template or "",
        "symptom": symptom or "", "chip": chip or "", "citations": list(citations or []),
        "root_cause": _section(content_text, "예상 근본원인", "근본 원인", "근본원인") or content_text[:600],
        "resolution": _section(content_text, "권장 해결", "적용 해결", "해결책", "권장 해결 단계"),
        "workaround": _section(content_text, "임시 우회책", "우회책"),
        "content_text": content_text or "",
        "author": author or "", "approved_at": approved_at or _now(),
        "updated_at": _now(), "content_hash": _hash(content_text),
        "schema_version": SCHEMA_VERSION, "verified": True, "curated": True,
    }
    missing = [f for f in REQUIRED if not str(rec.get(f, "")).strip()]
    if missing:
        raise ValueError(f"지식 품질 게이트 위반 — 필수 결측: {missing}")

    env = _load_envelope()
    recs = env["records"]
    for i, r in enumerate(recs):
        if r.get("key") == key:
            if r.get("content_hash") == rec["content_hash"]:
                return r  # 동일 내용 → no-op
            rec["approved_at"] = r.get("approved_at", rec["approved_at"])  # 최초 승인일 보존
            recs[i] = rec
            _save_envelope(env)
            return rec
    recs.append(rec)
    _save_envelope(env)
    return rec


# --------------------------------------------------------------------------- #
# 읽기 — recommender KB 환류용
# --------------------------------------------------------------------------- #
def kb_records() -> list[dict]:
    """영속 큐레이션 지식 → recommender KB 레코드(verified=True 가중). rca_feedback과 동일 shape."""
    out = []
    for e in records():
        out.append({
            "key": e["key"], "summary": e.get("summary", ""), "status": "완료",
            "priority": "", "labels": [], "components": [],
            "chip": e.get("chip", ""), "category": e.get("category", ""),
            "severity": "", "customer": "", "fw_version": "",
            "symptom": e.get("symptom", ""), "debug_approach": "",
            "root_cause": e.get("root_cause", ""), "resolution": e.get("resolution", ""),
            "workaround": e.get("workaround", ""), "investigation": "",
            "verified": True, "context_text": e.get("content_text", ""),
            "entities": [], "curated": True,
        })
    return out


def stats() -> dict:
    recs = records()
    return {
        "total": len(recs),
        "with_comment_id": sum(1 for r in recs if r.get("comment_id")),
        "store_path": str(STORE_FILE.relative_to(ROOT)),
        "tracked_in_git": True,  # data/ 하위 — gitignore 대상 아님
    }


# --------------------------------------------------------------------------- #
# 마이그레이션 — 기존 rca_feedback(tmp_db) → 영속 저장소 1회 이전
# --------------------------------------------------------------------------- #
def migrate_from_feedback() -> int:
    """tmp_db/rca_feedback.json 의 큐레이션 지식을 영속 저장소로 이전(중복 무시)."""
    import rca_feedback
    n = 0
    for e in rca_feedback.kb_records():
        src = e["key"][:-4] if e["key"].endswith("-rca") else e["key"]
        try:
            upsert(src, e.get("summary", ""), e.get("context_text", "") or e.get("root_cause", ""),
                   category=e.get("category", ""), symptom=e.get("symptom", ""),
                   chip=e.get("chip", ""))
            n += 1
        except ValueError:
            continue
    return n


# --------------------------------------------------------------------------- #
# Jira 환류 — 봇 댓글에서 지식 자산 재구성(재해 복구 / 머신 간 동기화)
# --------------------------------------------------------------------------- #
def rebuild_from_jira(bot_marker: str, project: str | None = None,
                      max_workers: int = 8) -> dict:
    """프로젝트 전 이슈의 봇 댓글(bot_marker 포함)을 스캔해 지식 자산을 재구성한다.

    로컬 저장소/머신 유실 시 Jira(조직 SoT)에서 큐레이션 지식을 복원하는 경로.
    """
    from concurrent.futures import ThreadPoolExecutor
    from ingest import jira_session, _all_keys
    from jira_commenter import get_comments

    s, base = jira_session()
    proj = project or os.getenv("JIRA_PROJECT_KEY", "LSI")
    keys = [k for k, _st in _all_keys(s, base, proj)]
    # 복구는 누락분만 채운다 — 승인 시 적재된 풍부한 레코드(마크다운 정본·summary)를
    # Jira wiki 본문/빈 summary로 덮어쓰지 않도록 기존 key는 건너뛴다.
    existing = {r.get("key") for r in records()}

    def _scan(issue_key: str):
        found = []
        try:
            for c in get_comments(issue_key):
                if bot_marker in (c.get("body", "")[:200]):
                    found.append((issue_key, c))
        except Exception:
            pass
        return found

    restored, scanned = 0, 0
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        for hits in ex.map(_scan, keys):
            scanned += 1
            for issue_key, c in hits:
                if f"{issue_key}-rca" in existing:
                    continue  # 이미 보유 — 덮어쓰지 않음(누락분만 복구)
                body = c.get("body", "")
                cites = sorted(issue_keys.find_set(body))
                try:
                    upsert(issue_key, "", body, comment_id=str(c.get("id", "")),
                           citations=cites, author=c.get("author", ""),
                           approved_at=c.get("created", ""))
                    restored += 1
                except ValueError:
                    continue
    return {"issues_scanned": scanned, "records_restored": restored, "total": len(records())}
