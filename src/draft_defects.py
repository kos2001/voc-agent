"""초안 결함 **사전 탐지** — 사람이 읽기 전에 잡을 수 있는 것은 잡는다.

배경(왜 만들었나):
  `draft_feedback` 는 사람이 초안을 읽고 고친 **뒤에야** 원인을 안다. 그 구조에는
  구멍이 하나 있다 — 결함이 이미 사람 앞까지 갔다는 것이다. 검토 한 번이 통째로
  낭비되고, 같은 결함은 다음 초안에도 그대로 나간다. 사후 집계가 프롬프트 규칙으로
  환류되기까지는 최소 GUIDANCE_MIN_COUNT 회의 반복이 필요하니, 그동안 나가는
  초안들은 이미 아는 실수를 반복한다.

  그런데 분류 체계(`draft_feedback.CAUSES`)를 만들고 보니, 그중 상당수는 **초안과
  근거 목록만 있으면 즉시 확인 가능**하다. 근거 목록에 없는 키를 인용했는지는
  대조하면 끝이고, 한자·섹션 누락·번호 절차 부재도 마찬가지다. 사람의 판단이
  필요한 것은 "근본원인이 맞느냐" 같은 내용 판정뿐이다.

이 모듈의 계약:
  1) 탐지 코드는 `draft_feedback.CAUSES` 것만 쓴다. 분류 체계가 둘이 되면 사전
     탐지와 사후 집계가 서로 다른 말을 하고, loop 는 어느 쪽을 믿을지 알 수 없다.
  2) **오탐 0 을 정탐보다 우선한다.** 이 신호는 사람의 검토 화면에 뜨고 일부는
     게이트로 쓰인다. 멀쩡한 초안을 잡기 시작하면 사람이 곧 무시하고, 그러면
     탐지기는 없는 것과 같다. 그래서 규칙을 두 등급으로 나눈다:
       certain — 대조할 정답이 있다(근거 키 목록, 섹션 규격, 한자 유무). 게이트 가능.
       likely  — 문면에서 추정한다(단정 표현, 일반론). 경고만. 절대 막지 않는다.
  3) 모르는 것은 판정하지 않는다. 근거 키 목록이 없으면 인용 정합을 아예 보지 않는다 —
     "모르니까 결함" 은 오탐을 만드는 가장 흔한 길이다.

`quality_gate` 와 무엇이 다른가: 저건 **KB 사례**(입력 지식)의 충실도를 본다.
이건 **생성된 초안**(출력)을 본다. 대상도 규칙도 겹치지 않는다.
"""
from __future__ import annotations

import re

import issue_keys
from draft_feedback import CAUSES, lever_of

CERTAINTY = ("certain", "likely")

# 초안 **종류마다 약속이 다르다.** 이걸 하나로 묶으면 곧바로 오탐 홍수가 난다 —
# 실측으로 확인했다: LLM 분석 기준(불확실성 섹션·번호 절차)을 제안 기반 초안에
# 들이대니 멀쩡한 초안 전부가 결함으로 잡혔다.
#
#   proposal — 과거 사례에서 뽑은 **짧은 요약**(backend `_rca_comment_body`).
#              근본원인·해결책·우회책 세 줄이 전부고, 애초에 불확실성 섹션도
#              번호 절차도 약속한 적이 없다. 없는 약속을 어겼다고 할 수 없다.
#   analysis — LLM 심층 분석(backend `_rca_prompt`). 섹션 구성과 "번호가 있는
#              구체적 순서" 를 프롬프트가 **명시적으로 요구**하므로 대조가 성립한다.
#
# 제목 텍스트로만 매칭한다 — 이모지는 모델이 곧잘 흘린다.
DRAFT_KINDS = {
    "proposal": {"sections": ("근본원인", "해결"), "numbered_steps": False},
    "analysis": {"sections": ("근본원인", "권장 해결", "불확실성"), "numbered_steps": True},
}
DEFAULT_KIND = "analysis"

_HANJA_RE = re.compile(r"[一-鿿]")
_HEDGE_RE = re.compile(r"\((?:배경|추정)\)")
_NUMBERED_RE = re.compile(r"^\s*\d+[.)]\s", re.M)

# 근거 없이 쓰면 안 되는 **단정 표현**. 좁게 잡는다 — 넓히면 곧바로 오탐이 된다.
# 전부 "확답·보장·전칭" 뜻이라, 과거 사례 인용으로도 뒷받침하기 어려운 말들이다.
_ABSOLUTE_RE = re.compile(
    r"(완전히\s*해결|100%|절대\s*(?:발생|재발)하지\s*않|반드시\s*해결|"
    r"확실히\s*해결|문제\s*없습니다|보장합니다|모든\s*경우에)")

# 마크다운(`### 제목`)과 Jira 위키(`h3. 제목`) 둘 다 받는다. 정본은 마크다운이지만
# 큐에는 Jira 형식으로 저장된 옛 초안이 남아 있다. 한쪽만 읽으면 **파싱 실패를
# 결함으로 착각한다** — 실측에서 Jira 형식 초안이 "필수 섹션 없음" 으로 잡혔다.
# 못 읽는 것과 없는 것은 다르다.
_SECTION_SPLIT_RE = re.compile(r"^(?:#{2,4}\s+|h[1-6]\.\s*)", re.M)


def _sections(body: str) -> dict[str, str]:
    """제목 → 본문. 제목 줄의 이모지·기호는 무시하고 텍스트만 남긴다."""
    out: dict[str, str] = {}
    parts = _SECTION_SPLIT_RE.split(body or "")
    for p in parts[1:]:
        head, _, rest = p.partition("\n")
        out[head.strip()] = rest.strip()
    return out


def _find(sections: dict[str, str], needle: str) -> str | None:
    for head, text in sections.items():
        if needle in head:
            return text
    return None


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?。])\s+|\n+", text or "") if s.strip()]


def _finding(cause: str, certainty: str, detail: str) -> dict:
    return {"cause": cause, "label": CAUSES.get(cause, CAUSES["other"])["label"],
            "lever": lever_of(cause), "certainty": certainty, "detail": detail}


def detect(body: str, *, evidence_keys=None, query: dict | None = None,
           kind: str = DEFAULT_KIND) -> list[dict]:
    """초안에서 **확인 가능한** 결함을 찾는다. 내용의 옳고 그름은 판정하지 않는다.

    evidence_keys: 이 초안에 제공된 근거 사례 키. 비우면 인용 정합을 보지 않는다.
    query:         미해결 이슈 레코드. 있으면 요지 이탈(wrong_scope)까지 본다.
    kind:          초안 종류(DRAFT_KINDS). 종류마다 **약속한 형식이 다르므로**
                   섹션·절차 규칙이 달라진다. 모르는 값이면 기본(엄격)으로 본다.
    """
    contract = DRAFT_KINDS.get(kind) or DRAFT_KINDS[DEFAULT_KIND]
    body = body or ""
    if not body.strip():
        return [_finding("other", "certain", "본문이 비어 있습니다.")]

    out: list[dict] = []
    secs = _sections(body)
    cited = issue_keys.find_set(body)

    # ---- certain: 대조할 정답이 있는 규칙 --------------------------------- #
    # ① 근거 목록 밖의 키 인용. 목록을 모르면(빈 값) 판정 자체를 하지 않는다.
    if evidence_keys:
        allowed = issue_keys.expand({str(k) for k in evidence_keys})
        stray = sorted(k for k in cited if k not in allowed)
        if stray:
            out.append(_finding("bad_citation", "certain",
                                f"근거 목록에 없는 키를 인용했습니다: {', '.join(stray)}"))

    # ② 한자·CJK — 프롬프트가 명시적으로 금지한 것이라 대조가 아니라 규격 위반이다.
    hanja = sorted(set(_HANJA_RE.findall(body)))
    if hanja:
        out.append(_finding("tone_format", "certain",
                            f"한자가 섞여 있습니다: {''.join(hanja[:8])}"))

    # ③ 필수 섹션 누락 — 사람이 어디를 봐야 하는지 모르게 된다.
    missing = [s for s in contract["sections"] if _find(secs, s) is None]
    if missing:
        out.append(_finding("tone_format", "certain",
                            f"필수 섹션이 없습니다: {', '.join(missing)}"))

    # ---- likely: 문면에서 추정하는 규칙. 경고만 한다 ---------------------- #
    # ④ 해결 단계에 번호 절차가 없다 = 실행할 수 없는 조치.
    #    섹션이 아예 없으면 ③ 이 이미 말했으므로 중복해서 말하지 않는다.
    res = _find(secs, "해결")
    if contract["numbered_steps"] and res is not None and not _NUMBERED_RE.search(res):
        out.append(_finding("missing_action", "likely",
                            "'권장 해결 단계' 에 번호가 붙은 실행 절차가 없습니다."))

    # ⑤ 인용도 표시도 없는 단정. **문장 단위로** 본다 — 문서 전체에 인용이 있어도
    #    그 문장이 뒷받침된다는 뜻은 아니다.
    for s in _sentences(body):
        if not _ABSOLUTE_RE.search(s):
            continue
        if _HEDGE_RE.search(s) or issue_keys.find_set(s):
            continue          # (배경)/(추정) 표시가 있거나 인용이 붙었으면 통과
        out.append(_finding("unsupported_claim", "likely",
                            f"근거도 (추정) 표시도 없는 단정: “{s[:60]}”"))
        break                 # 한 건이면 충분하다 — 같은 지적을 반복해 화면을 덮지 않는다

    # ⑥ 인용이 하나도 없다 = 근거에 매인 글이 아니다(일반론).
    if not cited:
        out.append(_finding("too_shallow", "likely",
                            "본문에 근거 사례 인용이 한 건도 없습니다 — 일반론일 가능성이 높습니다."))

    # ⑦ 요지 이탈 — 질의의 고유 용어가 초안 어디에도 없을 때만. 질의를 모르면 보지 않는다.
    if query:
        terms = _distinctive_terms(query)
        if terms and not any(t in body for t in terms):
            out.append(_finding("wrong_scope", "likely",
                                f"질의의 핵심 용어가 초안에 없습니다: {', '.join(sorted(terms)[:5])}"))
    return out


# 질의 고유 용어 추출: 너무 짧거나 흔한 말은 제외한다. 한 글자 겹침으로 "답했다"고
# 판정하면 이 규칙은 아무것도 못 잡는다.
_STOP = {"발생", "문제", "현상", "확인", "요청", "관련", "이슈", "동작", "상태", "경우"}


def _distinctive_terms(query: dict) -> set[str]:
    text = f"{query.get('summary', '')} {query.get('symptom', '')}"
    toks = re.findall(r"[A-Za-z][A-Za-z0-9_]{2,}|[가-힣]{2,}", text)
    return {t for t in toks if len(t) >= 3 and t not in _STOP}


def blocking(findings: list[dict]) -> list[dict]:
    """게이트가 실제로 막아야 할 것 — certain 만. 추정으로는 절대 막지 않는다.

    막는 근거가 재현되지 않으면(추정 규칙이 그렇다) 사람은 왜 막혔는지 설명할 수
    없고, 그런 게이트는 곧 꺼진다.
    """
    return [f for f in (findings or []) if f.get("certainty") == "certain"]


def summary(findings: list[dict]) -> dict:
    """검토 화면·API 용 요약. 원인 코드는 그대로 두어 사후 집계와 맞물리게 한다."""
    fs = findings or []
    return {
        "count": len(fs),
        "blocking": len(blocking(fs)),
        "causes": sorted({f["cause"] for f in fs}),
        "levers": sorted({f["lever"] for f in fs}),
        "findings": fs,
    }
