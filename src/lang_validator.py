"""Language-compliance validator.

Detects characters outside the allowed alphabet (Korean Hangul + Latin ASCII +
common punctuation/digits/emoji) and optionally rewrites them using an LLM.

생성 모델이 한국어 출력에 한자를 흘리는 일이 있다 — deepseek-v4-flash 에서
"언제恢复正常하나요" 같은 형태로 실측됐다. 이 검증기가 그것을 잡는다.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

from agno.agent import Agent
from agno.models.openrouter import OpenRouter

# Unicode ranges of CJK ideographs (Han characters). Hangul (한글) is excluded.
HAN_RE = re.compile(
    r"["
    r"㐀-䶿"   # CJK Unified Ideographs Extension A
    r"一-鿿"   # CJK Unified Ideographs (basic)
    r"豈-﫿"   # CJK Compatibility Ideographs
    r"\U00020000-\U0002A6DF"  # Extension B
    r"]"
)

# Allowed characters: ASCII printable, Hangul, common punctuation, digits, whitespace, emoji
ALLOWED_RE = re.compile(
    r"["
    r"\x09-\x0D\x20-\x7E"            # ASCII printable + whitespace
    r"가-힣"                  # Hangul syllables
    r"ᄀ-ᇿ㄰-㆏"     # Hangul Jamo / Compatibility Jamo
    r" -⁯⸀-⹿"     # General punctuation
    r"‐-‧‰-⁞"     # Hyphen, dashes, etc.
    r" -ÿ"                  # Latin-1 supplement
    r"←-⇿⌀-⏿"     # Arrows + misc technical
    r"─-╿■-◿"     # Box / geometric
    r"☀-➿"                  # Misc symbols + dingbats (✓ ☑ etc.)
    r"\U0001F300-\U0001FAFF"          # Emoji
    r"]"
)


@dataclass
class ValidationResult:
    ok: bool
    violations: list[str]
    cleaned: str  # text with violations replaced (only used if rewrite=False)
    rewritten: str | None = None  # 위반 줄만 고친 판본 (나머지 줄은 원문 그대로)
    notes: str = ""               # 무엇을 고치고 무엇을 마스킹했는지


def find_violations(text: str) -> list[str]:
    """Return list of Han characters appearing in text (deduped, order preserved)."""
    seen = []
    for ch in text:
        if HAN_RE.match(ch) and ch not in seen:
            seen.append(ch)
    return seen


def validate(text: str) -> ValidationResult:
    """Pure-Python check + masked-replacement fallback."""
    violations = find_violations(text)
    if not violations:
        return ValidationResult(ok=True, violations=[], cleaned=text)
    cleaned = HAN_RE.sub("⟦?⟧", text)
    return ValidationResult(ok=False, violations=violations, cleaned=cleaned)


_REWRITER: Agent | None = None


def _get_rewriter() -> Agent:
    global _REWRITER
    if _REWRITER is not None:
        return _REWRITER
    api_key = os.environ["OPENROUTER_API_KEY"]
    model_id = os.getenv("RVP_VALIDATOR_MODEL") or os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini")
    base_url = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    _REWRITER = Agent(
        name="lang-rewriter",
        model=OpenRouter(id=model_id, api_key=api_key, base_url=base_url),
        instructions=[
            "You are a strict language-compliance editor.",
            "Allowed scripts: Korean Hangul, Latin (English), digits, punctuation, emoji.",
            "Forbidden: any Chinese / Japanese kanji / Hanja characters (CJK ideographs).",
            "Rewrite the user's text so the meaning is preserved but ALL forbidden "
            "characters are replaced with natural Korean (or English when the surrounding "
            "context is English).",
            "Do not add commentary. Output ONLY the rewritten text.",
        ],
        markdown=False,
        telemetry=False,
    )
    return _REWRITER


# 재작성 후보 줄의 길이 허용 배수. 한자 몇 자를 한글로 바꾸는 작업이므로 길이가
# 크게 달라질 이유가 없다 — 벗어나면 모델이 요약·부연을 한 것으로 보고 버린다.
_LEN_TOLERANCE = 1.8


def _rewrite_line(agent, line: str) -> str | None:
    """한 줄만 재작성. 실패하거나 검증에 걸리면 None."""
    try:
        resp = agent.run(line)
        out = (resp.content if hasattr(resp, "content") else str(resp)).strip()
    except Exception:
        return None
    if not out or find_violations(out):
        return None                      # 여전히 한자가 있으면 쓸 수 없다
    lo, hi = len(line) / _LEN_TOLERANCE, len(line) * _LEN_TOLERANCE
    if not (lo <= len(out) <= hi):
        return None                      # 길이가 크게 변하면 내용이 바뀐 것이다
    return out


def validate_and_fix(text: str) -> ValidationResult:
    """한자가 섞인 **줄만** 골라 재작성한다.

    예전에는 한자 한 글자만 나와도 문서 전체를 LLM 에 넘겨 통째로 다시 쓰게 하고,
    그 결과를 검증 없이 채택했다(`rewritten if post.ok else rewritten` — 양쪽이
    같은 무의미한 삼항). 그래서 멀쩡한 문장까지 모델이 바꿔 놓았다 — 실제로
    "펌웨어" 가 "펌 - 만웨어" 로 깨져 나왔다.

    지금은 위반이 있는 줄에만 손대고, 나머지 줄은 **원문 그대로** 둔다. 재작성
    결과도 (a) 한자가 사라졌는지, (b) 길이가 크게 변하지 않았는지 확인하고,
    통과하지 못하면 그 줄은 마스킹본(⟦?⟧)으로 되돌린다 — 내용이 조용히
    바뀌는 것보다 표시가 깨져 보이는 편이 낫다.
    """
    result = validate(text)
    if result.ok:
        return result

    lines = text.split("\n")
    bad_idx = [i for i, ln in enumerate(lines) if find_violations(ln)]
    try:
        agent = _get_rewriter()
    except Exception as e:
        result.rewritten = result.cleaned    # 마스킹본으로 폴백
        result.notes = f"rewriter 사용 불가({str(e)[:80]}) — 마스킹 적용"
        return result

    out = list(lines)
    fixed, masked = 0, 0
    for i in bad_idx:
        rl = _rewrite_line(agent, lines[i])
        if rl is None:
            out[i] = HAN_RE.sub("⟦?⟧", lines[i])
            masked += 1
        else:
            out[i] = rl
            fixed += 1
    result.rewritten = "\n".join(out)
    result.notes = (f"{len(bad_idx)}줄에 한자 — {fixed}줄 재작성, {masked}줄 마스킹 "
                    f"(나머지 {len(lines) - len(bad_idx)}줄은 원문 유지)")
    return result


if __name__ == "__main__":
    import sys
    samples = [
        "안녕하세요. 어떻게 도와드릴까요?",
        "언제 恢复正常 하나요?",
        "Error E033 means thermal throttling.",
        "디바이스가 自动 으로 재시작됩니다.",
    ]
    if len(sys.argv) > 1 and sys.argv[1] == "--fix":
        from dotenv import load_dotenv
        from pathlib import Path
        load_dotenv(Path(__file__).resolve().parent.parent / ".env")
        for s in samples:
            r = validate_and_fix(s)
            print(f"[{'OK' if r.ok else 'BAD'}] {s!r}")
            if not r.ok:
                print(f"   violations: {r.violations}")
                print(f"   rewritten : {r.rewritten!r}")
    else:
        for s in samples:
            r = validate(s)
            print(f"[{'OK' if r.ok else 'BAD'}] {s!r}  violations={r.violations}")
