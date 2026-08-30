import { useEffect, useState } from "react";

const API = (import.meta as any).env ?.VITE_API ?? "";

/** Jira base_url — /config/status 가 정본이다. 하드코딩하면 배포마다 링크가 깨진다. */
export function useJiraBase(): string {
  const [base, setBase] = useState("");
  useEffect(() => {
    fetch(`${API}/config/status`).then((r) => (r.ok ? r.json() : null))
      .then((d) => d && setBase(d.jira?.base_url ?? "")).catch(() => {});
  }, []);
  return base;
}

export const browseUrl = (base: string, key: string) =>
  `${(base || "").replace(/\/$/, "")}/browse/${encodeURIComponent(key)}`;

// 이슈 키 패턴은 서버(src/issue_keys.py)와 같은 모양이다: PROJ-123.
// 하이픈 뒤가 숫자로만 이뤄져야 하므로 PM9C3-NVMe·HS-G4 같은 도메인 토큰은 걸리지 않는다.
//
// 앞에 올 수 없는 것: 단어문자·하이픈(토큰 중간), `[`(이미 링크 텍스트),
// `](`(링크 대상), `/`(URL 경로 안).
// **여는 괄호는 막지 않는다** — 우리 인용 형식이 바로 `(LSI-7)` 이라, 괄호를 막으면
// 정작 링크가 가장 필요한 자리가 통째로 빠진다(실제로 그렇게 빠져 있었다).
const KEY_RE = /(?<![\w\-[/])(?<!\]\()([A-Z][A-Z0-9]*-\d+)(?![\w\-])/g;

/**
 * 마크다운 본문의 이슈 키를 Jira 원본 링크로 바꾼다.
 *
 * 코드 블록·인라인 코드는 건드리지 않는다 — 로그 발췌 안의 문자열을 링크로 만들면
 * 그 줄을 복사해 붙일 때 마크다운이 섞여 들어간다. 이미 링크인 것(`[KEY](...)`)도
 * 다시 감싸지 않는다(정규식의 lookbehind 가 `[` 와 `(` 를 막는다).
 */
export function linkifyKeys(md: string, base: string): string {
  if (!md || !base) return md || "";
  return (md.split(/(```[\s\S]*?```|`[^`\n]*`)/g)
    .map((seg, i) => (i % 2 === 1 ? seg
      : seg.replace(KEY_RE, (_m, k) => `[${k}](${browseUrl(base, k)})`)))
    .join(""));
}

/** 이슈 키 칩 — 누르면 Jira 원본이 새 탭으로 열린다. base 가 없으면 평문으로 둔다. */
export function IssueKeyChip({ base, issueKey, onOpenInApp, className = "" }: {
  base: string; issueKey: string; onOpenInApp?: (key: string) => void; className?: string;
}) {
  const cls = `rounded border border-zinc-700 bg-zinc-950 px-1.5 py-0.5 font-mono text-[11px] ${className}`;
  if (!base) {
    return onOpenInApp
      ? <button onClick={() => onOpenInApp(issueKey)} className={`${cls} text-sky-400 hover:border-sky-600`}>{issueKey}</button>
      : <span className={`${cls} text-zinc-300`}>{issueKey}</span>;
  }
  // 키를 누르면 **Jira 원문**이 열린다. 앱 안에서 열기는 옆의 작은 버튼으로 둔다 —
  // 사용자가 이슈 번호를 클릭할 때 기대하는 것은 원문이다.
  return (
    <span className="inline-flex items-center gap-1">
      <a href={browseUrl(base, issueKey)} target="_blank" rel="noreferrer"
        title={`Jira에서 ${issueKey} 원문 열기 (새 탭)`}
        className={`${cls} text-sky-400 hover:border-sky-600`}>{issueKey} ↗</a>
      {onOpenInApp && (
        <button onClick={() => onOpenInApp(issueKey)} title="이 화면에서 열기"
          className="rounded border border-zinc-700 px-1 text-[10px] text-zinc-400 hover:border-sky-600 hover:text-sky-400">
          여기서
        </button>
      )}
    </span>
  );
}
