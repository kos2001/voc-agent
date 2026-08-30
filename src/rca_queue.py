"""RCA 댓글 HITL 승인 큐 — 초안을 보관하고, 사람 승인 시에만 Jira에 게시한다.

상태 전이·영속화는 hitl_queue.Queue 가 담당한다(고객 답변 큐와 공유). 여기서는
이 큐의 정체성(저장 위치)만 정한다.
"""
from __future__ import annotations

from pathlib import Path

from hitl_queue import Queue

ROOT = Path(__file__).resolve().parent.parent
QUEUE_FILE = ROOT / "tmp_db" / "rca_pending.json"

_Q = Queue(QUEUE_FILE)

upsert = _Q.upsert
supersede = _Q.supersede
get = _Q.get
set_state = _Q.set_state
items = _Q.items
counts = _Q.counts
