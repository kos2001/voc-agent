"""고객 대응 VOC 에이전트 — 고객이 **읽을 답변**을 만든다.

왜 별도 계층인가:
  기존 산출물(RCA 초안)은 **엔지니어가 읽는 분석**이다 — 근본원인·검증 절차·사례 키
  인라인 인용. 그대로 고객에게 나가면 세 가지가 동시에 깨진다.

    · 내부 이슈 키(LSI-42)·내부 용어가 고객에게 노출된다
    · 고객이 실제로 물은 것("언제 고쳐지나요")에 답하지 않는다 — 요청 유형이 다르면
      답의 골격 자체가 달라야 한다
    · 확정 일정·환불 같은 **약속**이 검토 없이 외부로 나갈 수 있다

  그래서 "RCA 를 고객 말투로 다듬는" 후처리가 아니라, 요청 유형에서 시작하는
  별도 파이프라인으로 둔다.

에이전트를 셋으로 나눈다. 하나의 프롬프트로 뭉치면 실패 원인을 분리할 수 없다 —
답이 이상할 때 유형을 잘못 잡은 건지, 근거가 없던 건지, 문체가 문제인지 알 수 없다.

  1) 의도(intent)  — 고객 요청 유형 + 요청 문장 추출. 규칙 기반이라 LLM 없이 돈다.
  2) 답변(reply)   — 유형별 골격에 근거를 얹어 고객 언어로. LLM 이 없으면 결정적 템플릿.
  3) 정책(policy)  — 발송 전 검사. **차단은 여기서만** 한다(한 곳에서만 막아야 샌 곳을 안다).

정책 위반의 심각도는 둘이다:
  · block — 사람이 고치기 전에는 발송 불가 (내부 키 노출·한자·확정 약속·반말)
  · warn  — 발송은 가능하되 검토 표시 (다음 단계 누락·내부 용어 흔적·과도한 길이)
"""
from __future__ import annotations

import re

import issue_keys

# ── 1) 의도 분류 ────────────────────────────────────────────────────────────
# 유형마다 '답의 골격'이 다르다. sections 는 답변이 반드시 담아야 하는 항목이고,
# ReplyAgent 프롬프트와 결정적 템플릿이 같은 목록을 쓴다 — 두 경로가 갈라지면
# LLM 유무에 따라 고객이 받는 답의 구조가 달라진다.
INTENTS: dict[str, dict] = {
    "defect_report": {
        "label": "고장·오작동 신고",
        "goal": "무엇이 확인됐고, 지금 무엇을 하면 되고, 앞으로 어떻게 진행되는지",
        "sections": ("확인한 내용", "현재 파악된 원인", "지금 적용해 보실 수 있는 조치",
                     "앞으로의 진행", "추가로 알려주시면 도움이 되는 정보"),
        "keywords": ("안 됩니다", "안돼", "안 돼", "안됨", "먹통", "멈춰", "멈춥니다", "끊깁니다",
                     "끊김", "재부팅", "오류", "에러", "고장", "불량", "인식이 안", "느려",
                     "발열", "튕깁니다", "죽습니다", "작동하지", "꺼집니다", "꺼져", "꺼짐",
                     "안 켜", "안켜", "이상합니다", "재현"),
    },
    "status_inquiry": {
        "label": "진행 상황·일정 문의",
        "goal": "현재 어디까지 왔고 다음에 무엇이 언제 있는지 (확정 약속 없이)",
        "sections": ("문의 주신 사항", "현재 진행 상황", "다음 안내 시점",
                     "그동안 도움이 되는 방법"),
        "keywords": ("언제", "며칠", "얼마나 걸", "진행 상황", "진행상황", "어떻게 되고",
                     "아직인가", "업데이트 예정", "일정", "기다리", "답변이 없"),
    },
    "workaround_request": {
        "label": "당장 쓸 방법 요청",
        "goal": "지금 바로 적용 가능한 우회책과 그 한계",
        "sections": ("문의 주신 사항", "지금 바로 적용 가능한 방법", "적용 시 유의사항",
                     "근본 조치 진행"),
        "keywords": ("당장", "임시로", "우회", "급합니다", "급한데", "지금 쓸", "방법 없", "대안"),
    },
    "howto": {
        "label": "사용법·설정 문의",
        "goal": "요청한 동작을 수행하는 절차",
        "sections": ("문의 주신 사항", "설정 방법", "정상 동작 확인 방법", "추가 문의"),
        "keywords": ("어떻게 하", "방법을 알려", "설정", "사용법", "쓰는 법", "하려면",
                     "가능한가요", "지원하나요", "쓸 수 있나요"),
    },
    "spec_inquiry": {
        "label": "규격·호환성 문의",
        "goal": "규격/호환 여부와 근거, 확인이 필요한 조건",
        "sections": ("문의 주신 사항", "규격·호환성 안내", "확인이 필요한 조건", "추가 문의"),
        "keywords": ("스펙", "규격", "호환", "지원 범위", "버전", "표준", "인증", "데이터시트"),
    },
    "rma_request": {
        "label": "교환·환불·보상 요청",
        "goal": "처리 절차와 필요한 정보 안내 (승인 권한은 담당 부서)",
        "sections": ("문의 주신 사항", "확인이 필요한 사항", "처리 절차 안내", "다음 단계"),
        "keywords": ("환불", "교환", "반품", "보상", "배상", "새 제품", "교체해",
                     "돈을 돌려", "무상"),
    },
    "complaint": {
        "label": "불만·항의",
        "goal": "사실 확인과 조치. 사과는 하되 책임 범위를 임의로 확정하지 않는다",
        "sections": ("불편을 드린 점에 대해", "확인한 사실", "취한 조치", "앞으로의 진행"),
        "keywords": ("불만", "화가", "실망", "몇 번째", "계속 이런", "책임", "항의",
                     "이해할 수 없", "너무합니다"),
    },
    "other": {
        "label": "기타 문의",
        "goal": "요청 사항에 대한 직접적인 답",
        "sections": ("문의 주신 사항", "안내 드립니다", "다음 단계"),
        "keywords": (),
    },
}

# 유형 판정 우선순위. 한 문의에 여러 신호가 섞이면(대개 섞인다) **고객이 가장 원하는
# 것**을 먼저 잡는다: 돈·감정이 걸린 요청 > 시급한 우회책 > 일정 > 고장 신고 > 단순 문의.
# 고장 신고가 뒤에 있는 이유 — "안 됩니다. 언제 고쳐지나요?" 에서 고객이 기다리는 답은
# 증상 접수가 아니라 일정이다.
_PRIORITY = ("rma_request", "complaint", "workaround_request", "status_inquiry",
             "defect_report", "spec_inquiry", "howto", "other")

# 요청 신호: 물음표, 또는 요청·질의형 어미. 문장 단위로 판정한다 — 한 덩어리로 정규식을
# 돌리면 "왜 그런가요? 방법을 알려주세요." 가 한 문장으로 붙어 요청 2건이 1건이 된다.
_ASK_RE = re.compile(
    r"\?|주세요|주십시오|주시겠|주시기\s*바랍|부탁\s*드립니다|바랍니다|"
    r"가능한가요|되나요|하나요|인가요|건가요|줄까요|해\s*주실|"
    r"필요합니다|궁금합니다|알고\s*싶습니다|확답")
_SENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")


def extract_asks(text: str, limit: int = 5) -> list[str]:
    """고객이 실제로 '요청한 문장'만 뽑는다.

    답변이 요지를 빗나가는 가장 흔한 원인은 증상 서술에 눌려 질문을 놓치는 것이다.
    뽑아낸 문장을 프롬프트에 따로 얹어 각 항목에 답하게 만든다.
    """
    out: list[str] = []
    for raw in _SENT_SPLIT_RE.split(text or ""):
        s = raw.strip()
        if len(s) >= 5 and _ASK_RE.search(s) and s not in out:
            out.append(s[:200])
        if len(out) >= limit:
            break
    return out


# 요청 반영 여부 판정용 — 조사 제거 + 아무 답변에나 나오는 흔한 말 제외.
_PARTICLE_RE = re.compile(r"(은|는|이|가|을|를|의|에|에서|으로|로|와|과|도|만|까지|부터|인지|인가)$")
# 아무 답변에나 나오는 말과 **내용이 없는 기능어**는 뺀다. 남겨두면 "되는지/아니면"
# 같은 토큰이 핵심어 자리를 차지해, 정작 내용어("롤백")가 6개 안에 못 든다.
_ASK_STOP = {"알려주세요", "알려", "주세요", "부탁드립니다", "답변", "확인", "필요합니다",
             "있습니다", "합니다", "판단", "문제", "여부", "관련", "대해", "위해", "정도",
             "경우", "되는지", "하는지", "인지", "아니면", "포함해", "그리고", "때문에",
             "때문", "무엇", "어떻게", "현재", "혹시", "다시", "지금", "쪽에서", "명확히",
             "건지", "인데", "동일", "먼저", "이번", "저희",
             "please", "advise", "could", "would", "about", "issue", "problem"}


# 어미까지 붙은 채로 비교하면 제대로 답한 문장이 '미응답' 으로 잡힌다 — 실측에서
# 요청의 `롤백하면` 이 본문의 `롤백` 과 안 맞아 오탐이 났다. 형태소 분석기를 들이지
# 않고(정책 검사는 무거운 의존성 없이 결정적으로 돌아야 한다) 흔한 어미만 벗긴다.
_ENDING_RE = re.compile(r"(하면|해야|하는지|되는지|입니다|합니다|하고|해서|한다|된다|였다|"
                        r"이라도|라도|인지|한지|까지|부터|에서|으로|이고|이며|된|한|할|될|인)$")


def _stem(tok: str) -> str:
    """어미 → 조사 순으로 두 번까지 벗긴다. 2자 미만으로 줄면 되돌린다.

    조사를 먼저 떼면 어미가 깨진다 — `임시로라도` 에서 `도` 를 먼저 떼면 `라도` 가
    남아 `임시로라` 라는 없는 말이 된다.
    """
    t = tok
    for _ in range(2):
        cut = _PARTICLE_RE.sub("", _ENDING_RE.sub("", t))
        if len(cut) >= 2 and cut != t:
            t = cut
        else:
            break
    return t


def ask_terms(ask: str, n: int = 6) -> list[str]:
    """요청 문장에서 '답변에 나타나야 할' 핵심어. 긴 것부터 n개."""
    toks = [_stem(t) for t in re.split(r"[\s,.·]+", ask or "")]
    toks = [t for t in toks if len(t) >= 2 and t.lower() not in _ASK_STOP and not t.isdigit()]
    seen, out = set(), []
    for t in sorted(toks, key=len, reverse=True):
        if t.lower() in seen:
            continue
        seen.add(t.lower())
        out.append(t)
        if len(out) >= n:
            break
    return out


def unanswered_asks(asks, body: str, *, min_hits: int = 1) -> list[str]:
    """답변이 건드리지 않은 것으로 **보이는** 요청.

    어휘 일치라 하한이다 — 뜻은 맞는데 말이 다르면 놓친다. 그래서 차단이 아니라
    경고다. 그래도 필요하다: 답변이 요지를 빗나가는 것이 초안 거부 사유 1순위인데,
    형식·안전 검사만으로는 그 실패가 전혀 보이지 않는다.

    기본 임계는 **핵심어가 하나도 안 나올 때**다. 더 조이면("2개 이상 겹쳐야 답한 것")
    말만 바꿔 제대로 답한 문장이 줄줄이 걸리고, 소음이 된 경고는 아무도 안 본다.
    품질 측정(하네스)은 더 엄한 임계를 쓴다 — 목적이 다르다.
    """
    out = []
    low = (body or "").lower()
    for a in asks or []:
        terms = ask_terms(a)
        if not terms:
            continue
        if sum(1 for t in terms if t.lower() in low) < min_hits:
            out.append(a)
    return out


def classify(text: str) -> dict:
    """요청 유형 분류. 규칙 기반 — LLM 없이 항상 답이 나와야 하는 자리다.

    반환: {intent, label, goal, sections, matched, scores, asks}
    """
    t = (text or "")
    scores = {name: sum(1 for kw in spec["keywords"] if kw in t)
              for name, spec in INTENTS.items()}
    best = max(_PRIORITY, key=lambda n: (scores.get(n, 0) > 0, -_PRIORITY.index(n)))
    if scores.get(best, 0) == 0:
        best = "other"
    spec = INTENTS[best]
    return {
        "intent": best, "label": spec["label"], "goal": spec["goal"],
        "sections": list(spec["sections"]),
        "matched": [kw for kw in spec["keywords"] if kw in t],
        "scores": {k: v for k, v in scores.items() if v},
        "asks": extract_asks(t),
    }


# ── 언어 ────────────────────────────────────────────────────────────────────
# 고객이 영어로 물었는데 한국어로 답하면, 내용이 아무리 정확해도 대응 실패다.
# 판정은 한글 비율 하나로 한다 — 사내 VOC 는 한국어 아니면 영어이고, 사전이나 모델을
# 끌어오면 이 경로가 그 의존성 때문에 죽을 수 있다(정책 검사와 같은 이유로 결정적이어야 한다).
_HANGUL_RE = re.compile(r"[가-힣]")


def detect_lang(text: str) -> str:
    """'ko' | 'en'. 한글이 한 글자라도 의미 있게 섞이면 한국어로 본다.

    한국어 문의에 영어 로그·제품명이 잔뜩 섞이는 것은 흔하지만, 영어 문의에 한글이
    섞이는 일은 드물다. 그래서 임계를 낮게 잡는다(오판의 비용이 비대칭이다).
    """
    t = re.sub(r"[\s\d\W_]+", "", text or "")
    if not t:
        return "ko"
    return "ko" if len(_HANGUL_RE.findall(t)) / len(t) >= 0.05 else "en"


# ── 2) 답변 생성 ────────────────────────────────────────────────────────────
_STYLE_RULES = (
    "작성 규칙 (고객이 직접 읽는 글입니다):\n"
    "- 존댓말(-습니다/-입니다)로 씁니다. 반말·개조식(-임/-함/-다) 금지.\n"
    "- **내부 이슈 번호·티켓 키(LSI-42 같은 형태)를 절대 쓰지 마세요.** 과거 사례는 "
    "'유사한 사례에서는' 처럼 번호 없이 언급합니다.\n"
    "- 내부 용어(지식베이스·인용·RCA·임베딩·게이트 등)를 쓰지 마세요. 고객의 말로 씁니다.\n"
    "- 한자/CJK 한자 금지.\n"
    "- **다른 고객사의 이름이나 사례를 언급하지 마세요.** '유사한 사례에서는' 까지만 씁니다.\n"
    "- 고객이 '다른 고객사에서도 발생하는지' 를 묻더라도 다른 고객의 정보는 공유할 수 "
    "없습니다. 그 사실을 밝히고, 대신 알려드릴 수 있는 것(알려진 동작인지, 조치 계획)으로 "
    "답합니다. 질문을 무시하지 마세요.\n"
    "- **확정 일정을 약속하지 마세요.** '언제까지 완료하겠습니다' 대신 "
    "'확인되는 대로 안내드리겠습니다' 로 씁니다.\n"
    "- **환불·교환·보상을 확약하지 마세요.** 절차와 다음 단계만 안내합니다.\n"
    "- 확인되지 않은 원인은 단정하지 말고 '가능성이 높습니다' 처럼 씁니다.\n"
    "- 고객이 **확답**을 요구했는데 지금 확정할 수 없다면, 확정할 수 없다는 사실을 "
    "분명히 밝히고 ① 무엇이 확인되면 확답이 가능한지 ② 언제 다시 안내하는지를 씁니다. "
    "**답할 수 없는 질문을 침묵으로 넘기지 마세요** — 고객이 가장 크게 불만을 갖는 지점입니다.\n"
    "- 고객이 회신 기한을 제시했으면 그 기한을 언급하고 '그때까지 확인된 내용을 정리해 "
    "드리겠습니다' 처럼 답합니다. 완료를 약속하지는 않습니다.\n"
    "- 마지막에 다음 단계(무엇을 언제 다시 안내하는지)를 반드시 넣습니다.\n"
    "- 600자 내외, 문단 3~5개. 장황한 배경 설명은 넣지 않습니다.\n")


_STYLE_RULES_EN = (
    "Writing rules (the customer reads this directly):\n"
    "- Write in English, in a polite professional support-desk register.\n"
    "- **Never write internal ticket keys** (anything like ABC-123). Refer to past cases "
    "as 'a similar case' with no identifier.\n"
    "- Never use internal jargon (knowledge base, citation, RCA, embedding, gate).\n"
    "- Never name another customer or their case. If the customer asks whether others "
    "report the same issue, say you cannot share other customers' information, then answer "
    "with what you can share (whether it is known behaviour, and the plan). Do not ignore "
    "the question.\n"
    "- **Never promise a completion date.** Say 'we will update you as soon as we confirm' "
    "instead of 'we will fix it by <date>'.\n"
    "- **Never promise a refund, replacement or compensation.** Describe the process only.\n"
    "- Do not state an unconfirmed cause as fact — say 'this is likely'.\n"
    "- If the customer asks for a definitive answer you cannot give yet, say so explicitly, "
    "then state (1) what needs to be confirmed before you can answer and (2) when you will "
    "come back to them. **Never pass over a question in silence** — that is what customers "
    "complain about most.\n"
    "- If the customer states a reply deadline, acknowledge it and say what you will have "
    "ready by then. Do not promise completion.\n"
    "- End with a concrete next step (what you will tell them, and when you will be in touch).\n"
    "- About 350 words, 3-5 paragraphs. No long background essays.\n")


def _evidence_block(matches: list[dict], proposal: dict | None) -> str:
    """근거를 **번호 없이** 내부용 재료로 정리 — 프롬프트에도 키를 넣지 않는다.

    프롬프트에 키가 들어가면 모델은 높은 확률로 그걸 본문에 인용한다(실측). 애초에
    보여주지 않는 것이 규칙으로 막는 것보다 확실하다.
    """
    lines = []
    for i, m in enumerate(matches[:3], 1):
        lines.append(f"[유사 사례 {i}] 증상: {m.get('summary', '')}\n"
                     f"  원인: {m.get('root_cause', '') or '—'}\n"
                     f"  조치: {m.get('resolution', '') or '—'}\n"
                     f"  우회책: {m.get('workaround', '') or '—'}")
    p = proposal or {}
    if p:
        lines.append(f"[종합 제안] 원인: {p.get('root_cause', '') or '—'} / "
                     f"조치: {p.get('resolution', '') or '—'} / "
                     f"우회책: {p.get('workaround', '') or '—'}")
    return "\n".join(lines) if lines else "(확인된 유사 사례 없음 — 원인을 단정하지 말 것)"


def reply_prompt(rec: dict, matches: list[dict], proposal: dict | None,
                 intent: dict, *, guidance: str = "", lang: str = "ko") -> str:
    """고객 답변 생성 프롬프트. 유형별 골격 + 요청 문장 + 근거(키 제거) + 문체 규칙.

    lang="en" 이면 답변 언어만 바꾼다 — 골격·근거·규칙은 같다. 규칙을 언어별로
    따로 관리하면 한쪽만 갱신되어 영어 답변에서 약속 금지 규칙이 빠지는 식으로 갈라진다.
    """
    asks = intent.get("asks") or []
    if lang == "en":
        ask_block = ("\n".join(f"- {a}" for a in asks) if asks
                     else "- (no explicit question — acknowledge and state the plan)")
        sections = "\n".join(f"### {s}" for s in intent.get("sections", []))
        return (
            "You are a technical support engineer replying to a semiconductor component "
            "customer. Write **the reply that will be sent to the customer as-is**, in "
            "English Markdown.\n\n"
            f"Request type: {intent.get('label', '')} — {intent.get('goal', '')}\n\n"
            "## The customer's requests — every one must be answered\n" + ask_block + "\n\n"
            "## Required structure (this order; translate these headings into English)\n"
            + sections + "\n\n" + _STYLE_RULES_EN + "\n"
            f"## Customer inquiry\n{rec.get('summary', '')}\n{rec.get('symptom', '')}\n"
            + (f"What they asked for: {rec.get('customer_ask', '')}\n" if rec.get("customer_ask") else "")
            + "\n## Reference material (internal, in Korean. Ground the reply in it, but "
              "rewrite it in the customer's words — never paste it)\n"
            + _evidence_block(matches, proposal)
            + (f"\n\n## Repeatedly raised in past reviews\n{guidance}" if guidance else ""))
    ask_block = ("\n".join(f"- {a}" for a in asks) if asks
                 else "- (명시적 질문 없음 — 접수 사실과 진행 계획을 안내)")
    sections = "\n".join(f"### {s}" for s in intent.get("sections", []))
    return (
        "당신은 반도체 부품 고객사를 응대하는 기술지원 담당자입니다. 아래 고객 문의에 "
        "**고객에게 그대로 보낼 답변**을 한국어 마크다운으로 작성하세요.\n\n"
        f"이 문의의 유형: {intent.get('label', '')} — {intent.get('goal', '')}\n\n"
        "## 반드시 답해야 할 고객의 요청\n" + ask_block + "\n\n"
        "## 답변 골격 (이 순서, 이 제목 그대로)\n" + sections + "\n\n"
        + _STYLE_RULES + "\n"
        f"## 고객 문의\n{rec.get('summary', '')}\n{rec.get('symptom', '')}\n"
        + (f"고객이 요청한 것: {rec.get('customer_ask', '')}\n" if rec.get("customer_ask") else "")
        + "\n"
        "## 참고 자료 (내부 자료입니다. 이 내용을 근거로 삼되 그대로 옮기지 말고 "
        "고객이 이해할 말로 바꿔 쓰세요)\n" + _evidence_block(matches, proposal)
        + (f"\n\n## 과거 검토에서 반복 지적된 사항\n{guidance}" if guidance else ""))


def _render_reply_en(rec: dict, matches: list[dict], proposal: dict | None) -> str:
    """영어 문의용 결정적 초안.

    유형별 골격을 영어로 다시 두지 않는다 — 이건 LLM 이 없을 때의 **폴백**이고, 골격을
    두 언어로 관리하면 한쪽이 낡는다. 어떤 유형이든 성립하는 4단 구조로 간다.
    """
    has = bool(matches)
    # **한국어 근거를 그대로 붙이지 않는다.** 붙이면 영어 답변에 한국어 문장이 섞여
    # 나간다(실측으로 그렇게 나갔다). 이건 LLM 이 없을 때의 폴백이고, 번역은 사람이
    # 한다 — 근거 사례는 큐 항목에 남아 있어 검토자가 화면에서 볼 수 있다.
    found = ("We have found a similar case in our records and are checking whether the "
             "same cause applies here."
             if has else "We have not yet found a confirmed matching case, so we are not "
                         "stating a cause. An engineer is looking into it directly.")
    return ("Hello, thank you for reaching out. We have reviewed your inquiry.\n\n"
            "### What we understood\n"
            f"{(rec.get('summary', '') or '').strip()}\n\n"
            "### What we found so far\n" + found + "\n\n"
            "### Next steps\n"
            "Our engineers are working on this and we will update you on this ticket as "
            "soon as we confirm the cause. If you can share the conditions in which the "
            "issue appears (environment, how to reproduce, when it happened), it will "
            "speed up the analysis.\n\nThank you.")


def render_reply(rec: dict, matches: list[dict], proposal: dict | None,
                 intent: dict, *, lang: str = "ko") -> str:
    """LLM 없이 만드는 결정적 답변 초안.

    LLM 이 없거나 실패했을 때 **빈손으로 돌려주지 않기 위한** 경로다. 사람이 채워야
    하는 자리는 빈칸이 아니라 문장으로 남긴다 — 빈칸은 그대로 발송되지만 문장은
    검토자가 반드시 읽는다.
    """
    if lang == "en":
        return _render_reply_en(rec, matches, proposal)
    p = proposal or {}
    has = bool(matches)
    parts = [f"안녕하세요, 문의 주신 내용 확인했습니다."]
    for sec in intent.get("sections", []):
        parts.append(f"\n### {sec}")
        if sec in ("문의 주신 사항", "확인한 내용", "불편을 드린 점에 대해"):
            if sec == "불편을 드린 점에 대해":
                parts.append("불편을 드려 죄송합니다. 말씀해 주신 내용을 확인했습니다.")
            asks = intent.get("asks") or []
            parts.append("말씀해 주신 내용: " + (rec.get("summary", "") or "").strip())
            if asks:
                parts.append("요청하신 사항: " + " / ".join(asks))
        elif sec in ("현재 파악된 원인", "확인한 사실"):
            parts.append(_cause_sentence(p.get("root_cause", "")) if has else _NO_EVIDENCE)
        elif sec in ("지금 적용해 보실 수 있는 조치", "지금 바로 적용 가능한 방법",
                     "그동안 도움이 되는 방법", "설정 방법"):
            parts.append(_action_sentence(p.get("workaround") or p.get("resolution", "")) if has
                         else "현재 안내드릴 수 있는 임시 조치가 확인되지 않았습니다. "
                              "확인되는 대로 우선 안내드리겠습니다.")
        elif sec in ("앞으로의 진행", "취한 조치", "근본 조치 진행", "현재 진행 상황"):
            parts.append("담당 엔지니어가 원인 확인을 진행하고 있으며, 확인되는 대로 "
                         "결과를 안내드리겠습니다.")
        elif sec == "처리 절차 안내":
            # 교환·환불은 이 시스템이 판정할 수 없다 — 절차와 담당만 안내하고
            # 승인 여부는 어디에서도 암시하지 않는다.
            parts.append("교환·환불 등 처리 여부는 담당 부서에서 제품 확인 후 결정됩니다. "
                         "제품 정보와 구매 정보를 함께 보내주시면 담당 부서로 전달드리겠습니다.")
        elif sec in ("다음 안내 시점", "다음 단계", "추가 문의"):
            parts.append("추가로 확인되는 내용이 있으면 이 문의에 이어서 안내드리겠습니다. "
                         "궁금하신 점은 언제든 회신해 주세요.")
        elif sec in ("추가로 알려주시면 도움이 되는 정보", "확인이 필요한 사항",
                     "확인이 필요한 조건", "정상 동작 확인 방법"):
            parts.append("증상이 나타나는 상황(사용 환경·재현 조건·발생 시각)을 알려주시면 "
                         "원인 확인이 빨라집니다.")
        else:
            # 유형이 무엇이든 근거가 없으면 같은 말로 답한다 — 유형별로 다르게 쓰면
            # 어떤 경로에서는 원인을 단정하는 문장이 슬쩍 들어간다.
            parts.append("담당자가 확인 후 안내드리겠습니다." if has else _NO_EVIDENCE)
    parts.append("\n감사합니다.")
    return "\n".join(parts)


_NO_EVIDENCE = ("동일한 증상의 확인된 사례가 아직 없어, 원인을 단정하지 않고 담당 엔지니어가 "
                "직접 확인하고 있습니다.")

_ASSERT_RE = re.compile(r"(입니다|이다|였습니다|때문입니다)\s*[.]?$")


def _clean(text: str) -> str:
    """내부 서술을 문장 재료로 정리 — 키 제거 + 끝의 마침표/종결어미 정리."""
    s = redact(str(text or "").strip())
    return re.sub(r"[.\s]+$", "", s)


def _cause_sentence(text: str) -> str:
    """원인 문장 — 단정하지 않는다. 근거는 '유사 사례'이지 이 문의의 확정 원인이 아니다."""
    s = _clean(text)
    if not s:
        return "원인은 아직 확인 중입니다."
    return (f"유사한 사례에서는 '{s}' 로 확인된 경우가 있었습니다. "
            "이 문의도 같은 가능성이 있어 확인하고 있습니다.")


def _action_sentence(text: str) -> str:
    """조치 문장 — 시켜야 할 일이므로 명사구를 그대로 두고 안내형으로 감싼다."""
    s = _clean(text)
    if not s:
        return "현재 안내드릴 수 있는 조치가 확인되지 않았습니다."
    return f"유사한 사례에서는 다음 방법이 도움이 되었습니다 — {s}."


# ── 교정(proofread) ─────────────────────────────────────────────────────────
# 생성 모델이 한국어 오타를 낸다 — 실측에서 "송괘하게"(송구하게)·"콘트볼러"(컨트롤러)·
# "작엄"(작업)·"완학"(완화) 이 그대로 나왔다. 고객이 읽는 글이라 오타 하나가 답변
# 전체의 신뢰를 깎는다. 사전 없이 한국어 오타를 판정할 방법은 없으므로 **교정은 LLM
# 이 하고, 그 교정이 내용을 바꾸지 않았는지는 규칙이 검사한다.**
#
# 이 순서가 중요하다. 교정 모델에게 "고쳐라" 만 시키면 문장을 다시 쓰면서 수치·조건·
# 약속을 조용히 바꾼다 — 고객 답변에서 그건 오타보다 훨씬 큰 사고다. 그래서 바뀌면
# 안 되는 것(숫자·제품명·제목·길이·언어)을 대조해, 하나라도 어긋나면 **교정을 버리고
# 원문을 쓴다**. 교정은 있으면 좋은 것이고, 내용 보존은 양보할 수 없는 것이다.
PROOFREAD_PROMPT = (
    "다음 글의 **오타와 띄어쓰기만** 고치세요.\n"
    "- 문장을 다시 쓰지 마세요. 표현을 다듬지도 마세요.\n"
    "- 숫자·단위·제품명·영문 용어·마크다운 제목은 **한 글자도** 바꾸지 마세요.\n"
    "- 내용을 더하거나 빼지 마세요.\n"
    "- 고칠 것이 없으면 원문을 그대로 출력하세요.\n"
    "- 설명 없이 고친 글만 출력하세요.\n\n{text}")

_NUM_RE = re.compile(r"\d+")
_LATIN_RE = re.compile(r"[A-Za-z][A-Za-z0-9._\-]*")


def _headings(text: str) -> list[str]:
    return [ln.strip() for ln in (text or "").split("\n") if ln.strip().startswith("#")]


def safe_replace(original: str, rewritten: str, *, max_delta: float = 0.15) -> tuple[str, str]:
    """교정본을 받아들일지 판정한다. 반환 (채택할 본문, 사유).

    사유가 빈 문자열이면 교정 채택, 아니면 원문 유지 + 그 이유.
    """
    a, b = (original or "").strip(), (rewritten or "").strip()
    if not b:
        return original, "교정 결과가 비어 있음"
    if a == b:
        return original, ""
    # 순서는 '무엇을 알려줄 것인가' 로 정한다 — 어느 규칙이 먼저 걸리든 교정은
    # 폐기되지만, 사유는 가장 행동 가능한 것이 남아야 한다. 내부 키 유출이 제일 위다.
    if issue_keys.find_set(b) - issue_keys.find_set(a):
        return original, "없던 이슈 키가 생김"
    if _headings(a) != _headings(b):
        return original, "마크다운 제목이 바뀜"
    if sorted(_NUM_RE.findall(a)) != sorted(_NUM_RE.findall(b)):
        return original, "숫자가 바뀜"
    if sorted(_LATIN_RE.findall(a)) != sorted(_LATIN_RE.findall(b)):
        return original, "제품명·영문 용어가 바뀜"
    if detect_lang(a) != detect_lang(b):
        return original, "언어가 바뀜"
    if abs(len(b) - len(a)) / max(len(a), 1) > max_delta:
        return original, f"길이가 {len(a)}→{len(b)} 로 크게 변함 (내용 변경 의심)"
    return b, ""


# ── 3) 정책 검사 ────────────────────────────────────────────────────────────
# 한자 판정은 lang_validator 가 단일 소스다. 다만 그 모듈은 LLM 교정 경로 때문에
# 최상위에서 agno 를 import 한다 — 정책 검사가 LLM 의존성 때문에 죽으면 안 되므로
# 지연 import 하고, 실패하면 같은 범위의 로컬 패턴으로 떨어진다.
_HAN_FALLBACK = re.compile(r"[㐀-䶿一-鿿豈-﫿]")


def _han_re():
    try:
        from lang_validator import HAN_RE
        return HAN_RE
    except Exception:
        return _HAN_FALLBACK


# 확정 일정 약속: 시점 표현 + 완료 약속이 한 문장에 같이 있을 때만 잡는다.
# "다음 주에 확인해 보겠습니다" 는 약속이 아니다 — 잡으면 검토자가 경고를 무시하게 된다.
_WHEN = (r"(?:\d{1,2}\s*월\s*\d{1,2}\s*일|\d{4}[-./]\d{1,2}[-./]\d{1,2}|내일|모레|"
         r"이번\s*주|다음\s*주|이번\s*달|다음\s*달|금주|차주|\d+\s*(?:일|주|달|개월)\s*(?:내|안|이내)?)")
_PROMISE_RE = re.compile(
    rf"[^.\n]*{_WHEN}[^.\n]*(?:완료(?:해\s*드리|하겠|됩니다|입니다)|해결(?:해\s*드리|하겠|됩니다)|"
    rf"배포(?:해\s*드리|하겠|됩니다|예정입니다)|수정(?:해\s*드리|하겠|됩니다)|조치(?:해\s*드리|하겠))[^.\n]*")
_COMPENSATE_RE = re.compile(
    r"[^.\n]*(?:환불|교환|반품|보상|배상|무상\s*교체)[^.\n]{0,20}"
    r"(?:해\s*드리겠|해\s*드립니다|가능합니다|처리해\s*드리|약속드리|진행해\s*드리겠)[^.\n]*")
# 반말·개조식: '니다' 로 끝나지 않는 '다', 그리고 명사형 종결(-임/-함/-됨).
_PLAIN_RE = re.compile(r"(?:(?<!니)(?<!습)다|[가-힣](?:임|함|됨|음))\s*[.!]?$")
_JARGON = ("지식베이스", "임베딩", "인용", "RCA", "coverage", "BM25", "게이트",
           "프롬프트", "티켓 키", "근거 사례", "(추정)", "(배경)", "KB")
# 사전 없이 확실히 잡을 수 있는 표기 이상만 본다 — 단독 자모(ㅋ, ㅜ), 같은 글자
# 3회 이상 반복, 문장 부호 앞 공백. 진짜 오타("콘트볼러")는 여기서 못 잡는다.
# 못 잡는 것을 잡는 척하지 않는다 — LLM 교정이 그 몫이고, 이건 그것이 꺼져 있을 때의
# 최소한이다.
_JAMO_RE = re.compile(r"(?<![가-힣])[ㄱ-ㅎㅏ-ㅣ]+")
_REPEAT_RE = re.compile(r"(.)\1{3,}")
_SPACE_PUNCT_RE = re.compile(r"\s+[.,!?)]")
_NEXTSTEP = ("안내드리", "안내 드리", "회신", "연락", "확인되는 대로", "다시 안내",
             "업데이트", "알려드리", "알려 드리")
_NEXTSTEP_EN = ("we will update", "we will let you know", "get back to you",
                "as soon as we", "next step", "keep you posted", "follow up")
_JARGON_EN = ("knowledge base", "RCA", "embedding", "citation", "coverage gate", "BM25")
# 영어 답변의 약속 표현. 한국어 규칙을 그대로 두면 영어 답변에서는 약속 금지가 통째로
# 빠진다 — 규칙이 언어에 따라 사라지면 그 규칙은 없는 것과 같다.
_WHEN_EN_RE = re.compile(
    r"\bby (?:next|this) (?:week|month|monday|tuesday|wednesday|thursday|friday)\b|"
    r"\bby \w+ \d{1,2}\b|\bwithin \d+ (?:days?|weeks?|months?)\b|\b\d{4}-\d{2}-\d{2}\b|"
    r"\bin \d+ (?:days?|weeks?|months?)\b|\btomorrow\b", re.I)
# 어간 뒤 `\w+` 를 요구하면 "will deploy" 같은 원형이 통째로 샌다 — 실제로 샜다.
_DONE_EN_RE = re.compile(
    r"\b(?:will (?:be )?(?:fix|resolv|complet|deliver|releas|deploy|ship|patch)\w*|"
    r"guarantee\w*|we commit)\b", re.I)
_COMPENSATE_EN_RE = re.compile(
    r"\b(?:refund|replacement|replace|compensat\w+|reimburse\w*)\b", re.I)
_COMPENSATE_YES_EN_RE = re.compile(
    r"\b(?:will be (?:issued|provided|approved|arranged)|we will (?:issue|provide|approve|process|arrange)|"
    r"is approved|has been approved)\b", re.I)


BLOCKING = ("internal_key", "han_char", "date_promise", "compensation_promise",
            "plain_speech", "third_party", "wrong_language")


def _sentences(text: str) -> list[str]:
    """문체 검사 대상 문장. 제목·글머리표·표는 제외 — 명사구로 끝나는 것이 정상이다."""
    out = []
    for raw in re.split(r"[\n]|(?<=[.!?])\s+", text or ""):
        s = raw.strip()
        if len(s) < 8 or s.startswith(("#", "-", "*", "|", ">", "_")):
            continue
        out.append(s)
    return out


def _sentence_hit(text: str, a: re.Pattern, b: re.Pattern) -> str:
    """한 문장 안에 두 신호가 **함께** 있을 때만 잡는다.

    영어는 시점이 동사 뒤에 온다("we will fix it by next week") — 한국어처럼 앞뒤
    순서를 고정한 정규식으로는 통째로 새어 나간다. 실제로 그렇게 새는 것을 확인했다.
    """
    for raw in re.split(r"(?<=[.!?])\s+|\n+", text or ""):
        if a.search(raw) and b.search(raw):
            return raw.strip()
    return ""


def check(text: str, *, has_evidence: bool = True, forbidden: tuple = (),
          lang: str = "ko", asks=()) -> dict:
    """발송 전 정책 검사. 반환: {ok, blocked, violations:[{code,severity,detail}]}

    forbidden — 이 답변에 **나오면 안 되는 고유명사**(다른 고객사명 등). 근거 사례의
    본문은 다른 고객의 고장 이력이다. "다른 고객사 ○○ 에서도 같은 문제가" 는 사실이어도
    남의 정보를 흘리는 것이라, 자동으로 지우지 않고 차단해 사람이 고치게 한다.
    """
    v: list[dict] = []

    def add(code: str, severity: str, detail: str) -> None:
        v.append({"code": code, "severity": severity, "detail": detail})

    keys = sorted(issue_keys.find_set(text))
    if keys:
        add("internal_key", "block", f"내부 이슈 키 노출: {', '.join(keys[:5])}")
    han = _han_re().findall(text or "")
    if han:
        add("han_char", "block", f"한자 사용: {''.join(sorted(set(han))[:10])}")
    hit = _PROMISE_RE.search(text or "")
    detail = hit.group(0).strip() if hit else (
        _sentence_hit(text, _WHEN_EN_RE, _DONE_EN_RE) if lang == "en" else "")
    if detail:
        add("date_promise", "block", f"확정 일정 약속: {detail[:80]}")
    hit = _COMPENSATE_RE.search(text or "")
    detail = hit.group(0).strip() if hit else (
        _sentence_hit(text, _COMPENSATE_EN_RE, _COMPENSATE_YES_EN_RE) if lang == "en" else "")
    if detail:
        add("compensation_promise", "block", f"보상·환불 확약: {detail[:80]}")
    third = [f for f in forbidden if f and f in (text or "")]
    if third:
        add("third_party", "block", f"다른 고객사·제3자 정보 노출: {', '.join(third[:3])}")
    # 존댓말 검사는 한국어에만 적용한다 — 영어 문장에 걸 규칙이 아니다.
    plain = [s for s in _sentences(text) if _PLAIN_RE.search(s)] if lang == "ko" else []
    if plain:
        add("plain_speech", "block", f"존댓말이 아닌 문장 {len(plain)}건: {plain[0][:60]}")
    low = (text or "").lower()
    jargon = ([w for w in _JARGON if w in (text or "")]
              + [w for w in _JARGON_EN if w.lower() in low])
    if jargon:
        add("internal_jargon", "warn", f"내부 용어: {', '.join(sorted(set(jargon)))}")
    nextstep = (_NEXTSTEP if lang == "ko" else _NEXTSTEP_EN)
    if not any(w.lower() in low for w in nextstep):
        add("missing_next_step", "warn", "다음 단계(언제 무엇을 안내하는지)가 없습니다")
    missed = unanswered_asks(asks, text)
    if missed:
        add("unanswered_ask", "warn",
            f"답변이 건드리지 않은 요청 {len(missed)}건: {missed[0][:60]}")
    typo = []
    if _JAMO_RE.search(text or ""):
        typo.append("단독 자모")
    if _REPEAT_RE.search(text or ""):
        typo.append("같은 글자 반복")
    if _SPACE_PUNCT_RE.search(text or ""):
        typo.append("문장부호 앞 공백")
    if typo:
        add("suspect_spelling", "warn", f"표기 이상: {', '.join(typo)}")
    if len(text or "") > 2500:
        add("too_long", "warn", f"{len(text)}자 — 고객 답변으로 깁니다")
    if not has_evidence and _ASSERT_RE.search((text or "").strip()[-200:] or ""):
        add("unsupported_certainty", "warn", "확인된 사례가 없는데 단정 어조입니다")
    if (text or "").strip() and detect_lang(text) != lang:
        add("wrong_language", "block",
            f"고객은 {lang} 로 문의했는데 답변 언어가 다릅니다")
    blocked = any(x["severity"] == "block" for x in v)
    return {"ok": not v, "blocked": blocked, "violations": v}


def redact(text: str) -> str:
    """내부 이슈 키를 고객이 읽어도 되는 표현으로 치환. 발송 전 마지막 안전망."""
    return issue_keys.STEM_RE.sub("유사 사례", text or "")
