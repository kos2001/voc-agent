"""역할 기반 접근 제어(RBAC) — 관리자 / 사용자.

권한 모델은 두 역할로 나눈다:

  admin(관리자) : 운영·설정·외부 게시. Jira 댓글 게시, 설정 변경, 지식 자산 편집,
                  동기화·예열·캐시 조작, 평가·자기점검 실행.
  user(사용자)  : 분석 업무. 이슈 조회·추천·심층 분석·RCA 초안 제출·피드백·VOC.
                  초안은 **승인 대기 큐까지만** 갈 수 있고 Jira 게시는 못 한다.

핵심 원칙 — 권한은 **역할이 아니라 기능(capability)** 단위로 검사한다. 엔드포인트가
`require("rca.approve")` 처럼 필요한 기능을 선언하고, 역할→기능 표는 여기 한 곳에만
둔다. 역할이 늘어나도 엔드포인트를 고치지 않는다.

신원(id)은 **이메일 또는 아이디**다. SSO(IdP)가 주는 것은 이메일이므로 SSO 로 들어오는
계정은 이메일 id 를 쓰고, `admin` 같은 로컬 아이디는 프록시 헤더나 개발용 로그인처럼
IdP 를 거치지 않는 경로에서 쓴다(IdP 가 그 값을 이메일 클레임으로 주지 않는 한
OIDC 로는 로그인되지 않는다 — 로컬 운영 계정용이다).

인가 대상 목록은 `data/users.yaml`(또는 RVP_USERS_FILE)이다. 파일이 없으면
**인증 비활성**(전체 권한) — 기존 로컬 개발 흐름을 깨지 않기 위한 것이고, 운영에서는
파일이나 RVP_ADMIN_EMAILS 를 반드시 둔다. 상태는 `/auth/config` 로 노출해
"인증이 꺼져 있는지"가 화면에서 보이게 한다.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger("uvicorn.error")

Role = Literal["admin", "user"]

# 사용자(user)가 가지는 기능 — 분석 업무에 필요한 것만.
_USER_CAPS = {
    "issue.read",        # 이슈 목록·그래프·통계 조회
    "reco.read",         # 추천 + 심층 분석(LLM) 조회
    "knowledge.read",    # 지식 현황 대시보드 조회
    "rca.draft",         # RCA 초안 생성 → 승인 대기 큐 (게시 아님)
    "rca.read",          # 승인 대기 목록 조회
    "feedback.write",    # 추천 유용성 피드백 · VOC 제출
    "reply.draft",       # 고객 답변 초안 생성 → 발송 대기 큐 (발송 아님)
    "reply.read",        # 발송 대기 답변 조회
}

# 관리자(admin)는 사용자 기능 전부 + 운영 기능.
_ADMIN_ONLY_CAPS = {
    "rca.approve",       # Jira 실제 게시 / 거부 — 외부로 나가는 행위
    "reply.send",        # 고객 답변 실제 발송 / 거부 — 고객에게 나가는 행위
    "knowledge.write",   # 고장모드 기사·수명주기·온톨로지·부정지식·소유자 편집
    "config.write",      # LLM/Jira 접속 설정 변경
    "ops.sync",          # Jira 동기화·재적재·추천기 리로드
    "ops.cache",         # 심층 분석 캐시 비우기·예열
    "ops.eval",          # 평가셋 빌드·자기점검·파라미터 적용
    "voc.manage",        # VOC 목록 조회·상태 변경
    "improve.manage",    # 개선 큐 상태 변경·제안 생성
}

CAPABILITIES: dict[str, set[str]] = {
    "admin": _USER_CAPS | _ADMIN_ONLY_CAPS,
    "user": set(_USER_CAPS),
}

ALL_CAPABILITIES = CAPABILITIES["admin"]


@dataclass(frozen=True)
class User:
    """인증된 신원 + 역할. subject 는 안정 식별자(이메일 또는 아이디, 소문자)."""
    subject: str
    name: str
    role: str
    email: str = ""
    via: str = ""            # 인증 경로: oidc | proxy | dev | disabled

    def can(self, capability: str) -> bool:
        return capability in CAPABILITIES.get(self.role, set())

    def public(self) -> dict:
        return {"subject": self.subject, "name": self.name, "role": self.role,
                "email": self.email, "via": self.via,
                "capabilities": sorted(CAPABILITIES.get(self.role, set()))}


# 인증이 꺼져 있을 때 쓰는 신원. 역할은 admin 이지만 via 로 구분되므로
# 화면과 /auth/config 에서 "인증 비활성"임을 알 수 있다.
ALL_ACCESS = User(subject="", name="(인증 비활성)", role="admin", via="disabled")


def normalize_id(value: str) -> str:
    """식별자 정규화 — 공백 제거 + 소문자. 이메일과 아이디 모두 같은 규칙."""
    return (value or "").strip().lower()


# 이전 이름 — 호출부가 남아 있어 별칭으로 유지한다.
normalize_email = normalize_id


def is_email(value: str) -> bool:
    return "@" in (value or "")


def _users_path() -> Path:
    override = os.getenv("RVP_USERS_FILE", "").strip()
    return Path(override) if override else ROOT / "data" / "users.yaml"


def _admin_emails() -> set[str]:
    """RVP_ADMIN_EMAILS — users.yaml 없이도 관리자를 지정하는 경로.

    SSO만 붙이고 사용자 목록을 따로 관리하지 않는 배포를 위한 것이다.
    """
    raw = os.getenv("RVP_ADMIN_EMAILS", "")
    return {normalize_email(e) for e in raw.replace(";", ",").split(",") if e.strip()}


def _default_role() -> str | None:
    """목록에 없는 인증된 사용자에게 줄 역할. 빈 값이면 거부."""
    r = os.getenv("RVP_SSO_DEFAULT_ROLE", "user").strip().lower()
    return r if r in CAPABILITIES else None


def allowed_domains() -> set[str]:
    """자동 등록을 허용할 이메일 도메인. 비면 제한 없음.

    사내 도메인이 `xxx.samsung.com` 형태이므로 서브도메인까지 인정한다
    (`samsung.com` 을 넣으면 `sec.samsung.com` 도 통과).
    """
    raw = os.getenv("RVP_ALLOWED_EMAIL_DOMAINS", "")
    return {d.strip().lower().lstrip("@") for d in raw.replace(";", ",").split(",") if d.strip()}


def domain_allowed(ident: str) -> bool:
    """목록에 없는 신원을 기본역할로 받아 줄지. 명시적으로 등록된 계정에는 적용 안 함."""
    doms = allowed_domains()
    if not doms:
        return True
    if not is_email(ident):
        return False                 # 도메인 제한이 켜졌으면 아이디 계정은 자동 등록 불가
    host = ident.rsplit("@", 1)[-1]
    return any(host == d or host.endswith("." + d) for d in doms)


def load_users() -> dict[str, User] | None:
    """email → User. 목록이 아예 없으면 None(인증 비활성).

    is_file() 로 확인한다 — Docker 는 바인드 마운트 소스가 없으면 그 경로에
    디렉터리를 만들어 버리므로 exists() 로는 부족하다(read_text 가 터진다).
    """
    users: dict[str, User] = {}
    path = _users_path()
    if path.is_file():
        import yaml
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception as e:
            log.warning("users.yaml 파싱 실패 — 인증 비활성으로 두지 않고 빈 목록으로 본다: %s", e)
            raw = {}
        for item in (raw.get("users") or []):
            if item.get("revoked"):
                continue                      # 폐기 항목은 파일에 남겨 이력이 보이게 한다
            # id 가 정식 키이고 email 은 이전 형식 — 둘 다 받는다.
            ident = normalize_id(str(item.get("id") or item.get("email") or ""))
            if not ident:
                continue
            role = str(item.get("role") or "").strip().lower()
            if role not in CAPABILITIES:
                # 오타 하나로 서비스가 죽거나(KeyError) 의도 없이 권한이 생기는 것을
                # 둘 다 막는다 — 항목을 버리고 알린다.
                log.warning("users.yaml 항목 무시: 알 수 없는 역할 %r (id=%s). 허용: %s",
                            role, ident, ", ".join(sorted(CAPABILITIES)))
                continue
            users[ident] = User(subject=ident, name=str(item.get("name") or ident),
                                role=role, email=ident if is_email(ident) else "")
    for ident in _admin_emails():
        # 환경변수 관리자는 파일보다 우선 — 잠금 해제 경로로 쓸 수 있어야 한다.
        prev = users.get(ident)
        users[ident] = User(subject=ident, name=(prev.name if prev else ident),
                            role="admin", email=ident if is_email(ident) else "")
    if not users:
        return None
    return users


def resolve_email(users: dict[str, User] | None, ident: str, via: str) -> User | None:
    """IdP·프록시가 인증한 식별자(이메일 또는 아이디) → User. 인가 안 되면 None."""
    if users is None:
        return ALL_ACCESS
    e = normalize_id(ident)
    if not e:
        return None
    hit = users.get(e)
    if hit is not None:
        return User(subject=hit.subject, name=hit.name, role=hit.role, email=hit.email, via=via)
    role = _default_role()
    if role is None:
        return None                            # 목록 밖은 거부(화이트리스트 운영)
    if not domain_allowed(e):
        # IdP 가 외부·게스트 계정을 인증해 줄 수도 있다 — 사내 도메인만 자동 등록한다.
        log.warning("자동 등록 거부(허용 도메인 아님): %s", e)
        return None
    return User(subject=e, name=e, role=role, email=e if is_email(e) else "", via=via)


# 이름이 이메일만 받는 것처럼 읽혀서 별칭을 둔다(호출부는 점진 이행).
resolve_identity = resolve_email


def fingerprint(secret: str) -> str:
    """비밀값을 로그·저장에 쓰지 않기 위한 해시."""
    return hashlib.sha256((secret or "").encode("utf-8")).hexdigest()


def auth_status(users: dict[str, User] | None) -> dict:
    return {
        "enabled": users is not None,
        "users_file": str(_users_path()),
        "users_file_present": _users_path().is_file(),
        "admin_emails_env": len(_admin_emails()),
        "default_role": _default_role() or "(거부)",
        "allowed_domains": sorted(allowed_domains()),
        "roles": {r: sorted(c) for r, c in CAPABILITIES.items()},
    }
