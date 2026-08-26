"""Jira 웹훅 등록/목록/해제 — `POST /webhook/jira` 수신부와 짝을 이룬다.

폴링(src/jira_sync.py)이 기본 갱신 수단이다. 이 스크립트는 서버가 공개 URL로
노출된 뒤 즉시 반영(초 단위)이 필요할 때 쓴다. 둘 다 켜도 무해하다 — 같은
이슈를 중복 upsert할 뿐이다.

Jira Cloud는 `/rest/api/3/webhook`(동적 웹훅)을 Connect/OAuth2 앱에만 허용하므로,
API 토큰으로 쓸 수 있는 레거시 `/rest/webhooks/1.0/webhook`을 사용한다.

전제: 대상 URL이 인터넷에서 Jira Cloud로 도달 가능해야 한다(로컬 localhost 불가).

사용:
    set -a && source .env && set +a
    .venv/bin/python scripts/jira_webhook_register.py list
    .venv/bin/python scripts/jira_webhook_register.py register https://<공개호스트>
    .venv/bin/python scripts/jira_webhook_register.py delete <webhook-id>

JIRA_WEBHOOK_SECRET 을 .env 에 설정하면 URL 쿼리에 붙여 등록하고, 서버가 대조한다.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlencode

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from ingest import jira_session  # noqa: E402

WEBHOOK_API = "/rest/webhooks/1.0/webhook"
# 신규 등록에 쓸 이름. 프로젝트 리네임(voc-agent) 이전에 등록된 웹훅은 옛 이름으로
# Jira 에 남아 있으므로, 조회·해제는 두 이름을 모두 본다 — 새 이름만 보면 기존
# 등록분을 찾지 못해 "없다"고 하고, 중복 등록으로 이어진다.
NAME = "voc-agent KB sync"
LEGACY_NAMES = ("lsi-error-analyzer KB sync",)
KNOWN_NAMES = (NAME, *LEGACY_NAMES)
EVENTS = ["jira:issue_created", "jira:issue_updated", "jira:issue_deleted",
          "comment_created", "comment_updated", "comment_deleted"]


def _list(s, base):
    r = s.get(base + WEBHOOK_API, timeout=30)
    r.raise_for_status()
    return r.json()


def cmd_list() -> int:
    s, base = jira_session()
    hooks = _list(s, base)
    if not hooks:
        print("등록된 웹훅 없음")
        return 0
    for h in hooks:
        hid = (h.get("self") or "").rsplit("/", 1)[-1]
        print(f"[{hid}] {h.get('name')}  enabled={h.get('enabled')}\n"
              f"      url={h.get('url')}\n      events={h.get('events')}\n"
              f"      jqlFilter={(h.get('filters') or {}).get('issue-related-events-section')}")
    return 0


def cmd_register(public_base: str) -> int:
    s, base = jira_session()
    project = os.getenv("JIRA_PROJECT_KEY", "LSI")
    secret = os.getenv("JIRA_WEBHOOK_SECRET", "")
    url = public_base.rstrip("/") + "/webhook/jira"
    if secret:
        url += "?" + urlencode({"secret": secret})
    elif not _confirm("JIRA_WEBHOOK_SECRET 미설정 — 인증 없는 공개 엔드포인트가 됩니다. 계속?"):
        return 1
    if not url.startswith("https://"):
        print(f"[중단] Jira Cloud는 https 웹훅만 호출합니다: {url}")
        return 2

    for h in _list(s, base):
        if h.get("name") in KNOWN_NAMES:
            hid = (h.get("self") or "").rsplit("/", 1)[-1]
            print(f"[중단] 같은 이름의 웹훅이 이미 있습니다(id={hid}). "
                  f"먼저 delete 하거나 이름을 바꾸세요.")
            return 2

    body = {"name": NAME, "url": url, "events": EVENTS,
            "filters": {"issue-related-events-section": f"project = {project}"},
            "excludeBody": False}
    print(f"등록할 내용:\n{json.dumps({**body, 'url': _mask(url)}, ensure_ascii=False, indent=2)}")
    if not _confirm(f"{base} 에 위 웹훅을 생성합니다. 계속?"):
        return 1
    r = s.post(base + WEBHOOK_API, json=body,
               headers={"Content-Type": "application/json"}, timeout=30)
    if not r.ok:
        print(f"[실패] {r.status_code} {r.text[:300]}")
        return 1
    print(f"[완료] 생성됨: {r.json().get('self')}")
    return 0


def cmd_delete(hook_id: str) -> int:
    s, base = jira_session()
    if not _confirm(f"웹훅 {hook_id} 을(를) 삭제합니다. 계속?"):
        return 1
    r = s.delete(f"{base}{WEBHOOK_API}/{hook_id}", timeout=30)
    if not r.ok:
        print(f"[실패] {r.status_code} {r.text[:300]}")
        return 1
    print("[완료] 삭제됨")
    return 0


def _mask(url: str) -> str:
    return url.split("?")[0] + ("?secret=***" if "secret=" in url else "")


def _confirm(msg: str) -> bool:
    if os.getenv("JIRA_WEBHOOK_YES") == "1":
        return True
    return input(f"{msg} [y/N] ").strip().lower() in ("y", "yes")


def main() -> int:
    ap = argparse.ArgumentParser(description="Jira 웹훅 등록/목록/해제")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    reg = sub.add_parser("register")
    reg.add_argument("public_base", help="공개 베이스 URL (예: https://lsi.example.com)")
    dele = sub.add_parser("delete")
    dele.add_argument("hook_id")
    args = ap.parse_args()

    if not os.getenv("JIRA_BASE_URL"):
        print("[중단] .env 를 source 하세요: set -a && source .env && set +a")
        return 2
    try:
        if args.cmd == "list":
            return cmd_list()
        if args.cmd == "register":
            return cmd_register(args.public_base)
        return cmd_delete(args.hook_id)
    except requests.HTTPError as e:
        print(f"[실패] {e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
