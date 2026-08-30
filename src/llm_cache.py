"""LLM 생성물 캐시 — 같은 입력이면 다시 만들지 않는다.

문제: AI 심층 분석(설명)은 질의당 수 초 + API 비용인데, 이슈도 근거 사례도 그대로인
상태로 사용자가 화면을 다시 열 때마다 새로 생성했다.

키는 **내용 주소**(content-addressed)다. 전역 "KB 버전" 같은 것을 쓰지 않고
생성에 실제로 들어간 것만 해싱한다:

    질의 이슈의 내용 + 근거 사례들의 내용 + 모델 + 프롬프트 버전

이렇게 하면 무관한 이슈가 하나 바뀌었다고 캐시가 통째로 날아가지 않고, 반대로
근거 사례의 근본원인이 수정되면 그 항목만 자연히 키가 달라져 재생성된다.

저장은 키마다 파일 하나(tmp_db/llm_cache/<prefix>/<key>.json). 단일 JSON 파일은
동시 쓰기에서 서로를 덮어쓰고, 커지면 매번 전체를 읽어야 한다.

PROMPT_VERSION: 프롬프트 문구나 출력 형식을 바꾸면 반드시 올린다 — 안 그러면
옛 형식의 캐시가 계속 나간다.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "tmp_db" / "llm_cache"

PROMPT_VERSION = "2026-08-30.1"  # 심층 분석 → 에이전트 고객 응대 답변으로 전환

# 캐시 유효기간(초). 0 이면 무기한. 내용이 바뀌면 키가 달라지므로 만료는 보조 장치일
# 뿐이다 — 모델 쪽이 조용히 바뀌는 경우를 대비한 안전망.
TTL_SEC = int(os.getenv("RVP_LLM_CACHE_TTL", "0") or 0)


def _norm(v) -> str:
    return " ".join(str(v or "").split())


def issue_fingerprint(rec: dict) -> str:
    """이슈에서 생성 결과에 영향을 주는 필드만 골라 지문을 만든다.

    status/created 처럼 생성물에 안 들어가는 필드는 제외 — 넣으면 무의미한
    캐시 미스가 난다.
    """
    parts = [rec.get("key", "")]
    for f in ("summary", "symptom", "chip", "category",
              "debug_approach", "root_cause", "resolution", "workaround",
              "investigation"):
        parts.append(f"{f}={_norm(rec.get(f))}")
    return "|".join(parts)


def make_key(kind: str, query_rec: dict, evidence: list[dict], model: str,
             extra: str = "") -> str:
    """kind(생성 종류) + 질의 + 근거 + 모델 + 프롬프트 버전 → 캐시 키."""
    payload = "\n".join([
        f"kind={kind}", f"prompt={PROMPT_VERSION}", f"model={model}",
        f"extra={extra}",
        "query=" + issue_fingerprint(query_rec),
        *(f"evidence={issue_fingerprint(r)}" for r in evidence),
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _path(key: str) -> Path:
    return CACHE_DIR / key[:2] / f"{key}.json"


def get(key: str) -> dict | None:
    p = _path(key)
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if TTL_SEC and time.time() - float(d.get("created_at", 0)) > TTL_SEC:
        return None
    return d.get("value")


def put(key: str, value: dict, meta: dict | None = None) -> None:
    p = _path(key)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(
            {"created_at": time.time(), "meta": meta or {}, "value": value},
            ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)                      # 원자적 교체 — 반쯤 쓰인 파일을 읽지 않게
    except OSError:
        pass                                # 캐시 쓰기 실패는 치명적이지 않다


def stats() -> dict:
    n = 0
    size = 0
    newest = 0.0
    for f in CACHE_DIR.rglob("*.json"):
        try:
            st = f.stat()
        except OSError:
            continue
        n += 1
        size += st.st_size
        newest = max(newest, st.st_mtime)
    return {"entries": n, "bytes": size,
            "newest_at": newest or None,
            "dir": str(CACHE_DIR.relative_to(ROOT)),
            "prompt_version": PROMPT_VERSION,
            "ttl_sec": TTL_SEC}


def clear() -> int:
    """캐시 전체 삭제. 반환: 지운 항목 수."""
    n = 0
    for f in CACHE_DIR.rglob("*.json"):
        try:
            f.unlink()
            n += 1
        except OSError:
            pass
    return n
