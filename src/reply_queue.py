"""고객 답변 HITL 발송 큐 — 승인 전에는 **고객에게 아무것도 나가지 않는다**.

RCA 큐와 분리한 이유는 hitl_queue 의 설명 참조(같은 이슈에 둘 다 존재 가능,
승인 권한이 다름).
"""
from __future__ import annotations

from pathlib import Path

from hitl_queue import Queue

ROOT = Path(__file__).resolve().parent.parent
QUEUE_FILE = ROOT / "tmp_db" / "reply_pending.json"

_Q = Queue(QUEUE_FILE)

upsert = _Q.upsert
supersede = _Q.supersede
get = _Q.get
set_state = _Q.set_state
items = _Q.items
counts = _Q.counts
