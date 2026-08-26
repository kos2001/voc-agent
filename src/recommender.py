"""해결책 추천기 — 과거 *해결된* 이슈로 미해결 이슈의 root-cause/해결책을 제안.

입력 질의(미해결 이슈의 관찰 가능한 부분: 요약/증상/칩/분류/라벨)에 대해
지식베이스(해결 이슈)에서 가장 유사한 사례를 검색하고, 상위 사례의
근본원인·해결책·우회책을 제안으로 반환한다.

검색 방법(method):
    graph  : 엔티티 중첩(레코드 entities) — repo GraphRetriever 방식 (baseline)
    bm25   : 이슈 텍스트 BM25 (rank_bm25)
    hybrid : bm25 + graph RRF 융합 + 동일 칩/분류 부스트  (기본)
    embed  : 다국어 임베딩 코사인 (fastembed, 선택)
    hybrid_embed : bm25 + graph + embed RRF + 부스트

설계상 단일 책임: 인덱싱 + 랭킹 + 제안. 데이터 적재/전처리는 ingest/preprocess 담당.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

import threading

# 파생 임베딩 디스크 캐시가 담는 최대 텍스트 수(태그별). KB 가 바뀌며 낡은 항목이
# 쌓이므로 상한을 둔다 — 이번 요청분을 먼저 지키고 남는 자리에 과거 항목을 채운다.
_EMB_CACHE_MAX = int(os.getenv("RVP_EMB_CACHE_MAX", "50000"))

from rank_bm25 import BM25Okapi

# HTTP 연결 재사용 — 매 호출 새 연결이면 TLS 핸드셰이크를 다시 한다
# (실측 2026-08-02: 378ms → 311ms, -68ms/호출).
_HTTP = None
_HTTP_LOCK = threading.Lock()


def _http():
    global _HTTP
    if _HTTP is None:
        import requests
        with _HTTP_LOCK:
            if _HTTP is None:
                _HTTP = requests.Session()
    return _HTTP

import sys as _sys
from pathlib import Path
_sys.path.insert(0, str(Path(__file__).resolve().parent))
from preprocess import tokenize, extract_entities  # noqa: E402  (단일 소스)

# 같은 템플릿(=같은 근본원인 클래스) 식별: 요약에서 칩 prefix와 변형 suffix 제거.
# 변형 suffix는 항상 " (고객사 / 호스트)" 형태(공백+슬래시 포함)이므로,
# 본문에 포함된 "(ghosting)" 같은 괄호와 구분하여 그것만 제거한다.
# 호스트명에 중첩 괄호가 올 수 있음 — 예: "(Vega / Android NFC stack (NCI 2.3))"
_PREFIX = re.compile(r"^\s*\[[^\]]*\]\s*")
_SUFFIX = re.compile(r"\s*\((?:[^()]|\([^()]*\))*\s/\s(?:[^()]|\([^()]*\))*\)\s*$")


def env_embed_kwargs() -> dict:
    """운영과 **같은** 임베딩 백엔드·모델 인자. Recommender 를 만드는 모든 곳이 쓴다.

    왜 함수인가: 이 표현이 파일마다 복사돼 있었고, 빠뜨린 곳들이 조용히 클래스 기본값
    (로컬 MiniLM)으로 떨어졌다. 유사도 공간이 달라지면 같은 임계값·같은 코드가 다른
    답을 낸다 — 실제로 두 번 당했다:

      · embed_cos 게이트(2026-08-01): 하네스가 로컬만 평가해 운영에서 죽은 게이트를 놓침
      · 자기개선 루프(2026-08-02): 대시보드 "모순 없음" 과 개선 큐 "모순 1건" 이 공존

    일부러 모델을 바꿔 비교하는 A/B(scripts/ab_embedding_models.py)만 예외다.
    """
    import os as _os
    backend = _os.getenv("RVP_EMBED_BACKEND", "fastembed")
    return {"embed_backend": backend,
            "embed_model": (_os.getenv("RVP_EMBED_MODEL", "")
                            or ("baai/bge-m3" if backend == "openrouter" else ""))}


def template_key(summary: str) -> str:
    s = _PREFIX.sub("", summary or "")
    s = _SUFFIX.sub("", s)
    return s.strip()


def query_entities(rec: dict) -> set[str]:
    blob = " ".join([rec.get("summary", ""), rec.get("symptom", ""),
                     rec.get("chip", ""), rec.get("category", "")])
    ents = extract_entities(blob)
    ents.update(rec.get("labels", []) or [])
    ents.update(rec.get("components", []) or [])
    ents.discard("customer-report")
    return {e for e in ents if len(e) >= 2}


def _doc_text(rec: dict, analysis: bool = True) -> str:
    """KB 문서 표현 (검색 대상).

    이슈는 제기 → 분석 → 해결 단계로 진행된다. 유사 이슈 매칭은
    "이슈 제기(요약/증상)"와 "문제 분석(디버깅 접근/근본 원인)" 내용으로 하고,
    해결 단계(resolution/workaround)는 질의(미해결 이슈)에 존재할 수 없는
    정보이므로 문서 표현에서 제외한다.
    """
    parts = [
        rec.get("summary", ""), rec.get("symptom", ""),
        " ".join(rec.get("labels", []) or []),
        rec.get("chip", ""), rec.get("category", ""),
    ]
    if analysis:
        parts += [rec.get("debug_approach", ""), rec.get("root_cause", "")]
    return " ".join(p for p in parts if p)


def _query_text(rec: dict) -> str:
    """질의 표현 — 이슈 제기 + (진행 중 이슈에 있다면) 분석 단계 내용.

    해결 필드(resolution/workaround)는 미해결 질의에 존재할 수 없으므로 사용 안 함.
    investigation: 진행 중 이슈의 조사·트리아지 코멘트에서 추출한 관찰/분석 단계
    신호(관찰 가능 — 확정 근본원인 아님). 이슈가 진행될수록 질의 표현이 풍부해진다.
    """
    # chip 을 **넣지 않는다**. 요약에 이미 "[PM9C3-NVMe] ..." 형태로 들어 있어
    # 구조화 필드로 한 번 더 넣으면 중복인데, 신고자가 칩을 잘못 적으면 그 오류가
    # rerank 질의 텍스트를 오염시켜 엉뚱한 사례를 1위로 올린다.
    # 실측(2026-08-02, confusable 34건): 칩 포함 시 정확한 칩 P@1 1.000 / **틀린 칩
    # 0.824**, 칩 제외 시 정확한 칩 1.000 / 틀린 칩 **1.000** — 잃는 것 없이
    # 오기입 견고성만 얻는다. 칩 신호는 _apply_boost 와 문서 표현(_doc_text)이 담당한다.
    return " ".join(p for p in [
        rec.get("summary", ""), rec.get("symptom", ""),
        rec.get("category", ""),
        rec.get("debug_approach", ""), rec.get("root_cause", ""),
        rec.get("investigation", ""),
    ] if p)


@dataclass
class Recommender:
    kb: list[dict]                       # 해결(Resolved) 이슈 레코드
    method: str = "hybrid"
    rrf_k: int = 60
    boost: float = 0.15                  # 동일 칩/분류 가산 — **현 KB 에서는 효과 없음**.
    # 측정(2026-08-02, confusable+paraphrase): 0.30 / 0.15 / 0.00 이 rerank ON·OFF
    # 양쪽에서 **완전히 동일한 지표**를 냈다. RRF 융합 점수(≈0.03 단위)에 0.15 를
    # 더하면 순위가 크게 흔들릴 것 같지만, 실제로는 동일 칩·분류 사례가 이미 상위에
    # 몰려 있어 상대 순서가 바뀌지 않는다. rerank 가 켜지면 재채점이 1차 순위를
    # 덮으므로 더더욱 무효다.
    # → 튜닝해도 소용없다(RVP_BOOST 포함). KB 구성이 크게 달라지면 다시 재라.
    #   지금 지우지 않는 이유: 칩·분류가 흩어진 KB 에서는 의미가 생길 수 있고,
    #   제거는 되돌리기보다 비싸다.
    signals: bool = True                 # 강도 신호(embed_cos 등) 산출 — 게이트/표시용
    gate_cos: float = 0.48               # coverage 게이트: 임베딩 코사인 임계
    # 0.48 근거: paraphrase 정답 중 0.485/0.496이 0.50 직하에서 차단(FN)되는 반면,
    # 무관 질의 분포는 0.474 이하(예외 n06 0.503은 어느 임계든 통과). 측정 2026-06-11.
    # 임계는 모델별 코사인 분포에 종속 — 미지정 시 _GATE_COS_BY_MODEL 로 보정한다.
    doc_analysis: bool = True            # KB 문서에 분석 단계(디버깅 접근/근본 원인) 포함
    verified_tiebreak: float = 1e-4      # 검증 완료(✅+🙌) 사례 동점 시 우선 노출(M2).
    # 1e-4 근거: 텍스트/메타 신호(RRF≈0.03, boost 0.15)를 절대 덮지 않는 크기 —
    # 점수가 사실상 같을 때만 검증 사례를 위로 올리는 순수 tie-breaker.
    # 임베딩 백엔드·모델. **미지정이면 환경변수를 따른다**(__post_init__ 에서 채움) —
    # 명시하지 않은 호출부가 조용히 로컬 MiniLM 으로 떨어지는 것을 막기 위해서다.
    # 예전에는 클래스 기본값이 곧 MiniLM 이었고, 인자를 빠뜨린 곳들이 운영(bge-m3)과
    # **다른 유사도 공간**에서 KB 를 봤다. 같은 임계값·같은 코드가 다른 답을 냈고,
    # 두 번 당했다: embed_cos 게이트 무동작(08-01), 대시보드와 개선 큐의 모순 불일치(08-02).
    # 폴백 자체는 남긴다(키·네트워크 없이 도는 테스트가 있다) — 다만 **조용하지 않게** 한다.
    # 명시 인자가 항상 이긴다(모델을 일부러 바꿔 비교하는 A/B 를 위해).
    embed_model: str | None = None        # None → 환경변수, "" → 클래스 기본(MiniLM)
    embed_backend: str | None = None      # None → 환경변수, 그 외 fastembed|openrouter
    # ---- 2차 재순위(reranker) — cross-encoder로 1차 top-N 재채점 ----
    rerank: bool = False                  # True → recommend()에서 top-N 재순위 + 강도 게이트
    rerank_model: str = "cohere/rerank-v3.5"
    rerank_top_n: int = 20                # 1차에서 재순위에 넘길 후보 수
    rerank_gate: float = 0.17             # coverage 게이트 임계(rerank relevance_score).
    # 0.17 근거(재보정 2026-08-02, 정답 107 / 무관 46 — confusable+paraphrase+generated):
    # 정답 최상위 최소 0.187, 무관 최상위 최대 0.154 → 그 사이. FN=0, FP=0.
    # 이전 값 0.20 은 2026-06-13 측정(정답 최소 0.384 / 무관 최대 0.042)에서 왔는데,
    # 그때는 쉬운 셋만 있어 여유가 커 보였다. 재서술+혼동 후보를 넣은 변별 셋에서는
    # 0.20 이 정답 1건(conf-LSI-174, 0.187)을 차단했다 — 쉬운 셋으로 잡은 임계가
    # 어려운 질의를 자른 것이다.
    # 마진이 ±0.017 로 얇다. 무관 쪽 최대값은 n-conf-04("사원증 인식이 간헐적으로
    # 실패") 로, 하드웨어 간헐 고장과 표현이 겹쳐 점수가 높다 — 실제로 어려운
    # 음성 샘플이며 이 압축은 정직한 값이다. 모델·평가셋 변경 시 재보정할 것.
    rerank_timeout: int = 10              # /rerank 호출 타임아웃(초). 실측 호출당 ≈0.6s —
    # 게이트웨이가 /rerank 미지원일 때 질의가 기본 60s에 묶이지 않게 짧게 제한.
    lazy_embed: bool = False              # 정상 경로에서 질의 임베딩을 생략(E-1).
    # rerank 가 상위 후보를 다시 채점하므로 1차 랭커의 역할은 recall 뿐이다. 임베딩을
    # 빼고 rerank_top_n 을 늘리면 정답 회수는 유지되면서 API 왕복 1회가 사라진다
    # (실측 recall@30: bm25+graph 1.000). rerank 가 실패한 회차에만 embed_cos 를
    # 계산해 폴백 게이트를 세운다 — 안전장치는 지키고 비용만 없애는 것이다.
    # 대가: 정상 경로에서 매치별 embed_cos 표시가 사라진다.
    rerank_fail_limit: int = 3            # 연속 실패 시 rerank 자동 비활성(circuit breaker) —
    # 미지원 게이트웨이에서 질의마다 실패 요청을 반복 지불하지 않기 위함.
    #
    # 재시도 대기(초). 예전에는 한 번 열리면 **KB 를 다시 만들 때까지 영영 닫히지
    # 않았다** — 게이트웨이가 5분 끊긴 것만으로 그날 내내 약한 폴백 게이트로 돌았다.
    # (그 상태에서 메타 없는 자유 문장은 정답 통과가 1.000 → 0.947 로 떨어진다.)
    # 대기 후 한 번 더 시도해, 일시 장애면 스스로 복구한다. 0 이면 재시도 없음.
    rerank_retry_sec: float = 300.0
    _rerank_fails: int = field(default=0, repr=False)
    _rerank_tripped_at: float = field(default=0.0, repr=False)
    _embedder: object = field(default=None, repr=False)
    _kb_emb: object = field(default=None, repr=False)

    # 모델별 embed_cos 게이트 임계 — 코사인 분포가 모델마다 다르므로 임계도 다르다.
    # 같은 평가셋(paraphrase+generated 정답 73 / 무관 40)에서 잰 분리 지점이다.
    #
    #   bge-m3      0.57  : 정답 최소 0.576 / 무관 최대 0.563 (마진 +0.013, 2026-08-01)
    #   e5-large    0.853 : 정답 최소 0.862 / 무관 최대 0.844 (마진 +0.018, 2026-08-02)
    #   MiniLM      0.48  : 여유 마진이 컸음(초기 측정)
    #
    # 여기 없는 모델은 클래스 기본값 0.48 로 떨어지는데, 그 값이 그 모델에서 맞는다는
    # 보장이 없다. **새 임베딩 모델을 쓰기 전에 반드시 분리 지점을 재고 이 표에 넣는다.**
    # 재지 않으면 게이트가 조용히 무력해진다 — mpnet-base 는 정답 최소 0.396 /
    # 무관 최대 0.500 으로 애초에 **겹쳐서**(분리 불가) 어떤 임계로도 못 쓴다.
    # rerank 를 켜고 평가하면 rerank 게이트가 판정을 대신해 이 결함이 가려지므로,
    # 모델 교체 시에는 반드시 rerank OFF 경로로도 평가한다.
    # bge-m3 0.57 재검증(2026-08-02, scripts/calibrate_gate_cos.py · 정답 171 / 무관 66,
    # 교정된 평가셋 4종). **이미 최적이고 움직일 자리가 없다:**
    #   무관 최대 코사인 0.563 → 0.57 은 무관을 전부 막는 **최저** 임계다.
    #   0.55 로 내리면 무관 차단 1.000 → 0.955, 올리면 정답이 급격히 깎인다(0.60 에서 0.901).
    #
    # 남는 손실은 임계 문제가 아니다. 메타(chip·category) 없이 증상만 적는 자유 문장에서
    # 정답 통과 0.947 — 막히는 9건은 전부 confusable 셋(기술용어를 뺀 현장 말투 재서술)이고,
    # 그 코사인 0.498~0.561 이 무관 최대 0.563 과 **겹친다.** 어떤 임계로도 못 가른다.
    #   경로별: 이슈 선택(메타 있음) 1.000 · 자유 문장 0.947 (confusable 만 0.735)
    #
    # BM25 를 3번째 신호로 넣어 구제하는 안은 재보고 **버렸다** — 무관 최대 6.76 vs
    # 구제 대상 7.67 로 여유가 13% 뿐이다. BM25 원점수는 정규화가 없어 KB 가 커지면
    # IDF 와 함께 흔들리므로 고정 임계가 조용히 무너진다. z-점수 정규화는 더 나빴다
    # (여유 -71%: 무관 질의도 코퍼스가 평평하면 z 가 높다). 무엇보다 이건 **폴백** 게이트라
    # 두 실패의 값이 다르다 — 막으면 "사례 없음"(안전), 통과시키면 무관 사례에 근거한
    # 환각 근본원인(위험). 얇은 여유로 위험한 쪽을 늘릴 이유가 없다.
    _GATE_COS_BY_MODEL = {
        "baai/bge-m3": 0.57,
        "intfloat/multilingual-e5-large": 0.853,
    }

    def __post_init__(self):
        # 미지정(None)이면 환경변수 → 운영과 같은 유사도 공간. 명시 인자가 있으면 그대로.
        env = env_embed_kwargs()
        if self.embed_backend is None:
            self.embed_backend = env["embed_backend"]
        if self.embed_model is None:
            self.embed_model = env["embed_model"]
        if self.gate_cos == type(self).gate_cos:  # 사용자가 명시하지 않았을 때만 보정
            self.gate_cos = self._GATE_COS_BY_MODEL.get(
                self._model_name().lower(), self.gate_cos)
        self._docs = [_doc_text(r, analysis=self.doc_analysis) for r in self.kb]
        self._bm25 = BM25Okapi([tokenize(d) for d in self._docs])
        self._kb_ents = [query_entities(r) for r in self.kb]
        self._keys = [r["key"] for r in self.kb]
        self._kb_verified = [bool(r.get("verified")) for r in self.kb]
        if self.method in ("embed", "hybrid_embed"):
            # 임베딩 백엔드(fastembed 미설치 / openrouter 게이트웨이 /embeddings 미지원·오류)
            # 실패 시 BM25 경로로 우아하게 폴백한다. embed→bm25, hybrid_embed→hybrid.
            try:
                self._init_embed()
            except Exception as e:
                fallback = "bm25" if self.method == "embed" else "hybrid"
                print(f"[recommender] ⚠ 임베딩({self.embed_backend}/{self._model_name()}) "
                      f"초기화 실패 → '{self.method}'→'{fallback}' 폴백: {str(e)[:120]}\n"
                      f"[recommender] ⚠ 이 인스턴스의 검색 결과는 운영과 다르다 — "
                      f"평가·튜닝 수치로 쓰지 말 것.")
                self.method = fallback
                self.signals = False
                self._embedder = None
                self._kb_emb = None
        elif self.signals:
            # 게이트/신뢰도 표시에 코사인이 필요. 실패 시 신호 없이 동작.
            try:
                self._init_embed()
            except Exception:
                self.signals = False

    # ---------- 임베딩(선택, 교체 가능) ----------
    _EMBED_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

    def _model_name(self) -> str:
        return self.embed_model or self._EMBED_MODEL

    def _prefixed(self, texts: list[str], is_query: bool) -> list[str]:
        """e5 계열은 query:/passage: 프리픽스를 요구한다(다른 모델은 원문 그대로)."""
        if "e5" in self._model_name().lower():
            tag = "query: " if is_query else "passage: "
            return [tag + t for t in texts]
        return texts

    def _embed_texts(self, texts: list[str], is_query: bool):
        import numpy as np
        texts = self._prefixed(texts, is_query)
        if self.embed_backend == "openrouter":
            return np.array(self._openrouter_embed(texts))
        return np.array(list(self._embedder.embed(texts)))

    def _openrouter_embed(self, texts: list[str]) -> list[list[float]]:
        """OpenRouter /embeddings 호출(배치). OPENROUTER_API_KEY 필요."""
        import os
        from llm_headers import custom_headers
        key = os.environ["OPENROUTER_API_KEY"]
        base = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
        hdrs = {"Authorization": f"Bearer {key}", **custom_headers()}
        out: list[list[float]] = []
        for i in range(0, len(texts), 64):  # rate/payload 여유 배치
            chunk = texts[i:i + 64]
            data = self._embed_post(base, hdrs, chunk)
            out.extend(d["embedding"] for d in data)
        return out

    # 429/5xx 지수 백오프 재시도. 질의 경로(_cos_all)는 요청당 API 1회를 쓰므로
    # 레이트 리밋이 그대로 500으로 새어 나갔다(평가 배치에서 실측 2026-08-01).
    _RETRY_STATUS = (429, 500, 502, 503, 504)

    def _embed_post(self, base: str, hdrs: dict, chunk: list[str], tries: int = 4):
        import time
        for attempt in range(tries):
            r = _http().post(f"{base}/embeddings", headers=hdrs,
                             json={"model": self._model_name(), "input": chunk}, timeout=120)
            if r.status_code in self._RETRY_STATUS and attempt < tries - 1:
                # Retry-After 우선, 없으면 0.5·2^n (최대 8s)
                wait = float(r.headers.get("Retry-After") or min(0.5 * 2 ** attempt, 8.0))
                time.sleep(wait)
                continue
            r.raise_for_status()
            return sorted(r.json()["data"], key=lambda x: x["index"])
        raise RuntimeError("unreachable")

    def _init_embed(self):
        import hashlib
        import numpy as np
        from pathlib import Path
        self._np = np
        if self.embed_backend == "fastembed":
            from fastembed import TextEmbedding
            self._embedder = TextEmbedding(model_name=self._model_name())
        else:
            self._embedder = None  # API 백엔드는 객체 불필요(_openrouter_embed 사용)
        self._kb_emb = self._embed_docs_incremental(self._docs)

    def _embed_docs_incremental(self, docs: list[str]):
        """문서 **단위** 캐시 — 바뀐 문서만 다시 임베딩한다.

        예전에는 문서 집합 전체를 하나의 해시로 잡아, 이슈 1건만 바뀌어도 캐시가
        미스가 되고 KB 전량을 다시 임베딩했다(실측 4.8초). Jira 폴러가 5초 주기로
        도는 지금은 누군가 이슈를 하나 고치면 그 비용을 다음 사용자가 문다 —
        그것도 빌드 락을 쥔 요청 스레드에서라 그동안 들어온 요청이 함께 대기한다.

        저장 형태: 모델마다 파일 하나(hashes + emb 행렬). 문서 수만큼 파일을 만들면
        수천 건에서 파일시스템이 병목이 되고, 한 파일이면 로드가 한 번이다.
        """
        import hashlib
        from pathlib import Path
        np = self._np
        sig = self._model_name() + ("" if self.embed_backend == "fastembed"
                                    else f"@{self.embed_backend}")
        slug = hashlib.md5(sig.encode()).hexdigest()[:10]
        store = Path(__file__).resolve().parent.parent / "tmp_db" / f"docvec_{slug}.npz"

        want = [hashlib.sha1(d.encode("utf-8")).hexdigest() for d in docs]
        known: dict[str, int] = {}
        mat = None
        if store.exists():
            try:
                z = np.load(store, allow_pickle=False)
                mat = z["emb"]
                known = {h: i for i, h in enumerate(z["hashes"].tolist())}
            except Exception:
                mat, known = None, {}          # 손상된 저장소는 버리고 새로 만든다

        missing = [i for i, h in enumerate(want) if h not in known]
        if missing:
            new_emb = self._embed_texts([docs[i] for i in missing], is_query=False)
            new_emb = np.asarray(new_emb, dtype=np.float32)
            if mat is None or mat.size == 0:
                mat = new_emb
                known = {want[i]: k for k, i in enumerate(missing)}
            else:
                base = len(mat)
                mat = np.vstack([mat.astype(np.float32), new_emb])
                for k, i in enumerate(missing):
                    known[want[i]] = base + k
            try:
                store.parent.mkdir(exist_ok=True)
                # 현재 KB 에 없는 옛 벡터는 버린다 — 무한히 자라지 않게.
                keep = {h: known[h] for h in want if h in known}
                order = list(keep)
                np.savez_compressed(store, hashes=np.array(order),
                                    emb=mat[[keep[h] for h in order]])
            except OSError:
                pass                            # 캐시 쓰기 실패는 치명적이지 않다
        if missing:
            print(f"[embed] 문서 {len(docs)}건 중 {len(missing)}건만 신규 임베딩")
        return np.asarray([mat[known[h]] for h in want], dtype=np.float32)

    def embed_cached(self, texts: list[str], tag: str):
        """텍스트 묶음을 임베딩하되 **텍스트 하나하나를 내용 주소로** 디스크에 캐시한다.

        이걸 쓰지 않으면 파생 분석이 호출마다 KB 전체를 다시 임베딩한다 — 실제로
        /knowledge/contradictions 가 매 호출 4.0~4.4초를 썼다.

        **코퍼스 단위로 해시하면 안 된다.** 예전 구현은 캐시 키가
        `md5(모든 텍스트를 이어붙인 것)` 이었다. 그래서 레코드가 **하나만** 바뀌어도
        (RCA 승인 1건, Jira 폴링이 물어온 변경 1건) 전체 캐시가 무효가 되고 KB 전량을
        다시 임베딩했다 — 실측 207건 7.3초. 대시보드는 KB 가 바뀔 때마다 그 값을 냈다.
        텍스트별 주소로 두면 바뀐 것만 임베딩한다.
        """
        import hashlib
        from pathlib import Path
        np = self._np
        if not texts:
            return np.zeros((0, 1), dtype=np.float32)
        sig = self._model_name() + ("" if self.embed_backend == "fastembed" else f"@{self.embed_backend}")
        sig_digest = hashlib.md5((sig + tag).encode()).hexdigest()[:10]
        cache = Path(__file__).resolve().parent.parent / "tmp_db" / f"embc_{tag}_{sig_digest}.npz"

        def _h(t: str) -> str:
            return hashlib.sha1(t.encode("utf-8")).hexdigest()

        store: dict = {}
        if cache.exists():
            try:
                z = np.load(cache, allow_pickle=False)
                store = {k: v for k, v in zip(z["keys"].tolist(), z["emb"])}
            except Exception:
                store = {}                 # 손상된 캐시는 무시하고 다시 만든다

        hashes = [_h(t) for t in texts]
        missing_idx = {h: i for i, h in enumerate(hashes) if h not in store}
        if missing_idx:
            # 중복 텍스트는 한 번만 임베딩한다 — KB 에 같은 문구가 반복되는 것은 흔하다.
            order = list(missing_idx.values())
            fresh = self._embed_texts([texts[i] for i in order], is_query=False)
            for h, vec in zip(missing_idx.keys(), np.asarray(fresh, dtype=np.float32)):
                store[h] = vec
            try:
                # 무한 증가 방지: 이번 요청분을 먼저 지키고, 남는 자리에 과거 항목을 채운다.
                keep = dict.fromkeys(hashes)
                merged = {h: store[h] for h in keep if h in store}
                for h, v in store.items():
                    if len(merged) >= _EMB_CACHE_MAX:
                        break
                    merged.setdefault(h, v)
                cache.parent.mkdir(exist_ok=True)
                tmp = cache.with_name(cache.name + ".tmp.npz")
                np.savez_compressed(tmp, keys=np.array(list(merged), dtype="U40"),
                                    emb=np.asarray(list(merged.values()), dtype=np.float32))
                os.replace(tmp, cache)     # 원자적 교체 — 동시 요청이 반쯤 쓴 파일을 읽지 않게
            except OSError:
                pass
        return np.asarray([store[h] for h in hashes], dtype=np.float32)

    def _cos_all(self, q: str):
        """질의 vs KB 전체 코사인 유사도 배열 (강도 신호).

        recommend() 한 번에 rank(_embed_rank)와 신호 산출이 같은 질의로 두 번
        호출하므로 직전 질의 1건을 캐시 — 질의 임베딩 중복 계산(로컬 모델 추론
        또는 API 호출) 제거.
        """
        cached = getattr(self, "_cos_cache", None)
        if cached is not None and cached[0] == q:
            return cached[1]
        qv = self._embed_texts([q], is_query=True)[0]
        sims = self._kb_emb @ qv / (
            self._np.linalg.norm(self._kb_emb, axis=1) * self._np.linalg.norm(qv) + 1e-9)
        self._cos_cache = (q, sims)
        return sims

    def _embed_rank(self, q: str) -> list[tuple[int, float]]:
        sims = self._cos_all(q)
        order = self._np.argsort(-sims)
        return [(int(i), float(sims[i])) for i in order]

    # ---------- 개별 랭커 ----------
    def _bm25_rank(self, q: str) -> list[tuple[int, float]]:
        scores = self._bm25.get_scores(tokenize(q))
        return sorted(((i, float(s)) for i, s in enumerate(scores)), key=lambda x: -x[1])

    def _graph_rank(self, q_ents: set[str]) -> list[tuple[int, float]]:
        scored = []
        for i, ents in enumerate(self._kb_ents):
            ov = len(q_ents & ents)
            if ov:
                scored.append((i, float(ov)))
        return sorted(scored, key=lambda x: -x[1])

    @staticmethod
    def _rrf(ranks: list[list[tuple[int, float]]], k: int,
             weights: list[float] | None = None) -> dict[int, float]:
        out: dict[int, float] = {}
        weights = weights or [1.0] * len(ranks)
        for w, rank_list in zip(weights, ranks):
            for pos, (idx, _) in enumerate(rank_list):
                out[idx] = out.get(idx, 0.0) + w / (k + pos)
        return out

    def _apply_boost(self, fused: dict[int, float], query_rec: dict) -> None:
        qchip, qcat = query_rec.get("chip", ""), query_rec.get("category", "")
        for i, r in enumerate(self.kb):
            if i in fused:
                if qchip and r.get("chip") == qchip:
                    fused[i] += self.boost
                if qcat and r.get("category") == qcat:
                    fused[i] += self.boost

    # ---------- 추천 ----------
    def rank(self, query_rec: dict, exclude_key: str | None = None) -> list[tuple[int, float]]:
        qtext = _query_text(query_rec)
        qents = query_entities(query_rec)

        if self.method == "graph":
            fused = dict(self._graph_rank(qents))
        elif self.method == "bm25":
            fused = dict(self._bm25_rank(qtext))
        elif self.method == "embed":
            fused = dict(self._embed_rank(qtext))
        elif self.method == "bm25_boost":
            fused = dict(self._bm25_rank(qtext))
            self._apply_boost(fused, query_rec)
        elif self.method == "hybrid_embed":
            if self.lazy_embed and self.rerank:
                # 1차는 recall 만 책임진다 — rerank 가 어차피 재채점한다.
                lists = [self._bm25_rank(qtext), self._graph_rank(qents)]
                fused = self._rrf(lists, self.rrf_k, weights=[2.0, 0.5])
            else:
                # bm25 우세 + embed 보조 + graph 약하게, 동일 칩/분류 부스트
                lists = [self._bm25_rank(qtext), self._embed_rank(qtext), self._graph_rank(qents)]
                fused = self._rrf(lists, self.rrf_k, weights=[2.0, 1.5, 0.5])
            self._apply_boost(fused, query_rec)
        else:  # hybrid (bm25 우세 + graph 약하게 + 부스트)
            lists = [self._bm25_rank(qtext), self._graph_rank(qents)]
            fused = self._rrf(lists, self.rrf_k, weights=[2.0, 0.5])
            self._apply_boost(fused, query_rec)

        # 검증 완료 사례 동점 tie-break(M2) — 실신호를 못 덮는 크기로만 가산.
        if self.verified_tiebreak:
            for i in list(fused):
                if self._kb_verified[i]:
                    fused[i] += self.verified_tiebreak
        ranked = sorted(fused.items(), key=lambda x: -x[1])
        if exclude_key is not None:
            ranked = [(i, s) for i, s in ranked if self._keys[i] != exclude_key]
        return ranked

    def recommend(self, query_rec: dict, k: int = 3, exclude_key: str | None = None) -> dict:
        # 단계별 소요(ms)를 응답에 실어 보낸다 — 격리 벤치가 서버 지연을 예측하지
        # 못한다는 것을 실측으로 배웠다(백로그 E-0). 상시 수치가 있어야 회귀를 잡는다.
        import time as _t
        timing: dict[str, float] = {}
        _t0 = _t.perf_counter()
        ranked_all = self.rank(query_rec, exclude_key)
        timing["rank_ms"] = round((_t.perf_counter() - _t0) * 1000, 1)
        qtext = _query_text(query_rec)
        # ---- 2차 재순위(reranker): 1차 top-N을 cross-encoder로 재채점 ----
        rr_scores: dict[int, float] = {}
        # 차단기 재시도 — 대기가 지났으면 한 번 더 열어 본다. 다시 실패하면 아래
        # 카운터가 즉시 임계를 넘겨(누적값을 유지한다) 곧바로 다시 닫힌다.
        if (not self.rerank and self._rerank_tripped_at and self.rerank_retry_sec > 0
                and _t.time() - self._rerank_tripped_at >= self.rerank_retry_sec):
            self.rerank = True
            self._rerank_fails = self.rerank_fail_limit - 1
            self._rerank_tripped_at = 0.0
            print("[recommender] rerank 재시도 — 대기 경과")

        if self.rerank and ranked_all:
            from reranker import rerank as _rerank  # 지연 임포트(선택 의존)
            cand = [i for i, _ in ranked_all[:self.rerank_top_n]]
            docs = [_doc_text(self.kb[i], analysis=self.doc_analysis) for i in cand]
            _t1 = _t.perf_counter()
            try:
                order = _rerank(qtext, docs, model=self.rerank_model,
                                timeout=self.rerank_timeout)
                timing["rerank_ms"] = round((_t.perf_counter() - _t1) * 1000, 1)
                rr_scores = {cand[idx]: sc for idx, sc in order}
                reranked = [(cand[idx], sc) for idx, sc in order]
                tail = [(i, s) for i, s in ranked_all if i not in rr_scores]
                ranked_all = reranked + tail
                self._rerank_fails = 0
            except Exception as e:
                # rerank 실패 시 1차 순위로 폴백(파이프라인 무중단).
                timing["rerank_ms"] = round((_t.perf_counter() - _t1) * 1000, 1)
                timing["rerank_failed"] = 1
                self._rerank_fails += 1
                if self._rerank_fails >= self.rerank_fail_limit:
                    self.rerank = False
                    self._rerank_tripped_at = _t.time()
                    print(f"[recommender] ⚠ rerank {self._rerank_fails}회 연속 실패 → "
                          f"비활성(1차 순위+embed_cos 폴백 게이트): {str(e)[:120]}\n"
                          f"[recommender] ⚠ 폴백 게이트는 메타 없는 자유 문장에서 정답의 "
                          f"약 5%를 막는다(실측). {self.rerank_retry_sec:.0f}초 뒤 재시도.")
        ranked = ranked_all[:k]
        q_ents = query_entities(query_rec)
        # 강도 신호 — RRF(순위 기반) 점수와 별개. 게이트/신뢰도 표시는 이것만 사용한다.
        cos = bm25_raw = None
        _t2 = _t.perf_counter()
        # 게으른 모드: rerank 가 점수를 냈으면 게이트는 rerank 가 맡으므로 임베딩이
        # 필요 없다. rerank 가 실패한 회차에만 계산해 embed_cos 폴백 게이트를 세운다.
        _skip_embed = self.lazy_embed and bool(rr_scores)
        # 조건은 _kb_emb(임베딩 보유 여부) — _embedder는 fastembed 전용 객체라
        # openrouter 백엔드에서 항상 None이었고, 그 탓에 신호·embed_cos 게이트가
        # 통째로 생략돼 coverage가 무조건 True였다(무관 차단율 0.0, 2026-08-01 실측).
        if self.signals and self._kb_emb is not None and not _skip_embed:
            cos = self._cos_all(qtext)
            bm25_raw = self._bm25.get_scores(tokenize(qtext))
        timing["signals_ms"] = round((_t.perf_counter() - _t2) * 1000, 1)
        matches = []
        for i, score in ranked:
            r = self.kb[i]
            m = {
                "key": r["key"], "score": round(score, 4),
                "summary": r["summary"], "chip": r.get("chip", ""),
                "category": r.get("category", ""),
                "root_cause": r.get("root_cause", ""),
                "resolution": r.get("resolution", ""),
                "workaround": r.get("workaround", ""),
                "debug_approach": r.get("debug_approach", ""),
                "entity_overlap": len(q_ents & self._kb_ents[i]),
                "verified": self._kb_verified[i],
            }
            if cos is not None:
                m["embed_cos"] = round(float(cos[i]), 3)
                m["bm25_raw"] = round(float(bm25_raw[i]), 2)
            if i in rr_scores:
                m["rerank_score"] = round(rr_scores[i], 4)
            matches.append(m)
        # coverage 게이트: 무관 질의 차단.
        # 기본값을 False 로 둔다 — 판정하지 못했으면 통과가 아니라 차단이다(fail closed).
        coverage = False
        gate = None
        if self.rerank and rr_scores:
            # rerank 게이트는 rerank가 실제 점수를 냈을 때만. /rerank 실패(rr_scores 빈
            # dict)면 아래 embed_cos 게이트, 둘 다 없으면 마지막 else 에서 차단한다.
            # 강도 기반 게이트 — rerank relevance_score(재보정 임계 rerank_gate).
            top_rr = max(rr_scores.values(), default=0.0)
            coverage = bool(matches) and top_rr >= self.rerank_gate
            gate = {"signal": "rerank", "rerank_top": round(top_rr, 3),
                    "threshold": self.rerank_gate, "passed": coverage}
        elif cos is not None and len(cos):
            # 측정(2026-06): 무관 질의 max_cos<=0.474 & 엔티티 겹침 0, 정답 cos>=0.529.
            masked = cos.copy()
            if exclude_key is not None and exclude_key in self._keys:
                masked[self._keys.index(exclude_key)] = -1.0
            max_cos = float(masked.max())
            top_overlap = max((m["entity_overlap"] for m in matches), default=0)
            coverage = bool(matches) and (max_cos >= self.gate_cos or top_overlap >= 1)
            gate = {"signal": "embed_cos", "max_cos": round(max_cos, 3),
                    "top_entity_overlap": top_overlap,
                    "cos_threshold": self.gate_cos, "passed": coverage}
        else:
            # **판정 신호가 하나도 없다** — 재순위도 임베딩도 못 쓴다.
            # 예전에는 이 경우 gate=None 이고 coverage 가 `bool(matches)` 로 남아
            # **무조건 True** 였다. BM25 는 무관 질의에도 항상 무언가를 돌려주므로,
            # 구내식당 결제 문의에 칩 고장 사례가 붙고 그 위에 LLM 근본원인이
            # 생성된다 — 게이트가 막으려던 바로 그 환각이다.
            #
            # 임베딩과 재순위는 둘 다 외부 API 다. 게이트웨이 장애면 **동시에** 죽으니
            # 드문 조합이 아니라 오히려 흔한 조합이다.
            #
            # 그래서 **닫힌 채로 실패한다**(fail closed). 다만 "사례 없음" 과는 구분한다 —
            # 사례가 없는 게 아니라 판정을 못 하는 것이고, 사용자에게 할 말이 다르다.
            coverage = False
            gate = {"signal": "none", "available": False, "passed": False,
                    "reason": "판정 신호 없음(재순위·임베딩 모두 사용 불가) — "
                              "근거 없는 분석을 막기 위해 차단했습니다",
                    "candidates": len(matches)}
        # 제안: 상위 매치들의 다수결 클래스 → 그 클래스 대표의 해결책 (게이트 통과 시에만)
        proposal = None
        if matches and coverage:
            from collections import Counter
            classes = Counter(template_key(m["summary"]) for m in matches)
            top_class, cnt = classes.most_common(1)[0]
            in_class = [m for m in matches if template_key(m["summary"]) == top_class]
            # 대표 사례: 동일 클래스 내 '검증 완료' 사례를 우선(없으면 최상위)(M2).
            rep = next((m for m in in_class if m["verified"]), in_class[0])
            # 신뢰도 = 합의(agreement) × 관련도(rerank) + 검증(verified) 소폭 상향.
            #  - agreement: top-k 중 다수결 클래스 비율(사례들이 한 원인으로 수렴하는가)
            #  - relevance: 대표 사례 rerank relevance(0~1, 보정 점수) — 합의는 높아도
            #    실제 관련도가 낮으면(약한 매칭) 신뢰도를 끌어내려 과신을 방지.
            #    rerank 미사용 시 None → 기존(합의만)으로 폴백(회귀 없음).
            #  - verified: 고객 검증까지 끝난 근거면 (1-conf)*0.1 만큼 상향(상한 1.0).
            agreement = cnt / len(matches)
            relevance = rep.get("rerank_score")
            conf = agreement * relevance if relevance is not None else agreement
            if rep["verified"]:
                conf = conf + (1.0 - conf) * 0.10
            proposal = {
                "root_cause": rep["root_cause"], "resolution": rep["resolution"],
                "workaround": rep["workaround"], "based_on": rep["key"],
                "confidence": round(conf, 2),
                "based_on_verified": rep["verified"],
                # 신뢰도 근거 분해(UI/댓글 톤·디버깅용)
                "confidence_basis": {
                    "agreement": round(agreement, 2),
                    "rerank_relevance": round(relevance, 3) if relevance is not None else None,
                    "verified_bonus": rep["verified"],
                },
            }
        timing["total_ms"] = round((_t.perf_counter() - _t0) * 1000, 1)
        return {"matches": matches, "proposal": proposal, "coverage": coverage,
                "gate": gate, "timing": timing}


if __name__ == "__main__":
    import json
    from pathlib import Path
    ROOT = Path(__file__).resolve().parent.parent
    recs = json.load((ROOT / "data" / "processed_issues.json").open())
    rec = Recommender(recs, method="hybrid")
    demo = {"summary": "[PM9C3-NVMe] 고온 지속 쓰기 중 timeout", "symptom": "장시간 쓰기 후 link down, AER 폭주",
            "chip": "PM9C3-NVMe", "category": "Thermal", "labels": ["Thermal", "SSD-Controller"]}
    out = rec.recommend(demo, k=3)
    for m in out["matches"]:
        print(f"{m['key']} ({m['score']}) {m['summary'][:40]}")
    print("제안 근본원인:", (out["proposal"] or {}).get("root_cause", "")[:120])
