"""KB 원천 로더 — "지식베이스는 어떤 파일들로 이루어지는가"의 **단일 소스**.

왜 필요한가:
  이 서비스의 목표가 'Jira 로 들어오는 VOC 에 답하기' 로 옮겨가면서, KB 에는 Jira
  미러 말고도 다른 원천(VOC 목 데이터, 마이그레이션한 과거 지식 등)이 들어와야 한다.
  그런데 원천 경로가 `backend/server.py`·`src/self_improve.py`·평가 스크립트에 각각
  하드코딩돼 있었다. 한 곳만 바꾸면 **서버와 자기개선 loop 가 다른 KB 를 본다** —
  이 저장소가 이미 한 번 당한 실패 유형이다(대시보드는 "모순 없음", 개선 큐는
  "모순 1건"). 그래서 로더를 하나로 모은다.

왜 Jira 미러에 섞지 않는가(중요):
  `src/jira_sync.py` 는 `removed = known - live` 로 삭제를 대조한다. `known` 은
  `all_raw_issues.json` 의 전체 키다. 여기에 Jira 에 없는 키(VOC-1 등)를 넣으면
  다음 대조 회차에 **전부 삭제된다**. 미러는 jira_sync 가 소유하는 파일이고,
  보조 지식은 별도 파일로 두고 **읽을 때만 합친다**.

설정:
  RVP_KB_EXTRA — 보조 원천 파일 목록(콤마 구분, ROOT 상대 또는 절대 경로).
                 미설정이면 미러만 쓴다(기존 동작 그대로).
  예) RVP_KB_EXTRA=data/voc_mock_issues.json
"""
from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MIRROR = ROOT / "data" / "all_raw_issues.json"   # jira_sync 가 소유 — 쓰지 말 것
ENV_EXTRA = "RVP_KB_EXTRA"

# 해결 상태 이름은 **Jira 워크플로우에 종속**된다(한글 team-managed 는 "완료",
# 영문은 "Done"). 여러 모듈이 각자 "완료" 를 박아두면 워크플로우가 다른 조직에서
# KB 가 통째로 '미해결' 로 잡히고, 추천 근거가 0건이 된다 — 조용히 아무것도 못 찾는다.
RESOLVED_STATUS = os.getenv("RVP_RESOLVED_STATUS", "완료")


def extra_paths() -> list[Path]:
    """보조 원천 경로 목록. 존재하지 않는 경로도 그대로 돌려준다 — 조용히 빠지면
    "왜 VOC 지식이 안 보이지" 를 추적할 수 없다. 존재 여부는 sources() 가 알린다."""
    raw = os.getenv(ENV_EXTRA, "").strip()
    if not raw:
        return []
    out = []
    for part in raw.replace(";", ",").split(","):
        p = part.strip()
        if not p:
            continue
        path = Path(p)
        out.append(path if path.is_absolute() else ROOT / path)
    return out


def _read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    return d if isinstance(d, list) else []


def raw_issues() -> list[dict]:
    """미러 + 보조 원천을 합친 raw 이슈 목록.

    키 충돌 시 **미러가 이긴다** — Jira 가 정본이고, 보조 파일은 보강이다.
    보조 파일끼리 겹치면 앞선 파일이 이긴다(목록 순서가 우선순위).
    """
    out = _read(MIRROR)
    seen = {r.get("key") for r in out if r.get("key")}
    for p in extra_paths():
        for r in _read(p):
            k = r.get("key")
            if not k or k in seen:
                continue
            seen.add(k)
            out.append(r)
    return out


def sources() -> list[dict]:
    """원천별 현황 — 지식 현황 화면·API 가 "무엇이 KB 를 이루는가" 를 보여주기 위한 것.

    건수만이 아니라 **해결/미해결**을 나눠 센다. 추천의 근거가 되는 것은 해결 사례뿐이라,
    "500건 있다" 보다 "해결 320건이 근거로 쓰인다" 가 실제로 알아야 할 숫자다.
    """
    def _summarize(path: Path, kind: str) -> dict:
        items = _read(path)
        resolved = sum(1 for r in items if r.get("status") == RESOLVED_STATUS)
        return {
            "kind": kind,
            "path": _rel(path),
            "present": path.exists(),
            "total": len(items),
            "resolved": resolved,
            "unresolved": len(items) - resolved,
            "key_prefixes": sorted({str(r.get("key", "")).rsplit("-", 1)[0]
                                    for r in items if r.get("key")})[:5],
        }
    out = [_summarize(MIRROR, "jira_mirror")]
    out += [_summarize(p, "extra") for p in extra_paths()]
    return out


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def status() -> dict:
    """KB 원천 요약 — 합계는 중복 제거 후 실제 적재 기준으로 센다.

    파일별 건수를 그냥 더하면 키가 겹칠 때 실제보다 많게 나온다. 지식 '현황' 이
    실제 적재량과 다르면 그 화면은 신뢰를 잃는다.
    """
    merged = raw_issues()
    resolved = sum(1 for r in merged if r.get("status") == RESOLVED_STATUS)
    srcs = sources()
    return {
        "sources": srcs,
        "total": len(merged),
        "resolved": resolved,
        "unresolved": len(merged) - resolved,
        "extra_configured": bool(extra_paths()),
        "missing_sources": [s["path"] for s in srcs if not s["present"]],
        "env": ENV_EXTRA,
    }
