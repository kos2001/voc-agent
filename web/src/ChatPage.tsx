import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { inputCls } from "./ui";
import { IssueKeyChip, linkifyKeys, useJiraBase } from "./issueLinks";

const API = (import.meta as any).env?.VITE_API ?? "";

type Source = { key: string; summary: string; status: string };
type Guide = { title: string; section: string; url: string };
type Turn = {
  role: "user" | "assistant";
  content: string;
  sources?: Source[];
  guides?: Guide[];
  citations?: string[];
  unsupported?: string[];
  pending?: boolean;
};

const EXAMPLES = [
  "지금 미해결 문의는 몇 건이고 어떤 분류가 많아?",
  "thermal throttle 로 성능이 떨어지는 사례들의 근본원인이 뭐야?",
  "무선 충전 중 NFC 인식 실패는 어떻게 해결했어?",
  "권한 요청은 보통 어떻게 처리했어?",
];

export default function ChatPage({ onOpenIssue, scopeKey }:
  { onOpenIssue: (key: string) => void; scopeKey?: string }) {
  const jiraBase = useJiraBase();
  const [turns, setTurns] = useState<Turn[]>([]);
  const [q, setQ] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => { endRef.current?.scrollIntoView({ behavior: "smooth" }); }, [turns]);

  const ask = async (question: string) => {
    const text = question.trim();
    if (!text || busy) return;
    setErr(""); setQ(""); setBusy(true);
    // 히스토리는 **보내기 전 상태**로 만든다 — 방금 질문을 넣은 뒤 만들면 같은 질문이
    // 대화 이력에도 한 번 더 들어가 모델이 되묻는 답을 낸다.
    const history = turns.filter((t) => !t.pending)
      .map((t) => ({ role: t.role, content: t.content }));
    setTurns((prev) => [...prev,
      { role: "user", content: text },
      { role: "assistant", content: "", pending: true }]);

    const patchLast = (patch: Partial<Turn>) =>
      setTurns((prev) => prev.map((t, i) => (i === prev.length - 1 ? { ...t, ...patch } : t)));

    try {
      const res = await fetch(`${API}/chat/stream`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question: text, history, key: scopeKey || "" }),
      });
      if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`);
      const reader = res.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        const parts = buf.split("\n\n");
        buf = parts.pop() ?? "";
        for (const part of parts) {
          const line = part.split("\n").find((l) => l.startsWith("data: "));
          if (!line) continue;
          const d = JSON.parse(line.slice(6));
          if (d.type === "sources") patchLast({ sources: d.sources ?? [], guides: d.guides ?? [] });
          else if (d.type === "delta") {
            setTurns((prev) => prev.map((t, i) =>
              i === prev.length - 1 ? { ...t, content: t.content + d.text } : t));
          } else if (d.type === "done") {
            patchLast({ pending: false, citations: d.citations ?? [],
                        unsupported: d.unsupported_mentions ?? [] });
          } else if (d.type === "error") {
            patchLast({ pending: false, content: `_(오류: ${d.message})_` });
          }
        }
      }
      patchLast({ pending: false });
    } catch (e: any) {
      setErr(e.message || "요청 실패");
      setTurns((prev) => prev.slice(0, -1));
    } finally { setBusy(false); }
  };

  return (
    <main className="flex h-full flex-col">
      <header className="shrink-0 border-b border-zinc-800 px-6 py-4 sm:px-8">
        <h1 className="text-lg font-semibold tracking-tight text-zinc-50">
          이슈 질문{scopeKey ? <span className="ml-2 font-mono text-sm text-sky-300">{scopeKey}</span> : null}
        </h1>
        <p className="mt-1 text-xs text-zinc-400">
          Jira 이슈 내용을 자연어로 묻습니다. 답변은 <b>찾아온 이슈 발췌</b>에서만 나오고,
          건수는 전수 집계로 답합니다. 근거 이슈 번호를 눌러 원본을 열 수 있습니다.
        </p>
      </header>

      <div className="min-h-0 flex-1 overflow-y-auto px-6 py-5 sm:px-8">
        <div className="mx-auto max-w-3xl space-y-4">
          {turns.length === 0 && (
            <div className="rounded-xl border border-zinc-800 bg-zinc-900/40 p-6">
              <div className="text-sm text-zinc-300">무엇이든 물어보세요. 예를 들면:</div>
              <div className="mt-3 flex flex-wrap gap-2">
                {EXAMPLES.map((e) => (
                  <button key={e} onClick={() => ask(e)}
                    className="rounded-lg border border-zinc-700 px-3 py-1.5 text-[13px] text-zinc-300 transition hover:border-sky-600 hover:text-sky-300">
                    {e}
                  </button>
                ))}
              </div>
            </div>
          )}

          {turns.map((t, i) => t.role === "user" ? (
            <div key={i} className="flex justify-end">
              <div className="max-w-[85%] rounded-2xl rounded-br-sm bg-sky-500/15 px-4 py-2 text-sm text-sky-100">
                {t.content}
              </div>
            </div>
          ) : (
            <div key={i} className="rounded-2xl rounded-bl-sm border border-zinc-800 bg-zinc-900/60 px-4 py-3">
              {(t.sources?.length ?? 0) > 0 && (
                <div className="mb-2 flex flex-wrap items-center gap-1.5 border-b border-zinc-800 pb-2">
                  <span className="text-[11px] text-zinc-500">찾아본 이슈</span>
                  {t.sources!.map((s) => (
                    <span key={s.key} title={`${s.summary} (${s.status})`}>
                      <IssueKeyChip base={jiraBase} issueKey={s.key} onOpenInApp={onOpenIssue} />
                    </span>
                  ))}
                </div>
              )}
              {(t.guides?.length ?? 0) > 0 && (
                <div className="mb-2 flex flex-wrap items-center gap-1.5">
                  <span className="text-[11px] text-zinc-500">참조한 지침</span>
                  {t.guides!.map((g, gi) => (
                    g.url ? (
                      <a key={gi} href={g.url} target="_blank" rel="noreferrer" title={g.title}
                        className="rounded border border-zinc-700 px-1.5 py-0.5 text-[11px] text-sky-400 hover:border-sky-600">
                        📘 {g.section || g.title} ↗
                      </a>
                    ) : (
                      <span key={gi} title={g.title}
                        className="rounded border border-zinc-700 px-1.5 py-0.5 text-[11px] text-zinc-300">
                        📘 {g.section || g.title}
                      </span>
                    )
                  ))}
                </div>
              )}
              {t.content ? (
                <div className="prose prose-sm prose-invert max-w-none prose-headings:text-sky-300 prose-headings:my-2 prose-p:my-1 prose-a:text-sky-400">
                  <ReactMarkdown remarkPlugins={[remarkGfm]}
                    components={{ a: ({ node, ...p }) => <a {...p} target="_blank" rel="noreferrer" /> }}>
                    {linkifyKeys(t.content, jiraBase)}
                  </ReactMarkdown>
                </div>
              ) : (
                <div className="text-sm text-zinc-500">답변 생성 중…</div>
              )}
              {(t.unsupported?.length ?? 0) > 0 && (
                // 근거에 없는 번호가 나오면 사용자는 그걸 찾으러 간다. 조용히 두면 안 된다.
                <div className="mt-2 text-[11px] text-red-400">
                  ⚠ 근거에 없는 이슈 번호가 답변에 있습니다: {t.unsupported!.join(", ")} — 신뢰하지 마세요.
                </div>
              )}
            </div>
          ))}
          <div ref={endRef} />
        </div>
      </div>

      {err && <div className="px-6 pb-2 text-xs text-red-400 sm:px-8">⚠ {err}</div>}

      <form onSubmit={(e) => { e.preventDefault(); ask(q); }}
        className="shrink-0 border-t border-zinc-800 px-6 py-4 sm:px-8">
        <div className="mx-auto flex max-w-3xl gap-2">
          <input value={q} onChange={(e) => setQ(e.target.value)} disabled={busy}
            placeholder={busy ? "답변 생성 중…" : "이슈에 대해 질문하세요"}
            className={`${inputCls} flex-1`} />
          <button type="submit" disabled={busy || !q.trim()}
            className="shrink-0 rounded-lg border border-sky-500/50 bg-sky-500/10 px-4 py-2 text-sm font-medium text-sky-300 transition hover:bg-sky-500/20 disabled:opacity-40">
            질문
          </button>
          {turns.length > 0 && (
            <button type="button" onClick={() => setTurns([])} disabled={busy}
              className="shrink-0 rounded-lg border border-zinc-700 px-3 py-2 text-sm text-zinc-400 transition hover:bg-zinc-800 disabled:opacity-40">
              새 대화
            </button>
          )}
        </div>
      </form>
    </main>
  );
}
