"""KB 원천 로더 검증 — 지식 현황이 실제 적재와 일치하는가.

여기서 지키는 계약:
  · 보조 원천(RVP_KB_EXTRA)이 미러와 합쳐진다 — VOC 지식이 KB 에 들어오는 경로
  · 키가 겹치면 **미러가 이긴다** (Jira 가 정본, 보조 파일은 보강)
  · 합계는 **중복 제거 후 실제 적재 기준** — 파일별 건수를 더하면 실제보다 많다
  · 없는 보조 경로는 조용히 빠지지 않고 missing_sources 로 드러난다
  · 미설정이면 미러만 — 기존 동작이 바뀌지 않는다
  · 로더는 미러 파일을 **쓰지 않는다** (jira_sync 소유 — 섞으면 삭제 대조가 지운다)

실행:
    .venv/bin/python tests/test_kb_source.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import kb_source as K  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'✓' if cond else '✗'} {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def _issue(key: str, status: str = "완료", summary: str = "s") -> dict:
    return {"key": key, "summary": summary, "status": status,
            "description": "", "comments": [], "labels": [], "components": []}


def _write(path: Path, items: list[dict]) -> None:
    path.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")


def setup() -> Path:
    """미러·보조 원천을 임시 디렉터리로 격리 — 실제 data/ 를 건드리지 않는다."""
    d = Path(tempfile.mkdtemp())
    K.MIRROR = d / "mirror.json"
    _write(K.MIRROR, [_issue("LSI-1"), _issue("LSI-2", "해야 할 일"), _issue("LSI-3")])
    os.environ.pop(K.ENV_EXTRA, None)
    return d


def test_mirror_only() -> None:
    setup()
    st = K.status()
    check("미설정이면 미러만", st["total"] == 3 and not st["extra_configured"], str(st["total"]))
    check("해결/미해결 분리", (st["resolved"], st["unresolved"]) == (2, 1), str(st))
    check("원천 1개", len(st["sources"]) == 1 and st["sources"][0]["kind"] == "jira_mirror")


def test_extra_merged() -> None:
    d = setup()
    voc = d / "voc.json"
    _write(voc, [_issue("VOC-1"), _issue("VOC-2", "해야 할 일")])
    os.environ[K.ENV_EXTRA] = str(voc)
    st = K.status()
    check("보조 원천이 합쳐짐", st["total"] == 5, str(st["total"]))
    check("보조 원천의 해결 사례가 근거로 잡힘", st["resolved"] == 3, str(st["resolved"]))
    keys = {r["key"] for r in K.raw_issues()}
    check("VOC 키가 KB 에 들어옴", {"VOC-1", "VOC-2"} <= keys, str(sorted(keys)))
    kinds = [s["kind"] for s in st["sources"]]
    check("원천 목록에 미러+보조", kinds == ["jira_mirror", "extra"], str(kinds))
    check("원천별 키 접두어 노출",
          st["sources"][1]["key_prefixes"] == ["VOC"], str(st["sources"][1]["key_prefixes"]))


def test_mirror_wins_on_conflict() -> None:
    d = setup()
    dup = d / "dup.json"
    # 같은 키를 보조 파일이 '미해결' 로 들고 있다 — 미러(완료)가 이겨야 한다.
    _write(dup, [_issue("LSI-1", "해야 할 일", "보조본"), _issue("VOC-9")])
    os.environ[K.ENV_EXTRA] = str(dup)
    by_key = {r["key"]: r for r in K.raw_issues()}
    check("키 충돌 시 미러가 이긴다(Jira 가 정본)",
          by_key["LSI-1"]["status"] == "완료" and by_key["LSI-1"]["summary"] == "s",
          str(by_key["LSI-1"]))
    st = K.status()
    check("합계는 중복 제거 후 실제 적재 기준", st["total"] == 4, str(st["total"]))
    summed = sum(s["total"] for s in st["sources"])
    check("파일별 합(5)과 실제 적재(4)가 다름을 드러냄", summed == 5 and st["total"] == 4,
          f"summed={summed} total={st['total']}")


def test_missing_source_is_visible() -> None:
    setup()
    os.environ[K.ENV_EXTRA] = "/nonexistent/voc.json"
    st = K.status()
    check("없는 경로가 조용히 빠지지 않음", st["missing_sources"] == ["/nonexistent/voc.json"],
          str(st["missing_sources"]))
    check("없는 원천이 있어도 미러는 정상 적재", st["total"] == 3, str(st["total"]))


def test_multiple_extras_order() -> None:
    d = setup()
    a, b = d / "a.json", d / "b.json"
    _write(a, [_issue("X-1", "완료", "a본")])
    _write(b, [_issue("X-1", "완료", "b본"), _issue("X-2")])
    os.environ[K.ENV_EXTRA] = f"{a},{b}"
    by_key = {r["key"]: r for r in K.raw_issues()}
    check("보조끼리 겹치면 앞선 파일이 이긴다", by_key["X-1"]["summary"] == "a본",
          by_key["X-1"]["summary"])
    check("뒤 파일의 고유 항목은 들어온다", "X-2" in by_key)


def test_loader_never_writes_mirror() -> None:
    """미러는 jira_sync 소유다. 로더가 쓰면 삭제 대조가 보조 지식을 지운다."""
    setup()
    before = K.MIRROR.read_bytes()
    d2 = Path(tempfile.mkdtemp()); voc = d2 / "v.json"
    _write(voc, [_issue("VOC-1")])
    os.environ[K.ENV_EXTRA] = str(voc)
    K.raw_issues(); K.status(); K.sources()
    check("로더 호출 후에도 미러 파일이 그대로", K.MIRROR.read_bytes() == before)


def main() -> int:
    for fn in (test_mirror_only, test_extra_merged, test_mirror_wins_on_conflict,
               test_missing_source_is_visible, test_multiple_extras_order,
               test_loader_never_writes_mirror):
        print(f"\n— {fn.__name__}")
        fn()
    os.environ.pop(K.ENV_EXTRA, None)
    print()
    if FAILS:
        print(f"실패 {len(FAILS)}건: {FAILS}")
        return 1
    print("모두 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
