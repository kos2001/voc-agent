"""LSI 불량 분석 MCP 서버 — Claude Code 등 MCP 클라이언트용.

백엔드(FastAPI)의 조회 API 를 MCP 도구로 노출하는 **얇은 포워더**다. 비즈니스
로직을 갖지 않고, 권한(RBAC)은 전부 백엔드가 토큰으로 판정한다. 도구를 늘려도
인가 규칙이 두 곳으로 갈라지지 않게 하려는 것이다.

전송 방식 두 가지를 같은 도구 정의로 지원한다:

- **stdio** (`python src/mcp_server.py`) — 클라이언트가 이 프로세스를 띄운다.
  백엔드는 네트워크로 호출하고 토큰은 `LSI_MCP_TOKEN` 에서 읽는다. 로컬용이며
  클라이언트마다 이 저장소와 파이썬 환경이 필요하다.
- **streamable-HTTP** (`build_http_app()` 로 FastAPI 에 마운트) — 백엔드가 `/mcp`
  를 직접 제공한다. 클라이언트는 **URL 과 토큰만** 있으면 되므로 배포가 쉽다.
  토큰은 요청 헤더에서 받아 ContextVar 로 요청 단위 격리한다.
  이 모드의 백엔드 호출은 `httpx.ASGITransport` 로 **인프로세스** 처리한다 —
  자기 자신에게 소켓을 다시 열지 않으면서 인증 의존성을 포함한 실제 경로를 탄다.

**의도적으로 뺀 것**

- `POST /rca/approve`·`/rca/reject` — Jira 에 실제로 글을 쓴다. 되돌리기 어렵고
  외부로 나가는 행위라 사람이 웹 UI 에서 승인한다. MCP 로는 초안 제출까지만.
- `/config`·`/auth/users`·`/jira/sync`·`/explain/cache`·`/selfcheck` 등 운영·설정
  — 에이전트가 만질 이유가 없고, 사고 시 파급이 크다.
- **심층 분석 생성** — 이미 만들어 둔 캐시본만 돌려준다(`get_cached_analysis`).
  MCP 클라이언트 자신이 추론 주체인데 백엔드에서 LLM 을 또 부르면 추론이 중첩되고
  비용·지연이 두 배가 된다. 근거(유사 사례·근본원인·해결책)는 도구로 충분히 주므로
  결론은 클라이언트가 낸다.

환경변수 (stdio 모드)
  LSI_API        백엔드 base URL (기본 http://127.0.0.1:8001)
  LSI_MCP_TOKEN  액세스 토큰. 인증이 활성일 때 필수. `POST /auth/token` 으로 발급.
"""

from __future__ import annotations

import contextlib
import json
import os
from contextvars import ContextVar
from typing import Any, AsyncIterator

import httpx

SERVER_NAME = "lsi-error-analysis"
INTERNAL_BASE = "http://lsi-internal"      # 인프로세스 호출용 가짜 호스트

INSTRUCTIONS = """LSI 칩/펌웨어 고장 분석 지식베이스.

과거에 **해결된** Jira 이슈를 근거로 미해결 이슈의 근본원인·해결책을 찾는다.

쓰는 순서:
  1. `find_similar` 또는 `analyze_issue` 로 유사 해결 사례를 받는다.
  2. 반환된 `coverage` 를 먼저 본다. **false 면 근거가 부족하다는 뜻**이므로
     매치를 근거로 결론을 내지 말고 "유사 사례 없음 — 시니어 검토 필요"로 답한다.
     `gate` 에 왜 막혔는지(신호·임계·실측값)가 들어 있다.
  3. 결론은 `matches[].key` 를 인용해 근거를 밝힌다. 제공되지 않은 이슈 키를
     지어내지 않는다.

`draft_rca` 는 초안을 **승인 대기 큐에 넣을 뿐** Jira 에 게시하지 않는다.
게시는 사람이 웹 UI 에서 승인할 때만 일어난다."""

# 요청 단위 토큰(HTTP 마운트 모드). stdio 모드에서는 비어 있고 환경변수를 쓴다.
_ctx_token: ContextVar[str] = ContextVar("lsi_mcp_token", default="")
# 인프로세스 클라이언트(HTTP 마운트 모드). None 이면 네트워크로 호출한다.
_asgi: httpx.AsyncClient | None = None


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _base_url() -> str:
    return _env("LSI_API", "http://127.0.0.1:8001").rstrip("/")


def _token() -> str:
    return _ctx_token.get() or _env("LSI_MCP_TOKEN")


async def _call(method: str, path: str, **kw) -> Any:
    """백엔드 호출. 마운트 모드면 인프로세스, 아니면 네트워크.

    오류를 예외로 던지지 않고 구조화해서 돌려준다 — MCP 클라이언트는 스택
    트레이스보다 "왜 실패했고 무엇을 하면 되는지"가 필요하다.
    """
    headers = {}
    tok = _token()
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    try:
        if _asgi is not None:
            r = await _asgi.request(method, path, headers=headers, **kw)
        else:
            async with httpx.AsyncClient(base_url=_base_url(), timeout=120.0) as c:
                r = await c.request(method, path, headers=headers, **kw)
    except Exception as e:
        return {"error": f"백엔드에 닿지 못했습니다({_base_url()}): {str(e)[:160]}",
                "hint": "서버가 떠 있는지, LSI_API 가 맞는지 확인하세요."}
    if r.status_code == 401:
        return {"error": "인증 실패(401)",
                "hint": "LSI_MCP_TOKEN 을 설정하세요. 발급: 웹 로그인 후 POST /auth/token"}
    if r.status_code == 403:
        return {"error": f"권한 없음(403): {_detail(r)}",
                "hint": "이 작업에는 더 높은 역할이 필요합니다. 관리자에게 요청하세요."}
    if r.status_code >= 400:
        return {"error": f"HTTP {r.status_code}: {_detail(r)}"}
    try:
        return r.json()
    except Exception:
        return {"error": "응답이 JSON 이 아닙니다", "body": r.text[:300]}


def _detail(r: httpx.Response) -> str:
    try:
        return str(r.json().get("detail") or r.text[:200])
    except Exception:
        return r.text[:200]


def _slim_match(m: dict) -> dict:
    """도구 응답에서 쓸 필드만 남긴다 — 컨텍스트를 아끼고 해석을 흐리지 않기 위해."""
    out = {k: m.get(k) for k in
           ("key", "summary", "chip", "category", "root_cause", "resolution",
            "workaround", "debug_approach", "verified")}
    for k in ("rerank_score", "embed_cos", "entity_overlap"):
        if m.get(k) is not None:
            out[k] = m[k]
    if m.get("known_issue"):
        out["known_issue"] = m["known_issue"]
    if (m.get("lifecycle") or {}).get("warnings"):
        out["lifecycle_warnings"] = m["lifecycle"]["warnings"]
    return out


def _guidance(gate: dict) -> str:
    """점수를 어떻게 읽어야 하는지까지 알려 준다.

    rerank_score·embed_cos 는 **모델 원점수**다. 백분율처럼 읽으면 안 된다 — 실측에서
    정답 rerank 최소가 0.384, 통과 임계는 0.17 이다. "38% 니까 관련 없다" 는 판단은
    틀린다. 판정 기준은 gate 안의 임계값이고, 그걸 같이 실어 보낸다.
    """
    if gate.get("signal") == "none":
        return ("판정 불가: 재순위·임베딩 서비스를 쓸 수 없어 관련도를 검증하지 "
                "못했습니다. **사례가 없다는 뜻이 아닙니다** — 후보는 있지만 검증되지 "
                "않았으니 근거로 삼지 말고, 일시 장애임을 알리고 재시도를 권하세요.")
    base = ("coverage=false 면 근거가 부족합니다 — 매치를 근거로 결론을 내지 말고 "
            "시니어 검토가 필요하다고 답하세요.")
    thr = gate.get("threshold") if gate.get("signal") == "rerank" else gate.get("cos_threshold")
    if thr is not None:
        base += (f" 점수(rerank_score·embed_cos)는 모델 원점수이니 백분율로 읽지 말고 "
                 f"이 질의의 통과 임계 {thr} 와 비교하세요. 판정은 이미 coverage 에 "
                 f"반영돼 있습니다.")
    return base


def _slim_reco(d: dict) -> dict:
    if "error" in d:
        return d
    return {
        "query": d.get("query"),
        "coverage": d.get("coverage"),
        "gate": d.get("gate"),
        "proposal": d.get("proposal"),
        "matches": [_slim_match(m) for m in d.get("matches", [])],
        "_guidance": _guidance(d.get("gate") or {}),
    }


# ---------------------------------------------------------------------------
# 도구 정의
# ---------------------------------------------------------------------------
def register(server) -> None:
    """MCPServer 인스턴스에 도구를 등록한다."""

    @server.tool()
    async def find_similar(summary: str, symptom: str = "", chip: str = "",
                           category: str = "", k: int = 4) -> str:
        """자유 텍스트 증상으로 과거 **해결된** 유사 사례를 찾는다.

        Jira 키를 모를 때 쓴다. coverage=false 면 근거가 부족하다는 뜻이다.

        Args:
            summary: 이슈 요약 또는 증상 설명 (필수)
            symptom: 추가 증상 상세
            chip: 칩 이름 (예: PM9C3-NVMe). 알면 정확도가 오른다
            category: 분류 (Firmware/Thermal/Power/Timing/Hardware 등)
            k: 반환할 유사 사례 수 (기본 4)
        """
        d = await _call("POST", "/recommend", json={
            "summary": summary, "symptom": symptom, "chip": chip,
            "category": category, "k": max(1, min(k, 10))})
        return json.dumps(_slim_reco(d), ensure_ascii=False, indent=2)

    @server.tool()
    async def analyze_issue(key: str, k: int = 4) -> str:
        """Jira 이슈 키로 유사 해결 사례 + 제안 근본원인/해결책을 받는다.

        Args:
            key: Jira 이슈 키 (예: LSI-7)
            k: 반환할 유사 사례 수 (기본 4)
        """
        d = await _call("POST", "/recommend", json={"key": key.strip().upper(),
                                                    "k": max(1, min(k, 10))})
        return json.dumps(_slim_reco(d), ensure_ascii=False, indent=2)

    @server.tool()
    async def list_unresolved(query: str = "", chip: str = "", category: str = "",
                              limit: int = 20) -> str:
        """미해결 이슈 목록을 조회한다(선택적 필터).

        Args:
            query: 키·요약·칩·증상에 대한 부분 일치 검색어
            chip: 칩으로 정확히 일치 필터
            category: 분류로 정확히 일치 필터
            limit: 최대 건수 (기본 20)
        """
        d = await _call("GET", "/issues/unresolved")
        if "error" in d:
            return json.dumps(d, ensure_ascii=False)
        items = d.get("issues", [])
        q = query.strip().lower()
        if q:
            items = [i for i in items
                     if q in f"{i.get('key','')}{i.get('summary','')}{i.get('chip','')}{i.get('symptom','')}".lower()]
        if chip:
            items = [i for i in items if i.get("chip") == chip]
        if category:
            items = [i for i in items if i.get("category") == category]
        total = len(items)
        items = items[:max(1, min(limit, 100))]
        return json.dumps({"total_matched": total, "returned": len(items),
                           "issues": [{k: i.get(k) for k in
                                       ("key", "summary", "status", "chip", "category", "symptom")}
                                      for i in items]}, ensure_ascii=False, indent=2)

    @server.tool()
    async def get_cached_analysis(key: str) -> str:
        """이미 생성해 둔 **고객 응대 답변**이 있으면 돌려준다(새로 생성하지 않는다).

        캐시본의 성격이 바뀌었다 — 예전에는 엔지니어용 심층 분석이었고 지금은
        고객에게 보낼 답변이다. 내부 이슈 키는 저장 전에 지워지므로 본문에 사례
        번호가 없는 것이 정상이고, 근거는 `evidence_keys` 로 따로 온다.
        발송 가능 여부(`policy`)도 함께 오지만, **발송은 MCP 로 하지 않는다** —
        사람이 웹 화면에서 검토하고 승인한다.

        없으면 그렇게 알린다 — 그때는 analyze_issue 의 근거로 직접 분석하면 된다.
        백엔드에서 LLM 을 다시 부르지 않는 이유: MCP 클라이언트가 이미 추론
        주체라 추론이 중첩되고 비용·지연이 두 배가 된다.

        Args:
            key: Jira 이슈 키 (예: LSI-7)
        """
        d = await _call("GET", "/recommend/explain/cached",
                        params={"key": key.strip().upper()})
        return json.dumps(d, ensure_ascii=False, indent=2)

    @server.tool()
    async def knowledge_overview() -> str:
        """지식베이스 현황 — 구성·인입 품질·중복 클러스터·모순·공백 요약.

        "이 KB 로 무엇을 답할 수 있는가/어디가 비어 있는가" 를 먼저 볼 때 쓴다.
        """
        stats = await _call("GET", "/reco/stats")
        quality = await _call("GET", "/knowledge/quality")
        clusters = await _call("GET", "/knowledge/clusters?threshold=0.80&min_size=2")
        contra = await _call("GET", "/knowledge/contradictions")
        gaps = await _call("GET", "/knowledge/gaps?top=10")
        return json.dumps({
            "kb": {k: stats.get(k) for k in ("resolved", "unresolved", "templates",
                                             "by_category", "method")} if "error" not in stats else stats,
            "quality": {"ok": quality.get("ok"), "violations": quality.get("violations"),
                        "deficient_keys": (quality.get("report") or {}).get("deficient_resolved_keys")}
                       if "error" not in quality else quality,
            "duplicate_clusters": clusters.get("count") if "error" not in clusters else clusters,
            "contradictions": contra.get("count") if "error" not in contra else contra,
            "knowledge_gaps": {"events": gaps.get("total_gap_events"),
                               "underserved": gaps.get("top_underserved_templates")}
                              if "error" not in gaps else gaps,
        }, ensure_ascii=False, indent=2)

    @server.tool()
    async def find_duplicate_clusters(threshold: float = 0.80, min_size: int = 2) -> str:
        """서로 유사도가 높아 뭉치는 해결 사례 묶음 — 같은 근본원인이 흩어져 있다는 신호.

        Args:
            threshold: 유사도 임계 (0~1, 기본 0.80)
            min_size: 최소 묶음 크기 (기본 2)
        """
        d = await _call("GET", "/knowledge/clusters",
                        params={"threshold": threshold, "min_size": min_size})
        if "error" in d:
            return json.dumps(d, ensure_ascii=False)
        cl = [{k: c.get(k) for k in ("members", "size", "representative", "chips",
                                     "categories", "avg_similarity")}
              for c in d.get("clusters", [])[:20]]
        return json.dumps({"count": d.get("count"), "shown": len(cl), "clusters": cl},
                          ensure_ascii=False, indent=2)

    @server.tool()
    async def find_contradictions(sim_hi: float = 0.85, rc_lo: float = 0.60) -> str:
        """같은 고장모드로 보이는데 근본원인이 엇갈리는 쌍 — 지식 신뢰도 점검용.

        Args:
            sim_hi: 사례 유사도 하한 (기본 0.85)
            rc_lo: 근본원인 유사도 상한 (기본 0.60)
        """
        d = await _call("GET", "/knowledge/contradictions",
                        params={"sim_hi": sim_hi, "rc_lo": rc_lo})
        return json.dumps(d, ensure_ascii=False, indent=2)

    @server.tool()
    async def draft_rca(key: str) -> str:
        """이슈의 RCA 댓글 초안을 만들어 **승인 대기 큐에 넣는다**.

        Jira 에 게시하지 않는다 — 게시는 사람이 웹 UI 에서 승인할 때만 일어난다.
        게이트를 통과하지 못하면 큐에 들어가지 않고 그 사유를 돌려준다.

        Args:
            key: Jira 이슈 키 (예: LSI-7)
        """
        d = await _call("POST", "/rca/draft", json={"key": key.strip().upper()})
        return json.dumps(d, ensure_ascii=False, indent=2)

    @server.tool()
    async def whoami() -> str:
        """지금 이 MCP 연결이 어떤 신원·권한으로 동작하는지 확인한다."""
        d = await _call("GET", "/auth/me")
        return json.dumps(d, ensure_ascii=False, indent=2)


def new_server():
    """도구가 등록된 새 MCPServer."""
    from mcp.server.mcpserver import MCPServer
    server = MCPServer(SERVER_NAME, instructions=INSTRUCTIONS)
    register(server)
    return server


# ---------------------------------------------------------------------------
# streamable-HTTP 마운트 (배포용)
# ---------------------------------------------------------------------------
def _allowed_hosts() -> list[str]:
    """DNS 리바인딩 보호용 허용 Host. 원격 배포는 LSI_MCP_ALLOWED_HOSTS 로 지정."""
    configured = _env("LSI_MCP_ALLOWED_HOSTS")
    if configured:
        return [h.strip() for h in configured.split(",") if h.strip()]
    return ["127.0.0.1:*", "localhost:*", "127.0.0.1", "localhost"]


def _allowed_origins() -> list[str]:
    configured = _env("LSI_MCP_ALLOWED_ORIGINS")
    return [o.strip() for o in configured.split(",") if o.strip()] if configured else []


def _token_from_scope(scope: dict) -> str:
    headers = {k.decode("latin-1").lower(): v.decode("latin-1")
               for k, v in scope.get("headers", [])}
    tok = headers.get("x-rvp-token", "").strip()
    if tok:
        return tok
    a = headers.get("authorization", "").strip()
    return a[7:].strip() if a.lower().startswith("bearer ") else ""


def _with_request_token(inner):
    """요청 헤더의 토큰을 ContextVar 에 실어 도구 호출까지 전달하는 ASGI 래퍼.

    스트리밍 응답이 끝날 때까지 같은 컨텍스트가 유지되므로 동시 접속자끼리
    토큰이 섞이지 않는다.
    """
    async def wrapped(scope, receive, send):
        if scope["type"] != "http":
            await inner(scope, receive, send)
            return
        reset = _ctx_token.set(_token_from_scope(scope))
        try:
            await inner(scope, receive, send)
        finally:
            _ctx_token.reset(reset)
    return wrapped


class MountedMcp:
    """FastAPI 에 붙일 MCP 엔드포인트 한 벌.

    사용 순서가 정해져 있다:
      1. FastAPI 생성 **전에** 만든다(lifespan 에 넘겨야 하므로)
      2. lifespan 안에서 `async with m.lifespan()`
      3. FastAPI 생성 **후에** `m.bind(app)` — 백엔드 호출을 인프로세스로
    """

    def __init__(self) -> None:
        from mcp.server.transport_security import TransportSecuritySettings
        self._server = new_server()
        self.app = _with_request_token(self._server.streamable_http_app(
            streamable_http_path="/",     # FastAPI 가 "/mcp" 에 마운트하므로 내부는 루트
            stateless_http=True,          # 요청마다 독립 — 재시작·다중 워커에 안전
            transport_security=TransportSecuritySettings(
                allowed_hosts=_allowed_hosts(), allowed_origins=_allowed_origins()),
        ))

    @contextlib.asynccontextmanager
    async def lifespan(self) -> AsyncIterator[None]:
        async with self._server.session_manager.run():
            yield

    def bind(self, app: Any) -> None:
        bind_asgi(app)


def build_http_app() -> MountedMcp:
    return MountedMcp()


def bind_asgi(app: Any) -> None:
    """도구의 백엔드 호출을 주어진 ASGI 앱으로 인프로세스 처리하게 한다."""
    global _asgi
    _asgi = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                              base_url=INTERNAL_BASE, timeout=120.0)


def unbind_asgi() -> None:
    global _asgi
    _asgi = None


def main() -> None:
    """stdio 모드 — 클라이언트가 이 프로세스를 띄운다."""
    new_server().run(transport="stdio")


if __name__ == "__main__":
    main()
