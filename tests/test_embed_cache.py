"""파생 임베딩 캐시 검증 — KB 가 바뀔 때 바뀐 것만 다시 임베딩하는가.

실제로 났던 문제: 캐시 키가 `md5(모든 텍스트를 이어붙인 것)` 이었다. 코퍼스 전체가
한 덩어리라 **레코드 하나만 바뀌어도** 전체가 무효가 되고 KB 전량을 다시 임베딩했다.
/knowledge/contradictions 가 RCA 승인·Jira 폴링 변경 때마다 7.3초를 냈다(실측).
대시보드는 그 카드 하나 때문에 매번 멈췄다.

여기서 지키는 계약:
  · 같은 입력이면 두 번째 호출은 임베더를 부르지 않는다
  · 텍스트 하나가 바뀌면 **그 하나만** 임베딩한다 (나머지 벡터는 그대로)
  · 캐시는 디스크에 남아 새 인스턴스·재기동에서도 재사용된다
  · 같은 텍스트가 여러 번 나오면 한 번만 임베딩한다
  · 반환 순서는 입력 순서와 같다 (캐시 적중 순서가 아니라)
  · 빈 입력에 죽지 않는다

실행:
    .venv/bin/python tests/test_embed_cache.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import numpy as np  # noqa: E402
import recommender as R  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'✓' if cond else '✗'} {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


class FakeReco:
    """embed_cached 만 떼어 검증 — 임베더 호출 횟수를 센다.

    실제 Recommender 를 쓰면 임베딩 모델 적재(수 초)와 네트워크가 끼어들어, 정작
    재는 대상인 '캐시가 무엇을 다시 계산하는가' 가 가려진다.
    """
    embed_backend = "fastembed"
    _np = np

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def _model_name(self) -> str:
        return "test-model"

    def _embed_texts(self, texts, is_query=False):
        self.calls.append(list(texts))
        # 결정론적 가짜 벡터 — 텍스트가 다르면 벡터도 달라야 비교가 의미 있다.
        return np.asarray([[float(len(t)), float(sum(map(ord, t[:8])))] for t in texts],
                          dtype=np.float32)

    embed_cached = R.Recommender.embed_cached


def fresh(tag: str) -> tuple[FakeReco, str]:
    """태그를 매번 새로 만들어 이전 테스트의 캐시 파일과 겹치지 않게 한다."""
    return FakeReco(), tag


TEXTS = ["근본원인 A 입니다", "근본원인 B 입니다", "근본원인 C 입니다"]


def test_warm_hit_skips_embedder() -> None:
    r, tag = fresh("t_warm")
    a = r.embed_cached(TEXTS, tag=tag)
    n1 = len(r.calls)
    b = r.embed_cached(TEXTS, tag=tag)
    check("두 번째 호출은 임베더를 부르지 않는다", len(r.calls) == n1, f"{n1} → {len(r.calls)}")
    check("같은 결과", np.allclose(a, b))
    check("모양 보존", a.shape == (3, 2), str(a.shape))


def test_single_change_reembeds_only_that_one() -> None:
    r, tag = fresh("t_delta")
    a = r.embed_cached(TEXTS, tag=tag)
    r.calls.clear()
    changed = list(TEXTS)
    changed[1] = "근본원인 B 를 정정했습니다"
    b = r.embed_cached(changed, tag=tag)
    embedded = r.calls[0] if r.calls else []
    check("바뀐 1건만 임베딩", embedded == [changed[1]], str(embedded))
    check("바뀌지 않은 항목의 벡터는 그대로",
          np.allclose(a[0], b[0]) and np.allclose(a[2], b[2]))
    check("바뀐 항목의 벡터는 달라짐", not np.allclose(a[1], b[1]))


def test_disk_cache_survives_new_instance() -> None:
    r1, tag = fresh("t_disk")
    a = r1.embed_cached(TEXTS, tag=tag)
    r2 = FakeReco()                      # 재기동에 해당 — 프로세스 메모리는 비었다
    b = r2.embed_cached(TEXTS, tag=tag)
    check("새 인스턴스가 디스크 캐시를 재사용", r2.calls == [], str(r2.calls))
    check("결과 동일", np.allclose(a, b))


def test_duplicates_embedded_once() -> None:
    r, tag = fresh("t_dup")
    dup = ["같은 문장", "다른 문장", "같은 문장"]
    out = r.embed_cached(dup, tag=tag)
    embedded = r.calls[0]
    check("중복 텍스트는 한 번만 임베딩", len(embedded) == 2, str(embedded))
    check("중복 위치에 같은 벡터", np.allclose(out[0], out[2]))
    check("반환 순서는 입력 순서", out.shape == (3, 2) and not np.allclose(out[0], out[1]))


def test_order_preserved_on_partial_hit() -> None:
    """일부만 캐시에 있을 때 반환 순서가 입력 순서와 같은가(적중 순서가 아니라)."""
    r, tag = fresh("t_order")
    r.embed_cached(["B"], tag=tag)       # B 만 미리 캐시
    out = r.embed_cached(["A", "B", "C"], tag=tag)
    expect = np.asarray([[1.0, float(ord("A"))], [1.0, float(ord("B"))],
                         [1.0, float(ord("C"))]], dtype=np.float32)
    check("부분 적중에도 순서 보존", np.allclose(out, expect), str(out.tolist()))


def test_empty_input() -> None:
    r, tag = fresh("t_empty")
    out = r.embed_cached([], tag=tag)
    check("빈 입력에 죽지 않는다", out.shape[0] == 0, str(out.shape))
    check("빈 입력은 임베더를 부르지 않는다", r.calls == [])


def main() -> int:
    # 캐시 파일은 tmp_db 에 쌓인다 — 테스트 전용 태그를 쓰고 끝나면 지운다.
    cache_dir = ROOT / "tmp_db"
    made = sorted(cache_dir.glob("embc_t_*.npz"))
    for f in made:
        f.unlink()
    for fn in (test_warm_hit_skips_embedder, test_single_change_reembeds_only_that_one,
               test_disk_cache_survives_new_instance, test_duplicates_embedded_once,
               test_order_preserved_on_partial_hit, test_empty_input):
        print(f"\n— {fn.__name__}")
        fn()
    for f in sorted(cache_dir.glob("embc_t_*.npz")):
        f.unlink()
    print()
    if FAILS:
        print(f"실패 {len(FAILS)}건: {FAILS}")
        return 1
    print("모두 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
