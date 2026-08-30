"""FastAPI server — LSI 고장 분석 추천 API.

Endpoints:
    POST /recommend          -> 유사 해결 사례 + root-cause/해결책 제안 (+ LLM 종합)
    GET  /issues/unresolved  -> 미해결 이슈 목록
    GET  /reco/stats         -> KB 통계
    GET  /health
"""
from __future__ import annotations

import json
import os
import sys
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field

from lang_validator import validate_and_fix  # noqa: E402
from preprocess import parse_issue  # noqa: E402
from recommender import Recommender, env_embed_kwargs, template_key  # noqa: E402
import app_config  # noqa: E402
import mcp_server  # noqa: E402
import auth  # noqa: E402
import oidc_sso  # noqa: E402
import session  # noqa: E402
import user_store  # noqa: E402
from json_store import read_json, write_json_atomic  # noqa: E402
from llm_headers import custom_headers  # noqa: E402  사내 게이트웨이 x-service-id/x-user-id

# stdlib(반복되던 지연 import 일원화)
import copy  # noqa: E402
import datetime as _dt  # noqa: E402
import time  # noqa: E402
import re  # noqa: E402
# 지식자산/자기개선 스토어 모듈 — 지연 import를 최상위로 일원화(순환 참조 없음)
import failure_modes  # noqa: E402
import improve_queue  # noqa: E402
import knowledge_export  # noqa: E402
import knowledge_gaps  # noqa: E402
import knowledge_store  # noqa: E402
import lifecycle  # noqa: E402
import llm_cache  # noqa: E402
import negative_knowledge  # noqa: E402
import ontology  # noqa: E402
import ownership  # noqa: E402
import quality_gate  # noqa: E402
import draft_feedback  # noqa: E402
import issue_keys  # noqa: E402
import kb_source  # noqa: E402
import rca_feedback  # noqa: E402
import rca_queue  # noqa: E402
import reply_queue  # noqa: E402
import voc_agents  # noqa: E402
import preprocess  # noqa: E402
import reco_feedback  # noqa: E402
import self_improve  # noqa: E402

# 저장된 온보딩 설정(LLM(OpenRouter)/Jira)을 env에 주입 — 서버 기동 시 1회.
app_config.load_into_env()

# LLM 설명 생성 엔진: agno(OpenRouter HTTP) 단일.

# MCP 엔드포인트는 FastAPI 생성 **전에** 만들어야 한다(lifespan 에 넘겨야 하므로).
# RVP_MCP=0 이면 마운트하지 않는다.
_MCP = mcp_server.build_http_app() if os.getenv("RVP_MCP", "1") == "1" else None


@asynccontextmanager
def _warm_kb() -> None:
    """KB 상태(파싱·BM25·임베딩)를 미리 만든다.

    로컬 임베딩 모델은 첫 사용 때 모델 로드 + KB 전량 임베딩을 한다 — 실측으로
    **첫 요청이 48.8초** 걸렸다(e5-large, 문서 137건, CPU). 사용자가 그 값을
    치르지 않도록 기동 직후 백그라운드에서 끝내 둔다. 캐시가 있으면 수십 ms 다.
    """
    t = time.perf_counter()
    try:
        _reco_state()
        print(f"[warmup] KB 준비 완료 {(time.perf_counter() - t) * 1000:.0f}ms")
    except Exception as e:
        print(f"[warmup] 실패(첫 요청에서 다시 시도): {str(e)[:160]}")


async def _lifespan(_app: FastAPI):
    _start_jira_poller()          # 정의는 아래 Jira 동기화 절 — 호출 시점에 해석된다
    # 워밍업이 먼저다 — 예열(prewarm)은 KB 상태를 쓰므로 순서가 뒤바뀌면 예열
    # 스레드가 그 48초를 대신 물고, 그 사이 들어온 요청도 같이 기다린다.
    threading.Thread(target=_warm_kb, name="kb-warmup", daemon=True).start()
    threading.Timer(8.0, _start_prewarm).start()
    if _MCP is None:
        yield
    else:
        async with _MCP.lifespan():
            yield
    _stop_jira_poller()


app = FastAPI(title="VOC Agent API", lifespan=_lifespan)

# ---------------------------------------------------------------------------
# 추천 엔진 (과거 해결 이슈 → 미해결 이슈의 root-cause/해결책 제안)
# ---------------------------------------------------------------------------
ALL_RAW = ROOT / "data" / "all_raw_issues.json"
# 해결 상태는 kb_source 가 단일 소스(RVP_RESOLVED_STATUS). 여기서 다시 박으면
# 서버와 KB 로더가 서로 다른 기준으로 '해결' 을 세게 된다.
RESOLVED_STATUS = kb_source.RESOLVED_STATUS

_RECO_STATE: dict = {}
# 빌드 락 — 백그라운드 Jira 폴러가 무효화하고 요청 스레드가 재빌드하므로 경합이 잦다.
# 락이 없으면 (a) 동시 요청이 각자 전체 재빌드(수 초)를 중복 수행하고,
# (b) `if _RECO_STATE` 통과 직후 무효화가 끼어들면 빈 dict가 반환된다.
_RECO_LOCK = threading.Lock()


def _invalidate_reco() -> None:
    """KB 캐시 무효화 — 다음 _reco_state()에서 재빌드.

    dict를 제자리에서 비우지 않고 새 dict로 교체한다. 읽는 쪽은 항상 '완전한 예전
    상태' 아니면 '빈 상태'만 보게 되어, 반쯤 지워진 dict를 잡는 경우가 없다.
    """
    global _RECO_STATE
    _RECO_STATE = {}


def _reco_state() -> dict:
    """all_raw_issues.json 로드 → 레코드 파싱 → recommender(해결 KB) 1회 빌드(캐시)."""
    global _RECO_STATE
    st = _RECO_STATE           # 지역 참조로 고정 — 이후 무효화에 영향받지 않는다
    if st:
        return st
    with _RECO_LOCK:
        if _RECO_STATE:        # 락 대기 중 다른 스레드가 빌드를 마쳤다
            return _RECO_STATE
        return _build_reco_state()


def _build_reco_state() -> dict:
    global _RECO_STATE
    if not ALL_RAW.exists() and not kb_source.extra_paths():
        raise RuntimeError(
            "data/all_raw_issues.json 없음 — 먼저 실행: "
            ".venv/bin/python src/eval_recommender.py (또는 src/ingest.py --status all)")
    # KB 원천은 kb_source 가 단일 소스다(미러 + RVP_KB_EXTRA 보조 원천).
    # 여기서 직접 파일을 읽으면 자기개선 loop 와 다른 KB 를 보게 된다.
    raw = kb_source.raw_issues()
    records = [parse_issue(r) for r in raw]
    resolved = [r for r in records if r["status"] == RESOLVED_STATUS]
    unresolved = [r for r in records if r["status"] != RESOLVED_STATUS]
    # 인입 품질 게이트(P1-2): 무음 추출 실패를 서빙 시점에 표면화(차단 아님, 경고).
    try:
        _q = quality_gate.validate(records, resolved_status=RESOLVED_STATUS)
        if not _q["ok"]:
            print("[server] ⚠ KB 품질 경고: " + " / ".join(_q["violations"]))
    except Exception:
        pass
    # KB 환류: 사람이 승인·수정한 RCA를 큐레이션 KB로 추가(같은 클래스 검색·제안 개선).
    # 1순위는 영속 저장소(data/knowledge_store.json, git 추적), rca_feedback는 폴백.
    # 동일 key는 영속 저장소 우선으로 dedupe.
    try:
        curated, seen = [], set()
        for r in knowledge_store.kb_records() + rca_feedback.kb_records():
            if r["key"] in seen:
                continue
            seen.add(r["key"])
            curated.append(r)
        if curated:
            resolved = resolved + curated
            records = records + curated
    except Exception:
        pass
    new_state = {
        "records": records,
        "by_key": {r["key"]: r for r in records},
        "resolved": resolved,
        "unresolved": unresolved,
        # hybrid_embed + 단계 인지 문서(제기+분석) + 2차 cross-encoder 재순위 기본 활성.
        # 재순위 효과(재검증 2026-07-02): paraphrase P@1 .898→1.0, 게이트 .939→1.0,
        # 무관 차단 .95→1.0. 실패 시 1차 순위 폴백 + 연속 실패 시 자동 비활성(circuit
        # breaker)이라 /rerank 미지원 게이트웨이에서도 무중단. RVP_RERANK=0 으로 끈다.
        # (A/B: tmp_db/ab_reranker.json, claudedocs/similarity_search_plan.md)
        "reco": Recommender(
            resolved,
            method=os.getenv("RVP_RECO_METHOD", "hybrid_embed"),
            rerank=os.getenv("RVP_RERANK", "1") == "1",
            rerank_model=os.getenv("RVP_RERANK_MODEL", "cohere/rerank-v3.5"),
            # 게으른 임베딩(E-1) — 기본 꺼짐. 실서버 A/B 후 판단한다.
            lazy_embed=os.getenv("RVP_LAZY_EMBED", "0") == "1",
            **({"rerank_top_n": int(os.environ["RVP_RERANK_TOP_N"])}
               if os.getenv("RVP_RERANK_TOP_N") else {}),
            # 임베딩 백엔드/모델. 사내 게이트웨이 시 openrouter+bge-m3.
            #
            # 로컬 임베딩(e5-large)을 시도했다가 되돌렸다 — 격리 벤치에서는 질의
            # 임베딩이 485ms→64~348ms 로 빨라 보였지만, **실서버에서는 더 느렸다**:
            # 동일 조건 16건 중앙 bge-m3@API 646ms vs e5@local 1138ms (p90 1036 vs 1793).
            # ONNX CPU 추론이 요청 스레드풀과 경쟁해 네트워크 왕복보다 비싸진다.
            # 격리 벤치로 서버 지연을 예측하지 말 것(claudedocs/performance_backlog.md E-0).
            **env_embed_kwargs(),
            # 파라미터 override(미설정 시 클래스 기본값 — 현행과 동일).
            # 주의: RVP_BOOST 는 현 KB 에서 **효과가 없다**(0.30~0.00 동일, 2026-08-02
            # 실측). 튜닝 대상으로 삼지 말 것 — 근거는 recommender.boost 주석 참조.
            **{kw: float(os.environ[env]) for kw, env in
               (("gate_cos", "RVP_GATE_COS"), ("boost", "RVP_BOOST")) if os.getenv(env)},
        ),
    }
    _RECO_STATE = new_state
    return new_state

app.add_middleware(
    CORSMiddleware,
    # 쿠키 세션을 쓰므로 자격증명 허용이 필요하고, 그러면 와일드카드 오리진은 못 쓴다
    # (브라우저가 거부한다). 개발용 Vite 오리진을 기본 허용하고 RVP_CORS_ORIGINS 로 넓힌다.
    allow_origins=[o for o in (os.getenv(
        "RVP_CORS_ORIGINS",
        "http://localhost:5173,http://127.0.0.1:5173,http://localhost:4173,http://127.0.0.1:4173",
    ).split(",")) if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# 인증(SSO) · 인가(RBAC) — 관리자 / 사용자
#
# 세 경로를 지원한다:
#   oidc  : 사내 IdP 인증 코드 플로우(PKCE). 백엔드가 코드를 교환·검증한다.
#   proxy : 앞단 SSO 프록시가 검증한 이메일 헤더를 신뢰(RVP_SSO_EMAIL_HEADER).
#   dev   : 로컬 개발용 수동 로그인(RVP_AUTH_DEV_LOGIN=1). 운영에서는 켜지 않는다.
#
# 인가 목록(users.yaml / RVP_ADMIN_EMAILS)이 아예 없으면 인증 비활성 = 전체 권한.
# 기존 로컬 흐름을 깨지 않기 위한 것이고, 그 상태는 /auth/config 로 드러난다.
# ---------------------------------------------------------------------------
_USERS: dict[str, auth.User] | None = None
_USERS_LOADED = False


def _users() -> dict[str, auth.User] | None:
    global _USERS, _USERS_LOADED
    if not _USERS_LOADED:
        _USERS = auth.load_users()
        _USERS_LOADED = True
        st = auth.auth_status(_USERS)
        print(f"[auth] {'활성' if st['enabled'] else '비활성(전체 권한)'} · "
              f"users_file={'있음' if st['users_file_present'] else '없음'} · "
              f"admin_env={st['admin_emails_env']} · 기본역할={st['default_role']}")
    return _USERS


def _reload_users() -> None:
    global _USERS_LOADED
    _USERS_LOADED = False
    _users()


def _proxy_email(request: Request) -> str:
    """앞단 SSO 프록시가 넣어 준 이메일 헤더. 헤더 이름이 설정돼야만 신뢰한다.

    기본값을 두지 않는 이유: 임의의 클라이언트가 그 헤더를 직접 보내면 신원을
    가로챌 수 있다. 프록시 뒤에 있다는 사실을 배포자가 명시해야만 켜진다.
    """
    name = os.getenv("RVP_SSO_EMAIL_HEADER", "").strip()
    return request.headers.get(name, "") if name else ""


def _bearer_token(request: Request) -> str:
    """헤더로 온 토큰 — MCP·CLI 처럼 쿠키를 쓸 수 없는 클라이언트용.

    쿠키와 **같은 서명 토큰**이다. 별도 자격증명 체계를 만들지 않는 편이
    검증 경로가 하나로 유지되고, 만료·폐기 규칙도 갈라지지 않는다.
    """
    h = request.headers.get("authorization", "").strip()
    if h.lower().startswith("bearer "):
        return h[7:].strip()
    return request.headers.get("x-rvp-token", "").strip()


def current_user(request: Request) -> auth.User | None:
    """요청의 신원. 인증 비활성이면 ALL_ACCESS, 미인증이면 None."""
    users = _users()
    if users is None:
        return auth.ALL_ACCESS
    tok = request.cookies.get(session.COOKIE_NAME, "") or _bearer_token(request)
    body = session.verify(tok) if tok else None
    if body:
        # sub 가 정식 키. email 은 이전 형식(아이디 계정은 email 이 비어 있어 쓸 수 없다).
        ident = str(body.get("sub") or body.get("email") or "")
        u = auth.resolve_email(users, ident, via=str(body.get("via", "oidc")))
        if u is not None:
            return u                          # 쿠키가 있어도 인가 목록이 기준이다
    email = _proxy_email(request)
    if email:
        return auth.resolve_email(users, email, via="proxy")
    return None


def _actor(request: Request) -> str:
    """검토자 식별자 — 원인 라벨이 누구 판단인지 남긴다(인증 비활성이면 빈 문자열)."""
    try:
        u = current_user(request)
        return (u.subject or u.email or u.name) if u else ""
    except Exception:
        return ""


def require(capability: str):
    """해당 기능 권한이 있어야 통과하는 의존성.

    401(미인증)과 403(권한 없음)을 구분한다 — 프런트가 로그인 유도와 권한 안내를
    다르게 처리해야 한다.
    """
    def dep(request: Request) -> auth.User:
        u = current_user(request)
        if u is None:
            raise HTTPException(status_code=401, detail="로그인이 필요합니다")
        if not u.can(capability):
            raise HTTPException(
                status_code=403,
                detail=f"권한 없음: 이 작업에는 '{capability}' 권한이 필요합니다 (현재 역할: {u.role})")
        return u
    return dep


@app.get("/auth/config")
def auth_config():
    """로그인 화면이 필요한지, 어떤 경로가 열려 있는지 — 인증 전에도 볼 수 있어야 한다."""
    oidc = oidc_sso.settings_from_env()
    return {
        **auth.auth_status(_users()),
        "modes": {
            "oidc": oidc.enabled,
            "proxy": bool(os.getenv("RVP_SSO_EMAIL_HEADER", "").strip()),
            "dev": os.getenv("RVP_AUTH_DEV_LOGIN", "0") == "1",
        },
        "oidc_discovery": oidc.discovery_url,
    }


@app.get("/auth/me")
def auth_me(request: Request):
    u = current_user(request)
    if u is None:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다")
    return u.public()


@app.get("/auth/login")
def auth_login(next: str = ""):
    """IdP 로그인 화면으로 리다이렉트."""
    st = oidc_sso.settings_from_env()
    if not st.enabled:
        raise HTTPException(status_code=503,
                            detail="SSO(OIDC)가 설정되지 않았습니다 — RVP_OIDC_* 를 확인하세요")
    try:
        url = oidc_sso.authorization_url(st, next_url=next)
    except oidc_sso.SsoError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return RedirectResponse(url, status_code=302)


@app.get("/auth/callback")
def auth_callback(code: str = "", state: str = "", error: str = "",
                  error_description: str = ""):
    """IdP 콜백 — 코드 교환 → id_token 검증 → 인가 → 세션 쿠키."""
    st = oidc_sso.settings_from_env()
    if error:
        raise HTTPException(status_code=401, detail=f"IdP 로그인 실패: {error} {error_description}".strip())
    pending = oidc_sso.pop_pending(state)
    if pending is None:
        # state 는 1회용 + 만료됨. 재사용·위조·시간초과를 구분해 알려 주지 않는다.
        raise HTTPException(status_code=400, detail="로그인 요청이 만료되었거나 유효하지 않습니다 — 다시 시도하세요")
    try:
        tok = oidc_sso.exchange_code(st, code, pending.verifier)
        claims = oidc_sso.verify_id_token(st, tok.get("id_token", ""))
    except oidc_sso.SsoError as e:
        raise HTTPException(status_code=401, detail=str(e))
    email = oidc_sso.email_from_claims(st, claims)
    u = auth.resolve_email(_users(), email, via="oidc")
    if u is None:
        raise HTTPException(status_code=403,
                            detail=f"{email or '(이메일 없음)'} 은 이 서비스에 인가되지 않았습니다 — 관리자에게 요청하세요")
    dest = pending.next_url or st.post_login_url or "/"
    resp = RedirectResponse(dest, status_code=302)
    # IdP 토큰은 담지 않는다 — 검증 결과(이메일)만 남긴다.
    resp.set_cookie(session.COOKIE_NAME,
                    session.issue({"sub": u.subject, "via": "oidc"}),
                    **session.cookie_kwargs())
    print(f"[auth] 로그인 {u.email} · 역할 {u.role} · via oidc")
    return resp


class DevLoginBody(BaseModel):
    email: str


@app.post("/auth/dev-login")
def auth_dev_login(body: DevLoginBody, response: Response):
    """로컬 개발용 로그인 — RVP_AUTH_DEV_LOGIN=1 일 때만 열린다.

    인가 목록에 있는 이메일만 받는다. IdP 없이 역할 분리를 확인하기 위한 통로이고,
    운영에서 켜면 이메일만 알면 누구나 그 역할이 되므로 기본값은 꺼짐이다.
    """
    if os.getenv("RVP_AUTH_DEV_LOGIN", "0") != "1":
        raise HTTPException(status_code=404, detail="개발용 로그인이 비활성입니다")
    users = _users()
    if users is None:
        raise HTTPException(status_code=400,
                            detail="인가 목록이 없어 인증이 비활성 상태입니다(이미 전체 권한)")
    u = users.get(auth.normalize_id(body.email))
    if u is None:
        raise HTTPException(status_code=403, detail="인가 목록에 없는 이메일입니다")
    response.set_cookie(session.COOKIE_NAME,
                        session.issue({"sub": u.subject, "via": "dev"}),
                        **session.cookie_kwargs())
    print(f"[auth] 개발 로그인 {u.email} · 역할 {u.role}")
    return {**u.public(), "via": "dev"}


@app.post("/auth/logout")
def auth_logout(response: Response):
    response.delete_cookie(session.COOKIE_NAME, path="/")
    return {"ok": True}


@app.post("/auth/reload")
def auth_reload(_u: auth.User = Depends(require("config.write"))):
    """users.yaml 을 다시 읽는다 — 사용자 추가 후 재기동하지 않기 위함."""
    _reload_users()
    return {"ok": True, **auth.auth_status(_users())}


class TokenBody(BaseModel):
    days: int = 30
    label: str = ""


@app.post("/auth/token")
def auth_issue_token(body: TokenBody, request: Request):
    """MCP·CLI 용 액세스 토큰 발급 — 로그인한 본인 신원으로만 발급된다.

    쿠키와 같은 서명 토큰이라 검증 경로가 하나다. 서버에 저장하지 않으므로
    개별 폐기는 불가능하고, 폐기 수단은 두 가지다:
      · 사용자 회수(users.yaml revoked) — 그 신원의 모든 토큰이 즉시 무효가 된다
        (토큰은 신원만 담고, 권한은 매 요청 인가 목록에서 다시 읽기 때문).
      · RVP_SESSION_SECRET 교체 — 전체 무효화.
    """
    u = current_user(request)
    if u is None:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다")
    if u.via == "disabled":
        raise HTTPException(status_code=400,
                            detail="인증이 비활성 상태입니다 — 토큰이 필요 없습니다(전체 권한)")
    days = max(1, min(int(body.days or 30), 365))
    token = session.issue({"sub": u.subject, "via": "token"}, ttl=days * 86400)
    print(f"[auth] 토큰 발급 {u.subject} · {days}일 · label={body.label or '-'}")
    return {"token": token, "subject": u.subject, "role": u.role,
            "expires_in_days": days,
            "usage": "Authorization: Bearer <token> 또는 X-RVP-Token 헤더",
            "note": "이 값은 다시 보여주지 않습니다. 안전한 곳에 보관하세요."}


# ---------------------------------------------------------------------------
# 사용자 관리 (관리자 전용) — 설정 화면의 "사용자 관리"
#
# 목록을 고친 뒤에는 곧바로 다시 읽는다(_reload_users). 그러지 않으면 방금 등록한
# 사람이 다음 재기동까지 로그인하지 못한다.
# ---------------------------------------------------------------------------
class UserUpsertBody(BaseModel):
    email: str
    name: str = ""
    role: str = "user"


class UserRevokeBody(BaseModel):
    email: str
    revoked: bool = True


@app.get("/auth/users")
def auth_users(_u: auth.User = Depends(require("config.write"))):
    try:
        return user_store.listing()
    except user_store.UserStoreError as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/auth/users")
def auth_users_upsert(body: UserUpsertBody,
                      u: auth.User = Depends(require("config.write"))):
    """사용자·관리자 등록(또는 역할 변경). 회수 상태였다면 함께 복구된다."""
    try:
        out = user_store.upsert(body.email, body.name, body.role, actor=u.email or u.subject)
    except user_store.UserStoreError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _reload_users()
    return {"ok": True, **out, "auth": auth.auth_status(_users())}


@app.post("/auth/users/revoke")
def auth_users_revoke(body: UserRevokeBody,
                      u: auth.User = Depends(require("config.write"))):
    """권한 회수/복구. 자기 자신을 회수하는 것은 막는다 — 실수로 잠기는 경로다."""
    if auth.normalize_id(body.email) == auth.normalize_id(u.subject) and body.revoked:
        raise HTTPException(status_code=400,
                            detail="자기 자신의 권한은 회수할 수 없습니다 — 다른 관리자에게 요청하세요.")
    try:
        out = user_store.revoke(body.email, body.revoked, actor=u.email or u.subject)
    except user_store.UserStoreError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _reload_users()
    return {"ok": True, **out, "auth": auth.auth_status(_users())}


@app.get("/health")
def health():
    return {"ok": True}


# ---------------------------------------------------------------------------
# 설정 온보딩 (LLM(OpenRouter) / Jira) — 미설정 시 프론트가 강제 진입
# ---------------------------------------------------------------------------
class ConfigBody(BaseModel):
    jira: Optional[dict] = None       # {base_url, project_key, email, api_token, pat}
    llm: Optional[dict] = None        # {gateway_url, api_key, model} → OpenRouter(agno)


@app.get("/config/status", dependencies=[Depends(require("issue.read"))])
def config_status():
    return app_config.status()


@app.post("/config", dependencies=[Depends(require("config.write"))])
def config_save(body: ConfigBody):
    st = app_config.save(body.jira, body.llm)
    _invalidate_reco()  # Jira 변경 반영 위해 KB 캐시 무효화
    return st


@app.post("/config/test/jira", dependencies=[Depends(require("config.write"))])
def config_test_jira(body: ConfigBody):
    import requests
    j = body.jira or {}
    base = (j.get("base_url") or os.getenv("JIRA_BASE_URL", "")).rstrip("/")
    project = j.get("project_key") or os.getenv("JIRA_PROJECT_KEY", "")
    if not base or not project:
        return {"ok": False, "error": "base_url 과 project_key 가 필요합니다."}
    s = requests.Session()
    pat = j.get("pat") or os.getenv("JIRA_PAT")
    if pat:
        s.headers["Authorization"] = f"Bearer {pat}"
    else:
        email = j.get("email") or os.getenv("JIRA_EMAIL")
        token = j.get("api_token") or os.getenv("JIRA_API_TOKEN")
        if not (email and token):
            return {"ok": False, "error": "인증 정보 부족: PAT 또는 (email + API token)"}
        s.auth = (email, token)
    try:
        r = s.get(f"{base}/rest/api/2/myself", timeout=15)
        r.raise_for_status()
        me = r.json().get("displayName") or r.json().get("name")
        rp = s.get(f"{base}/rest/api/2/project/{project}", timeout=15)
        rp.raise_for_status()
        return {"ok": True, "user": me, "project": rp.json().get("name")}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


@app.post("/config/test/llm", dependencies=[Depends(require("config.write"))])
def config_test_llm(body: ConfigBody):
    """OpenRouter(agno) 연결 테스트 — /models 조회."""
    import requests
    h = body.llm or {}
    base = (h.get("gateway_url") or os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")).rstrip("/")
    key = h.get("api_key") or os.getenv("OPENROUTER_API_KEY")
    if not key:
        return {"ok": False, "error": "API key 가 필요합니다."}
    try:
        r = requests.get(f"{base}/models", headers={"Authorization": f"Bearer {key}", **custom_headers()}, timeout=15)
        r.raise_for_status()
        n = len(r.json().get("data", []))
        return {"ok": True, "models": n}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


# ---------------------------------------------------------------------------
# Jira 웹훅 — 새 이슈/변경/삭제 시 KB(all_raw_issues.json) 증분 갱신 + 캐시 무효화
# ---------------------------------------------------------------------------
def _upsert_raw_issue(issue: dict) -> int:
    """단건 raw 이슈를 ALL_RAW에 upsert(키 기준 교체/추가). 반환: 총 이슈 수."""
    data = read_json(ALL_RAW, [])
    if not isinstance(data, list):
        data = []
    out = [r for r in data if r.get("key") != issue.get("key")]
    out.append(issue)
    write_json_atomic(ALL_RAW, out)
    return len(out)


def _delete_raw_issue(key: str) -> int:
    """ALL_RAW에서 해당 키 이슈 제거. 반환: 총 이슈 수."""
    data = read_json(ALL_RAW, [])
    if not isinstance(data, list):
        return 0
    out = [r for r in data if r.get("key") != key]
    write_json_atomic(ALL_RAW, out)
    return len(out)


@app.post("/webhook/jira")
def jira_webhook(body: dict, secret: str = ""):
    """Jira 웹훅 수신 — 이슈 생성/변경/삭제 시 해당 이슈만 재적재(증분) 후 KB 캐시 무효화.

    Jira 웹훅 URL에 ?secret=... 를 넣고 JIRA_WEBHOOK_SECRET 와 일치해야 처리(설정 시).
    payload의 issue.key만 신뢰하고 api/2로 직접 재조회(KB 포맷 일관성). 빠르게 200 반환.
    """
    expected = os.getenv("JIRA_WEBHOOK_SECRET", "")
    if expected and secret != expected:
        raise HTTPException(status_code=401, detail="invalid webhook secret")
    event = (body.get("webhookEvent") or "").lower()
    issue = body.get("issue") or {}
    key = issue.get("key", "")
    proj = os.getenv("JIRA_PROJECT_KEY", "LSI")
    if not key or not key.startswith(proj + "-"):
        return {"ok": True, "skipped": "no/other-project key", "key": key, "event": event}
    try:
        if "issue_deleted" in event:
            n = _delete_raw_issue(key)
            action = "deleted"
        else:                                   # created/updated/comment 등 → 재조회 upsert
            from ingest import fetch_issue
            n = _upsert_raw_issue(fetch_issue(key))
            action = "upserted"
        _invalidate_reco()                     # 다음 요청 시 갱신된 KB로 재빌드
        return {"ok": True, "action": action, "key": key, "event": event, "kb_total": n}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200], "key": key, "event": event}


# ---------------------------------------------------------------------------
# Jira 폴링 동기화 — 웹훅과 같은 결과를 공개 URL 없이 얻는 경로.
# Jira Cloud가 로컬 서버에 도달할 수 없는 환경에서 기본 갱신 수단으로 쓴다.
# 웹훅을 등록해 두면 둘 다 동작해도 무해하다(같은 upsert를 중복 수행할 뿐).
# ---------------------------------------------------------------------------
_JIRA_POLL: dict = {"thread": None, "stop": None, "last": None, "error": None,
                    "started_at": None, "polls": 0, "invalidations": 0}


def _jira_poll_loop(interval: int, stop: threading.Event) -> None:
    import jira_sync
    while not stop.wait(interval):            # 첫 폴도 interval 후 — 기동을 막지 않는다
        try:
            r = jira_sync.sync()
            _JIRA_POLL["last"] = r
            _JIRA_POLL["error"] = None
            _JIRA_POLL["polls"] += 1
            # 변경이 있을 때만 무효화 — 무효화는 전체 재빌드(임베딩 캐시 미스 시 수 초)를
            # 부르므로 빈 폴에서 지불하면 순손해다.
            if r.get("changed"):
                _invalidate_reco()
                _JIRA_POLL["invalidations"] += 1
                print(f"[jira_poll] KB 갱신 {r} → 추천 캐시 무효화")
                # 바뀐 이슈만 키가 달라지므로, 예열을 다시 돌려도 나머지는 건너뛴다.
                _start_prewarm()
        except Exception as e:
            _JIRA_POLL["error"] = str(e)[:200]
            print(f"[jira_poll] 실패(다음 주기에 재시도): {str(e)[:160]}")


def _start_jira_poller() -> None:
    """RVP_JIRA_POLL_SEC 주기로 백그라운드 폴링 시작. 0 이면 비활성.

    기본 5초 근거(실측 2026-08-01): 무변경 폴 1회 = JQL 1건 240ms(중앙), 삭제 대조
    포함 회차 780ms. 5초면 하루 17,280회 — 폴 하나가 주기의 5%만 점유하므로 Jira
    레이트 리밋 대비 여유가 크고, 평균 반영 지연 2.5초(최악 5초).
    이보다 짧게 가려면 폴링이 아니라 웹훅(공개 URL 필요)으로 바꿔야 한다.
    """
    interval = int(os.getenv("RVP_JIRA_POLL_SEC", "5") or 0)
    if interval <= 0 or not os.getenv("JIRA_BASE_URL"):
        print("[jira_poll] 비활성 (RVP_JIRA_POLL_SEC=0 또는 JIRA_BASE_URL 없음)")
        return
    stop = threading.Event()
    t = threading.Thread(target=_jira_poll_loop, args=(interval, stop),
                         name="jira-poll", daemon=True)
    _JIRA_POLL.update({"thread": t, "stop": stop,
                       "started_at": _dt.datetime.now().isoformat(timespec="seconds")})
    t.start()
    print(f"[jira_poll] {interval}초 주기 폴링 시작")


def _stop_jira_poller() -> None:
    stop = _JIRA_POLL.get("stop")
    if stop:
        stop.set()


@app.get("/jira/sync/status", dependencies=[Depends(require("issue.read"))])
def jira_sync_status():
    """폴러 상태 + 마지막 동기화 결과."""
    import jira_sync
    return {
        "poll_interval_sec": int(os.getenv("RVP_JIRA_POLL_SEC", "5") or 0),
        "running": bool(_JIRA_POLL["thread"] and _JIRA_POLL["thread"].is_alive()),
        "started_at": _JIRA_POLL["started_at"],
        "polls": _JIRA_POLL["polls"],
        "invalidations": _JIRA_POLL["invalidations"],
        "last": _JIRA_POLL["last"],
        "error": _JIRA_POLL["error"],
        "state": jira_sync.load_state(),
    }


@app.post("/jira/sync", dependencies=[Depends(require("ops.sync"))])
def jira_sync_now(full: bool = False, reconcile: bool = False):
    """수동 동기화 — 폴 주기를 기다리지 않고 즉시 반영."""
    import jira_sync
    try:
        r = jira_sync.sync(full=full, reconcile=reconcile)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Jira 동기화 실패: {str(e)[:200]}")
    if r.get("changed"):
        _invalidate_reco()
    return {"ok": True, **r}


# ---------------------------------------------------------------------------
# 고장 분석 추천 엔드포인트
# ---------------------------------------------------------------------------


class RecommendRequest(BaseModel):
    key: Optional[str] = None          # 미해결 이슈 키 (예: LSI-7)
    summary: Optional[str] = None      # 또는 자유 입력
    symptom: Optional[str] = None
    chip: Optional[str] = None
    category: Optional[str] = None
    labels: Optional[list[str]] = None
    k: int = 3
    explain: bool = False              # LLM으로 종합 설명 생성


@app.get("/reco/stats", dependencies=[Depends(require("issue.read"))])
def reco_stats():
    st = _reco_state()
    reco = st["reco"]
    from collections import Counter
    cats = Counter(r["category"] for r in st["resolved"])
    return {
        "resolved": len(st["resolved"]),
        "unresolved": len(st["unresolved"]),
        "templates": len(set(template_key(r["summary"]) for r in st["resolved"])),
        "by_category": dict(cats),
        "method": reco.method,
    }


class RecoFeedbackBody(BaseModel):
    query_key: str = ""
    query_summary: str = ""
    match_key: str
    rating: str                       # "helpful" | "not_helpful"
    is_actual_root_cause: bool = False
    match_rank: Optional[int] = None
    match_score: Optional[float] = None
    note: str = ""


@app.post("/reco/feedback", dependencies=[Depends(require("feedback.write"))])
def reco_feedback_record(req: RecoFeedbackBody):  # 함수명: 모듈 reco_feedback과 충돌 회피
    """추천 유용성/결과 피드백 기록(P1-3) — 도움됨·아님, 실제 근본원인 여부."""
    try:
        ev = reco_feedback.record(
            query_key=req.query_key, match_key=req.match_key, rating=req.rating,
            query_summary=req.query_summary,
            query_template=template_key(req.query_summary) if req.query_summary else "",
            is_actual_root_cause=req.is_actual_root_cause,
            match_rank=req.match_rank, match_score=req.match_score, note=req.note)
        return {"ok": True, "event": ev, "stats": reco_feedback.stats()}
    except ValueError as e:
        return {"ok": False, "error": str(e)}


@app.get("/reco/feedback/stats", dependencies=[Depends(require("knowledge.read"))])
def reco_feedback_stats():
    """유용성 집계 + ROI 프록시 + 실전형 평가셋 정답 쌍."""
    return {"stats": reco_feedback.stats(), "eval_pairs": reco_feedback.eval_pairs()}


# ---------------------------------------------------------------------------
# VOC (Voice of Customer) — 서비스 자체에 대한 사용자 피드백
# ---------------------------------------------------------------------------
class VocBody(BaseModel):
    category: str = "other"      # bug | improvement | praise | question | other
    message: str
    author: str = ""
    context: str = ""            # 어느 화면/맥락에서 남겼는지(선택)


@app.post("/voc", dependencies=[Depends(require("feedback.write"))])
def voc_submit(req: VocBody):
    """VOC 등록(버그·개선요청·칭찬·문의)."""
    import voc_store
    try:
        return {"ok": True, "item": voc_store.add(req.category, req.message,
                                                  author=req.author, context=req.context),
                "stats": voc_store.stats()}
    except ValueError as e:
        return {"ok": False, "error": str(e)}


@app.get("/voc", dependencies=[Depends(require("voc.manage"))])
def voc_list(state: str = ""):
    """VOC 목록 + 집계."""
    import voc_store
    return {"items": voc_store.items(state), "stats": voc_store.stats()}


class VocStateBody(BaseModel):
    id: str
    state: str                   # open | triaged | resolved | wont_fix


@app.post("/voc/state", dependencies=[Depends(require("voc.manage"))])
def voc_set_state(req: VocStateBody):
    """VOC 상태 변경(분류/해결/보류)."""
    import voc_store
    try:
        it = voc_store.set_state(req.id, req.state)
        return {"ok": bool(it), "item": it, "stats": voc_store.stats()}
    except ValueError as e:
        return {"ok": False, "error": str(e)}


@app.get("/issues/unresolved", dependencies=[Depends(require("issue.read"))])
def unresolved_issues():
    st = _reco_state()
    out = []
    for r in st["unresolved"]:
        out.append({
            "key": r["key"], "summary": r["summary"], "status": r["status"],
            "chip": r["chip"], "category": r["category"],
            "priority": r["priority"], "severity": r.get("severity", ""),
            "symptom": r["symptom"],
        })
    # 상태(진행 중 먼저) → 키 순
    out.sort(key=lambda x: (0 if x["status"] == "진행 중" else 1, x["key"]))
    return {"count": len(out), "issues": out}


@app.get("/graph", dependencies=[Depends(require("issue.read"))])
def issue_graph(key: Optional[str] = None, k: int = 12, min_shared: int = 2):
    """이슈 간 관계 그래프 — 공유 엔티티(칩/분류/기술용어/라벨) 기반.

    key 지정 시: 그 이슈 중심 ego-그래프(가장 많이 겹치는 이웃 top-k + 이웃 간 엣지).
    미지정 시: 미해결 이슈를 시드로 한 소규모 샘플.
    엣지 가중치=공유 엔티티 수, same_template=동일 근본원인 클래스(굵게 표시용).
    """
    st = _reco_state()
    recs = st["records"]
    by_key = st["by_key"]
    ent = {r["key"]: set(r.get("entities", [])) for r in recs}

    from recommender import _doc_text  # KB 문서 표현(요약+증상+분석) 재사용

    rr: dict[str, float] = {}   # center→이웃 rerank 관련도(0~1)
    if key and key in by_key:
        c = ent[key]
        scored = sorted(
            ((r, len(c & ent[r["key"]])) for r in recs if r["key"] != key),
            key=lambda x: -x[1])
        neigh = [r for r, w in scored if w >= min_shared][:k]
        # 엣지 강도를 reranker(cross-encoder)로 계산 — center를 질의로, 이웃을 문서로.
        # 1회 호출. 실패/미설정 시 공유 엔티티 가중치로 폴백.
        try:
            from reranker import rerank as _rerank
            docs = [_doc_text(r, analysis=True) for r in neigh]
            # 타임아웃을 명시한다 — 기본 60초라 게이트웨이가 /rerank 를 미지원하면
            # 이슈를 하나 고를 때마다 1분씩 멈춘다(Recommender 는 같은 이유로 10초).
            order = _rerank(_doc_text(by_key[key], analysis=True), docs, timeout=10)
            rr = {neigh[idx]["key"]: float(sc) for idx, sc in order}
            neigh.sort(key=lambda r: -rr.get(r["key"], 0.0))  # 관련도 내림차순
        except Exception:
            rr = {}
        nodeset = [by_key[key]] + neigh
    else:
        nodeset = st["unresolved"][:k] or recs[:k]

    ekeys = [r["key"] for r in nodeset]
    nodes = [{
        "id": r["key"], "label": r["summary"], "status": r["status"],
        "category": r["category"], "chip": r["chip"],
        "template": template_key(r["summary"]),
        "center": bool(key) and r["key"] == key,
        # center 대비 rerank 관련도(0~1) — 노드 크기/거리 인코딩용. center=1.0.
        "relevance": 1.0 if (key and r["key"] == key) else rr.get(r["key"]),
    } for r in nodeset]
    edges = []
    for i in range(len(ekeys)):
        for j in range(i + 1, len(ekeys)):
            a, b = ekeys[i], ekeys[j]
            w = len(ent[a] & ent[b])
            if w < min_shared:
                continue
            touches_center = bool(key) and (a == key or b == key)
            other = (b if a == key else a) if touches_center else None
            edges.append({
                "source": a, "target": b, "weight": w,
                "same_template": (template_key(by_key[a]["summary"])
                                  == template_key(by_key[b]["summary"])),
                # center 엣지는 rerank 관련도(0~1)를 강도로 — 굵기/투명도 인코딩.
                "rerank": rr.get(other) if touches_center else None,
            })
    return {"center": key, "nodes": nodes, "edges": edges, "has_rerank": bool(rr)}


class RcaExplanation(BaseModel):
    """LLM 종합 분석의 구조화 출력 (agno output_schema)."""
    root_cause: str = Field(description="예상 근본 원인 (한국어, 간결)")
    resolution: str = Field(description="권장 해결 단계 (한국어, 간결)")
    workaround: str = Field(default="", description="임시 우회책 (한국어, 없으면 빈 문자열)")
    cited_keys: list[str] = Field(
        default_factory=list,
        description="분석 근거로 인용한 과거 이슈 키 목록. 반드시 제공된 '과거 해결 사례'의 키 중에서만 선택(새 키 창작 금지). 예: LSI-49")


def _explain_prompt(query_rec: dict, matches: list[dict]) -> str:
    cases = "\n\n".join(
        f"[{m['key']}] {m['summary']}\n근본원인: {m['root_cause']}\n해결책: {m['resolution']}\n우회책: {m['workaround']}"
        for m in matches)
    return (
        "당신은 LSI 칩/펌웨어 불량 분석 시니어 엔지니어입니다. 아래 미해결 이슈에 대해, "
        "제공된 '과거 해결 사례'만 근거로 예상 근본원인·권장 해결 단계·임시 우회책을 한국어로 간결히 작성하세요. "
        "cited_keys에는 근거로 쓴 과거 사례의 키만 넣으세요(새 키 창작 금지). 한자/CJK 한자 금지.\n\n"
        f"## 미해결 이슈\n{query_rec.get('summary','')}\n증상: {query_rec.get('symptom','')}\n"
        f"칩: {query_rec.get('chip','')} / 분류: {query_rec.get('category','')}\n\n"
        f"## 과거 해결 사례\n{cases}\n")


def _agno_explain(prompt: str) -> "RcaExplanation | None":
    """agno Agent + output_schema 로 구조화 RCA 생성 (citations 포함)."""
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        return None
    from agno.agent import Agent
    from agno.models.openrouter import OpenRouter
    model_id = os.getenv("RVP_MODEL") or os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini")
    base = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    agent = Agent(
        model=OpenRouter(id=model_id, api_key=api_key, base_url=base,
                         default_headers=custom_headers() or None),
        output_schema=RcaExplanation,
        use_json_mode=True,  # 모델 무관 호환(네이티브 structured 미지원 모델 대비)
        instructions=[
            "LSI 칩/펌웨어 불량 분석 시니어 엔지니어로서 답한다.",
            "제공된 '과거 해결 사례'만 근거로 사용하고, cited_keys에는 그 사례의 키만 넣는다(창작 금지).",
            "모든 텍스트는 한국어. 한자/CJK 한자 금지 — 한글/영문/숫자/문장부호만.",
        ],
        markdown=False, telemetry=False,
    )
    out = agent.run(input=prompt)
    return out.content if isinstance(out.content, RcaExplanation) else None


def mark_unsupported(md: str, dropped: list[str]) -> str:
    """본문이 언급한 **미제공 사례 키**를 그 자리에서 표시한다.

    왜 필요한가(실측 2026-08-04, 설명 30건 중 2건):
      LSI-112 의 본문은 근거에 없는 LSI-70·LSI-133 을 인용한다. 모델 스스로
      "본문에 직접 키가 명시되지 않았지만 LSI-7-rca 에 포함된 것으로 간주됩니다"
      라고 적는다 — 즉 지어낸 것이다.

    시스템은 이걸 이미 감지해 `dropped` 로 분리했다. 그런데 **본문은 그대로 두고**
    화면에는 "매치 외 인용 제거됨" 이라고 적었다. 제거된 것이 없으므로 거짓이다.
    사용자는 본문을 읽고 LSI-70 을 찾으러 간다.

    LLM 으로 다시 쓰지 않는다 — 문서 전체를 LLM 에 맡겼다가 무관한 문장의 "펌웨어"가
    "펌 - 만웨어"로 깨진 적이 있다. 여기서는 **정확히 그 키 토큰만** 결정적으로
    치환한다(코드 블록·URL 안은 건드리지 않는다).
    """
    if not dropped:
        return md
    import re as _re
    marked = md
    for k in sorted(set(dropped), key=len, reverse=True):
        if not issue_keys.is_key(k):
            continue                       # 예상 밖 형식이면 손대지 않는다
        # 이미 표시된 것/코드 블록(`...`)/링크 대상은 제외
        pat = _re.compile(rf"(?<![\w-]){_re.escape(k)}(?![\w-])(?!\(미제공\))")
        out, last, buf = [], 0, marked
        for seg, is_code in _split_code(buf):
            out.append(seg if is_code else pat.sub(f"{k}(미제공)", seg))
        marked = "".join(out)
    return marked


def _split_code(md: str):
    """(구간, 코드여부) 로 쪼갠다 — 코드 블록·인라인 코드는 치환 대상에서 뺀다."""
    import re as _re
    parts, pos = [], 0
    for m in _re.finditer(r"```.*?```|`[^`\n]*`", md, flags=_re.S):
        if m.start() > pos:
            parts.append((md[pos:m.start()], False))
        parts.append((m.group(0), True))
        pos = m.end()
    if pos < len(md):
        parts.append((md[pos:], False))
    return parts


def _compose_explanation(exp: "RcaExplanation", valid_keys: set[str]) -> tuple[str, list[str], list[str]]:
    """구조화 출력 → 표시용 마크다운 + 검증된 인용/탈락 인용. (인용 게이트가 구조적으로 해결됨)"""
    cited = [k for k in exp.cited_keys if k in valid_keys]
    dropped = [k for k in exp.cited_keys if k not in valid_keys]  # 환각/무관 키
    md = f"### 🔍 예상 근본원인\n{exp.root_cause}\n\n### ✅ 권장 해결책\n{exp.resolution}\n"
    if (exp.workaround or "").strip():
        md += f"\n### ↪ 임시 우회책\n{exp.workaround}\n"
    md += f"\n_근거(검증됨): {', '.join(cited)}_" if cited else "\n_근거로 인용된 과거 사례 없음_"
    # 본문이 언급한 미제공 키를 그 자리에 표시 — 목록만 따로 보여주면 본문을 읽는
    # 사람에게는 닿지 않는다.
    return mark_unsupported(md, dropped), cited, dropped


def _llm_explain(query_rec: dict, matches: list[dict]) -> dict:
    """상위 매치 근거로 종합 설명 생성. 반환: {markdown, citations, dropped}.

    agno output_schema 로 구조화 출력 → cited_keys 를 매치 키와 대조 검증해
    환각 인용을 제거(기존 정규식 인용 게이트를 구조적으로 대체).
    """
    model = os.getenv("RVP_MODEL") or os.getenv("OPENROUTER_MODEL", "")
    ckey = llm_cache.make_key("explain_struct", query_rec, matches, model)
    hit = llm_cache.get(ckey)
    if hit is not None:
        return {**hit, "cached": True}
    prompt = _explain_prompt(query_rec, matches)
    valid_keys = {m["key"] for m in matches}
    try:
        exp = _agno_explain(prompt)
    except Exception as e:
        # 실패는 캐시하지 않는다 — 일시적 오류를 영구히 되돌려주게 된다.
        return {"markdown": f"(LLM 설명 생성 실패: {e})", "citations": [], "dropped": []}
    if exp is None:
        return {"markdown": "", "citations": [], "dropped": []}
    md, cited, dropped = _compose_explanation(exp, valid_keys)
    vr = validate_and_fix(md)  # CJK 안전망
    if not vr.ok and vr.rewritten:
        md = vr.rewritten
    out = {"markdown": md, "citations": cited, "dropped": dropped}
    llm_cache.put(ckey, out, meta={"query_key": query_rec.get("key", ""),
                                   "evidence": [m.get("key", "") for m in matches]})
    return {**out, "cached": False}


# 큐레이션 RCA 기사(`{원본}-rca`)의 본문에는 다른 사례 키가 박혀 있다 — 사람이 쓴
# 종합 분석이라 "참고 사례: LSI-36, LSI-183, LSI-205" 같은 줄이 그대로 들어간다.
# 실측(2026-08-05): 내용이 있는 -rca 4건 **전부** 그렇다.
#
# 이게 "인용 환각" 으로 보이던 것의 정체다. 모델이 지어낸 게 아니라 **프롬프트에
# 보이는 키를 충실히 옮긴 것**이고, 검증기는 그 키가 근거 목록에 없으니 환각으로
# 셌다. A/B 로 잡힌 4건이 예외 없이 여기 해당했다(근거 본문에 실제로 존재).
#
# 읽는 사람은 그 사례를 볼 수 없다 — 근거로 제시되지 않았기 때문이다. 그래서
# 직렬화 단계에서 지운다. 모델을 탓하는 대신 입력을 고치는 쪽이 맞다.
_EMBEDDED_KEY = _re_embedded = None


def _strip_embedded_keys(text: str, keep: set[str]) -> str:
    """근거 본문에 박힌 다른 사례 키를 지운다. `keep`(이번 근거 목록)은 남긴다."""
    import re as _re
    # "참고 사례: LSI-36, LSI-183, LSI-205" 같은 줄은 통째로 뺀다
    text = _re.sub(r"참고 사례\s*:\s*[^\n]*\n?", "", text)
    def sub(m):
        k = m.group(0)
        return k if k in keep else "(다른 사례)"
    return _re.sub(issue_keys.STEM, sub, text)


def _case_block(r: dict, keep: set[str] | None = None) -> str:
    """근거 사례를 풍부하게 직렬화 — 증상/디버깅 접근/근본원인/해결책/우회책.

    keep 이 주어지면 본문에 박힌 **다른** 사례 키를 지운다(위 주석 참조).
    """
    parts = [f"[{r.get('key','')}] {r.get('summary','')}"]
    for label, field in (("증상", "symptom"), ("디버깅 접근", "debug_approach"),
                         ("근본원인", "root_cause"), ("해결책", "resolution"), ("우회책", "workaround")):
        v = (r.get(field) or "").strip()
        if v:
            parts.append(f"{label}: {v}")
    out = "\n".join(parts)
    return _strip_embedded_keys(out, keep) if keep is not None else out


def _example_key(match_recs: list[dict]) -> str:
    """인용 형식 예시에 쓸 키 — 이번 근거에서 고른다(예시가 새는 것을 막는다)."""
    for r in match_recs:
        k = str(r.get("key", ""))
        if k:
            return k
    return issue_keys.placeholder()


def _allowed_keys_block(match_recs: list[dict]) -> str:
    """본문에서 쓸 수 있는 사례 키를 **닫힌 목록으로** 못 박는다.

    기존 규칙은 "제공된 키만, 창작 금지" 였는데, 허용 키를 나열하지 않아 모델이
    사례 블록에서 유추해야 했다. 실측(2026-08-04, 설명 30건)에서 근거에 `-rca` 키가
    섞인 6건 중 2건이 본문에 없는 사례를 인용했다 — LSI-112 는 "LSI-70과 LSI-133은
    LSI-7-rca에 포함된 것으로 간주됩니다" 라고 **스스로 지어냈다**. 종합 RCA 문서가
    다른 사례를 품고 있다고 추측한 것이다.
    같은 질의·같은 근거로 만든 캐시 두 건 중 하나만 환각이 있었다 — 확률적이라
    규칙을 더 좁게 주는 것이 맞다.

    **기본 꺼짐.** 효과가 아직 측정되지 않았다 — A/B(`scripts/ab_citation_keylist.py`)
    가 끝나기 전에 켜면 "고쳤다" 는 근거 없는 주장이 된다. 측정 후 기본값을 정한다.
    RVP_EXPLAIN_KEYLIST=1 로 켠다.
    """
    if os.getenv("RVP_EXPLAIN_KEYLIST", "0") != "1":
        return ""
    keys = [str(r.get("key", "")) for r in match_recs if r.get("key")]
    if not keys:
        return ""
    return ("\n\n## 사용 가능한 사례 키(닫힌 목록)\n"
            + ", ".join(keys)
            + "\n이 목록에 없는 LSI-번호는 **본문 어디에도** 쓰지 마세요. "
              "위 사례가 다른 사례를 '포함한다'고 추측하지 마세요 — 목록이 전부입니다.\n")


def _explain_prompt_md(query_rec: dict, match_recs: list[dict]) -> str:
    """스트리밍용 심화 분석 프롬프트 — 인과/사례종합/검증방법/재발방지/불확실성 + 인라인 인용."""
    # 이번 근거 목록에 있는 키만 남긴다 — 본문에 박힌 다른 사례 키는 읽는 사람이
    # 볼 수 없으므로 프롬프트에서 지운다.
    keep = {str(r.get("key", "")) for r in match_recs}
    keep = issue_keys.expand(keep)
    keep.add(str(query_rec.get("key", "")))
    cases = "\n\n".join(_case_block(r, keep) for r in match_recs)
    q = query_rec
    q_extra = f"\n진행 단서(조사/트리아지): {q.get('investigation','')}" if (q.get("investigation") or "").strip() else ""
    # 성능 개선 루프: 사람이 검토·수정한 과거 분석을 문체/수준 가이드(few-shot)로 주입
    fewshot = ""
    try:
        # 같은 고장 클래스(동일 템플릿/분류)의 사람 수정만 — 무관 이슈 예시 주입 방지
        ex = rca_feedback.relevant_edits(category=q.get("category", ""),
                                         template=template_key(q.get("summary", "")),
                                         n=2, max_len=450)
        if ex:
            blocks = "\n\n".join(f"[{e['key']}] {e['summary'][:50]}\n{e['final_body']}" for e in ex)
            fewshot = ("\n\n## 같은 유형에서 사람이 검토·수정한 분석 예시 (문체·정정 방향 참고, 내용 복붙 금지)\n"
                       + blocks + "\n")
    except Exception:
        pass
    # 초안 결함 원인 환류: 같은 고장 클래스에서 **반복 지적된** 항목을 규칙으로 주입.
    # rca_feedback few-shot 이 "이렇게 써라"(모범)라면, 이건 "이건 하지 마라"(실패)다.
    # 둘 다 필요하다 — 모범만 보여주면 모델은 자기가 반복하는 실수를 못 본다.
    # 1회 지적은 규칙이 되지 않는다(GUIDANCE_MIN_COUNT) — 노이즈가 규칙이 되면
    # 프롬프트가 한 사람의 취향으로 흘러간다.
    guidance = ""
    try:
        guidance = draft_feedback.prompt_guidance(
            category=q.get("category", ""), template=template_key(q.get("summary", "")))
    except Exception:
        pass
    # 부정지식(P2-7): 질의·근거 사례에서 이미 기각된 가설을 주입 → 재안 방지
    negatives = ""
    try:
        keys = [k for k in [query_rec.get("key")] if k] + [r.get("key") for r in match_recs]
        negatives = negative_knowledge.prompt_block(keys)
    except Exception:
        pass
    return (
        "당신은 LSI 칩/펌웨어 불량 분석 시니어 엔지니어입니다. 제공된 '과거 해결 사례'만 근거로 "
        "아래 미해결 이슈를 깊이 있게 분석하세요. 다음 섹션을 순서대로 **모두 빠짐없이** 한국어 마크다운으로 작성합니다:\n"
        "### 🎯 예상 근본원인\n"
        "### 🔍 증상→원인 인과 분석  (관찰 증상이 어떤 메커니즘으로 해당 원인을 시사하는지 단계적으로)\n"
        "### ✅ 권장 해결 단계  (번호가 있는 구체적 순서)\n"
        "### ↪ 임시 우회책\n"
        "### 🔬 근본원인 검증 방법  (어떤 신호·측정·재현 절차로 확인하는지 구체적으로)\n"
        "### 🧩 사례 종합 / 재발 방지  (인용 사례의 공통 패턴·차이점 + 예방 포인트)\n"
        "### ⚠ 불확실성·주의  (근거가 약하거나 사례와 다른 부분)\n"
        # 형식 예시에 **진짜처럼 보이는 키**를 쓰면 모델이 그대로 베낀다 — 실측에서
        # LSI-141 설명이 근거에 없는 LSI-49 를 인용했는데, 출처가 바로 이 예시였다.
        # 이번 근거의 첫 키를 예시로 쓴다: 유효한 예시이면서 새는 키가 될 수 없다.
        f"각 핵심 주장 옆에 근거 사례 키를 ({_example_key(match_recs)})처럼 인라인 "
        "인용하세요(제공된 키만, 창작 금지). 한자/CJK 한자 금지.\n"
        "**근거 구분(중요)**: 제공된 사례에서 확인되는 내용과 일반 도메인 지식을 섞지 마세요.\n"
        " - 사례로 확인되는 주장 → 반드시 인라인 인용을 답니다.\n"
        " - 사례에 없지만 이해를 돕는 배경 설명(규격 정의·일반 동작 원리 등) → 문장 앞에 "
        "`(배경)` 을 붙입니다. 이 표시가 없는 문장은 사례로 뒷받침된다는 뜻입니다.\n"
        " - 이 이슈에 대한 **추정**은 `(추정)` 을 붙입니다.\n"
        "엔지니어가 '어디까지가 검증된 사례이고 어디부터가 일반 지식인지' 를 한눈에 "
        "구분할 수 있어야 합니다 — 그 구분이 안 되면 분석을 신뢰할 수 없습니다.\n\n"
        f"## 미해결 이슈\n{q.get('summary','')}\n증상: {q.get('symptom','')}\n"
        f"칩: {q.get('chip','')} / 분류: {q.get('category','')}{q_extra}\n\n"
        f"## 과거 해결 사례\n{cases}\n{fewshot}{guidance}{negatives}"
        + _allowed_keys_block(match_recs))


def _llm_stream(prompt: str, reasoning: bool = False):
    """OpenRouter chat/completions 스트리밍 — 콘텐츠 토큰(str)만 순차 yield.

    agno 스트리밍 래퍼는 추론 모델(deepseek-v4-flash 등)에서 콘텐츠 스트림을
    조기 종료시켜 답변이 헤더/문장 도중에 잘리는 문제가 있다(비스트리밍/직접
    스트리밍은 정상 완결). 따라서 OpenRouter SSE를 직접 호출한다. 추론 델타는
    별도 'reasoning' 필드로 오므로 무시하고 최종 콘텐츠만 전송한다.
    """
    import urllib.request
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        return
    model_id = os.getenv("RVP_MODEL") or os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini")
    base = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    # 한국어는 토큰 소모가 커 상한이 낮으면 도중에 잘린다. 기본 8000, env로 조정.
    max_tokens = int(os.getenv("RVP_EXPLAIN_MAX_TOKENS", "8000"))
    sys_msg = (
        "LSI 칩/펌웨어 불량 분석 시니어 엔지니어로서 한국어 마크다운으로 깊이 있게 답한다. "
        "지시된 모든 섹션을 순서대로 빠짐없이 작성한다(특히 권장 해결 단계·우회책 누락 금지). "
        "제공된 '과거 해결 사례'만 근거로 사용하고, 근거 키는 (LSI-49)처럼 본문에 인라인 인용한다. "
        "표면적 요약이 아니라 메커니즘 수준의 인과와 검증 방법까지 제시한다. "
        "한자/CJK 한자 금지 — 한글/영문/숫자/문장부호만."
    )
    payload = {
        "model": model_id, "max_tokens": max_tokens, "stream": True,
        "messages": [{"role": "system", "content": sys_msg},
                     {"role": "user", "content": prompt}],
    }
    req = urllib.request.Request(
        base.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json",
                 **custom_headers()})
    with urllib.request.urlopen(req, timeout=180) as resp:
        for raw in resp:
            line = raw.decode("utf-8").strip()
            if not line.startswith("data: "):
                continue
            data = line[6:]
            if data == "[DONE]":
                break
            try:
                ev = json.loads(data)
            except Exception:
                continue
            piece = ev.get("choices", [{}])[0].get("delta", {}).get("content")
            if piece:
                yield piece


# 검색 결과 캐시 — 같은 질의에 임베딩·rerank API를 두 번 지불하지 않기 위함.
# KB가 바뀌면 _RECO_STATE 가 통째로 교체되므로, 캐시도 그 상태 객체에 매달아 둔다
# (상태가 새로 만들어지면 캐시도 자연히 비워진다). 크기는 작게 유지 — 목적은
# "방금 본 이슈를 곧바로 다시 조회하는" 경로를 없애는 것이지 장기 보관이 아니다.
_RECO_CACHE_MAX = 64


def _recommend_cached(query_rec: dict, k: int, exclude_key: Optional[str]) -> dict:
    _t0 = time.perf_counter()
    st = _reco_state()
    cache: dict = st.setdefault("_reco_cache", {})
    ck = (query_rec.get("key") or "", k, exclude_key or "",
          "" if query_rec.get("key") else llm_cache.issue_fingerprint(query_rec))
    hit = cache.get(ck)
    was_hit = hit is not None
    if hit is None:
        hit = st["reco"].recommend(query_rec, k=k, exclude_key=exclude_key)
        if len(cache) >= _RECO_CACHE_MAX:
            cache.clear()                   # 단순 비우기 — LRU를 둘 만큼 크지 않다
        cache[ck] = hit
    # 사본을 준다 — 호출측이 matches 에 주석(known_issue·lifecycle 경고 등)을 덧붙이므로
    # 캐시 원본을 그대로 넘기면 조회할 때마다 주석이 겹쳐 쌓인다.
    out = copy.deepcopy(hit)
    out["_cache_hit"] = was_hit
    if was_hit:
        # 캐시본은 생성 당시의 timing 을 물고 있다 — 그대로 기록하면 계측이
        # 거짓말을 한다(실측 782ms 로 잡혔다). 캐시 히트의 실제 소요로 바꾼다.
        out["timing"] = {"total_ms": round((time.perf_counter() - _t0) * 1000, 2)}
    return out


def _explain_md_cached(query_rec: dict, match_recs: list[dict]) -> dict | None:
    """심층 분석(마크다운) 캐시 조회. 키는 질의·근거의 내용에서 나온다."""
    model = os.getenv("RVP_MODEL") or os.getenv("OPENROUTER_MODEL", "")
    key = llm_cache.make_key("explain_md", query_rec, match_recs, model,
                             extra=os.getenv("RVP_EXPLAIN_REASONING", "0"))
    return llm_cache.get(key)


def _explain_md_store(query_rec: dict, match_recs: list[dict], value: dict) -> None:
    model = os.getenv("RVP_MODEL") or os.getenv("OPENROUTER_MODEL", "")
    key = llm_cache.make_key("explain_md", query_rec, match_recs, model,
                             extra=os.getenv("RVP_EXPLAIN_REASONING", "0"))
    llm_cache.put(key, value, meta={"query_key": query_rec.get("key", ""),
                                    "evidence": [r.get("key", "") for r in match_recs]})


# ---------------------------------------------------------------------------
# 심층 분석 생성 작업 — 클라이언트가 떠나도 끝까지 만들어 캐시에 넣는다.
#
# 예전에는 SSE 응답 제너레이터 안에서 직접 생성했다. 사용자가 다른 이슈·페이지로
# 옮기면 연결이 끊기고 제너레이터가 취소돼 **거기까지 쓴 토큰이 통째로 버려졌다**.
# 이제 생성은 워커 스레드가 맡고 SSE 는 그 결과를 따라 읽기만 한다:
#   · 중간에 나가도 생성은 계속되고 완료 시 캐시에 들어간다 → 돌아오면 즉시 표시
#   · 같은 분석을 두 곳에서 열어도 작업은 하나만 돈다(둘 다 같은 버퍼를 읽는다)
# ---------------------------------------------------------------------------
class _ExplainJob:
    def __init__(self) -> None:
        self.chunks: list[str] = []
        self.done = False
        self.error: str | None = None
        self.tick = threading.Event()      # 새 조각이 붙을 때마다 깨운다

    def add(self, text: str) -> None:
        self.chunks.append(text)
        self.tick.set()

    def finish(self, error: str | None = None) -> None:
        self.error = error
        self.done = True
        self.tick.set()


_EXPLAIN_JOBS: dict[str, _ExplainJob] = {}
_EXPLAIN_LOCK = threading.Lock()


def _explain_job(query_rec: dict, match_recs: list[dict], ckey: str) -> _ExplainJob:
    """진행 중인 작업이 있으면 그걸 주고, 없으면 시작한다."""
    with _EXPLAIN_LOCK:
        job = _EXPLAIN_JOBS.get(ckey)
        if job is not None and not (job.done and job.error):
            return job                      # 진행 중이거나 정상 완료된 작업에 붙는다
        job = _ExplainJob()
        _EXPLAIN_JOBS[ckey] = job

    def work() -> None:
        t0 = time.perf_counter()
        try:
            for delta in _generate_explain_md(query_rec, match_recs):
                job.add(delta)
            job.finish()
            _record_metric("explain", {"total_ms": round((time.perf_counter() - t0) * 1000, 1),
                                       "chars": sum(len(c) for c in job.chunks), "cached": False})
        except Exception as e:
            job.finish(str(e)[:200])
            _record_metric("explain", {"total_ms": round((time.perf_counter() - t0) * 1000, 1),
                                       "failed": 1, "cached": False})
        finally:
            # 완료본은 캐시에 있으므로 작업 기록은 오래 들고 있을 이유가 없다.
            threading.Timer(60.0, lambda: _EXPLAIN_JOBS.pop(ckey, None)).start()

    threading.Thread(target=work, name=f"explain:{query_rec.get('key','?')}",
                     daemon=True).start()
    return job


def _proofread(body: str) -> tuple[str, dict]:
    """오타·띄어쓰기 교정. 반환 (본문, 기록).

    교정본이 내용을 바꿨으면 **버리고 원문을 쓴다**(voc_agents.safe_replace).
    교정은 있으면 좋은 것이고, 내용 보존은 양보할 수 없는 것이다. 실패해도 원문이
    남으므로 이 단계는 답변 생성을 절대 막지 않는다.

    RVP_PROOFREAD=0 으로 끈다(비용·지연이 LLM 호출 1회만큼 늘어난다).
    """
    if os.getenv("RVP_PROOFREAD", "1") != "1" or not os.environ.get("OPENROUTER_API_KEY"):
        return body, {"ran": False}
    t0 = time.perf_counter()
    try:
        out = "".join(_llm_stream(voc_agents.PROOFREAD_PROMPT.format(text=body))).strip()
        final, reason = voc_agents.safe_replace(body, _strip_preamble(out))
        rec = {"ran": True, "applied": not reason and final != body,
               "ms": round((time.perf_counter() - t0) * 1000, 1)}
        if reason:
            rec["rejected"] = reason
            print(f"[proofread] 교정 폐기 — {reason}")
        return final, rec
    except Exception as e:
        print(f"[proofread] 실패(원문 유지): {str(e)[:120]}")
        return body, {"ran": True, "applied": False, "error": str(e)[:120]}


def _generate_explain_md(query_rec: dict, match_recs: list[dict]):
    """**고객에게 보낼 답변**을 생성하며 토큰을 흘려보낸다(제너레이터). 완료 시 캐시 저장.

    예전에는 여기서 엔지니어용 RCA 분석(근본원인·검증 절차·사례 키 인용)을 만들었다.
    그런데 이 서비스의 최종 산출물은 **고객이 받는 글**이다. 화면의 '심층 분석' 이
    엔지니어 문서면, 사람이 그걸 읽고 고객 답변을 다시 쓰는 단계가 통째로 남는다 —
    에이전트가 하라고 만든 일을 사람이 하게 된다. 그래서 생성기 자체를 고객 응대
    답변으로 바꾼다. 캐시·예열·SSE 경로는 그대로 재사용한다.

    RCA 분석 경로가 사라지는 것은 아니다 — `/rca/draft` 가 근거 기반 RCA 댓글을
    만들고, 그쪽은 여전히 엔지니어가 읽는 글이다.
    """
    ctx = _reply_context(query_rec)
    intent = voc_agents.classify(ctx, _voc_profile())
    lang = voc_agents.detect_lang(ctx)
    guidance = ""
    try:
        guidance = draft_feedback.prompt_guidance(
            category=query_rec.get("category", ""),
            template=template_key(query_rec.get("summary", "")))
    except Exception:
        pass
    proposal = None
    if match_recs:
        top = match_recs[0]
        proposal = {"root_cause": top.get("root_cause", ""),
                    "resolution": top.get("resolution", ""),
                    "workaround": top.get("workaround", "")}
    # 같은 유형에 이미 발송한 정본 답변이 있으면 그걸 기준으로 쓴다 — 같은 문의에
    # 고객사마다 다른 말이 나가면 그 자체가 사고다.
    canonical, canon_id = failure_modes.canonical_reply_for(
        [r.get("key") for r in match_recs])
    prompt = voc_agents.reply_prompt(query_rec, match_recs, proposal, intent,
                                     guidance=guidance, lang=lang,
                                     forbidden=tuple(_forbidden_names(query_rec)),
                                     canonical=canonical, prof=_voc_profile())
    acc: list[str] = []
    for delta in _llm_stream(prompt, reasoning=os.getenv("RVP_EXPLAIN_REASONING", "0") == "1"):
        acc.append(delta)
        yield delta
    full = "".join(acc)
    if not full.strip():
        return
    # 내부 키는 규칙이 아니라 치환으로 지운다 — 모델이 규칙을 어겨도 고객에게는 안 나간다.
    # 스트리밍 중에는 원문이 스쳐 지나가므로, 최종본은 done 이벤트로 다시 내려보낸다.
    body = voc_agents.redact(_strip_preamble(full), _voc_profile())
    body, proof = _proofread(body)      # 오타 교정 — 내용이 바뀌면 원문을 유지한다
    forbidden = tuple(_forbidden_names(query_rec))
    policy = voc_agents.check(body, has_evidence=bool(match_recs), forbidden=forbidden,
                              lang=lang, asks=intent["asks"], prof=_voc_profile())
    _explain_md_store(query_rec, match_recs,
                      {"markdown": body, "citations": [], "dropped": [],
                       "policy": policy, "intent": intent["intent"],
                       "intent_label": intent["label"], "asks": intent["asks"],
                       "lang": lang, "proofread": proof,
                       "canonical_from": canon_id})


@app.get("/recommend/explain/stream", dependencies=[Depends(require("reco.read"))])
def explain_stream(key: Optional[str] = None, summary: str = "", symptom: str = "",
                   chip: str = "", category: str = "", k: int = 4, refresh: bool = False):
    """LLM 종합 분석 SSE 스트리밍 — 본문은 토큰 단위로, 인용 검증은 완료 시.

    이벤트: {type:delta,text} 반복 → {type:done,citations,cached} | {type:error,message}.

    질의·근거가 그대로면 캐시본을 즉시 흘려보낸다(LLM 호출 0회). refresh=1 로 무시.
    """
    st = _reco_state()
    query_rec = (st["by_key"].get(key) if key else None) or {
        "summary": summary, "symptom": symptom, "chip": chip, "category": category, "labels": []}
    # 앞선 /recommend 와 같은 인자면 재계산하지 않는다 — 예전에는 사용자 상호작용
    # 한 번에 검색(임베딩+rerank API)을 두 번 지불했다.
    result = _recommend_cached(query_rec, k=k, exclude_key=key)
    matches = result["matches"]
    coverage = result.get("coverage", bool(matches))
    match_recs = [st["by_key"].get(m["key"], m) for m in matches]

    def gen():
        if not matches or not coverage:
            yield f"data: {json.dumps({'type': 'done', 'citations': [], 'no_coverage': True}, ensure_ascii=False)}\n\n"
            return
        try:
            hit = None if refresh else _explain_md_cached(query_rec, match_recs)
            if hit:
                _record_metric("explain", {"total_ms": 0.0, "cached": True,
                                           "chars": len(hit.get("markdown", ""))})
                # 캐시본도 델타로 흘려보낸다 — 프론트의 SSE 처리 경로를 하나로 유지.
                # 표시는 읽을 때도 건다 — 이 기능 이전에 저장된 캐시본에는 표시가
                # 없다. 재생성(89초 × 127건)을 요구하지 않고 소급 적용한다. 멱등이라
                # 새로 생성된 본문에 두 번 붙지 않는다.
                md = hit.get("markdown", "")
                for i in range(0, len(md), 400):
                    yield f"data: {json.dumps({'type': 'delta', 'text': md[i:i + 400]}, ensure_ascii=False)}\n\n"
                yield ("data: " + json.dumps(
                    {"type": "done", "cached": True, "text": md,
                     "policy": hit.get("policy"), "intent_label": hit.get("intent_label", ""),
                     "asks": hit.get("asks", []), "lang": hit.get("lang", "ko"),
                     "proofread": hit.get("proofread"),
                     "citations": hit.get("citations", [])}, ensure_ascii=False) + "\n\n")
                return
            # 생성은 워커가 맡고 여기서는 따라 읽기만 한다 — 연결이 끊겨도
            # 생성은 계속되고 완료 시 캐시에 들어간다.
            model = os.getenv("RVP_MODEL") or os.getenv("OPENROUTER_MODEL", "")
            ckey = llm_cache.make_key("explain_md", query_rec, match_recs, model,
                                      extra=os.getenv("RVP_EXPLAIN_REASONING", "0"))
            job = _explain_job(query_rec, match_recs, ckey)
            sent = 0
            while True:
                while sent < len(job.chunks):
                    yield f"data: {json.dumps({'type': 'delta', 'text': job.chunks[sent]}, ensure_ascii=False)}\n\n"
                    sent += 1
                if job.done:
                    break
                job.tick.wait(timeout=0.25)
                job.tick.clear()
            if job.error:
                yield f"data: {json.dumps({'type': 'error', 'message': job.error}, ensure_ascii=False)}\n\n"
                return
            # 완성본은 워커가 이미 캐시에 넣었다 — 같은 판정(인용/미제공)을 재사용해
            # 두 경로가 다른 답을 내지 않게 한다.
            # 최종본은 워커가 내부 키를 지우고 정책까지 매긴 판본이다. 스트리밍 중에는
            # 원문이 스쳐 지나가므로 done 에서 화면을 이 판본으로 교체한다.
            done_hit = _explain_md_cached(query_rec, match_recs) or {}
            yield ("data: " + json.dumps(
                {"type": "done", "cached": False, "text": done_hit.get("markdown", ""),
                 "policy": done_hit.get("policy"), "intent_label": done_hit.get("intent_label", ""),
                 "asks": done_hit.get("asks", []), "lang": done_hit.get("lang", "ko"),
                 "proofread": done_hit.get("proofread"),
                 "citations": done_hit.get("citations", [])}, ensure_ascii=False) + "\n\n")
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)[:200]}, ensure_ascii=False)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------------------
# 서빙 지연 상설 계측 (D-14)
#
# 일회성 벤치로는 회귀를 못 잡는다 — 실제로 이번(2026-08) 격리 벤치가 서버 지연을
# 2배 이상 잘못 예측했다(백로그 E-0). 최근 요청의 단계별 소요를 링버퍼에 들고
# /metrics 로 노출한다. 저장소를 두지 않으므로 재기동하면 비워진다 — 장기 추세가
# 필요하면 이 값을 외부로 긁어 가면 된다.
# ---------------------------------------------------------------------------
_METRICS_MAX = 500
_METRICS: dict[str, list] = {"recommend": [], "explain": []}
_METRICS_LOCK = threading.Lock()


def _record_metric(kind: str, sample: dict) -> None:
    with _METRICS_LOCK:
        buf = _METRICS.setdefault(kind, [])
        buf.append(sample)
        if len(buf) > _METRICS_MAX:
            del buf[: len(buf) - _METRICS_MAX]


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    v = sorted(values)
    return round(v[min(int(len(v) * q), len(v) - 1)], 1)


@app.get("/metrics", dependencies=[Depends(require("knowledge.read"))])
def metrics():
    """최근 요청의 단계별 지연 분포 — 회귀 감시용.

    cached=true 인 요청은 캐시 히트라 분포를 왜곡한다. 나눠서 보여 준다.
    """
    with _METRICS_LOCK:
        snap = {k: list(v) for k, v in _METRICS.items()}
    out: dict = {"window": _METRICS_MAX}
    for kind, rows in snap.items():
        live = [r for r in rows if not r.get("cached")]
        cached = [r for r in rows if r.get("cached")]
        stage_keys = sorted({k for r in live for k in r if k.endswith("_ms")})
        out[kind] = {
            "count": len(rows), "cache_hits": len(cached),
            "cache_hit_rate": round(len(cached) / len(rows), 3) if rows else None,
            "stages_ms": {
                k: {"p50": _pct([r[k] for r in live if k in r], 0.5),
                    "p90": _pct([r[k] for r in live if k in r], 0.9),
                    "max": _pct([r[k] for r in live if k in r], 1.0),
                    "n": sum(1 for r in live if k in r)}
                for k in stage_keys},
            "cached_total_ms": {"p50": _pct([r["total_ms"] for r in cached if "total_ms" in r], 0.5)}
                               if cached else None,
        }
    out["rerank_failures"] = sum(1 for r in snap.get("recommend", []) if r.get("rerank_failed"))

    # 재순위 상태 — **링버퍼가 아니라 살아 있는 추천기에서 읽는다.**
    # rerank_failures 만 보면 안 된다: 차단기가 열리면 재순위를 아예 시도하지 않으므로
    # 실패가 더 이상 기록되지 않고, 링버퍼가 밀려나면 0 이 된다. 문제가 영구화되는
    # 순간 지표가 사라지는 셈이다. degraded=True 면 coverage 판정이 약한 폴백
    # 게이트(embed_cos)로 내려가 있다 — 메타 없는 자유 문장에서 정답의 약 5%가 막힌다
    # (실측 0.947, claudedocs/performance_backlog.md E-6).
    try:
        reco = _reco_state()["reco"]
        out["rerank"] = {
            "enabled": bool(reco.rerank),
            "degraded": not reco.rerank,
            "consecutive_fails": int(getattr(reco, "_rerank_fails", 0)),
            "tripped_at": (_dt.datetime.fromtimestamp(reco._rerank_tripped_at)
                           .isoformat(timespec="seconds")
                           if getattr(reco, "_rerank_tripped_at", 0) else None),
            "retry_sec": float(getattr(reco, "rerank_retry_sec", 0.0)),
            "gate": "rerank" if reco.rerank else "embed_cos(폴백)",
        }
    except Exception as e:
        out["rerank"] = {"error": str(e)[:120]}
    return out


# ---------------------------------------------------------------------------
# 심층 분석 예열(prewarm) — 사용자가 누르기 전에 미리 만들어 둔다.
#
# 캐시만 두면 "처음 여는 이슈"는 여전히 수 초를 기다린다. 미해결 이슈는 목록이
# 정해져 있으므로 백그라운드에서 미리 생성해 두면 첫 클릭도 즉시 뜬다.
#
# 비용이 있는 작업(이슈당 LLM 1회)이라 기본은 보수적으로 잡았다:
#   · 이미 캐시에 있으면 건너뛴다(변화가 없으면 다시 만들지 않는다)
#   · 한 번에 RVP_PREWARM_LIMIT 건까지, 사이에 텀을 둬 API를 몰아치지 않는다
#   · RVP_PREWARM=0 이면 아예 돌지 않는다
# ---------------------------------------------------------------------------
_PREWARM: dict = {"thread": None, "running": False, "done": 0, "skipped": 0,
                  "failed": 0, "total": 0, "last_key": "", "error": None,
                  "finished_at": None}


def _prewarm_once(limit: int, gap_sec: float, only_key: str = "") -> None:
    st = _reco_state()
    targets = [r for r in st["unresolved"] if not only_key or r["key"] == only_key]
    # 최신 이슈부터 — 지금 작업 중인 것이 먼저 열릴 가능성이 높다. created 가 없으면
    # 뒤로 보낸다(정렬이 뒤집히지 않게 빈 문자열을 최소값으로 두고 역순 정렬).
    targets.sort(key=lambda r: str(r.get("created") or ""), reverse=True)
    _PREWARM.update({"running": True, "done": 0, "skipped": 0, "failed": 0,
                     "total": min(len(targets), limit), "error": None, "finished_at": None})
    try:
        for rec in targets:
            if _PREWARM["done"] + _PREWARM["skipped"] >= limit:
                break
            try:
                res = _recommend_cached(rec, k=4, exclude_key=rec["key"])
                if not res["matches"] or not res.get("coverage", True):
                    _PREWARM["skipped"] += 1     # 게이트 미통과 → 원래도 생성 안 함
                    continue
                match_recs = [st["by_key"].get(m["key"], m) for m in res["matches"]]
                if _explain_md_cached(rec, match_recs) is not None:
                    _PREWARM["skipped"] += 1     # 이미 있음 — 변화가 없으니 그대로 둔다
                    continue
                for _ in _generate_explain_md(rec, match_recs):
                    pass                          # 토큰은 버리고 캐시만 채운다
                _PREWARM["done"] += 1
                _PREWARM["last_key"] = rec["key"]
                time.sleep(gap_sec)
            except Exception as e:
                _PREWARM["failed"] += 1
                _PREWARM["error"] = f'{rec.get("key","")}: {str(e)[:120]}'
    finally:
        _PREWARM["running"] = False
        _PREWARM["finished_at"] = _dt.datetime.now().isoformat(timespec="seconds")
        print(f"[prewarm] 생성 {_PREWARM['done']} · 건너뜀 {_PREWARM['skipped']} "
              f"· 실패 {_PREWARM['failed']}")


def _prewarm_drain() -> None:
    """남은 미해결 이슈를 **끝까지** 천천히 채운다.

    기존에는 기동 직후 한 번(기본 20건)만 돌고 끝나, 미해결 127건 중 나머지는
    첫 클릭에서 수십 초를 기다렸다(실측 캐시 28/127). 여기서는 라운드를 이어
    돌리되 항목 사이 간격을 넉넉히 둬(RVP_PREWARM_IDLE_GAP_SEC, 기본 30초) API 를
    몰아치지 않는다 — 127건이면 대략 한 시간 안에 채워진다.

    이미 캐시에 있으면 건너뛰므로 재기동 비용은 거의 없고, KB 가 바뀌어 키가
    달라진 항목만 다시 만든다.
    """
    gap = float(os.getenv("RVP_PREWARM_IDLE_GAP_SEC", "30") or 30)
    rounds = 0
    while os.getenv("RVP_PREWARM", "1") == "1":
        rounds += 1
        before = _PREWARM["done"]
        _prewarm_once(limit=10_000, gap_sec=gap)     # 남은 전량, 느린 속도로
        made = _PREWARM["done"] - before
        _PREWARM["drain_rounds"] = rounds
        if made == 0:
            break                                     # 더 만들 것이 없다
        time.sleep(gap)
    print(f"[prewarm] 드레인 종료 — {rounds}라운드")


def _start_prewarm(limit: int | None = None, only_key: str = "") -> bool:
    """예열을 백그라운드로 시작. 이미 돌고 있으면 False."""
    if _PREWARM["running"]:
        return False
    if os.getenv("RVP_PREWARM", "1") != "1" and not only_key:
        return False
    lim = limit if limit is not None else int(os.getenv("RVP_PREWARM_LIMIT", "20") or 0)
    if lim <= 0:
        return False
    gap = float(os.getenv("RVP_PREWARM_GAP_SEC", "1.0") or 0)
    def run() -> None:
        _prewarm_once(lim, gap, only_key)
        # 특정 키만 요청받은 경우가 아니고 드레인이 켜져 있으면 나머지도 채운다.
        if not only_key and os.getenv("RVP_PREWARM_DRAIN", "1") == "1":
            _prewarm_drain()

    t = threading.Thread(target=run, name="explain-prewarm", daemon=True)
    _PREWARM["thread"] = t
    t.start()
    return True


@app.get("/explain/cache", dependencies=[Depends(require("knowledge.read"))])
def explain_cache_stats():
    """캐시 현황 + 예열 진행 상태."""
    total = len(_reco_state()["unresolved"])
    st = llm_cache.stats()
    return {"cache": {**st, "unresolved_total": total,
                      # 대략적 커버리지 — 캐시에는 해결 이슈 조회분도 섞이므로 상한 100%.
                      "coverage_pct": min(100, round(st["entries"] / total * 100)) if total else None},
            "prewarm": {k: v for k, v in _PREWARM.items() if k != "thread"}}


@app.post("/explain/prewarm", dependencies=[Depends(require("ops.cache"))])
def explain_prewarm(limit: int = 0, key: str = ""):
    """예열 수동 시작. limit=0 이면 환경변수 기본값, key 지정 시 그 이슈만."""
    started = _start_prewarm(limit or None, only_key=key)
    return {"ok": started,
            "reason": "" if started else ("이미 실행 중" if _PREWARM["running"] else "비활성(RVP_PREWARM=0 또는 limit=0)"),
            "prewarm": {k: v for k, v in _PREWARM.items() if k != "thread"}}


@app.delete("/explain/cache", dependencies=[Depends(require("ops.cache"))])
def explain_cache_clear():
    """캐시 비우기 — 프롬프트를 바꿨는데 버전을 안 올렸을 때의 탈출구."""
    return {"ok": True, "removed": llm_cache.clear()}


class InvestigateBody(BaseModel):
    key: Optional[str] = None
    summary: str = ""
    symptom: str = ""
    chip: str = ""
    category: str = ""
    k: int = 5


@app.post("/recommend/investigate", dependencies=[Depends(require("reco.read"))])
def investigate(req: InvestigateBody):
    """유사 사례가 없을 때의 **조사 계획** — coverage=false 전용.

    게이트는 무관 사례에 기댄 환각을 막으려고 있다. 그래서 사례가 없으면 제품이
    아무것도 하지 않았다 — 정직하지만 가장 어려운 이슈에서 도움이 0 이다.

    여기서 근본원인을 만들면 게이트를 무력화하는 것이므로, **산출물을 바꾼다**:
    결론이 아니라 "무엇을 재면 어떤 가설이 죽는지" 를 쓴다. 생성 후 결정적으로
    검증하고(절 완결·미제시 사례 언급·단정문·가설 표시) 하나라도 어기면 **내보내지
    않는다** — 검증 없이 여는 것은 게이트를 없앤 것과 같다.
    """
    import first_principles as fp

    st = _reco_state()
    if req.key:
        query_rec = st["by_key"].get(req.key)
        if not query_rec:
            raise HTTPException(status_code=404, detail=f"이슈 {req.key} 없음")
    else:
        if not (req.summary or req.symptom):
            raise HTTPException(status_code=400, detail="key 또는 summary/symptom 필요")
        query_rec = {"summary": req.summary, "symptom": req.symptom,
                     "chip": req.chip, "category": req.category, "labels": []}

    result = _recommend_cached(query_rec, k=max(req.k, 4),
                               exclude_key=req.key or None)
    gate = result.get("gate") or {}

    # 게이트를 통과했다면 이 기능은 필요 없다 — 근거 있는 분석(explain)을 쓰라고 알린다.
    if result.get("coverage"):
        return {"available": False, "reason": "coverage_passed",
                "message": "유사 사례가 있습니다 — 근거 기반 심층 분석(explain)을 사용하세요.",
                "gate": gate}
    # 판정 불가(장애)는 대상이 아니다. 사례가 없는 게 아니라 판정을 못 한 것이라,
    # 여기서 조사 계획을 만들면 있는 사례를 놓친 채 처음부터 파게 만든다.
    if gate.get("signal") == "none":
        return {"available": False, "reason": "undecidable",
                "message": "재순위·임베딩을 쓸 수 없어 관련도를 판정하지 못했습니다 — "
                           "일시 장애입니다. 잠시 후 다시 시도하세요.",
                "gate": gate}

    bundle = fp.gather(query_rec, st["reco"], result.get("matches", []), k_weak=req.k)
    prompt = fp.build_prompt(bundle, gate)

    cached = llm_cache.get(_investigate_ckey(query_rec, bundle))
    if cached:
        return {"available": True, "cached": True, "gate": gate,
                "markdown": cached.get("markdown", ""),
                "validation": cached.get("validation", {}), "bundle": _slim_bundle(bundle)}

    md = "".join(_llm_stream(prompt))
    fixed = validate_and_fix(md)
    if fixed.rewritten:
        md = fixed.rewritten
    v = fp.validate(md, bundle)
    ok, why = fp.is_acceptable(v)
    if not ok:
        # 내보내지 않는다. 왜 막았는지는 알려준다 — 조용히 비우면 장애와 구분이 안 된다.
        return {"available": False, "reason": "validation_failed", "message": why,
                "validation": v, "gate": gate}

    llm_cache.put(_investigate_ckey(query_rec, bundle),
                  {"markdown": md, "validation": v})
    return {"available": True, "cached": False, "gate": gate, "markdown": md,
            "validation": v, "bundle": _slim_bundle(bundle)}


def _investigate_ckey(query_rec: dict, bundle: dict) -> str:
    """질의 + 번들 내용 기반 캐시 키 — 번들이 바뀌면(KB 변경) 다시 만든다.

    explain 캐시와 같은 규약을 쓰되 kind 로 갈라 둔다. extra 에 번들 요약을 넣어,
    약한 매치나 분류 고장모드가 달라지면 새로 만든다(같은 질의라도 KB 가 바뀌면
    조사 계획의 재료가 달라진다).
    """
    import first_principles as fp
    extra = json.dumps({"keys": sorted(fp.allowed_keys(bundle)),
                        "modes": [m["template"] for m in bundle.get("category_modes", [])]},
                       ensure_ascii=False, sort_keys=True)
    model = os.getenv("RVP_MODEL") or os.getenv("OPENROUTER_MODEL", "")
    return llm_cache.make_key("investigate", query_rec, bundle.get("weak_matches", []),
                              model, extra=extra)


def _slim_bundle(bundle: dict) -> dict:
    """화면·MCP 로 보낼 요약 — 본문 전체를 되돌려줄 필요는 없다."""
    return {
        "weak_matches": [{"key": m["key"], "summary": m["summary"][:100],
                          "score": m.get("rerank_score") or m.get("embed_cos"),
                          "entity_overlap": m.get("entity_overlap", 0)}
                         for m in bundle.get("weak_matches", [])],
        "chip_history": [{"key": h["key"], "template": h["template"][:80],
                          "category": h["category"]} for h in bundle.get("chip_history", [])],
        "category_modes": bundle.get("category_modes", []),
        "query_entities": bundle.get("query_entities", []),
    }


@app.get("/recommend/explain/cached", dependencies=[Depends(require("reco.read"))])
def explain_cached(key: str, k: int = 4):
    """이미 만들어 둔 심층 분석만 돌려준다 — **생성하지 않는다**.

    MCP 처럼 스스로 추론하는 클라이언트를 위한 것이다. 없으면 없다고 알린다.
    """
    st = _reco_state()
    query_rec = st["by_key"].get(key)
    if not query_rec:
        raise HTTPException(status_code=404, detail=f"이슈 {key} 없음")
    result = _recommend_cached(query_rec, k=k, exclude_key=key)
    if not result["matches"] or not result.get("coverage", True):
        _record_gap(query_rec, result, "explain_no_coverage")
        return {"cached": False, "coverage": False, "gate": result.get("gate"),
                "reason": "게이트 미통과 — 근거가 부족해 분석을 생성하지 않습니다"}
    match_recs = [st["by_key"].get(m["key"], m) for m in result["matches"]]
    hit = _explain_md_cached(query_rec, match_recs)
    if hit is None:
        return {"cached": False, "coverage": True,
                "evidence_keys": [m["key"] for m in result["matches"]],
                "reason": "저장된 분석이 없습니다 — analyze_issue 의 근거로 직접 분석하세요"}
    # 캐시본은 이제 **고객에게 보낼 답변**이다(심층 분석 생성기가 그렇게 바뀌었다).
    # 내부 이슈 키는 저장 전에 지워지므로 인용 목록은 비어 있는 것이 정상이다 —
    # 대신 어떤 근거로 썼는지는 evidence_keys 로, 발송 가능 여부는 policy 로 준다.
    return {"cached": True, "coverage": True,
            "kind": "customer_reply",
            "markdown": hit.get("markdown", ""),
            "intent": hit.get("intent", ""), "intent_label": hit.get("intent_label", ""),
            "asks": hit.get("asks", []), "lang": hit.get("lang", "ko"),
            "policy": hit.get("policy"), "proofread": hit.get("proofread"),
            "evidence_keys": [m["key"] for m in result["matches"]],
            "citations": hit.get("citations", []),
            "unsupported_mentions": hit.get("dropped", [])}


def _record_gap(query_rec: dict, result: dict, reason: str) -> None:
    """coverage 게이트에 막힌 **사용자 요청**을 공백 신호로 남긴다.

    한 곳(/recommend)에만 붙여 뒀더니 정작 신호가 가장 센 순간 — 사용자가 자동 RCA 를
    요청했다가 "사례 없음" 으로 거절당하는 순간 — 이 기록되지 않았다. 게이트가 막는
    자리마다 부른다.

    부르지 않는 곳: **예열 배치**(_prewarm_once). 사람이 물어본 게 아니라 전량 훑기라,
    기록하면 KB 전체가 공백으로 들어와 "자주 묻는 영역" 신호를 덮는다.
    """
    try:
        gate = result.get("gate") or {}
        # 판정 불가(signal="none")는 **공백이 아니다.** 사례가 부족한 게 아니라 재순위·
        # 임베딩이 죽어 판정을 못 한 것이다. 이걸 세면 게이트웨이 장애 한 번에
        # "자주 묻지만 사례 없는 영역" 통계가 그날 트래픽으로 통째로 오염된다 —
        # 예열 배치를 빼는 것과 같은 이유다.
        if gate.get("signal") == "none":
            return
        knowledge_gaps.record(
            query_rec, reason=reason,
            template=template_key(query_rec.get("summary", "")),
            # 게이트 dict 의 강도 키는 신호에 따라 다르다(rerank_top / max_cos).
            top_score=(gate.get("rerank_top") or gate.get("max_cos"))
                      if isinstance(gate, dict) else None)
    except Exception:
        pass          # 관측성 때문에 본 요청이 실패하면 안 된다


_DEMOTED = ("deprecated", "superseded")


def _repick_proposal_after_lifecycle(result: dict) -> None:
    """제안 근거가 폐기·대체된 사례면 살아 있는 사례로 갈아 끼운다.

    바꿀 후보가 없으면(전부 강등) 제안을 지우지 않고 경고만 단다 — 근거가 낡았다는
    사실을 알리는 편이, 아무 제안도 없이 비워 두는 것보다 판단에 도움이 된다.
    """
    prop = result.get("proposal")
    matches = result.get("matches") or []
    if not prop or not matches:
        return
    by_key = {m.get("key"): m for m in matches}
    cur = by_key.get(prop.get("based_on"))
    if not cur or (cur.get("lifecycle") or {}).get("state") not in _DEMOTED:
        return                                    # 근거가 멀쩡하다
    alive = [m for m in matches if (m.get("lifecycle") or {}).get("state") not in _DEMOTED]
    if not alive:
        prop["lifecycle_warning"] = "근거 사례가 모두 폐기·대체됨 — 시니어 검토 필요"
        return
    new = alive[0]                                # 강등 정렬 뒤 최상위 = 살아 있는 최상위
    prop.update({
        "root_cause": new.get("root_cause", ""), "resolution": new.get("resolution", ""),
        "workaround": new.get("workaround", ""), "based_on": new.get("key", ""),
        "based_on_verified": bool(new.get("verified")),
        "lifecycle_warning": f"기존 근거 {cur.get('key')} 가 폐기·대체되어 {new.get('key')} 로 대체함",
    })


@app.post("/recommend", dependencies=[Depends(require("reco.read"))])
def recommend(req: RecommendRequest):
    st = _reco_state()
    if req.key:
        rec = st["by_key"].get(req.key)
        if not rec:
            return {"error": f"이슈 {req.key} 없음"}
        query_rec = rec
    else:
        query_rec = {
            "summary": req.summary or "", "symptom": req.symptom or "",
            "chip": req.chip or "", "category": req.category or "",
            "labels": req.labels or [],
        }
    # 해결 이슈 키로 질의해도 자기 자신은 매치에서 제외.
    # 캐시 경유 — 뒤이어 오는 explain 스트리밍이 같은 검색을 또 하지 않게 한다.
    result = _recommend_cached(query_rec, k=req.k, exclude_key=req.key)
    # 매치에 메타(생성일·FW) 보강 — 수명주기 신선도/경고 산출용
    for m in result["matches"]:
        src = st["by_key"].get(m.get("key"), {})
        m.setdefault("created", src.get("created", ""))
        m.setdefault("fw_version", src.get("fw_version", ""))
    # 고장모드 기사 주석(P2-4): 매치가 Known-Issue 기사에 속하면 묶어 노출하도록 표시
    try:
        failure_modes.annotate(result["matches"])
    except Exception:
        pass
    # 신선도·폐기 수명주기 주석(P2-5): 오래/폐기/대체 사례 경고 + 강등 정렬
    try:
        lifecycle.annotate(result["matches"])
        # 제안 근거가 방금 강등된 사례면 다시 고른다.
        # Recommender 는 lifecycle 을 모른 채 proposal 을 만들고, 강등은 그 **뒤에**
        # 일어난다. 그대로 두면 매치 목록에서는 "폐기됨" 으로 뒤로 밀린 사례가
        # proposal.based_on 으로는 최상위 제안이 되어 화면이 자기모순에 빠진다.
        _repick_proposal_after_lifecycle(result)
    except Exception:
        pass
    _record_metric("recommend", {**(result.get("timing") or {}),
                                 "cached": bool(result.pop("_cache_hit", False))})
    result.pop("timing", None)          # 내부 신호 — 응답 본문에는 싣지 않는다
    out = {
        "query": {"key": query_rec.get("key"), "summary": query_rec.get("summary"),
                  "symptom": query_rec.get("symptom"), "chip": query_rec.get("chip"),
                  "category": query_rec.get("category"), "status": query_rec.get("status")},
        "matches": result["matches"],
        "proposal": result["proposal"],
        "coverage": result.get("coverage", bool(result["matches"])),
        "gate": result.get("gate"),
    }
    # 고객이 무엇을 요청했는지는 답변을 만들기 **전에** 보여야 한다. 화면이 그걸
    # 모르면 담당자는 증상만 읽고 답을 판단하게 된다 — 답변이 요지를 빗나가는
    # 가장 흔한 경로다. 규칙 기반이라 비용이 없다.
    try:
        _intent = voc_agents.classify(_reply_context(query_rec), _voc_profile())
        out["intent"] = _intent["intent"]
        out["intent_label"] = _intent["label"]
        out["asks"] = _intent["asks"]
        out["customer_ask"] = query_rec.get("customer_ask", "")
        out["profile"] = _voc_profile()
    except Exception:
        pass
    # 지식 공백 관측성(P3-8): coverage 미통과 질의를 공백 신호로 기록(자기 개선 loop 입력)
    if not out["coverage"]:
        _record_gap(query_rec, result, "no_coverage")
    # 게이트 미통과 시 LLM 설명 생성 안 함 (무관 사례 기반 환각 방지)
    if req.explain and result["matches"] and out["coverage"]:
        ex = _llm_explain(query_rec, result["matches"])
        out["explanation"] = ex["markdown"]
        out["explanation_citations"] = ex["citations"]          # 검증된 인용 키
        if ex["dropped"]:
            out["explanation_dropped_citations"] = ex["dropped"]  # 환각으로 제거된 키
    return out


# ---------------------------------------------------------------------------
# RCA 자동 댓글 — HITL 승인 큐 (생성 → 대기 → 사람 승인 시에만 Jira 게시)
# ---------------------------------------------------------------------------
BOT_MARKER = "자동 근본원인 분석"  # preprocess.BOT_COMMENT_MARKER 와 동일(파싱 제외용)


def _convert_md_tables(md: str) -> str:
    """마크다운 표 → Jira wiki 표. 헤더는 '||a||b||', 데이터는 '|a|b|', 구분행(|---|)은 제거.

    api/2 wiki엔 마크다운의 '|---|' 구분행 개념이 없어 그대로 두면 쓰레기 행으로 렌더된다.
    """
    def _cells(line: str) -> list[str]:
        parts = line.strip().split("|")
        if parts and parts[0].strip() == "":
            parts = parts[1:]
        if parts and parts[-1].strip() == "":
            parts = parts[:-1]
        return [p.strip() for p in parts]

    def _is_row(line: str) -> bool:
        return line.strip().startswith("|")

    def _is_sep(line: str) -> bool:
        c = line.strip().strip("|")
        return bool(c) and "-" in c and all(ch in " -:|" for ch in c)

    lines, out, i = md.split("\n"), [], 0
    while i < len(lines):
        if not _is_row(lines[i]):
            out.append(lines[i]); i += 1
            continue
        j = i
        block = []
        while j < len(lines) and _is_row(lines[j]):
            block.append(lines[j]); j += 1
        sep_idx = [k for k, l in enumerate(block) if _is_sep(l)]
        if sep_idx:                                   # 진짜 표(구분행 존재)
            header_idx = sep_idx[0] - 1
            for k, row in enumerate(block):
                if _is_sep(row):
                    continue
                cs = _cells(row)
                out.append("||" + "||".join(cs) + "||" if k == header_idx
                           else "|" + "|".join(cs) + "|")
        else:                                         # 구분행 없음 → 표 아님, 원본 유지
            out.extend(block)
        i = j
    return "\n".join(out)


def _md_to_jira(md: str) -> str:
    """게시 직전 마크다운 → Jira wiki markup 변환(api/2가 wiki를 렌더하므로).

    헤딩 #..# → h1.~h6., **굵게** → *굵게*, '- ' 글머리 → '* ', 표 → Jira 표. 본문/큐/
    미리보기는 마크다운 정본을 유지하고, 게시 시점에만 변환한다.
    """
    md = _convert_md_tables(md)                       # 표 먼저 변환(헤딩/글머리 처리 전)
    lines = []
    for ln in md.split("\n"):
        m = re.match(r"^(#{1,6})\s+(.*)$", ln)
        if m:
            lines.append(f"h{len(m.group(1))}. {m.group(2)}")
        else:
            lines.append(re.sub(r"^(\s*)-\s+", r"\1* ", ln))  # 글머리 - → *
    s = "\n".join(lines)
    s = re.sub(r"`([^`\n]+)`", r"{{\1}}", s)              # 인라인 코드 → Jira monospace
    # 인라인 강조 마커(**, *)는 평문화한다. Jira 볼드 *x*는 닫는 *에 한글 조사가 붙으면
    # (*CRC*를) 렌더가 깨져 '*'가 그대로 노출되고, 변환 잔재 단독 '*'도 남는다. RCA 본문엔
    # 정상 '*'가 없으므로, 줄머리 글머리표('* ')만 남기고 그 외 '*'는 모두 제거한다.
    out = []
    for ln in s.split("\n"):
        m = re.match(r"^(\s*\*\s)(.*)$", ln)              # 글머리표 줄
        out.append((m.group(1) + m.group(2).replace("*", "")) if m else ln.replace("*", ""))
    s = "\n".join(out)
    # 이슈 키(LSI-123) monospace 래핑: 맨키워드는 Jira가 요약·상태 카드로 자동 확장돼
    # 참조가 길어진다. {{...}}로 감싸면 짧은 평문으로 렌더(카드 미확장). 중복 래핑 방지.
    s = re.sub(rf"\{{\{{{issue_keys.STEM}\}}\}}|{issue_keys.STEM}",
               lambda m: m.group(0) if m.group(0).startswith("{{") else "{{" + m.group(0) + "}}", s)
    return s


def _strip_preamble(md: str) -> str:
    """첫 헤딩(###) 이전의 LLM 서두(예: '네, ...하겠습니다')를 제거."""
    idx = md.find("\n### ")
    if md.lstrip().startswith("### "):
        return md.strip()
    return (md[idx + 1:].strip() if idx != -1 else md.strip())


def _rca_comment_body(query_rec: dict, result: dict) -> str:
    """RCA 댓글 본문(마크다운 정본; 게시 시 _md_to_jira로 변환). 참조는 Jira ID만."""
    p = result.get("proposal") or {}
    matches = result.get("matches", [])
    cited = ", ".join(m["key"] for m in matches[:3])
    conf = p.get("confidence", 0)
    label = "높음" if (conf >= 0.67 and p.get("based_on_verified")) else "중간"
    return (
        f"🤖 **{BOT_MARKER}** (RCA-bot · 신뢰도 {label})\n\n"
        f"### 예상 근본원인\n{p.get('root_cause','')}\n\n"
        f"### 권장 해결책\n{p.get('resolution','')}\n\n"
        f"### 임시 우회책\n{p.get('workaround') or '—'}\n\n"
        f"참고 사례: {cited}\n\n"
        f"_과거 해결 이슈 기반 자동 분석 (사람 승인 후 게시)._")


class KeyBody(BaseModel):
    key: str


def _not_queued(code: str, reason: str, **extra) -> dict:
    """큐 미진입 사유를 구조화해 반환 — UI/개발자가 '왜 안 들어갔는지' 명확히 알도록."""
    return {"queued": False, "reason_code": code, "reason": reason, "error": reason, **extra}


def _queue_result(saved: dict) -> dict:
    """upsert 결과를 queued/reason으로 표준화(신규 pending vs 이미 승인됨 구분)."""
    if saved.get("state") == "approved":
        cid = saved.get("comment_id", "")
        return {"queued": False, "reason_code": "already_approved",
                "reason": f"이미 승인·게시된 이슈입니다{f' (댓글 #{cid})' if cid else ''} — 새 초안을 만들지 않았습니다.",
                "item": saved, "counts": rca_queue.counts()}
    return {"queued": True, "reason_code": "queued",
            "reason": "승인 대기 큐에 추가됨 (상단 '📤 승인 대기'에서 검토·게시).",
            "item": saved, "counts": rca_queue.counts()}


@app.post("/rca/draft", dependencies=[Depends(require("rca.draft"))])
def rca_draft(req: KeyBody):
    """미해결 이슈에 대한 RCA 댓글 초안 생성 → 승인 큐(pending)에 적재. Jira 쓰기 없음."""
    st = _reco_state()
    rec = st["by_key"].get(req.key)
    if not rec:
        return _not_queued("not_found", f"이슈 {req.key} 를 KB에서 찾을 수 없습니다.")
    if rec.get("status") == RESOLVED_STATUS:
        return _not_queued("resolved", "이미 해결(완료)된 이슈입니다 — 미해결 이슈만 RCA 대상입니다.")
    result = st["reco"].recommend(rec, k=4, exclude_key=req.key)
    if not result["matches"] or not result.get("coverage"):
        # 사용자가 자동 RCA 를 원했는데 근거가 없어 거절한 자리 — 공백 신호가 가장 센 지점이다.
        _record_gap(rec, result, "rca_refused")
        return _not_queued("no_coverage",
                           "유사 과거 사례가 없습니다(coverage 게이트 미통과). 근거 없는 자동 RCA를 막기 위해 "
                           "초안을 만들지 않았습니다 — 시니어 직접 검토가 필요합니다. (대안: '✨ AI 심층 분석' 후 "
                           "'📤 이 분석을 RCA로'는 게이트 없이 큐에 넣을 수 있습니다.)")
    p = result["proposal"] or {}
    conf = p.get("confidence", 0)
    verified = bool(p.get("based_on_verified"))
    item = {
        "key": req.key, "summary": rec.get("summary", ""), "status": rec.get("status", ""),
        "body": _rca_comment_body(rec, result),
        "confidence": conf, "based_on_verified": verified,
        # 신뢰도 낮거나 미검증 근거면 반드시 사람 검토(조건부 HITL)
        "needs_review": (conf < 0.8) or (not verified),
        "based_on": p.get("based_on", ""),
        "source": "proposal",
        "created_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "state": "pending",
    }
    return _queue_result(rca_queue.upsert(item))


# ── 고객 대응(VOC) 답변 ────────────────────────────────────────────────────
# RCA 초안과 무엇이 다른가: 읽는 사람이 다르다. RCA 는 엔지니어가 읽는 분석이고,
# 여기 산출물은 **고객이 그대로 받는 글**이다. 그래서 세 가지가 다르다.
#   · 근거가 없어도 초안을 만든다 — RCA 는 근거 없으면 만들지 않는 게 옳지만
#     (틀린 원인 단정), 고객 문의는 답을 안 하는 것이 가장 나쁜 실패다.
#     대신 원인을 단정하지 않는 골격으로 간다.
#   · 해결된 이슈도 대상이다 — "해결됐습니다" 를 알리는 것도 고객 대응이다.
#   · 발송 전 정책 검사를 통과해야 한다(내부 키·확정 약속·반말은 차단).
REPLY_MARKER = preprocess.REPLY_COMMENT_MARKER


def _voc_profile() -> str:
    """대응 프로파일 — external(외부 고객사) | internal(사내 VOC).

    조직마다 하나로 고정된다. 요청마다 바뀌는 값이 아니므로 환경변수로 둔다.
    잘못된 값은 voc_agents.profile() 이 기본값으로 접는다.
    """
    return os.getenv("RVP_VOC_PROFILE", voc_agents.DEFAULT_PROFILE)


def _forbidden_names(rec: dict) -> list[str]:
    """이 답변에 나오면 안 되는 고유명사 — KB 에 있는 **다른 고객사** 이름.

    근거 사례는 남의 고장 이력이다. 사실이어도 고객에게 다른 고객사를 알려주는 순간
    비밀유지 문제가 된다. 자동으로 지우지 않는다 — 문장 뜻이 바뀌므로 사람이 고쳐야 한다.
    """
    # 사내 VOC 에서는 이 목록을 쓰지 않는다 — 사내 답변에 등장하는 조직명은
    # 대개 우리 팀·요청 팀 이름이고, 그걸 금지어로 잡으면 경고가 소음이 된다.
    if _voc_profile() == "internal":
        return []
    mine = str(rec.get("customer", "") or "").strip()
    names = {str(r.get("customer", "") or "").strip()
             for r in _reco_state()["by_key"].values()}
    # 3자 미만은 뺀다 — 짧은 약칭은 본문의 평범한 단어와 겹쳐 오탐이 된다.
    # 자기 회사 이름은 당연히 써도 된다.
    return sorted(n for n in names if len(n) >= 3 and n != mine)


def _reply_context(rec: dict) -> str:
    """의도 분류에 쓸 고객 원문 — 요약 + 증상 + 고객 요청 + 조사 스레드."""
    return "\n".join(str(rec.get(f, "") or "")
                     for f in ("summary", "symptom", "customer_ask", "investigation"))


def _generate_reply(rec: dict, matches: list[dict], proposal: dict | None,
                    intent: dict, lang: str = "ko") -> tuple[str, str]:
    """답변 본문 생성. (본문, 엔진) — LLM 이 없거나 실패하면 결정적 템플릿으로 떨어진다.

    고객 대응에서 '생성 실패 = 무응답' 은 허용되지 않는다. 템플릿은 빈약하지만
    사람이 손볼 초안으로는 유효하고, 어느 경로로 나왔는지 큐에 남긴다.
    """
    if os.environ.get("OPENROUTER_API_KEY"):
        try:
            guidance = ""
            try:
                guidance = draft_feedback.prompt_guidance(
                    category=rec.get("category", ""),
                    template=template_key(rec.get("summary", "")))
            except Exception:
                pass
            canonical, _ = failure_modes.canonical_reply_for([m.get("key") for m in matches])
            prompt = voc_agents.reply_prompt(rec, matches, proposal, intent,
                                             guidance=guidance, lang=lang,
                                             forbidden=tuple(_forbidden_names(rec)),
                                             canonical=canonical, prof=_voc_profile())
            body = "".join(_llm_stream(prompt)).strip()
            if len(body) >= 80 and voc_agents.detect_lang(body) == lang:
                fixed, _ = _proofread(_strip_preamble(body))
                return fixed, "llm"
            elif len(body) >= 80:
                print(f"[reply] 생성 언어 불일치(기대 {lang}) → 템플릿 사용")
            else:
                # 짧은 응답도 실패다. 조용히 템플릿으로 떨어지면 "LLM 경로로 돌렸는데
                # 전부 템플릿" 인 상태를 아무도 눈치채지 못한다.
                print(f"[reply] 생성 결과가 너무 짧음({len(body)}자) → 템플릿 사용")
        except Exception as e:
            print(f"[reply] LLM 생성 실패 → 템플릿 사용: {str(e)[:160]}")
    return voc_agents.render_reply(rec, matches, proposal, intent, lang=lang), "template"


def _reply_item(rec: dict, key: str, body: str, engine: str,
                matches: list[dict], intent: dict, lang: str) -> dict:
    """큐 항목 한 벌. 초안 경로가 둘(자동 생성 / 화면에서 받은 본문)이라 한 곳에서 만든다 —
    두 곳에서 만들면 재검사 기준(근거·금지어·언어)이 갈라진다."""
    forbidden = _forbidden_names(rec)
    policy = voc_agents.check(body, has_evidence=bool(matches), forbidden=tuple(forbidden),
                              lang=lang, asks=intent["asks"], prof=_voc_profile())
    return {
        "key": key, "summary": rec.get("summary", ""), "status": rec.get("status", ""),
        "body": body, "intent": intent["intent"], "intent_label": intent["label"],
        "asks": intent["asks"], "engine": engine, "lang": lang,
        "evidence": [m["key"] for m in matches[:3]],   # 내부 추적용(본문에는 없음)
        "has_evidence": bool(matches),
        "forbidden": forbidden,      # 재검사(편집·발송)가 같은 기준을 쓰도록 함께 보관
        "policy": policy,
        # 근거가 없거나, 정책 위반이 남아 있거나, 돈·감정이 걸린 유형이면 사람이 반드시 본다.
        # 템플릿 초안도 항상 검토 대상이다 — 그 폴백은 접수 사실과 진행 계획만 말하고
        # 고객의 질문에 실제로 답하지 않는다(측정: 요청 반영 3/32 vs LLM 31/32).
        "needs_review": (not matches) or (not policy["ok"])
                        or intent["intent"] in ("rma_request", "complaint")
                        or engine == "template",
        "created_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "state": "pending",
    }


def _queue_reply(item: dict, prev: dict | None) -> dict:
    """큐 적재 — 이미 발송된 건이면 이전 판본을 history 로 보존하고 대체한다."""
    if prev and prev.get("state") == "approved":
        item["history"] = (prev.get("history") or []) + [{
            "body": prev.get("final_body") or prev.get("body", ""),
            "comment_id": prev.get("comment_id", ""), "sent_at": prev.get("sent_at", ""),
            "sender": prev.get("sender", ""), "edited": bool(prev.get("edited")),
        }]
        saved = reply_queue.supersede(item)
        return {"queued": True, "reason_code": "queued_followup",
                "reason": f"후속 답변 초안을 만들었습니다 (이전 발송 {len(item['history'])}건 보존).",
                "item": saved, "counts": reply_queue.counts()}
    item["history"] = (prev.get("history") if prev else None) or []
    saved = reply_queue.upsert(item)
    return {"queued": True, "reason_code": "queued",
            "reason": "발송 대기 큐에 추가됨 (검토·수정 후 발송).",
            "item": saved, "counts": reply_queue.counts()}


def _already_sent(prev: dict | None, again: bool) -> dict | None:
    if prev and prev.get("state") == "approved" and not again:
        return {"queued": False, "reason_code": "already_sent",
                "reason": "이미 발송된 답변이 있습니다. 후속 답변을 쓰려면 "
                          "'다시 초안'(again)으로 요청하세요.",
                "item": prev, "counts": reply_queue.counts()}
    return None


class ReplyDraftBody(BaseModel):
    key: str
    use_llm: bool = True
    again: bool = False     # 이미 발송한 건에 **후속 답변**을 새로 쓴다


@app.post("/voc/reply/draft", dependencies=[Depends(require("reply.draft"))])
def voc_reply_draft(req: ReplyDraftBody):
    """고객 문의 → 고객에게 보낼 답변 초안 → **발송 대기 큐**. 외부 발송 없음."""
    st = _reco_state()
    rec = st["by_key"].get(req.key)
    if not rec:
        return _not_queued("not_found", f"이슈 {req.key} 를 KB에서 찾을 수 없습니다.")
    # 이미 발송된 건이면 **생성 전에** 멈춘다 — 뒤에서 막으면 LLM 비용을 치르고
    # 버리는 초안을 만들게 된다.
    prev = reply_queue.get(req.key)
    stop = _already_sent(prev, req.again)
    if stop:
        return stop
    result = st["reco"].recommend(rec, k=4, exclude_key=req.key)
    matches = result.get("matches", []) if result.get("coverage") else []
    proposal = result.get("proposal") if result.get("coverage") else None
    if not matches:
        # 근거 없이도 답은 나간다. 다만 '무엇을 몰랐는지' 는 공백 신호로 남긴다 —
        # 남기지 않으면 지식 공백이 고객 응대 품질로만 새어 나가고 집계되지 않는다.
        _record_gap(rec, result, "reply_no_evidence")
    ctx = _reply_context(rec)
    intent = voc_agents.classify(ctx, _voc_profile())
    # 고객이 쓴 언어로 답한다. 내용이 정확해도 언어가 다르면 대응 실패다.
    lang = voc_agents.detect_lang(ctx)
    body, engine = ((voc_agents.render_reply(rec, matches, proposal, intent, lang=lang), "template")
                    if not req.use_llm else _generate_reply(rec, matches, proposal, intent, lang))
    # 내부 키는 규칙으로 막기 전에 지운다 — 모델이 규칙을 어겨도 고객에게는 안 나간다.
    body = voc_agents.redact(body, _voc_profile())
    return _queue_reply(_reply_item(rec, req.key, body, engine, matches, intent, lang), prev)


class ReplyFromTextBody(BaseModel):
    key: str
    body: str
    again: bool = False


@app.post("/voc/reply/draft-from-text", dependencies=[Depends(require("reply.draft"))])
def voc_reply_draft_from_text(req: ReplyFromTextBody):
    """화면에서 생성·확인한 답변을 **그대로** 발송 대기 큐로.

    다시 만들지 않는다 — 재생성하면 사람이 읽고 판단한 글과 큐에 들어가는 글이
    달라진다. 검토의 의미가 사라지는 종류의 실수다.
    """
    st = _reco_state()
    rec = st["by_key"].get(req.key)
    if not rec:
        return _not_queued("not_found", f"이슈 {req.key} 를 KB에서 찾을 수 없습니다.")
    if not (req.body or "").strip():
        return _not_queued("empty_body",
                           "답변 본문이 비어 있습니다. 먼저 '✨ AI 고객 응대 답변'을 생성하세요.")
    prev = reply_queue.get(req.key)
    stop = _already_sent(prev, req.again)
    if stop:
        return stop
    result = _recommend_cached(rec, k=4, exclude_key=req.key)
    matches = result.get("matches", []) if result.get("coverage") else []
    ctx = _reply_context(rec)
    intent = voc_agents.classify(ctx, _voc_profile())
    lang = voc_agents.detect_lang(ctx)
    body = voc_agents.redact(req.body.strip(), _voc_profile())
    item = _reply_item(rec, req.key, body, "llm", matches, intent, lang)
    item["needs_review"] = True     # 화면을 거쳤어도 생성물인 것은 같다
    return _queue_reply(item, prev)


@app.get("/voc/reply/status", dependencies=[Depends(require("reply.read"))])
def voc_reply_status(key: str):
    """이 문의의 답변 상태 — 없음 / 발송 대기 / 발송됨.

    화면이 이걸 모르면 담당자가 이미 답장한 문의에 또 초안을 만든다.
    """
    it = reply_queue.get(key)
    if not it:
        return {"key": key, "state": "none"}
    return {"key": key, "state": it.get("state", "pending"),
            "needs_review": bool(it.get("needs_review")),
            "policy": it.get("policy"), "intent_label": it.get("intent_label", ""),
            "sent_at": it.get("sent_at", ""), "comment_id": it.get("comment_id", ""),
            "history": len(it.get("history") or [])}


@app.get("/voc/reply/pending", dependencies=[Depends(require("reply.read"))])
def voc_reply_pending():
    return {"items": reply_queue.items("pending"), "counts": reply_queue.counts()}


@app.get("/voc/reply/intents", dependencies=[Depends(require("reply.read"))])
def voc_reply_intents():
    """요청 유형 분류 체계 — UI 가 라벨/골격을 하드코딩하지 않도록 서버가 내려준다."""
    pf = _voc_profile()
    return {"profile": pf, "profile_label": voc_agents.profile(pf)["label"],
            "intents": [{"code": k, "label": v["label"], "goal": v["goal"],
                         "sections": list(v["sections"])}
                        for k, v in voc_agents.intents_of(pf).items()],
            "blocking": voc_agents.blocking_for(pf)}


class ReplyCheckBody(BaseModel):
    body: str
    key: str = ""                # 주면 큐 항목에서 검사 기준을 그대로 가져온다
    has_evidence: bool = True


@app.post("/voc/reply/check", dependencies=[Depends(require("reply.draft"))])
def voc_reply_check(req: ReplyCheckBody):
    """사람이 고친 본문 재검사 — 편집 중에도 발송 가능 여부를 바로 본다.

    기준(근거 유무·금지 고유명사·언어)은 **큐 항목이 정본**이다. 클라이언트가 보낸
    값으로만 검사하면 편집 화면의 판정이 발송 시점 판정보다 느슨해질 수 있다.
    """
    item = reply_queue.get(req.key) if req.key else None
    has_ev = bool(item.get("has_evidence")) if item else req.has_evidence
    forbidden = tuple(item.get("forbidden") or ()) if item else ()
    lang = str(item.get("lang") or "ko") if item else "ko"
    return voc_agents.check(req.body, has_evidence=has_ev, forbidden=forbidden, lang=lang,
                            asks=(item.get("asks") or []) if item else (),
                            prof=_voc_profile())


class ReplySendBody(BaseModel):
    key: str
    body: Optional[str] = None   # 사람이 수정한 본문(있으면 이걸 발송)
    note: str = ""


@app.post("/voc/reply/send", dependencies=[Depends(require("reply.send"))])
def voc_reply_send(req: ReplySendBody, request: Request):
    """HITL 게이트 — 사람 승인 시에만 고객에게 나간다(Jira 댓글로 게시).

    정책 차단(block)이 남아 있으면 **발송하지 않는다**. 경고(warn)는 사람이 보고
    판단할 몫이라 막지 않는다 — 전부 막으면 검토자가 검사 자체를 무시하게 된다.
    """
    item = reply_queue.get(req.key)
    if not item:
        return {"ok": False, "error": "발송 대기 큐에 없습니다."}
    if item.get("state") == "approved":
        return {"ok": True, "already": True, "item": item}
    original = item.get("body", "")
    final = (req.body if (req.body and req.body.strip()) else original)
    policy = voc_agents.check(final, has_evidence=bool(item.get("has_evidence")),
                              forbidden=tuple(item.get("forbidden") or ()),
                              lang=str(item.get("lang") or "ko"),
                              asks=item.get("asks") or [], prof=_voc_profile())
    if policy["blocked"]:
        return {"ok": False, "error": "정책 위반이 남아 있어 발송하지 않았습니다.",
                "policy": policy}
    try:
        from jira_commenter import post_comment
        body_out = f"📮 **{REPLY_MARKER}**\n\n{final}"
        res = post_comment(req.key, _md_to_jira(body_out))
        updated = reply_queue.set_state(
            req.key, "approved", comment_id=str(res.get("id", "")), final_body=final,
            edited=(original.strip() != final.strip()), policy=policy,
            note=(req.note or "")[:1000], sender=_actor(request),
            sent_at=_dt.datetime.now().isoformat(timespec="seconds"))
        # 사람이 검토·발송한 글은 이 유형의 **정본**이다. 같은 유형의 다음 문의는
        # 이걸 기준으로 쓴다 — 사람이 고쳐 놓은 표현이 다음 답변에 남지 않으면
        # 검토가 매번 처음부터 반복된다. 실패해도 발송은 유효해야 하므로 삼킨다.
        canon = None
        try:
            _, aid = failure_modes.canonical_reply_for(item.get("evidence") or [])
            aid = aid or failure_modes.member_index().get((item.get("evidence") or [""])[0], "")
            if aid:
                failure_modes.set_canonical_reply(aid, final, from_key=req.key,
                                                  author=_actor(request))
                canon = aid
        except Exception:
            pass
        return {"ok": True, "item": updated, "policy": policy,
                "edited": original.strip() != final.strip(), "canonical_for": canon,
                "counts": reply_queue.counts(), "stats": _reply_stats()}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


class ReplyRejectBody(BaseModel):
    key: str
    reason: str = ""


@app.post("/voc/reply/reject", dependencies=[Depends(require("reply.send"))])
def voc_reply_reject(req: ReplyRejectBody, request: Request):
    """거부 — 사유를 함께 남긴다. 왜 못 나갔는지가 남지 않으면 다음 초안이 같은 실수를 한다."""
    updated = reply_queue.set_state(req.key, "rejected",
                                    reject_reason=(req.reason or "")[:1000],
                                    reviewer=_actor(request))
    return {"ok": bool(updated), "item": updated, "counts": reply_queue.counts(),
            "stats": _reply_stats()}


def _reply_stats() -> dict:
    """고객 답변 품질 — 무수정 발송률·발송률·유형 분포·차단 원인.

    RCA 초안 지표(draft_feedback)와 **섞지 않는다**. 읽는 사람도 실패의 의미도
    다르므로, 한 지표로 합치면 어느 쪽이 나빠졌는지 알 수 없다.
    """
    from collections import Counter
    all_items = reply_queue.items()
    # 후속 답변으로 대체된 판본도 **고객에게 나간 글**이다. 빼면 발송 건수가 줄어들어
    # 지표가 실제보다 좋아 보인다(무수정 발송률의 분모가 사라진다). 판정 수에도 같이
    # 넣어야 한다 — 한쪽에만 넣으면 발송률이 1을 넘는다.
    history = [h for x in all_items for h in (x.get("history") or [])]
    decided = ([x for x in all_items if x.get("state") in ("approved", "rejected")]
               + history)
    sent = [x for x in all_items if x.get("state") == "approved"] + history
    clean = [x for x in sent if not x.get("edited")]
    causes = Counter()
    for x in all_items:
        for v in (x.get("policy") or {}).get("violations", []):
            causes[v["code"]] += 1
    return {
        "total": len(all_items) + len(history),
        "pending": sum(1 for x in all_items if x.get("state") == "pending"),
        "sent": len(sent), "decided": len(decided),
        "followups": len(history),
        "clean_rate": round(len(clean) / len(sent), 3) if sent else None,
        "send_rate": round(len(sent) / len(decided), 3) if decided else None,
        "by_intent": dict(Counter(x.get("intent", "other") for x in all_items)),
        "by_engine": dict(Counter(x.get("engine", "") for x in all_items)),
        "by_lang": dict(Counter(x.get("lang", "ko") for x in all_items)),
        "policy_violations": dict(causes),
        "no_evidence": sum(1 for x in all_items if not x.get("has_evidence")),
    }


@app.get("/voc/reply/stats", dependencies=[Depends(require("reply.read"))])
def voc_reply_stats():
    return _reply_stats()


@app.get("/rca/pending", dependencies=[Depends(require("rca.read"))])
def rca_pending():
    return {"items": rca_queue.items("pending"), "counts": rca_queue.counts()}


class ApproveBody(BaseModel):
    key: str
    body: Optional[str] = None   # 사람이 수정한 본문(있으면 이걸 게시·기록)
    causes: list[str] = []       # 수정 원인(draft_feedback.CAUSES). 없으면 diff 에서 자동 추정
    note: str = ""


@app.post("/rca/approve", dependencies=[Depends(require("rca.approve"))])
def rca_approve(req: ApproveBody, request: Request):
    """HITL 게이트 — 사람 승인(+수정) 시에만 Jira에 게시. 수정 내용은 피드백에 기록."""
    item = rca_queue.get(req.key)
    if not item:
        return {"error": "큐에 없음"}
    if item.get("state") == "approved":
        return {"ok": True, "already": True, "item": item}
    original = item.get("body", "")
    final = (req.body if (req.body and req.body.strip()) else original)  # 마크다운 정본
    try:
        from jira_commenter import post_comment
        res = post_comment(req.key, _md_to_jira(final))  # 게시 직전 Jira wiki로 변환
        now = _dt.datetime.now().isoformat(timespec="seconds")
        comment_id = str(res.get("id", ""))
        updated = rca_queue.set_state(req.key, "approved", comment_id=comment_id,
                                      final_body=final, edited=(original.strip() != final.strip()))
        # 사람 수정 피드백 저장(성능 개선용) — 클래스 매칭을 위해 분류/템플릿 동봉
        rec = _reco_state()["by_key"].get(req.key, {})
        cited = sorted(issue_keys.find_set(final))
        rca_feedback.record(req.key, item.get("summary", ""), item.get("source", ""),
                            original, final, item.get("based_on", ""), now,
                            category=rec.get("category", ""),
                            template=template_key(item.get("summary", "")),
                            symptom=rec.get("symptom", ""), chip=rec.get("chip", ""))
        # 초안 결함 원인 분류 적재 — 사람이 라벨을 주면 그것이 정본, 없으면 diff 에서
        # 추정한다. 무수정 승인도 남긴다(분모가 없으면 clean_rate 를 계산할 수 없다).
        # 실패해도 게시는 유효해야 하므로 삼킨다 — 신호 수집이 부작용을 막으면 안 된다.
        try:
            draft_feedback.record_edit(
                key=req.key, original=original, final=final,
                causes=req.causes, note=req.note,
                category=rec.get("category", ""), template=template_key(item.get("summary", "")),
                chip=rec.get("chip", ""), source=item.get("source", ""),
                citations=cited, confidence=item.get("confidence"),
                reviewer=_actor(request))
        except Exception:
            pass
        # 영속화: 큐레이션 지식을 git 추적 저장소에 적재(버전·백업·공유). 실패해도 게시는 유효.
        persisted = None
        try:
            persisted = knowledge_store.upsert(
                req.key, item.get("summary", ""), final,
                comment_id=comment_id, citations=cited,
                category=rec.get("category", ""), template=template_key(item.get("summary", "")),
                symptom=rec.get("symptom", ""), chip=rec.get("chip", ""),
                author=os.getenv("JIRA_EMAIL", ""), approved_at=now)
        except Exception:
            pass
        _invalidate_reco()  # KB 환류 반영 — 다음 요청 시 큐레이션 항목 포함해 재빌드
        return {"ok": True, "item": updated, "edited": original.strip() != final.strip(),
                "counts": rca_queue.counts(), "feedback": rca_feedback.stats(),
                "draft_feedback": draft_feedback.stats(),
                "persisted": bool(persisted), "knowledge": knowledge_store.stats()}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


@app.get("/rca/feedback", dependencies=[Depends(require("knowledge.read"))])
def rca_feedback_stats():
    return {"stats": rca_feedback.stats(), "recent_edits": rca_feedback.recent_edits(5)}


@app.get("/rca/draft-feedback", dependencies=[Depends(require("knowledge.read"))])
def rca_draft_feedback(template: str = "", cause: str = "", limit: int = 20):
    """초안 결함 원인 대시보드 — 축적된 거부/수정 사유와 그것이 가리키는 레버.

    taxonomy 를 같이 내려 프런트가 원인 목록을 하드코딩하지 않게 한다(닫힌 목록의
    단일 소스는 draft_feedback.CAUSES 하나다).
    """
    return {
        "stats": draft_feedback.stats(),
        "by_class": draft_feedback.by_class(),
        "trend": draft_feedback.trend(),
        "taxonomy": draft_feedback.taxonomy(),
        "recent": draft_feedback.events(template=template, cause=cause, limit=limit),
    }


# ---------------------------------------------------------------------------
# 지식 자산 영속화·환류 (P1-1)
# ---------------------------------------------------------------------------
@app.get("/knowledge/sources", dependencies=[Depends(require("knowledge.read"))])
def knowledge_sources():
    """KB 원천 현황 — 어떤 파일이 지식베이스를 이루고, 각각 몇 건이 근거로 쓰이는가.

    합계는 **중복 제거 후 실제 적재 기준**이다. 파일별 건수를 더한 값과 다를 수 있고,
    다르다면 키가 겹친 것이다. 서빙 중인 KB 와 대조해 드리프트도 함께 낸다 —
    설정을 바꾸고 캐시를 무효화하지 않으면 이 둘이 갈라진다.
    """
    st = kb_source.status()
    live = _reco_state()
    st["live"] = {
        "records": len(live["records"]),
        "resolved": len(live["resolved"]),        # 큐레이션 환류분 포함
        "unresolved": len(live["unresolved"]),
    }
    # 파일 기준과 서빙 기준의 차 — 큐레이션 지식(승인된 RCA) 유입분이라 보통 양수다.
    st["curated_in_live"] = st["live"]["resolved"] - st["resolved"]
    return st


@app.get("/knowledge/stats", dependencies=[Depends(require("knowledge.read"))])
def knowledge_stats():
    """영속 큐레이션 지식 저장소 현황(건수·출처·저장 경로)."""
    return {"knowledge": knowledge_store.stats()}


@app.get("/knowledge/quality", dependencies=[Depends(require("knowledge.read"))])
def knowledge_quality():
    """인입 KB 품질 리포트(P1-2) — 상태별 필드 충족률 + 무음 실패 의심 키."""
    st = _reco_state()
    # 큐레이션(-rca) 항목 제외하고 원본 인입 KB만 평가
    base = [r for r in st["records"] if not r.get("curated")]
    return quality_gate.validate(base, resolved_status=RESOLVED_STATUS)


# ---------------------------------------------------------------------------
# 고장모드(Known-Issue) 기사 계층 (P2-4)
# ---------------------------------------------------------------------------
@app.get("/knowledge/clusters", dependencies=[Depends(require("knowledge.read"))])
def knowledge_clusters(threshold: float = 0.80, min_size: int = 2):
    """해결 KB 임베딩 군집 → 고장모드 후보(중복 사례 묶음). 승격 검토용."""
    st = _reco_state()
    clusters = failure_modes.cluster_from_recommender(
        st["reco"], threshold=threshold, min_size=min_size)
    return {"threshold": threshold, "min_size": min_size,
            "count": len(clusters), "clusters": clusters, "stats": failure_modes.stats()}


class PromoteBody(BaseModel):
    title: str
    members: list[str]
    failure_summary: str = ""
    root_cause: str = ""
    resolution: str = ""
    workaround: str = ""
    chips: Optional[list] = None
    categories: Optional[list] = None
    article_id: str = ""             # 지정 시 기존 기사 갱신(멤버 합집합)


@app.post("/knowledge/known-issue", dependencies=[Depends(require("knowledge.write"))])
def knowledge_promote(req: PromoteBody):
    """후보 군집(또는 선택 사례)을 정규 Known-Issue 기사로 승격/갱신."""
    st = _reco_state()
    by_key = st["by_key"]
    # 본문 미지정 시 대표(검증 우선) 사례에서 정규 내용 자동 채움 — 사람이 추후 정제
    rc, rs, wa = req.root_cause, req.resolution, req.workaround
    if not (rc or rs):
        rep = next((by_key[m] for m in req.members
                    if by_key.get(m, {}).get("verified")), None) \
            or next((by_key[m] for m in req.members if m in by_key), None)
        if rep:
            rc = rc or rep.get("root_cause", "")
            rs = rs or rep.get("resolution", "")
            wa = wa or rep.get("workaround", "")
    chips = req.chips or sorted({by_key[m].get("chip", "") for m in req.members
                                 if by_key.get(m, {}).get("chip")})
    cats = req.categories or sorted({by_key[m].get("category", "") for m in req.members
                                     if by_key.get(m, {}).get("category")})
    try:
        art = failure_modes.promote(
            title=req.title, members=req.members, failure_summary=req.failure_summary,
            root_cause=rc, resolution=rs, workaround=wa, chips=chips, categories=cats,
            author=os.getenv("JIRA_EMAIL", ""), article_id=req.article_id)
        return {"ok": True, "article": art, "stats": failure_modes.stats()}
    except ValueError as e:
        return {"ok": False, "error": str(e)}


@app.get("/knowledge/known-issues", dependencies=[Depends(require("knowledge.read"))])
def knowledge_known_issues():
    """승격된 Known-Issue 기사 목록."""
    return {"articles": failure_modes.articles(), "stats": failure_modes.stats()}


# ---------------------------------------------------------------------------
# 신선도·폐기 수명주기 (P2-5)
# ---------------------------------------------------------------------------
class LifecycleBody(BaseModel):
    key: str
    state: str                       # active | deprecated | superseded
    superseded_by: str = ""
    reason: str = ""


@app.post("/knowledge/lifecycle", dependencies=[Depends(require("knowledge.write"))])
def knowledge_lifecycle(req: LifecycleBody):
    """사례 수명주기 상태 설정(폐기/대체). 폐기·대체 사례는 추천에서 강등·경고."""
    try:
        info = lifecycle.set_state(req.key, req.state,
                                   superseded_by=req.superseded_by, reason=req.reason)
        _invalidate_reco()
        return {"ok": True, "lifecycle": info, "stats": lifecycle.stats()}
    except ValueError as e:
        return {"ok": False, "error": str(e)}


@app.get("/knowledge/lifecycle/stats", dependencies=[Depends(require("knowledge.read"))])
def knowledge_lifecycle_stats():
    return {"stats": lifecycle.stats()}


# ---------------------------------------------------------------------------
# 온톨로지 거버넌스 (P2-6)
# ---------------------------------------------------------------------------
@app.get("/knowledge/ontology", dependencies=[Depends(require("knowledge.read"))])
def knowledge_ontology():
    """통제 어휘(동의어 그룹·통제 분류) 현황."""
    return {"vocab": ontology.vocab(), "stats": ontology.stats()}


@app.get("/knowledge/ontology/review", dependencies=[Depends(require("knowledge.read"))])
def knowledge_ontology_review(top: int = 40):
    """통제 어휘에 없는 엔티티/분류를 빈도순으로 — canonical 승격 검토 큐."""
    st = _reco_state()
    base = [r for r in st["records"] if not r.get("curated")]
    return ontology.review(base, top=top)


class SynonymBody(BaseModel):
    canonical: str
    aliases: list[str] = []


@app.post("/knowledge/ontology/synonym", dependencies=[Depends(require("knowledge.write"))])
def knowledge_ontology_synonym(req: SynonymBody):
    """동의어 그룹 추가/확장(alias→canonical). 다음 재빌드부터 엔티티 통합."""
    try:
        out = ontology.add_synonym(req.canonical, req.aliases)
        _invalidate_reco()  # 정규화 반영을 위해 KB 재빌드
        return {"ok": True, "group": out, "stats": ontology.stats()}
    except ValueError as e:
        return {"ok": False, "error": str(e)}


class CategoriesBody(BaseModel):
    categories: list[str]


@app.post("/knowledge/ontology/categories", dependencies=[Depends(require("knowledge.write"))])
def knowledge_ontology_categories(req: CategoriesBody):
    """통제 분류 어휘 설정."""
    out = ontology.set_categories(req.categories)
    return {"ok": True, **out, "stats": ontology.stats()}


# ---------------------------------------------------------------------------
# 부정지식(기각된 가설) (P2-7)
# ---------------------------------------------------------------------------
class NegativeBody(BaseModel):
    key: str
    hypothesis: str
    reason: str = ""


@app.post("/knowledge/negative", dependencies=[Depends(require("knowledge.write"))])
def knowledge_negative(req: NegativeBody):
    """기각된 가설 기록 — 심층 분석 시 재안 방지에 활용."""
    try:
        out = negative_knowledge.add(req.key, req.hypothesis, req.reason,
                                     author=os.getenv("JIRA_EMAIL", ""))
        return {"ok": True, **out, "stats": negative_knowledge.stats()}
    except ValueError as e:
        return {"ok": False, "error": str(e)}


@app.get("/knowledge/negative", dependencies=[Depends(require("knowledge.read"))])
def knowledge_negative_get(key: str):
    """특정 이슈의 기각된 가설 목록."""
    return {"key": key, "rejected": negative_knowledge.get(key), "stats": negative_knowledge.stats()}


# ---------------------------------------------------------------------------
# 지식 공백 관측성 (P3-8)
# ---------------------------------------------------------------------------
@app.get("/knowledge/gaps", dependencies=[Depends(require("knowledge.read"))])
def knowledge_gaps_report(top: int = 20):
    """지식 공백 대시보드 — 자주 질의되나 사례 없는(coverage 미통과) 영역 집계."""
    return knowledge_gaps.report(top=top)


# ---------------------------------------------------------------------------
# 결과·효능 추적 (자기개선 #1)
# ---------------------------------------------------------------------------
@app.post("/knowledge/outcomes/refresh", dependencies=[Depends(require("knowledge.write"))])
def knowledge_outcomes_refresh():
    """게시된 RCA 대상 이슈의 현재 Jira 상태를 조회해 효능(게시 후 해결 여부) 갱신."""
    import outcome_tracker
    try:
        out = outcome_tracker.refresh()
        _invalidate_reco()
        return {"ok": True, **out}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


@app.get("/knowledge/outcomes", dependencies=[Depends(require("knowledge.read"))])
def knowledge_outcomes():
    """효능 집계 — resolved_after_rca/pending 비율 + 효능율."""
    import outcome_tracker
    return outcome_tracker.report()


# ---------------------------------------------------------------------------
# 지식 모순 탐지 (자기개선 #2)
# ---------------------------------------------------------------------------
@app.get("/knowledge/contradictions", dependencies=[Depends(require("knowledge.read"))])
def knowledge_contradictions(sim_hi: float = 0.85, rc_lo: float = 0.60):
    """같은 고장모드(문서 유사↑)인데 근본원인이 엇갈리는(근본원인 유사↓) 쌍 = 모순 후보."""
    import contradictions
    st = _reco_state()
    return contradictions.report(st["reco"], sim_hi=sim_hi, rc_lo=rc_lo)


# ---------------------------------------------------------------------------
# 평가셋 빌더 (자기개선 #0 — 평가 기질)
# ---------------------------------------------------------------------------
@app.post("/eval/build", dependencies=[Depends(require("ops.eval"))])
def eval_build():
    """실신호 평가셋(real: outcome+feedback) + 변별 hard셋(증상만) 재빌드."""
    import eval_builder
    st = _reco_state()
    base = [r for r in st["records"] if not r.get("curated")]
    return {"real": eval_builder.real_pairs(st["by_key"]), "hard": eval_builder.hard_set(base)}


@app.post("/eval/paraphrase/generate", dependencies=[Depends(require("ops.eval"))])
def eval_paraphrase_generate(per_template: int = 1, max_templates: int = 10):
    """LLM 재서술로 변별 평가셋 확장(토큰 비용 — max_templates로 제한). 누적 저장."""
    import eval_builder
    st = _reco_state()
    base = [r for r in st["records"] if not r.get("curated")]
    return eval_builder.generate_paraphrases(base, per_template=per_template, max_templates=max_templates)


# ---------------------------------------------------------------------------
# 자기 개선 loop — L1 측정·진단·제안 (무변경)
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 저자·소유권 / find-the-expert (P3-9)
# ---------------------------------------------------------------------------
class OwnerBody(BaseModel):
    key: str
    author: str = ""
    validator: str = ""
    role: str = ""


@app.post("/knowledge/ownership", dependencies=[Depends(require("knowledge.write"))])
def knowledge_ownership(req: OwnerBody):
    """사례/기사의 저자·검증자·역할 기록(책임성·신뢰가중·전문가 탐색용)."""
    try:
        return {"ok": True, "owner": ownership.set_owner(
            req.key, author=req.author, validator=req.validator, role=req.role),
            "stats": ownership.stats()}
    except ValueError as e:
        return {"ok": False, "error": str(e)}


@app.get("/knowledge/experts", dependencies=[Depends(require("knowledge.read"))])
def knowledge_experts(category: str = "", template: str = "", top: int = 5):
    """find-the-expert — 고장 클래스별 기여 빈도순 전문가 후보."""
    return ownership.experts_for(category=category, template=template, top=top)


# ---------------------------------------------------------------------------
# 지식 export·상호운용 (P3-10)
# ---------------------------------------------------------------------------
@app.get("/knowledge/export", dependencies=[Depends(require("knowledge.read"))])
def knowledge_export_endpoint(format: str = "json"):  # 함수명: 모듈 knowledge_export과 충돌 회피
    """축적 지식 내보내기 — format=json(구조화) | markdown(위키 붙여넣기용)."""
    if format == "markdown":
        from fastapi.responses import PlainTextResponse
        return PlainTextResponse(knowledge_export.to_markdown(),
                                 media_type="text/markdown; charset=utf-8")
    return knowledge_export.bundle()


@app.get("/selfcheck", dependencies=[Depends(require("ops.eval"))])
def selfcheck(save: bool = True):
    """자기 개선 점검 — 모든 측정 신호 집계 + 직전 대비 드리프트 + 개선 제안.

    부작용 없음(지식 불변). save=true면 이력·날짜별 리포트 기록.
    """
    st = _reco_state()
    return self_improve.run(st["records"], save=save)


class ParamEvalBody(BaseModel):
    param: str           # gate_cos | boost
    value: float


@app.post("/selfcheck/evaluate-param", dependencies=[Depends(require("ops.eval"))])
def selfcheck_evaluate_param(req: ParamEvalBody):
    """L2 — 후보 파라미터를 동결 평가셋에 shadow 평가(READ-ONLY, live 불변).

    무회귀일 때만 safe=true. 안전해도 자동 적용 안 함 — apply는 별도 명시 호출.
    """
    try:
        return self_improve.evaluate_param(req.param, req.value)
    except ValueError as e:
        return {"error": str(e)}


@app.post("/selfcheck/apply-param", dependencies=[Depends(require("ops.eval"))])
def selfcheck_apply_param(req: ParamEvalBody):
    """L2 적용 — 무회귀 게이트를 재실행해 통과할 때만 override 영속·반영(되돌림 가능).

    회귀하면 거부. value를 비우면 override 제거(기본값 복귀). 사람이 명시 호출하는 단계.
    """
    env_key = self_improve.TUNABLE.get(req.param)
    if not env_key:
        return {"ok": False, "error": f"튜닝 가능 파라미터 아님: {req.param}"}
    verdict = self_improve.evaluate_param(req.param, req.value)
    if not verdict.get("safe"):
        return {"ok": False, "applied": False, "verdict": verdict,
                "error": "무회귀 게이트 미통과 — 적용 거부"}
    app_config.set_env(env_key, str(req.value))
    _invalidate_reco()  # 검증된 값 반영
    return {"ok": True, "applied": True, "param": req.param, "value": req.value,
            "env": env_key, "verdict": verdict}


@app.post("/selfcheck/reset-param", dependencies=[Depends(require("ops.eval"))])
def selfcheck_reset_param(param: str):
    """L2 되돌리기 — 파라미터 override 제거(클래스 기본값 복귀)."""
    env_key = self_improve.TUNABLE.get(param)
    if not env_key:
        return {"ok": False, "error": f"튜닝 가능 파라미터 아님: {param}"}
    app_config.set_env(env_key, "")
    _invalidate_reco()
    return {"ok": True, "reset": param, "env": env_key}


# ---------------------------------------------------------------------------
# 자기 개선 loop — L3 지식 변경 제안 큐 (사람 검토 전용, loop는 실행 안 함)
# ---------------------------------------------------------------------------
@app.post("/improve/suggest", dependencies=[Depends(require("improve.manage"))])
def improve_suggest():
    """신호에서 지식 변경 제안을 도출해 큐에 병합(거부/완료 상태 보존). loop는 실행 안 함."""
    st = _reco_state()
    base = [r for r in st["records"] if not r.get("curated")]
    generated = self_improve.suggest(reco=st["reco"], records=base)
    res = improve_queue.sync(generated)
    return {"generated": len(generated), **res, "open_items": improve_queue.items("open")}


@app.get("/improve/queue", dependencies=[Depends(require("knowledge.read"))])
def improve_queue_list(state: str = "open"):
    """제안 큐 조회(기본 open)."""
    return {"items": improve_queue.items(state), "counts": improve_queue.counts()}


class SuggestStateBody(BaseModel):
    id: str
    state: str           # open | done | dismissed


@app.post("/improve/queue/state", dependencies=[Depends(require("improve.manage"))])
def improve_queue_state(req: SuggestStateBody):
    """제안 상태 변경(사람 결정: done 완료 / dismissed 거부)."""
    try:
        it = improve_queue.set_state(req.id, req.state)
        return {"ok": bool(it), "item": it, "counts": improve_queue.counts()}
    except ValueError as e:
        return {"ok": False, "error": str(e)}


@app.post("/reco/reload", dependencies=[Depends(require("ops.sync"))])
def reco_reload():
    """추천 KB 캐시 무효화 — 새 큐레이션 지식을 서버 재시작 없이 즉시 반영."""
    _invalidate_reco()
    st = _reco_state()
    return {"ok": True, "kb_size": len(st["resolved"]), "by_key": len(st["by_key"])}


@app.post("/knowledge/rebuild-from-jira", dependencies=[Depends(require("ops.sync"))])
def knowledge_rebuild_from_jira():
    """재해 복구/머신 간 동기화 — Jira 봇 댓글(조직 SoT)에서 지식 자산을 재구성한다.

    로컬 data/knowledge_store.json 유실 시에도 Jira에서 큐레이션 지식을 복원.
    """
    try:
        out = knowledge_store.rebuild_from_jira(BOT_MARKER)
        _invalidate_reco()  # 복원된 지식 즉시 반영
        return {"ok": True, **out}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


class RejectBody(BaseModel):
    key: str
    causes: list[str] = []       # draft_feedback.CAUSES 코드 (닫힌 목록)
    note: str = ""               # 자유 서술(집계 대상 아님 — 원인은 causes 로만 센다)


@app.post("/rca/reject", dependencies=[Depends(require("rca.approve"))])
def rca_reject(req: RejectBody, request: Request):
    """거부 — **사유를 반드시 함께 남긴다.**

    예전에는 상태만 rejected 로 바꾸고 끝이라, "왜 이 초안이 못 쓰게 됐나" 가
    어디에도 남지 않았다. 그래서 다음 초안이 같은 실수를 반복해도 알 수 없었다.
    거부는 가장 강한 부정 신호다 — 여기서 버리면 개선할 근거 자체가 없다.
    """
    item = rca_queue.get(req.key) or {}
    updated = rca_queue.set_state(req.key, "rejected",
                                  reject_causes=draft_feedback.normalize_causes(req.causes),
                                  reject_note=(req.note or "")[:1000])
    fb = None
    if updated:
        rec = _reco_state()["by_key"].get(req.key, {})
        fb = draft_feedback.record(
            key=req.key, outcome="rejected", causes=req.causes,
            origin="human", note=req.note,
            category=rec.get("category", ""), template=template_key(item.get("summary", "")),
            chip=rec.get("chip", ""), source=item.get("source", ""),
            citations=sorted(issue_keys.find_set(item.get("body", ""))),
            confidence=item.get("confidence"), reviewer=_actor(request))
    return {"ok": bool(updated), "item": updated, "counts": rca_queue.counts(),
            "feedback": fb, "draft_feedback": draft_feedback.stats()}


class JudgeScore(BaseModel):
    """수정사항 검증 채점(구조화 출력)."""
    score: int = Field(description="1~10 정수 — 근거 충실도·인용 정합·실행가능성 종합")
    passed: bool = Field(description="7점 이상이면 true")
    reasoning: str = Field(description="채점 근거 (한국어 1~2문장)")


def _judge_rca(ctx: str, body: str) -> "JudgeScore | None":
    """RCA 분석을 근거 사례 대비 채점 — RcaExplanation과 동일한 구조화 출력 경로."""
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        return None
    try:
        from agno.agent import Agent
        from agno.models.openrouter import OpenRouter
        jm = os.getenv("RVP_JUDGE_MODEL") or os.getenv("RVP_MODEL") or os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini")
        base = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
        agent = Agent(
            model=OpenRouter(id=jm, api_key=api_key, base_url=base,
                             default_headers=custom_headers() or None),
            output_schema=JudgeScore, use_json_mode=True, markdown=False, telemetry=False,
            instructions=["LSI 불량 분석 RCA 채점관. 제공된 근거 사례에 비추어 평가한다.",
                          "기준: 근거 충실도(날조·환각 감점), 인용 정합, 권장 해결 단계의 구체성·실행가능성, 한자 금지 준수.",
                          "한국어로 간단히 채점한다."])
        out = agent.run(input=f"## 근거 사례\n{ctx}\n\n## 채점할 RCA 분석\n{body}")
        if not isinstance(out.content, JudgeScore):
            return None
        sc = out.content
        sc.score = max(1, min(10, int(sc.score)))   # 모델이 1~10 범위를 벗어나는 경우 보정
        sc.passed = sc.score >= 7                    # 보정 점수에 맞춰 통과 재계산
        return sc
    except Exception:
        return None


class ValidateBody(BaseModel):
    key: str
    body: Optional[str] = None   # 현재(수정된) 본문; 없으면 큐의 원본


@app.post("/rca/validate", dependencies=[Depends(require("rca.draft"))])
def rca_validate(req: ValidateBody):
    """수정사항 검증 — (1) 가드레일: 인용 키 ⊆ KB, 한자/CJK, 빈값  (2) Agent-as-Judge:
    근거 충실도·인용 정합·실행가능성 1~10 채점. 승인 전 품질 확인용(차단 아님)."""
    st = _reco_state()
    body = (req.body if (req.body and req.body.strip()) else (rca_queue.get(req.key) or {}).get("body", "")).strip()
    if not body:
        return {"error": "검증할 본문이 없습니다."}
    valid = set(st["by_key"].keys())
    cited = issue_keys.find_set(body)
    invalid = sorted(c for c in cited if c not in valid)
    vr = validate_and_fix(body)
    out = {
        "citations_ok": not invalid, "invalid_citations": invalid,
        "lang_ok": bool(vr.ok), "non_empty": True,
    }
    # LLM 판정 — 구조화 출력(검증된 use_json_mode 경로)으로 근거 충실도·실행가능성 채점
    rec = st["by_key"].get(req.key, {})
    cited_recs = [st["by_key"][k] for k in cited if k in st["by_key"]]
    ctx = (f"미해결 이슈: {rec.get('summary','')}\n증상: {rec.get('symptom','')}\n\n근거 사례:\n"
           + "\n".join(f"[{r['key']}] 근본원인: {r.get('root_cause','')} / 해결: {r.get('resolution','')}"
                       for r in cited_recs))
    jr = _judge_rca(ctx, body)
    if jr is not None:
        out["judge_score"] = jr.score
        out["judge_passed"] = jr.passed
        out["judge_reasoning"] = jr.reasoning[:400]
    return out


# MCP 엔드포인트 마운트 — 정적 프론트("/")보다 **먼저** 붙인다. 순서가 뒤바뀌면
# SPA 폴백이 /mcp 를 삼킨다. 백엔드 호출은 인프로세스(ASGITransport)로 돌려
# 자기 자신에게 소켓을 다시 열지 않으면서 인증 의존성을 그대로 통과시킨다.
if _MCP is not None:
    # 슬래시 없는 /mcp 도 받는다. Mount 는 POST 를 자동 리다이렉트하지 않아서, 문서대로
    # "https://<서버>/mcp" 를 넣은 클라이언트가 405 만 보고 이유를 알 수 없었다.
    # 307 이어야 메서드와 본문이 보존된다(301/302 는 POST 를 GET 으로 바꾼다).
    from fastapi.responses import RedirectResponse as _Redirect

    @app.api_route("/mcp", methods=["GET", "POST", "DELETE"], include_in_schema=False)
    async def _mcp_slash(request: Request):
        q = f"?{request.url.query}" if request.url.query else ""
        return _Redirect(f"/mcp/{q}", status_code=307)

    app.mount("/mcp", _MCP.app, name="mcp")
    _MCP.bind(app)
    print("[mcp] /mcp 마운트됨 (streamable-HTTP) — RVP_MCP=0 으로 끕니다")


# 프로덕션(Docker 등): 빌드된 프론트(web/dist)를 같은 오리진에서 정적 서빙.
# 디렉터리가 있을 때만 마운트하므로 개발(dist 없음)엔 영향 없음. API 라우트가 모두
# 등록된 뒤 '/'에 마운트 → 명시 경로(/health 등)가 우선, 나머지는 SPA(index.html).
_WEB_DIST = ROOT / "web" / "dist"
if _WEB_DIST.is_dir():
    from fastapi.staticfiles import StaticFiles
    app.mount("/", StaticFiles(directory=str(_WEB_DIST), html=True), name="web")


if __name__ == "__main__":
    import uvicorn
    import os as _os
    # 컨테이너에서는 0.0.0.0 바인드 필요 → RVP_HOST로 조정(기본 127.0.0.1 로컬 안전).
    #
    # 앱을 **객체로** 넘긴다. "server:app" 문자열이면 uvicorn 이 이 파일을 모듈로 다시
    # import 해서 모듈 본문이 두 번 돈다 — 이 파일은 __main__ 으로 이미 한 번 실행된
    # 뒤다. 실제로 MCP 앱과 FastAPI 앱이 두 벌씩 만들어지고 있었다(로그의 "[mcp]
    # 마운트됨" 이 두 번 찍혀 드러났다). 트래픽은 한쪽만 받으니 나머지는 통째로 사장된다.
    # 지금은 메모리 낭비 정도지만, 모듈 수준에 부작용(파일 쓰기·스레드·외부 등록)이
    # 하나라도 생기면 조용히 두 번 실행된다. reload 를 쓰지 않으므로 객체 전달이 맞다.
    uvicorn.run(app, host=_os.getenv("RVP_HOST", "127.0.0.1"),
                port=int(_os.getenv("RVP_PORT", "8001")), reload=False)
