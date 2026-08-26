"""이슈 키 패턴 — "무엇이 이슈 키인가"의 **단일 소스**.

왜 필요한가:
  `LSI-\\d+` 가 서버·first_principles·knowledge_store 등 13곳에 하드코딩돼 있었다.
  프로젝트 키가 LSI 하나뿐일 때는 안 보이던 결함인데, KB 에 VOC 원천이 들어오자마자
  **살아 있는 버그**가 됐다 — VOC 초안이 `VOC-12` 를 인용해도

    · `/rca/approve`      인용 0건으로 기록 (지식 자산에 근거가 안 남음)
    · `/rca/validate`     잘못된 인용을 검출하지 못함 (환각 가드가 뚫림)
    · `_unsupported_mentions`  근거 밖 사례 언급을 못 잡음

  즉 프로젝트를 하나 더 붙이는 것만으로 인용 검증 전체가 조용히 무력화된다.
  키 판정은 한 곳에만 있어야 한다.

패턴:
  Jira 키는 `PROJ-123` 형태다(대문자로 시작, 대문자·숫자, 하이픈, 숫자).
  이 저장소는 여기에 `-rca` 같은 **접미사**를 붙인 파생 키를 쓴다(큐레이션 지식).
  그래서 '줄기(stem)'와 '접미사 포함 전체'를 구분해서 다룬다.

도메인 오탐 주의:
  본문에는 `PM9C3-NVMe`, `HS-G4`, `ISO-DEP`, `NFC-A` 같은 토큰이 흔하다. 하이픈 뒤가
  **숫자로만** 이뤄져야 매칭되므로 이들은 걸리지 않는다. 반대로 `DDR5-1` 같은 표기가
  생기면 오탐이 가능하다 — 그래서 호출부는 대부분 추출 후 **KB 키 집합과 교집합**을
  취한다. 이 모듈은 후보를 넓게 뽑고, 확정은 호출부가 한다.
"""
from __future__ import annotations

import os
import re

# 줄기: PROJ-123. 접미사 없음.
STEM = r"[A-Z][A-Z0-9]*-\d+"
# 전체: 줄기 + 선택적 접미사(-rca 등)
FULL = rf"{STEM}(?:-\w+)?"

STEM_RE = re.compile(STEM)
FULL_RE = re.compile(FULL)
_STEM_FULLMATCH = re.compile(rf"{STEM}$")
_FULL_FULLMATCH = re.compile(rf"{FULL}$")


def find(text: str) -> list[str]:
    """본문에서 이슈 키 줄기를 등장 순서대로(중복 포함) 추출."""
    return STEM_RE.findall(text or "")


def find_set(text: str) -> set[str]:
    return set(find(text))


def find_full(text: str) -> list[str]:
    """접미사까지 포함해 추출 — 파생 키(LSI-7-rca)를 통째로 다뤄야 할 때."""
    return FULL_RE.findall(text or "")


def stem(key: str) -> str:
    """파생 키에서 원본 이슈 키를 얻는다. LSI-7-rca → LSI-7 (아니면 그대로)."""
    m = STEM_RE.match(str(key or ""))
    return m.group(0) if m else str(key or "")


def is_key(value) -> bool:
    """접미사를 포함한 유효한 이슈 키 형태인가 (본문 치환 전 안전장치)."""
    return bool(_FULL_FULLMATCH.fullmatch(str(value or "")))


def is_stem(value) -> bool:
    return bool(_STEM_FULLMATCH.fullmatch(str(value or "")))


def expand(keys) -> set[str]:
    """키 집합 + 각 키의 줄기 — 인용 허용 집합을 만들 때 쓴다.

    근거 키가 `LSI-7-rca` 인데 본문은 자연스럽게 `LSI-7` 로 쓴다. 줄기를 같이 허용하지
    않으면 정상 인용이 환각으로 잡힌다.
    """
    out: set[str] = set()
    for k in keys:
        k = str(k or "")
        if not k:
            continue
        out.add(k)
        out.add(stem(k))
    return out


def placeholder() -> str:
    """근거가 하나도 없을 때 쓸 예시 키. 실제 키처럼 보이면 모델이 베끼므로 -000 을 쓴다.

    프로젝트 키는 환경(JIRA_PROJECT_KEY)을 따른다 — 여기서도 하드코딩하면 다른
    프로젝트에서 남의 키를 예시로 보여주게 된다.
    """
    proj = (os.getenv("JIRA_PROJECT_KEY", "") or "ISSUE").strip().upper()
    if not is_stem(f"{proj}-0"):
        proj = "ISSUE"
    return f"{proj}-000"
