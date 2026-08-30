import { useEffect, useState } from "react";
import { inputCls } from "./ui";
import { getJson, postJson } from "./api";

const API = (import.meta as any).env?.VITE_API ?? "";

type Stats = {
  updated_at: string; sources: string[]; docs: number; sections: number;
  manual: number; by_kind: Record<string, number>;
  errors: { source: string; error: string }[]; confluence_base?: string;
};
type Manual = { id: string; title: string; text: string; author?: string; updated_at?: string };
type Hit = { doc_title: string; section: string; text: string; url: string; score: number };

const PLACEHOLDER = `confluence:123456
confluence-space:SUPPORT
https://wiki.example.com/faq
file:data/knowledge.md`;

export default function GuidesPage({ canWrite }: { canWrite: boolean }) {
  const [stats, setStats] = useState<Stats | null>(null);
  const [sources, setSources] = useState("");
  const [manual, setManual] = useState<Manual[]>([]);
  const [title, setTitle] = useState("");
  const [text, setText] = useState("");
  const [editId, setEditId] = useState("");
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<Hit[] | null>(null);
  const [busy, setBusy] = useState("");
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  const load = async () => {
    try {
      // 응답이 비어 있어도 화면이 터지지 않게 한다 — 원인은 api.ts 가 말해 준다.
      const s = await getJson<Stats>("/guides");
      setStats(s ?? null);
      setSources((s?.sources ?? []).join("\n"));
      const m = await getJson<{ items: Manual[] }>("/guides/manual");
      setManual(m?.items ?? []);
    } catch (e: any) { setMsg({ ok: false, text: e.message }); }
  };
  useEffect(() => { load(); }, []);

  const run = async (key: string, fn: () => Promise<any>, okText: string) => {
    setBusy(key); setMsg(null);
    try {
      const d = await fn();
      if (d?.ok === false) setMsg({ ok: false, text: d.error || "실패" });
      else setMsg({ ok: true, text: d?.reason || okText });
      await load();
    } catch (e: any) { setMsg({ ok: false, text: e.message }); }
    finally { setBusy(""); }
  };

  const saveSources = () => run("src",
    () => postJson("/guides/sources", { sources: sources.split("\n"), sync: true }),
    "원천을 저장하고 수집했습니다.");
  const sync = () => run("sync", () => postJson("/guides/sync", {}), "수집했습니다.");
  const saveManual = () => run("man",
    () => postJson("/guides/manual", { id: editId, title, text }), "지침을 저장했습니다.")
    .then(() => { setTitle(""); setText(""); setEditId(""); });
  const del = (id: string) => run("del" + id,
    () => fetch(`${API}/guides/manual/${id}`, { method: "DELETE" }).then((r) => r.json()),
    "삭제했습니다.");

  const preview = async () => {
    if (!q.trim()) return;
    setBusy("q");
    try {
      const d = await getJson<{ hits: Hit[] }>(`/guides/search?q=${encodeURIComponent(q)}&k=5`);
      setHits(d.hits ?? []);
    } catch (e: any) { setMsg({ ok: false, text: e.message }); }
    finally { setBusy(""); }
  };

  return (
    <main className="h-full overflow-y-auto px-6 py-6 sm:px-8">
      <div className="mx-auto max-w-4xl space-y-5">
        <div>
          <h1 className="text-lg font-semibold tracking-tight text-zinc-50">📘 답변 지침</h1>
          <p className="mt-1 text-xs text-zinc-400">
            답변이 따라야 할 규칙입니다. 과거 사례와 어긋나면 <b>지침이 우선</b>합니다 —
            규정이 바뀌었는데 옛 사례대로 답하면 그건 틀린 답이 아니라 사고입니다.
          </p>
        </div>

        {msg && (
          <div className={`rounded-lg border px-3 py-2 text-sm ${msg.ok
            ? "border-emerald-800 bg-emerald-950/30 text-emerald-300"
            : "border-rose-800 bg-rose-950/30 text-rose-300"}`}>{msg.text}</div>
        )}

        {stats && (
          <div className="grid grid-cols-4 gap-3">
            {[["문서", stats.docs], ["섹션", stats.sections], ["직접 작성", stats.manual],
              ["수집 실패", stats.errors?.length ?? 0]].map(([k, v]) => (
              <div key={k as string} className="rounded-xl border border-zinc-800 bg-zinc-900/60 p-3">
                <div className="text-[11px] text-zinc-500">{k as string}</div>
                <div className={`mt-0.5 text-xl font-semibold ${
                  k === "수집 실패" && (v as number) > 0 ? "text-red-400" : "text-zinc-100"}`}>{v as number}</div>
              </div>
            ))}
          </div>
        )}

        {/* 1) 직접 작성 — 위키가 없어도 여기서 바로 규칙을 넣을 수 있다. 가장 쉬운 경로를 위에 둔다. */}
        <section className="rounded-xl border border-zinc-800 bg-zinc-900/60 p-5">
          <h2 className="text-sm font-semibold text-zinc-100">직접 작성</h2>
          <p className="mt-1 text-[11px] text-zinc-400">
            위키에 없는 규칙을 여기에 적습니다. 저장 즉시 다음 답변부터 적용됩니다.
          </p>
          {canWrite && (
            <div className="mt-3 space-y-2">
              <input value={title} onChange={(e) => setTitle(e.target.value)}
                placeholder="제목 (예: 교환·환불 문의 응대)" className={inputCls} />
              <textarea value={text} onChange={(e) => setText(e.target.value)} rows={5}
                placeholder="규칙을 씁니다. 예) 환불 승인 권한은 CS 운영팀에 있습니다. 담당자는 승인 여부를 말하지 않고 접수 절차와 필요 정보만 안내합니다."
                className="w-full rounded-lg border border-zinc-700 bg-zinc-950 p-3 text-sm text-zinc-200 outline-none focus:border-sky-600" />
              <div className="flex items-center gap-2">
                <button onClick={saveManual} disabled={busy === "man" || !title.trim() || !text.trim()}
                  className="rounded-lg border border-sky-500/50 bg-sky-500/10 px-4 py-2 text-sm font-medium text-sky-300 transition hover:bg-sky-500/20 disabled:opacity-40">
                  {busy === "man" ? "저장 중…" : editId ? "수정 저장" : "지침 추가"}
                </button>
                {editId && (
                  <button onClick={() => { setEditId(""); setTitle(""); setText(""); }}
                    className="text-[12px] text-zinc-400 hover:text-sky-400">취소</button>
                )}
              </div>
            </div>
          )}
          <div className="mt-4 space-y-2">
            {manual.length === 0 && <div className="text-[12px] text-zinc-500">아직 작성한 지침이 없습니다.</div>}
            {manual.map((m) => (
              <div key={m.id} className="rounded-lg border border-zinc-800 bg-zinc-950/60 p-3">
                <div className="flex items-center gap-2">
                  <span className="text-sm font-medium text-zinc-200">{m.title}</span>
                  <span className="font-mono text-[10px] text-zinc-600">{m.id}</span>
                  {canWrite && (
                    <span className="ml-auto flex gap-2">
                      <button onClick={() => { setEditId(m.id); setTitle(m.title); setText(m.text); }}
                        className="text-[11px] text-zinc-400 hover:text-sky-400">수정</button>
                      <button onClick={() => del(m.id)} disabled={busy === "del" + m.id}
                        className="text-[11px] text-zinc-400 hover:text-red-400">삭제</button>
                    </span>
                  )}
                </div>
                <div className="mt-1 whitespace-pre-wrap text-[13px] text-zinc-400">{m.text}</div>
              </div>
            ))}
          </div>
        </section>

        {/* 2) 외부 원천 */}
        <section className="rounded-xl border border-zinc-800 bg-zinc-900/60 p-5">
          <h2 className="text-sm font-semibold text-zinc-100">Confluence · FAQ · 문서 연결</h2>
          <p className="mt-1 text-[11px] text-zinc-400">
            한 줄에 하나씩. Confluence 인증은 Jira 자격증명을 그대로 씁니다
            {stats?.confluence_base ? ` (base: ${stats.confluence_base})` : ""}.
          </p>
          <textarea value={sources} onChange={(e) => setSources(e.target.value)} rows={5}
            disabled={!canWrite} placeholder={PLACEHOLDER}
            className="mt-3 w-full rounded-lg border border-zinc-700 bg-zinc-950 p-3 font-mono text-[13px] text-zinc-200 outline-none focus:border-sky-600 disabled:opacity-60" />
          {canWrite && (
            <div className="mt-2 flex items-center gap-2">
              <button onClick={saveSources} disabled={busy === "src"}
                className="rounded-lg border border-sky-500/50 bg-sky-500/10 px-4 py-2 text-sm font-medium text-sky-300 transition hover:bg-sky-500/20 disabled:opacity-40">
                {busy === "src" ? "저장·수집 중…" : "저장하고 지금 수집"}
              </button>
              <button onClick={sync} disabled={busy === "sync"}
                className="rounded-lg border border-zinc-700 px-3 py-2 text-sm text-zinc-300 transition hover:bg-zinc-800 disabled:opacity-40">
                {busy === "sync" ? "수집 중…" : "다시 수집"}
              </button>
              <span className="text-[11px] text-zinc-500">
                마지막 수집 {stats?.updated_at || "—"}
              </span>
            </div>
          )}
          {(stats?.errors ?? []).map((e, i) => (
            // 수집 실패를 조용히 넘기지 않는다 — 위키 한 장이 막혔는데 모르면
            // 답변은 그 규칙 없이 나간다.
            <div key={i} className="mt-2 text-[11px] text-red-400">⚠ {e.source}: {e.error}</div>
          ))}
        </section>

        {/* 3) 미리보기 — 넣은 지침이 실제로 걸리는지 확인하는 자리 */}
        <section className="rounded-xl border border-zinc-800 bg-zinc-900/60 p-5">
          <h2 className="text-sm font-semibold text-zinc-100">어떤 지침이 걸리나</h2>
          <p className="mt-1 text-[11px] text-zinc-400">
            실제 문의 문장을 넣어 보세요. 여기서 안 걸리면 답변에도 반영되지 않습니다.
          </p>
          <div className="mt-3 flex gap-2">
            <input value={q} onChange={(e) => setQ(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter") preview(); }}
              placeholder="예: 환불해 주세요 / 스테이징 권한 주세요"
              className={`${inputCls} flex-1`} />
            <button onClick={preview} disabled={busy === "q" || !q.trim()}
              className="rounded-lg border border-zinc-700 px-3 py-2 text-sm text-zinc-300 transition hover:bg-zinc-800 disabled:opacity-40">
              확인
            </button>
          </div>
          {hits && (
            <div className="mt-3 space-y-2">
              {hits.length === 0 && (
                <div className="text-[12px] text-amber-400">
                  걸리는 지침이 없습니다 — 이 문의는 사례만 보고 답합니다.
                </div>
              )}
              {hits.map((h, i) => (
                <div key={i} className="rounded-lg border border-zinc-800 bg-zinc-950/60 p-3">
                  <div className="flex items-center gap-2 text-[12px]">
                    <span className="text-sky-300">📘 {h.section || h.doc_title}</span>
                    <span className="text-zinc-500">{h.doc_title}</span>
                    <span className="ml-auto font-mono text-[10px] text-zinc-500">{h.score}</span>
                    {h.url && (
                      <a href={h.url} target="_blank" rel="noreferrer"
                        className="text-[11px] text-sky-400 hover:underline">원문 ↗</a>
                    )}
                  </div>
                  <div className="mt-1 text-[12px] text-zinc-400">{h.text.slice(0, 200)}…</div>
                </div>
              ))}
            </div>
          )}
        </section>
      </div>
    </main>
  );
}
