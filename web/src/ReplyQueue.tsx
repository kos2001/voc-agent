import { useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

const API = (import.meta as any).env?.VITE_API ?? "";   // 빈 값 = 같은 오리진(개발은 vite 프록시)

type Violation = { code: string; severity: "block" | "warn"; detail: string };
type Policy = { ok: boolean; blocked: boolean; violations: Violation[] };
type RItem = {
  key: string; summary: string; status: string; body: string;
  intent: string; intent_label: string; asks: string[];
  engine: string; evidence: string[]; has_evidence: boolean;
  policy: Policy; needs_review: boolean; created_at: string; state: string;
  forbidden?: string[]; lang?: string;
  history?: { body: string; sent_at?: string; comment_id?: string }[];
  comment_id?: string;
};

// 차단(block)과 경고(warn)를 색으로 분리한다 — 같은 톤으로 그리면 검토자가
// "고쳐야 나가는 것"과 "판단해서 넘겨도 되는 것"을 구분하지 못한다.
const SEV_STYLE: Record<string, string> = {
  block: "border-rose-700 bg-rose-500/10 text-rose-300",
  warn: "border-amber-700 bg-amber-500/10 text-amber-300",
};

export default function ReplyQueue({ onBack, onChange }:
  { onBack: () => void; onChange?: () => void }) {
  const [items, setItems] = useState<RItem[]>([]);
  const [stats, setStats] = useState<any>(null);
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [tab, setTab] = useState<Record<string, "edit" | "preview">>({});
  const [live, setLive] = useState<Record<string, Policy>>({});   // 편집 중 재검사 결과
  const [busy, setBusy] = useState("");
  const [msg, setMsg] = useState<{ key: string; ok: boolean; text: string } | null>(null);
  const [loadErr, setLoadErr] = useState("");

  const load = () => {
    setLoadErr("");
    fetch(`${API}/voc/reply/pending`)
      .then((r) => (r.ok ? r.json() : r.json().catch(() => ({})).then((d) =>
        Promise.reject(new Error(d.detail || `HTTP ${r.status}`)))))
      .then((d) => setItems(d.items ?? []))
      .catch((e) => setLoadErr(e.message || "불러오기 실패"));
    fetch(`${API}/voc/reply/stats`).then((r) => (r.ok ? r.json() : null))
      .then((d) => d && setStats(d)).catch(() => {});
  };
  useEffect(() => { load(); }, []);

  const bodyOf = (it: RItem) => edits[it.key] ?? it.body;
  const isEdited = (it: RItem) => (it.key in edits) && edits[it.key].trim() !== it.body.trim();
  const policyOf = (it: RItem): Policy => live[it.key] ?? it.policy;

  // 편집한 본문은 저장된 검사 결과와 다르다 — 발송 버튼이 옛 판정으로 열려 있으면
  // 사람이 방금 넣은 위반을 보지 못한 채 보낸다. 편집 시 즉시 다시 검사한다.
  const recheck = async (it: RItem, text: string) => {
    try {
      const d = await fetch(`${API}/voc/reply/check`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key: it.key, body: text, has_evidence: it.has_evidence }),
      }).then((r) => r.json());
      setLive((v) => ({ ...v, [it.key]: d }));
    } catch { /* 검사 실패는 발송 버튼을 열어주지 않는다 — 이전 판정을 유지한다 */ }
  };

  const act = async (it: RItem, action: "send" | "reject") => {
    setBusy(it.key + action); setMsg(null);
    try {
      const payload = action === "send"
        ? { key: it.key, body: isEdited(it) ? bodyOf(it) : undefined }
        : { key: it.key, reason: "" };
      const d = await fetch(`${API}/voc/reply/${action}`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      }).then((r) => r.json());
      if (d.ok) {
        setMsg({ key: it.key, ok: true, text: action === "send" ? "고객에게 발송했습니다." : "거부했습니다." });
        load(); onChange?.();
      } else {
        setMsg({ key: it.key, ok: false, text: d.error || "실패" });
        if (d.policy) setLive((v) => ({ ...v, [it.key]: d.policy }));
      }
    } catch (e: any) {
      setMsg({ key: it.key, ok: false, text: e.message });
    } finally { setBusy(""); }
  };

  return (
    <main className="h-full overflow-auto px-6 py-5">
      <div className="mx-auto max-w-4xl">
        <div className="mb-4 flex items-center gap-3">
          <button onClick={onBack} className="text-sm text-zinc-400 hover:text-sky-400">← 돌아가기</button>
          <h1 className="text-lg font-semibold text-zinc-50">고객 답변 발송 대기</h1>
          <span className="text-[11px] text-zinc-500">승인 전에는 고객에게 아무것도 나가지 않습니다</span>
        </div>

        {stats && (
          <div className="mb-5 grid grid-cols-4 gap-3">
            {[["무수정 발송", stats.clean_rate === null ? "—" : `${Math.round(stats.clean_rate * 100)}%`],
              ["발송률", stats.send_rate === null ? "—" : `${Math.round(stats.send_rate * 100)}%`],
              ["대기", String(stats.pending)],
              ["근거 없이 작성", String(stats.no_evidence)]].map(([label, v]) => (
              <div key={label} className="rounded-xl border border-zinc-800 bg-zinc-900/60 p-3">
                <div className="text-[11px] text-zinc-500">{label}</div>
                <div className="mt-0.5 text-xl font-semibold text-zinc-100">{v}</div>
              </div>
            ))}
          </div>
        )}

        {loadErr && (
          <div className="mb-4 rounded-lg border border-rose-800 bg-rose-500/10 px-3 py-2 text-sm text-rose-300">
            불러오기 실패: {loadErr}
          </div>
        )}
        {!loadErr && items.length === 0 && (
          <div className="rounded-xl border border-zinc-800 bg-zinc-900/40 p-8 text-center text-sm text-zinc-400">
            발송 대기 중인 답변이 없습니다. 분석 화면에서 “📮 고객 답변 초안”을 만들어 보세요.
          </div>
        )}

        <div className="space-y-4">
          {items.map((it) => {
            const pol = policyOf(it);
            const t = tab[it.key] ?? "edit";
            return (
              <section key={it.key} className="rounded-xl border border-zinc-800 bg-zinc-900/60 p-4">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-mono text-sm text-sky-300">{it.key}</span>
                  <span className="text-sm text-zinc-200">{it.summary}</span>
                  <span className="rounded-full border border-zinc-700 px-2 py-0.5 text-[11px] text-zinc-300">
                    {it.intent_label}
                  </span>
                  <span className="rounded-full border border-zinc-700 px-2 py-0.5 text-[11px] text-zinc-400">
                    {it.engine === "llm" ? "AI 생성" : "템플릿"}
                  </span>
                  {it.lang && it.lang !== "ko" && (
                    <span className="rounded-full border border-sky-800 px-2 py-0.5 text-[11px] text-sky-300"
                      title="고객이 쓴 언어로 답합니다">
                      {it.lang.toUpperCase()}
                    </span>
                  )}
                  {it.needs_review && (
                    <span className="rounded-full border border-zinc-700 px-2 py-0.5 text-[11px] text-zinc-400"
                      title="근거 없음·정책 위반·교환/불만 유형 — 사람이 반드시 확인해야 하는 초안">
                      검토 필요
                    </span>
                  )}
                  {!it.has_evidence && (
                    <span className="rounded-full border border-amber-700 px-2 py-0.5 text-[11px] text-amber-300"
                      title="유사 사례를 찾지 못했습니다 — 원인을 단정하지 않는 골격으로 작성됐습니다">
                      근거 없음
                    </span>
                  )}
                </div>

                {(it.history?.length ?? 0) > 0 && (
                  <div className="mt-2 text-[11px] text-zinc-500">
                    이 문의에는 이미 {it.history!.length}건을 발송했습니다 — 이 초안은 후속 답변입니다.
                  </div>
                )}

                {it.asks.length > 0 && (
                  <div className="mt-2 rounded-lg border border-zinc-800 bg-zinc-950/60 p-2">
                    <div className="text-[11px] text-zinc-500">고객이 요청한 것 — 답변이 이걸 빠뜨리면 안 됩니다</div>
                    <ul className="mt-1 space-y-0.5 text-[13px] text-zinc-300">
                      {it.asks.map((a, i) => <li key={i}>· {a}</li>)}
                    </ul>
                  </div>
                )}

                {pol.violations.length > 0 && (
                  <div className="mt-2 space-y-1">
                    {pol.violations.map((v, i) => (
                      <div key={i} className={`rounded-lg border px-2 py-1 text-[12px] ${SEV_STYLE[v.severity]}`}>
                        <span className="font-medium">{v.severity === "block" ? "차단" : "경고"}</span>
                        <span className="ml-2 opacity-70">{v.code}</span>
                        <span className="ml-2">{v.detail}</span>
                      </div>
                    ))}
                  </div>
                )}

                <div className="mt-3 flex items-center gap-2">
                  {(["edit", "preview"] as const).map((k) => (
                    <button key={k} onClick={() => setTab((s) => ({ ...s, [it.key]: k }))}
                      className={`rounded-md px-2 py-1 text-[12px] ${t === k
                        ? "bg-zinc-800 text-zinc-100" : "text-zinc-400 hover:text-zinc-200"}`}>
                      {k === "edit" ? "편집" : "고객이 보는 모습"}
                    </button>
                  ))}
                  {isEdited(it) && <span className="text-[11px] text-amber-400">수정됨</span>}
                </div>

                {t === "edit" ? (
                  <textarea
                    value={bodyOf(it)}
                    onChange={(e) => {
                      const v = e.target.value;
                      setEdits((s) => ({ ...s, [it.key]: v }));
                      recheck(it, v);
                    }}
                    rows={14}
                    className="mt-2 w-full rounded-lg border border-zinc-700 bg-zinc-950 p-3 font-mono text-[13px] text-zinc-200 outline-none focus:border-sky-600" />
                ) : (
                  <div className="mt-2 rounded-lg border border-zinc-700 bg-zinc-950 p-4 prose prose-sm prose-invert max-w-none">
                    <ReactMarkdown remarkPlugins={[remarkGfm]}>{bodyOf(it)}</ReactMarkdown>
                  </div>
                )}

                <div className="mt-3 flex items-center gap-2">
                  <button onClick={() => act(it, "send")}
                    disabled={pol.blocked || busy === it.key + "send"}
                    title={pol.blocked ? "정책 차단이 남아 있어 발송할 수 없습니다 — 본문을 고치세요"
                                       : "고객에게 발송합니다 (Jira 댓글로 게시)"}
                    className="rounded-lg border border-emerald-600/60 bg-emerald-500/10 px-4 py-2 text-sm font-medium text-emerald-300 transition hover:bg-emerald-500/20 disabled:cursor-not-allowed disabled:opacity-40">
                    {busy === it.key + "send" ? "발송 중…" : "📮 고객에게 발송"}
                  </button>
                  <button onClick={() => act(it, "reject")} disabled={busy === it.key + "reject"}
                    className="rounded-lg border border-zinc-700 px-4 py-2 text-sm text-zinc-300 transition hover:bg-zinc-800 disabled:opacity-40">
                    거부
                  </button>
                  {msg?.key === it.key && (
                    <span className={`text-[12px] ${msg.ok ? "text-emerald-400" : "text-rose-400"}`}>{msg.text}</span>
                  )}
                </div>
              </section>
            );
          })}
        </div>
      </div>
    </main>
  );
}
