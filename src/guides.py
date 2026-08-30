"""답변 지침 원천 — Confluence 페이지 · FAQ URL · 로컬 문서.

**사례(이슈)와 역할이 다르다.** 사례는 "예전에 이랬다" 는 근거이고, 지침은 "이렇게
답해야 한다" 는 규칙이다. 그래서 프롬프트에서도 자리가 다르고, 충돌하면 **지침이
이긴다** — 규정이 바뀌었는데 옛 사례대로 답하면 그건 틀린 답이 아니라 사고다.

원천 세 종류를 같은 모양으로 접는다(`kind`):
  confluence  Confluence 페이지 (Cloud: /rest/api/content, Server/DC: 같은 경로 + PAT)
  url         임의의 FAQ/문서 페이지 (HTML → 텍스트)
  file        저장소 안의 마크다운/텍스트

인증은 Jira 와 같은 자격증명을 쓴다 — Atlassian 은 Jira/Confluence 가 같은 계정이고,
사내 위키가 별도면 `RVP_GUIDE_HEADERS` 로 헤더를 준다.

수집 결과는 `data/guides.json` 에 **섹션 단위**로 저장한다. 문서 통째로 넣으면 긴
페이지 하나가 프롬프트를 다 먹고, 어느 문단이 근거인지도 알 수 없다. 제목(heading)
으로 잘라 앵커까지 들고 있으면 답변에 그 문단 링크를 그대로 달 수 있다.
"""
from __future__ import annotations

import html as _html
import os
import re
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse

from json_store import now_iso, read_json, write_json_atomic

ROOT = Path(__file__).resolve().parent.parent
STORE_FILE = ROOT / "data" / "guides.json"

# 원천 목록 환경변수. 콤마 구분, 각 항목은 다음 중 하나다.
#   confluence:<pageId>             예) confluence:123456
#   confluence-space:<KEY>         예) confluence-space:SUPPORT  (그 스페이스 전체)
#   https://...                    임의 URL
#   file:data/knowledge.md         저장소 상대 경로
ENV_SOURCES = "RVP_GUIDE_SOURCES"
MAX_SECTION = 4000          # 섹션 1개 상한(자) — 더 길면 잘라 넣는다
MIN_SECTION = 30            # 이보다 짧은 조각은 지침이 아니다(목차·머리말 부스러기)
MAX_PAGES = 100             # 스페이스 수집 상한 — 위키 전체를 통째로 빨아오지 않는다


# --------------------------------------------------------------------------- #
# HTML → 텍스트
# --------------------------------------------------------------------------- #
_DROP_RE = re.compile(r"(?is)<(script|style|nav|footer|header)[^>]*>.*?</\1>")
_BR_RE = re.compile(r"(?i)<(br|/p|/div|/li|/tr|/h[1-6])\s*/?>")
_TAG_RE = re.compile(r"(?s)<[^>]+>")
_HEAD_RE = re.compile(r"(?is)<h([1-6])[^>]*>(.*?)</h\1>")


def html_to_sections(raw_html: str, *, title: str = "") -> list[tuple[str, str]]:
    """HTML → [(섹션 제목, 본문)]. 제목(h1~h6)에서 자른다.

    문서를 통째로 넣지 않는 이유: 긴 FAQ 한 장이 프롬프트를 다 먹고, 어느 문단이
    근거인지도 알 수 없게 된다.

    제목은 태그를 걷어내기 **전에** 표식으로 바꾼다. 순서를 바꾸면 제목이 본문에
    섞여 어디서 잘라야 할지 알 수 없게 된다.
    """
    body = _DROP_RE.sub(" ", raw_html or "")
    body = _HEAD_RE.sub(lambda m: f"\n\x00H\x00{_strip(m.group(2))}\x00\n", body)
    body = _BR_RE.sub("\n", body)
    text = _html.unescape(_TAG_RE.sub(" ", body))
    parts = re.split(r"\x00H\x00(.*?)\x00", text)   # [머리말, 제목1, 본문1, 제목2, 본문2, ...]
    out: list[tuple[str, str]] = []
    if _clean(parts[0]):
        out.append((title or "(본문)", _clean(parts[0])))
    for i in range(1, len(parts) - 1, 2):
        sec, txt = parts[i].strip(), _clean(parts[i + 1])
        if txt:
            out.append((sec or title or "(본문)", txt))
    return [(t, b[:MAX_SECTION]) for t, b in out if len(b) >= MIN_SECTION]


def md_to_sections(text: str, *, title: str = "") -> list[tuple[str, str]]:
    """마크다운 → [(제목, 본문)]. `#` 제목에서 자른다."""
    out, cur, buf = [], title or "(본문)", []
    for line in (text or "").split("\n"):
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            if _clean("\n".join(buf)):
                out.append((cur, _clean("\n".join(buf))))
            cur, buf = m.group(2).strip(), []
        else:
            buf.append(line)
    if _clean("\n".join(buf)):
        out.append((cur, _clean("\n".join(buf))))
    return [(t, b[:MAX_SECTION]) for t, b in out if len(b) >= MIN_SECTION]


def _strip(s: str) -> str:
    return _html.unescape(_TAG_RE.sub("", s or "")).strip()


def _clean(s: str) -> str:
    return re.sub(r"[ \t]+", " ", re.sub(r"\n{3,}", "\n\n", (s or "").strip()))


def anchor(section_title: str) -> str:
    """Confluence 앵커 규약 — 공백 제거. 문단으로 바로 보내려면 필요하다."""
    return re.sub(r"[^\w가-힣]", "", section_title or "")


# --------------------------------------------------------------------------- #
# 수집
# --------------------------------------------------------------------------- #
def _session():
    import requests
    s = requests.Session()
    pat = os.getenv("JIRA_PAT")
    if pat:
        s.headers["Authorization"] = f"Bearer {pat}"
    elif os.getenv("JIRA_EMAIL") and os.getenv("JIRA_API_TOKEN"):
        s.auth = (os.environ["JIRA_EMAIL"], os.environ["JIRA_API_TOKEN"])
    for kv in (os.getenv("RVP_GUIDE_HEADERS") or "").split(";"):
        if ":" in kv:
            k, v = kv.split(":", 1)
            s.headers[k.strip()] = v.strip()
    s.headers.setdefault("Accept", "application/json, text/html;q=0.9, */*;q=0.8")
    return s


def confluence_base() -> str:
    """Confluence base URL. 없으면 Jira base 에서 유추한다(Atlassian Cloud 는 같은 호스트)."""
    b = (os.getenv("CONFLUENCE_BASE_URL") or "").strip().rstrip("/")
    if b:
        return b
    jira = (os.getenv("JIRA_BASE_URL") or "").strip().rstrip("/")
    return f"{jira}/wiki" if jira.endswith(".atlassian.net") else jira


def fetch_confluence_page(page_id: str, *, session=None, base: str = "") -> dict | None:
    """Confluence 페이지 1건 → {title, url, html}."""
    base = (base or confluence_base()).rstrip("/")
    if not base:
        return None
    s = session or _session()
    r = s.get(f"{base}/rest/api/content/{quote(page_id)}",
              params={"expand": "body.storage,version,space"}, timeout=20)
    r.raise_for_status()
    d = r.json()
    return {"title": d.get("title", page_id),
            "url": f"{base}/pages/viewpage.action?pageId={page_id}"
                   if "/wiki" not in base else f"{base}/spaces/_/pages/{page_id}",
            "html": ((d.get("body") or {}).get("storage") or {}).get("value", "")}


def list_space_pages(space_key: str, *, session=None, base: str = "",
                     limit: int = MAX_PAGES) -> list[str]:
    """스페이스의 페이지 id 목록. 상한을 둔다 — 위키 전체를 통째로 빨아오면 안 된다."""
    base = (base or confluence_base()).rstrip("/")
    if not base:
        return []
    s = session or _session()
    r = s.get(f"{base}/rest/api/content",
              params={"spaceKey": space_key, "type": "page", "limit": min(limit, 100)},
              timeout=20)
    r.raise_for_status()
    return [str(x.get("id")) for x in (r.json().get("results") or []) if x.get("id")]


def fetch_url(url: str, *, session=None) -> dict | None:
    """임의 URL → {title, url, html}. FAQ 페이지 등."""
    s = session or _session()
    r = s.get(url, timeout=20)
    r.raise_for_status()
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", r.text or "")
    return {"title": _strip(m.group(1)) if m else urlparse(url).path.rsplit("/", 1)[-1] or url,
            "url": url, "html": r.text}


def _sections_for(doc: dict, kind: str) -> list[dict]:
    title = doc.get("title") or ""
    url = doc.get("url") or ""
    if kind == "file":
        # 확장자로 파서를 고른다. 전부 마크다운으로 읽으면 HTML 문서가 통째로 한
        # 섹션이 되고 본문에 태그가 남는다(실제로 그렇게 나왔다).
        text = doc.get("text", "")
        pairs = (html_to_sections(text, title=title)
                 if title.lower().endswith((".html", ".htm")) or text.lstrip()[:200].lower()
                 .startswith(("<!doctype", "<html"))
                 else md_to_sections(text, title=title))
    else:
        pairs = html_to_sections(doc.get("html", ""), title=title)
    out = []
    for sec_title, body in pairs:
        sec_url = url
        if url and kind == "confluence" and anchor(sec_title):
            sec_url = f"{url}#{anchor(sec_title)}"
        out.append({"doc_title": title, "section": sec_title, "text": body,
                    "url": sec_url, "kind": kind})
    return out


def collect(sources: list[str] | None = None) -> dict:
    """원천을 읽어 섹션으로 저장. 반환 {sections, docs, errors}.

    한 원천이 실패해도 나머지는 수집한다 — 위키 한 장이 권한 때문에 막혔다고 지침
    전체가 비면, 답변은 지침 없이 나가면서 그 사실을 아무도 모른다.
    """
    srcs = sources if sources is not None else env_sources()
    s = _session()
    sections: list[dict] = []
    docs: list[dict] = []
    errors: list[dict] = []
    for src in srcs:
        src = (src or "").strip()
        if not src:
            continue
        try:
            if src.startswith("confluence-space:"):
                key = src.split(":", 1)[1]
                for pid in list_space_pages(key, session=s):
                    doc = fetch_confluence_page(pid, session=s)
                    if doc:
                        docs.append({"source": f"confluence:{pid}", "title": doc["title"],
                                     "url": doc["url"]})
                        sections += _sections_for(doc, "confluence")
            elif src.startswith("confluence:"):
                doc = fetch_confluence_page(src.split(":", 1)[1], session=s)
                if doc:
                    docs.append({"source": src, "title": doc["title"], "url": doc["url"]})
                    sections += _sections_for(doc, "confluence")
            elif src.startswith("file:"):
                path = ROOT / src.split(":", 1)[1]
                text = path.read_text(encoding="utf-8")
                m = re.search(r"(?is)<title[^>]*>(.*?)</title>", text)
                doc = {"title": _strip(m.group(1)) if m else path.name, "url": "", "text": text}
                docs.append({"source": src, "title": doc["title"], "url": ""})
                sections += _sections_for(doc, "file")
            elif src.startswith("http://") or src.startswith("https://"):
                doc = fetch_url(src, session=s)
                if doc:
                    docs.append({"source": src, "title": doc["title"], "url": doc["url"]})
                    sections += _sections_for(doc, "url")
            else:
                errors.append({"source": src, "error": "알 수 없는 원천 형식"})
        except Exception as e:            # noqa: BLE001 — 원천 하나의 실패가 전체를 막지 않는다
            errors.append({"source": src, "error": str(e)[:200]})
    return {"sections": sections, "docs": docs, "errors": errors}


def env_sources() -> list[str]:
    return [x.strip() for x in (os.getenv(ENV_SOURCES) or "").split(",") if x.strip()]


def save(collected: dict) -> dict:
    env = {"schema_version": 1, "updated_at": now_iso(),
           "sources": env_sources(), **collected}
    write_json_atomic(STORE_FILE, env)
    return env


def load() -> dict:
    d = read_json(STORE_FILE, {})
    return d if isinstance(d, dict) else {}


# --------------------------------------------------------------------------- #
# 직접 작성 지침 — 위키에 없는 규칙을 화면에서 바로 적는다.
#
# 수집본(data/guides.json)과 파일을 나눈다. 수집본은 원천에서 언제든 다시 만드는
# 캐시라 git 이 추적하지 않는데, 사람이 쓴 지침은 **그 자체가 원본**이다. 같은 파일에
# 두면 다음 수집이 사람의 글을 덮어쓴다.
# --------------------------------------------------------------------------- #
MANUAL_FILE = ROOT / "data" / "guides_manual.json"


def manual_items() -> list[dict]:
    d = read_json(MANUAL_FILE, [])
    return d if isinstance(d, list) else []


def manual_upsert(title: str, text: str, *, item_id: str = "", author: str = "") -> dict:
    """직접 작성 지침 추가/수정. 제목과 본문 필수."""
    title, text = (title or "").strip(), (text or "").strip()
    if not title or not text:
        raise ValueError("제목과 본문이 모두 필요합니다")
    items = manual_items()
    now = now_iso()
    for it in items:
        if item_id and it.get("id") == item_id:
            it.update(title=title, text=text[:MAX_SECTION], updated_at=now, author=author or it.get("author", ""))
            write_json_atomic(MANUAL_FILE, items)
            return it
    new = {"id": f"G-{max([int(str(i.get('id','G-0')).split('-')[-1]) for i in items] or [0]) + 1}",
           "title": title, "text": text[:MAX_SECTION], "author": author,
           "created_at": now, "updated_at": now}
    items.append(new)
    write_json_atomic(MANUAL_FILE, items)
    return new


def manual_delete(item_id: str) -> bool:
    items = manual_items()
    left = [i for i in items if i.get("id") != item_id]
    if len(left) == len(items):
        return False
    write_json_atomic(MANUAL_FILE, left)
    return True


def _manual_sections() -> list[dict]:
    return [{"doc_title": "직접 작성 지침", "section": i.get("title", ""),
             "text": i.get("text", ""), "url": "", "kind": "manual", "id": i.get("id", "")}
            for i in manual_items() if (i.get("text") or "").strip()]


def sections() -> list[dict]:
    """검색 대상 전체 — 수집본 + 직접 작성.

    직접 작성한 지침을 **앞에 둔다**. 같은 주제가 겹치면 사람이 방금 쓴 규칙이 먼저
    걸려야 한다(동점일 때 순서가 승부를 가른다).
    """
    return _manual_sections() + (load().get("sections") or [])


def stats() -> dict:
    d = load()
    from collections import Counter
    secs = sections()
    return {"updated_at": d.get("updated_at", ""), "sources": d.get("sources", []),
            "docs": len(d.get("docs") or []), "sections": len(secs),
            "manual": len(manual_items()),
            "by_kind": dict(Counter(s.get("kind", "") for s in secs)),
            "errors": d.get("errors") or []}


# --------------------------------------------------------------------------- #
# 검색 — BM25. 임베딩을 쓰지 않는 이유: 지침은 수십~수백 섹션이고, 용어가 그대로
# 등장하는 문서다("환불", "권한 신청"). 무거운 의존성을 하나 더 붙일 값이 없다.
# --------------------------------------------------------------------------- #
_TOKEN_RE = re.compile(r"[0-9A-Za-z가-힣]+")


def _tok(s: str) -> list[str]:
    """토큰화 + 한국어 어간 추출.

    어미를 안 떼면 "환불해 주세요" 가 "환불은 …" 로 시작하는 지침과 한 글자도 겹치지
    않아 **규정이 있는데도 못 찾는다**(실측). 어간 추출기는 voc_agents 가 단일 소스다.
    """
    try:
        from voc_agents import stem_token
    except Exception:                     # 순환/부재 시에도 검색은 돌아야 한다
        def stem_token(t):                # noqa: ANN001
            return t
    out = []
    for t in _TOKEN_RE.findall(s or ""):
        if len(t) < 2:
            continue
        out.append(t.lower())
        st = stem_token(t)
        if st and st.lower() != t.lower() and len(st) >= 2:
            out.append(st.lower())
    return out


def search(query: str, k: int = 3, secs: list[dict] | None = None) -> list[dict]:
    """질의에 맞는 지침 섹션 상위 k개. 점수가 0이면 **버린다** — 아무 지침이나
    끌어다 붙이면 규칙이 아니라 소음이 된다."""
    secs = sections() if secs is None else secs
    q = _tok(query)
    if not secs or not q:
        return []
    corpus = [_tok(f"{s.get('doc_title','')} {s.get('section','')} {s.get('text','')}")
              for s in secs]
    scores: list[float] = []
    try:
        from rank_bm25 import BM25Okapi
        scores = list(BM25Okapi(corpus).get_scores(q))
    except Exception:
        scores = []
    # **문서가 적으면 BM25 는 전부 0 을 준다.** 어떤 단어가 문서의 절반에 나오면 IDF 가
    # 0 이 되기 때문이다 — 지침이 두어 개뿐인 도입 초기가 정확히 그 상황이고, 그때
    # 검색이 조용히 빈손이 되면 "지침을 넣었는데 반영이 안 된다" 가 된다.
    # 그래서 양수 점수가 하나도 없으면 어휘 겹침으로 떨어진다.
    if not scores or max(scores) <= 0:
        # 제목 일치를 본문보다 무겁게 센다. 단순 겹침만 세면 "권한" 이 본문에 스친
        # 환불 섹션과 제목이 '권한 신청 절차' 인 섹션이 동점이 되고, 먼저 나온 쪽이
        # 이긴다 — 그 답변은 규정을 잘못 인용한다.
        qs = set(q)
        scores = []
        for sec, doc in zip(secs, corpus):
            head = set(_tok(f"{sec.get('doc_title','')} {sec.get('section','')}"))
            scores.append(float(len(qs & set(doc)) + 2 * len(qs & head)))
    ranked = sorted(range(len(secs)), key=lambda i: scores[i], reverse=True)[:k]
    return [dict(secs[i], score=round(float(scores[i]), 3)) for i in ranked if scores[i] > 0]


def prompt_block(query: str, k: int = 3, secs: list[dict] | None = None) -> tuple[str, list[dict]]:
    """프롬프트에 넣을 지침 블록과 그 출처. 지침이 없으면 빈 문자열 — 없는 규칙을
    있는 척하지 않는다."""
    hits = search(query, k=k, secs=secs)
    if not hits:
        return "", []
    lines = []
    for h in hits:
        head = f"[{h.get('doc_title','')} — {h.get('section','')}]"
        lines.append(f"{head}\n{h.get('text','')[:1200]}")
    return "\n\n".join(lines), hits
