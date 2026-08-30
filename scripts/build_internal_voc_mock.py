"""사내 VOC 목 Jira 데이터 — **사내 SW 서비스**를 쓰는 동료·팀의 요청.

왜 따로 필요한가:
  `build_voc_mock.py` 가 만드는 것은 **외부 고객사**의 부품 고장 문의다. 사내 VOC 는
  입력의 성격이 다르다 — 요청자가 동료라 말이 짧고 기술 용어를 그대로 쓰며, 요청
  유형도 다르다(권한·데이터·배포·장애). 사내 프로파일을 외부 목 데이터로 재면
  분류·골격·정책이 전부 엉뚱한 것을 재게 된다. loop 는 loop 가 마주칠 입력으로 재야 한다.

스키마는 `data/all_raw_issues.json` 과 동일하고, 프로젝트 키는 `IVOC-` 로 분리한다.
Jira 미러에 섞으면 다음 삭제 대조에서 전부 지워진다(README 'KB 원천' 절 참조) —
`RVP_KB_EXTRA` 로 보조 원천으로만 붙인다.

실행:
    .venv/bin/python scripts/build_internal_voc_mock.py --count 60
    RVP_KB_EXTRA=data/voc_mock_issues.json,data/internal_voc_mock_issues.json
"""
from __future__ import annotations

import argparse
import json
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DEFAULT = ROOT / "data" / "internal_voc_mock_issues.json"


@dataclass
class Case:
    """사내 서비스 이슈 1종 + 그것이 요청으로 들어올 때의 말투."""
    tid: str
    service: str           # 사내 서비스명
    component: str
    category: str          # 요청 분류
    title: str             # 담당 엔지니어가 쓴 해결 사례 제목
    req_title: str         # 요청자가 쓴 제목 — 같은 문제, 다른 말투
    symptom: str
    req_symptom: str
    req_ask: str           # 요청자가 실제로 묻는 것 — 답변이 여기에 답해야 한다
    log: str
    root_cause: str
    resolution: str
    workaround: str
    debug: str
    entities: list[str] = field(default_factory=list)


CASES: list[Case] = [
    Case(
        tid="gw-timeout", service="api-gateway", component="Gateway", category="Outage",
        title="[api-gateway] 업스트림 커넥션 풀 고갈로 504 급증",
        req_title="[장애] 대시보드가 안 열립니다",
        symptom="피크 시간대 업스트림 커넥션 풀이 소진되며 504 Gateway Timeout 비율 12%.",
        req_symptom="오전 10시쯤부터 대시보드가 안 열립니다. 새로고침하면 가끔 되고 대부분 오류가 납니다.",
        req_ask="지금 쓸 수 있는 방법이 있나요? 복구 예상 시점을 알려주세요.",
        log="upstream_error: connect ETIMEDOUT; pool exhausted (in_use=200/200)",
        root_cause="업스트림 keep-alive 타임아웃이 게이트웨이보다 짧아 죽은 커넥션이 풀에 남았다.",
        resolution="게이트웨이 idle timeout 을 업스트림보다 짧게 조정하고 풀 크기를 200→400 으로 상향.",
        workaround="영향받은 화면은 CLI(`svc report --csv`)로 같은 데이터를 받을 수 있다.",
        debug="풀 사용량 메트릭과 업스트림 RST 타이밍을 겹쳐 보고 죽은 커넥션 잔존을 특정.",
        entities=["api-gateway", "504", "timeout", "connection pool"],
    ),
    Case(
        tid="sso-role", service="internal-portal", component="Auth", category="Access",
        title="[internal-portal] SSO 그룹 동기화 지연으로 신규 입사자 권한 미반영",
        req_title="[요청] 스테이징 접근 권한 주세요",
        symptom="IdP 그룹 변경이 최대 6시간 뒤에 반영되어 신규 계정이 403 을 받는다.",
        req_symptom="오늘 합류한 팀원이 스테이징에 못 들어갑니다. 계정은 만들어졌다고 합니다.",
        req_ask="권한을 어떻게 신청하나요? 승인은 누가 하는지도 알려주세요.",
        log="authz: subject=new.member group=[] required=stg-readers -> 403",
        root_cause="그룹 동기화가 6시간 배치라 입사 당일 반영되지 않는다.",
        resolution="그룹 변경 웹훅을 받아 즉시 반영하도록 변경, 배치는 대조용으로만 유지.",
        workaround="긴급 시 포털 '권한 즉시 동기화' 버튼으로 본인 그룹만 갱신할 수 있다.",
        debug="IdP 감사 로그와 포털 authz 로그의 시각차를 대조.",
        entities=["SSO", "IdP", "403", "role", "staging"],
    ),
    Case(
        tid="etl-late", service="data-platform", component="ETL", category="Data",
        title="[data-platform] 일일 집계 배치가 소스 지연으로 오전 리포트 누락",
        req_title="[문의] 어제 지표가 리포트에 안 보입니다",
        symptom="소스 테이블 적재가 03:20 이후로 밀리면 04:00 배치가 빈 파티션을 읽는다.",
        req_symptom="월요일 리포트에 어제 수치가 비어 있습니다. 회의에 써야 합니다.",
        req_ask="언제 채워지나요? 재적재를 요청하려면 어떻게 해야 하는지 알려주세요.",
        log="dag=daily_agg partition=2026-08-29 rows=0 upstream_ready=false",
        root_cause="배치가 소스 준비 여부를 확인하지 않고 시각만 보고 돈다.",
        resolution="업스트림 준비 센서를 붙이고, 미준비 시 재시도 후 실패로 끝내도록 변경.",
        workaround="`svc backfill --date <날짜>` 로 해당 파티션만 재적재할 수 있다(약 15분).",
        debug="적재 완료 시각과 DAG 실행 시각을 2주치 비교.",
        entities=["ETL", "batch", "partition", "backfill", "DAG"],
    ),
    Case(
        tid="ui-slow", service="internal-portal", component="Frontend", category="Performance",
        title="[internal-portal] 목록 화면 N+1 조회로 응답 8초",
        req_title="[문의] 목록 화면이 너무 느립니다",
        symptom="필터가 걸린 목록에서 행마다 상세 조회가 발생해 p95 8.2초.",
        req_symptom="목록을 열면 한참 걸립니다. 항목이 많은 팀은 더 심합니다.",
        req_ask="개선 예정인가요? 그동안 빠르게 볼 방법이 있으면 알려주세요.",
        log="GET /items?team=... 8213ms (queries=214)",
        root_cause="목록 직렬화가 행마다 상세를 다시 조회한다(N+1).",
        resolution="조인 한 번으로 묶고 목록 응답에 필요한 필드만 남김. p95 8.2s → 0.6s.",
        workaround="필터를 좁히거나 페이지 크기를 50 이하로 두면 체감이 크게 줄어든다.",
        debug="요청당 쿼리 수를 APM 에서 확인해 N+1 확정.",
        entities=["N+1", "latency", "p95", "pagination"],
    ),
    Case(
        tid="deploy-rollback", service="release-bot", component="CI/CD", category="Deploy",
        title="[release-bot] 배포 후 구버전 설정이 남아 기능 플래그 불일치",
        req_title="[장애] 배포하고 나서 기능이 사라졌습니다",
        symptom="설정 리로드가 일부 파드에서만 적용되어 플래그가 인스턴스마다 다르다.",
        req_symptom="어제 배포 뒤로 어떤 사람은 기능이 보이고 어떤 사람은 안 보입니다.",
        req_ask="롤백해야 하나요, 기다리면 되나요? 판단 기준을 알려주세요.",
        log="config_reload: applied=7/12 pods; flag=new_editor mismatch",
        root_cause="설정 리로드가 롤링 중 종료되는 파드에서 유실된다.",
        resolution="리로드를 배포 완료 훅으로 옮기고 적용 결과를 파드별로 확인하도록 변경.",
        workaround="`svc config sync --service <이름>` 으로 즉시 전체 재적용할 수 있다.",
        debug="파드별 설정 해시를 수집해 불일치 파드를 특정.",
        entities=["deploy", "rollout", "feature flag", "config"],
    ),
    Case(
        tid="api-quota", service="api-gateway", component="Quota", category="Feature",
        title="[api-gateway] 팀 단위 쿼터가 없어 한 팀의 배치가 전체 지연 유발",
        req_title="[요청] 저희 팀 API 호출 한도를 올려주세요",
        symptom="쿼터가 전역 하나뿐이라 특정 팀의 배치가 다른 팀 응답을 밀어낸다.",
        req_symptom="배치를 돌리면 한도에 걸립니다. 저희 팀 한도만 올려주실 수 있나요?",
        req_ask="한도 상향이 가능한지, 안 되면 대안이 있는지 알려주세요.",
        log="429 Too Many Requests: quota=global remaining=0",
        root_cause="쿼터 모델이 전역 단일이라 팀별 분리가 불가능하다.",
        resolution="팀 단위 쿼터를 도입하고 기본값을 팀별로 배분, 상향은 신청 절차로 처리.",
        workaround="배치를 야간 시간대로 옮기면 한도에 걸리지 않는다.",
        debug="429 발생 시각과 팀별 호출량을 대조해 특정 팀 배치와 일치함을 확인.",
        entities=["quota", "429", "rate limit", "batch"],
    ),
]

REQUESTERS = ["김담당 (플랫폼팀)", "이요청 (데이터팀)", "박운영 (SRE)",
              "최기획 (프로덕트)", "정테스트 (QA)"]
ENGINEERS = ["eng.han", "eng.yoon", "eng.jo", "eng.baek"]
SEVERITIES = ["Blocker", "Critical", "Major", "Minor"]
PRIORITY = {"Blocker": "Highest", "Critical": "High", "Major": "Medium", "Minor": "Low"}
# 사내 요청에 흔히 붙는 꼬리표 — 답변이 여기에 답하지 않으면 되묻게 된다.
ASK_SUFFIX = [
    "이번 주 안에 회신 부탁드립니다.",
    "다른 팀도 같은 증상인지 알려주세요.",
    "재발 방지까지 포함해 알려주세요.",
    "임시로 쓸 방법이 있으면 먼저 알려주세요.",
]

ANALYSIS_HEADER = "🔍 시니어 근본원인 분석"   # preprocess 파서 규약과 동일해야 한다


def _resolved_description(c: Case, requester: str, sev: str, found: str) -> str:
    return (
        f"h2. 사내 요청 접수\n\n"
        f"*요청 팀*: {requester}\n*서비스*: {c.service}\n*구성요소*: {c.component}\n"
        f"*고장 분류*: {c.category}\n*심각도*: {sev}\n*발견일*: {found}\n\n"
        f"h2. 증상 (Symptom)\n\n{c.symptom}\n\n"
        f"h2. 로그 발췌 (Log Excerpt)\n\n{{code}}\n{c.log}\n{{code}}\n")


def _request_description(c: Case, requester: str, sev: str, found: str, ask_extra: str) -> str:
    """요청자가 직접 쓴 글 — 짧고, 요구가 앞에 온다."""
    return (
        f"h2. 사내 요청 (VOC)\n\n"
        f"*요청 팀*: {requester}\n*서비스*: {c.service}\n*구성요소*: {c.component}\n"
        f"*고장 분류*: {c.category}\n*심각도*: {sev}\n*접수일*: {found}\n\n"
        f"h2. 증상 (Symptom)\n\n{c.req_symptom}\n\n"
        f"h2. 고객 요청 (Ask)\n\n{c.req_ask} {ask_extra}\n\n"
        f"h2. 로그 발췌 (Log Excerpt)\n\n{{code}}\n{c.log}\n{{code}}\n")


def _senior_comment(c: Case, eng: str) -> str:
    """라벨은 preprocess._comment_block 규약을 **정확히** 따른다 — 어긋나면 파서가
    root_cause/resolution 을 빈 문자열로 만들고, KB 는 비어 있는데 통과한 것처럼 보인다."""
    return (
        f"{ANALYSIS_HEADER} (작성: {eng})\n\n"
        f"*디버깅 접근*: {c.debug}\n\n"
        f"*근본 원인 (Root Cause)*:\n{c.root_cause}\n\n"
        f"*적용 해결책 (Resolution)*:\n{c.resolution}\n\n"
        f"*임시 우회책 (Workaround)*:\n{c.workaround}\n")


def build(count: int = 60, seed: int = 20260830) -> list[dict]:
    rng = random.Random(seed)
    base = datetime(2026, 6, 1, 9, 0, 0)
    issues: list[dict] = []
    n = 0
    while len(issues) < count:
        c = CASES[len(issues) % len(CASES)]
        n += 1
        resolved = len(issues) < int(count * 2 / 3)
        key = f"IVOC-{n}"
        requester, eng = rng.choice(REQUESTERS), rng.choice(ENGINEERS)
        sev = rng.choice(SEVERITIES)
        created = base + timedelta(days=rng.randint(0, 80), hours=rng.randint(0, 8))
        found = (created - timedelta(days=rng.randint(1, 10))).strftime("%Y-%m-%d")
        labels = [c.category, c.service, "internal-voc"]
        common = {
            "key": key, "priority": PRIORITY[sev], "components": [c.component],
            "created": created.strftime("%Y-%m-%dT%H:%M:%S.000+0900"),
        }
        if resolved:
            issues.append({**common, "summary": c.title,
                           "description": _resolved_description(c, requester, sev, found),
                           "labels": labels + ["resolved"], "status": "완료",
                           "comments": [_senior_comment(c, eng),
                                        "✅ 해결 및 검증 — 스테이징·운영에서 재현되지 않음.",
                                        "🙌 고객 검증 완료 — 요청 팀 확인 완료."]})
        else:
            issues.append({**common, "summary": c.req_title,
                           "description": _request_description(c, requester, sev, found,
                                                               rng.choice(ASK_SUFFIX)),
                           "labels": labels + ["request"], "status": "해야 할 일",
                           "comments": [f"📩 고객 추가 정보 ({requester})\n\n"
                                        f"재현은 사내망에서만 확인했습니다."]})
    return issues


def main() -> int:
    ap = argparse.ArgumentParser(description="사내 VOC 목 Jira 데이터 생성")
    ap.add_argument("--count", type=int, default=60)
    ap.add_argument("--seed", type=int, default=20260830)
    ap.add_argument("--out", type=Path, default=OUT_DEFAULT)
    a = ap.parse_args()
    issues = build(a.count, a.seed)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(issues, ensure_ascii=False, indent=2), encoding="utf-8")
    resolved = sum(1 for i in issues if i["status"] == "완료")
    print(f"✓ {len(issues)}건 생성 (해결 {resolved} / 요청 {len(issues) - resolved}) → {a.out}")
    print(f"  유형 {len(CASES)}종 · 키 IVOC-1..{len(issues)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
