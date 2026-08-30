"""HITL 승인 큐 — 초안을 보관하고 **사람이 승인할 때만** 외부로 나간다.

부작용(Jira 게시·고객 발송)은 approve 시점에만 발생한다. 상태: pending → approved | rejected.

큐가 둘인 이유(그리고 파일이 둘인 이유):
  RCA 초안은 **엔지니어가 읽을 분석**이고 고객 답변은 **고객이 받을 글**이다. 같은
  이슈 키에 둘 다 존재할 수 있으므로 한 파일에 담으면 서로를 덮어쓴다. 승인 권한도
  다르다(rca.approve vs reply.send). 저장·전이 로직만 공유한다.
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path

from json_store import read_json, write_json_atomic


class Queue:
    """key 로 식별되는 초안 큐 하나. 파일 경로만 다르고 동작은 같다."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _load(self) -> dict:
        d = read_json(self.path, {})
        return d if isinstance(d, dict) else {}

    def _save(self, data: dict) -> None:
        write_json_atomic(self.path, data)

    def upsert(self, item: dict) -> dict:
        """초안 추가/갱신 (key 기준). 이미 approved 면 덮어쓰지 않는다 —
        나간 것을 조용히 바꾸면 무엇이 나갔는지 알 수 없게 된다."""
        data = self._load()
        prev = data.get(item["key"])
        if prev and prev.get("state") == "approved":
            return prev
        item.setdefault("state", "pending")
        data[item["key"]] = item
        self._save(data)
        return item

    def supersede(self, item: dict) -> dict:
        """이미 나간 항목을 새 초안으로 **대체**한다 (후속 문의용).

        `upsert` 의 '승인된 것은 못 덮는다' 규칙을 의도적으로 넘는 유일한 통로다.
        호출부가 이전 판본을 `history` 에 보존할 책임을 진다 — 무엇이 나갔는지가
        사라지면 발송 이력이 거짓이 된다.
        """
        data = self._load()
        item.setdefault("state", "pending")
        data[item["key"]] = item
        self._save(data)
        return item

    def get(self, key: str) -> dict | None:
        return self._load().get(key)

    def set_state(self, key: str, state: str, **extra) -> dict | None:
        data = self._load()
        if key not in data:
            return None
        data[key].update(state=state, **extra)
        self._save(data)
        return data[key]

    def items(self, state: str | None = None) -> list[dict]:
        out = list(self._load().values())
        if state:
            out = [x for x in out if x.get("state") == state]
        return sorted(out, key=lambda x: x.get("created_at", ""))

    def counts(self) -> dict:
        c = Counter(x.get("state", "pending") for x in self._load().values())
        return {"pending": c.get("pending", 0), "approved": c.get("approved", 0),
                "rejected": c.get("rejected", 0)}
