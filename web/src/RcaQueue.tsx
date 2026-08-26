import { useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import { inputCls } from "./ui";
import remarkGfm from "remark-gfm";

const API = (import.meta as any).env?.VITE_API ?? "";   // 빈 값 = 같은 오리진(개발은 vite 프록시)

type QItem = {
  key: string; summary: string; status: string; body: string;
  confidence: number | null; based_on_verified: boolean; needs_review: boolean;
  based_on: string; created_at: string; state: string; comment_id?: string; source?: string;
};

type Cause = { code: string; label: string; lever: string; hint: string };

// 레버별 색 — 사람이 "이건 어느 쪽 문제인가"를 라벨 고르는 순간에 인지하게 한다.
const LEVER_STYLE: Record<string, string> = {
  retrieval: "border-amber-700 text-amber-300",
  generation: "border-sky-700 text-sky-300",
  knowledge: "border-violet-700 text-violet-300",
  presentation: "border-zinc-600 text-zinc-300",
  other: "border-zinc-700 text-zinc-400",
};

export default function RcaQueue({ onBack, onChange }: { onBack: () => void; onChange?: () => void }) {
  const [items, setItems] = useState<QItem[]>([]);
  const [busy, setBusy] = useState<string>("");
  const [msg, setMsg] = useState<{ key: string; ok: boolean; text: string } | null>(null);
  const [edits, setEdits] = useState<Record<string, string>>({});   // key → 수정 본문
  const [editing, setEditing] = useState<string>("");                // 본문 패널 열린 key
  const [panelTab, setPanelTab] = useState<Record<string, "edit" | "preview">>({}); // 편집/미리보기
  const [valid, setValid] = useState<Record<string, any>>({});       // key → 검증 결과
  const [validating, setValidating] = useState<string>("");
  // 원인 라벨 — 서버의 닫힌 목록(draft_feedback.CAUSES)을 받아 쓴다. 프런트에 하드코딩하면
  // 두 곳이 갈라지고, 갈라진 코드로 저장된 원인은 집계에서 조용히 'other' 로 접힌다.
  const [causes, setCauses] = useState<Cause[]>([]);
  const [picked, setPicked] = useState<Record<string, string[]>>({});   // key → 원인 코드
  const [note, setNote] = useState<Record<string, string>>({});
  const [labeling, setLabeling] = useState<string>("");                 // 라벨 패널 열린 key

  // 실패를 삼키면 items=[] 가 되어 "대기 중인 초안이 없습니다" 로 보인다 —
  // 승인 대기 건이 있는데도 없는 것처럼 보이는 것이 가장 나쁜 오표시다.
  const [loadErr, setLoadErr] = useState("");
  const load = () => {
    setLoadErr("");
    fetch(`${API}/rca/pending`)
      .then((r) => (r.ok ? r.json() : r.json().catch(() => ({})).then((d) =>
        Promise.reject(new Error(d.detail || `HTTP ${r.status}`)))))
      .then((d) => setItems(d.items ?? []))
      .catch((e) => setLoadErr(e.message || "불러오기 실패"));
  };
  useEffect(() => { load(); }, []);
  useEffect(() => {
    fetch(`${API}/rca/draft-feedback`).then((r) => (r.ok ? r.json() : null))
      .then((d) => d && setCauses(d.taxonomy ?? [])).catch(() => {});
  }, []);

  const toggleCause = (key: string, code: string) =>
    setPicked((p) => {
      const cur = p[key] ?? [];
      return { ...p, [key]: cur.includes(code) ? cur.filter((c) => c !== code) : [...cur, code] };
    });

  const bodyOf = (it: QItem) => (edits[it.key] ?? it.body);
  const isEdited = (it: QItem) => (it.key in edits) && edits[it.key].trim() !== it.body.trim();

  const validate = async (it: QItem) => {
    setValidating(it.key);
    try {
      const d = await fetch(`${API}/rca/validate`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key: it.key, body: bodyOf(it) }),
      }).then((r) => r.json());
      setValid((v) => ({ ...v, [it.key]: d }));
    } finally { setValidating(""); }
  };

  const act = async (it: QItem, action: "approve" | "reject") => {
    const key = it.key;
    setBusy(key + action); setMsg(null);
    try {
      const payload: any = { key, causes: picked[key] ?? [], note: note[key] ?? "" };
      if (action === "approve" && isEdited(it)) payload.body = edits[key];  // 수정본 게시
      const d = await fetch(`${API}/rca/${action}`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
      }).then((r) => r.json());
      if (action === "approve") {
        setMsg(d.ok ? { key, ok: true, text: `Jira 게시 완료${d.edited ? " (수정본, 메모리 저장됨)" : ""}${d.item?.comment_id ? ` · 댓글 #${d.item.comment_id}` : ""}` }
                    : { key, ok: false, text: `게시 실패: ${d.error || ""}` });
      } else {
        const n = (picked[key] ?? []).length;
        setMsg({ key, ok: true, text: n ? `거부됨 (게시 안 함) · 사유 ${n}건 기록` : "거부됨 (게시 안 함) — 사유 미기록" });
      }
      await load(); onChange?.();
    } finally { setBusy(""); }
  };

  return (
    <div className="h-full overflow-y-auto bg-zinc-800">
      <div className="max-w-3xl mx-auto p-6">
        <div className="flex items-center gap-3 mb-1">
          <button onClick={onBack} className="text-sm text-zinc-400 hover:text-sky-400">← 홈(분석 화면)</button>
          <h1 className="text-xl font-bold text-zinc-100">📤 RCA 댓글 승인 대기 (HITL)</h1>
        </div>
        <p className="text-sm text-zinc-400 mb-5">사람이 승인할 때만 Jira에 게시됩니다. 거부하면 게시되지 않습니다.</p>

        {items.length === 0 ? (
          loadErr ? (
          <div className="rounded-lg border border-red-900/60 bg-red-950/40 px-4 py-3 text-sm text-red-400">
            승인 대기 목록을 불러오지 못했습니다 — {loadErr}
            <button onClick={load} className="ml-2 underline hover:text-red-300">다시 시도</button>
          </div>
          ) : (
          <div className="text-center text-zinc-400 py-16 text-sm">대기 중인 초안이 없습니다. 분석 화면에서 미해결 이슈의 "RCA 초안 생성"으로 추가하세요.</div>
          )
        ) : (
          <div className="space-y-4">
            {items.map((it) => (
              <section key={it.key} className="bg-zinc-900/60 rounded-xl border border-zinc-800 p-5">
                <div className="flex items-center gap-2 mb-2 flex-wrap">
                  <span className="font-mono text-sm text-sky-400 font-semibold">{it.key}</span>
                  <span className="text-xs text-zinc-400">{it.status}</span>
                  <span className={`text-[11px] px-2 py-0.5 rounded-full ${it.needs_review ? "bg-amber-950/60 text-amber-400" : "bg-emerald-950/60 text-emerald-400"}`}>
                    {it.needs_review ? "⚠ 검토 필요" : "자동 게시 적합"}
                  </span>
                  <span className="text-[11px] px-2 py-0.5 rounded-full bg-sky-950/40 text-sky-400">
                    {it.source === "analysis" ? "LLM 종합" : "제안 기반"}
                  </span>
                  {it.confidence != null && (
                    <span className="text-[11px] text-zinc-400">신뢰도 {Math.round(it.confidence * 100)}%{it.based_on_verified ? " · 검증 근거" : ""}</span>
                  )}
                </div>
                <div className="text-sm font-medium leading-snug mb-2">{it.summary}</div>
                <div className="flex items-center gap-2 mb-1">
                  <button onClick={() => {
                    const open = editing === it.key;
                    setEditing(open ? "" : it.key);
                    if (!open) {
                      if (!(it.key in edits)) setEdits((e) => ({ ...e, [it.key]: it.body }));
                      if (!(it.key in panelTab)) setPanelTab((t) => ({ ...t, [it.key]: "preview" }));
                    }
                  }}
                    className="text-[11px] text-zinc-400 hover:text-sky-400">
                    {editing === it.key ? "▾ 본문 닫기" : "📝 게시 본문 미리보기·수정"}{isEdited(it) ? " (수정됨)" : ""}
                  </button>
                  {isEdited(it) && <span className="text-[11px] text-amber-400">● 수정본이 게시·저장됩니다</span>}
                </div>
                {editing === it.key && (
                  <div className="border border-zinc-800 rounded-lg overflow-hidden">
                    <div className="flex items-center text-[11px] border-b border-zinc-800 bg-zinc-950">
                      {(["preview", "edit"] as const).map((t) => (
                        <button key={t} onClick={() => setPanelTab((s) => ({ ...s, [it.key]: t }))}
                          className={`px-3 py-1.5 ${(panelTab[it.key] ?? "preview") === t ? "bg-zinc-900/60 text-sky-400 font-semibold border-b-2 border-sky-500" : "text-zinc-400 hover:text-sky-400"}`}>
                          {t === "preview" ? "👁 미리보기" : "✏️ 편집"}
                        </button>
                      ))}
                      <span className="ml-auto px-2 text-[10px] text-zinc-400">Markdown</span>
                    </div>
                    {(panelTab[it.key] ?? "preview") === "edit" ? (
                      <textarea value={bodyOf(it)} onChange={(e) => setEdits((s) => ({ ...s, [it.key]: e.target.value }))}
                        rows={14} spellCheck={false}
                        className={`${inputCls} resize-y rounded-none border-0 p-3 font-mono text-xs`} />
                    ) : (
                      <div
                        /* prose-invert 필수 — 빼면 typography 기본색(어두운 회색)이 남아
                           다크 배경에서 본문이 보이지 않는다. */
                        className="max-h-96 overflow-y-auto bg-zinc-900/60 p-3 prose prose-sm prose-invert max-w-none prose-headings:text-sky-300">
                        <ReactMarkdown remarkPlugins={[remarkGfm]}>{bodyOf(it)}</ReactMarkdown>
                      </div>
                    )}
                  </div>
                )}
                {valid[it.key] && (
                  <div className="mt-2 text-[11px] bg-zinc-950 border border-zinc-800 rounded p-2 space-y-0.5">
                    <div className={valid[it.key].citations_ok ? "text-emerald-400" : "text-red-400"}>
                      {valid[it.key].citations_ok ? "✓ 인용 모두 KB에 존재" : `✗ 매치 외 인용: ${(valid[it.key].invalid_citations || []).join(", ")}`}
                    </div>
                    <div className={valid[it.key].lang_ok ? "text-emerald-400" : "text-red-400"}>
                      {valid[it.key].lang_ok ? "✓ 언어 규칙 OK(한자 없음)" : "✗ 한자/CJK 검출"}
                    </div>
                    {valid[it.key].judge_score != null && (
                      <div className={valid[it.key].judge_passed ? "text-emerald-400" : "text-amber-400"}>
                        🧑‍⚖️ 품질 점수 {valid[it.key].judge_score}/10 {valid[it.key].judge_passed ? "(통과)" : "(검토 권장)"}
                        {valid[it.key].judge_reasoning ? ` — ${valid[it.key].judge_reasoning}` : ""}
                      </div>
                    )}
                    {valid[it.key].judge_error && <div className="text-zinc-400">판정 생략: {valid[it.key].judge_error}</div>}
                  </div>
                )}
                {msg && msg.key === it.key && (
                  <div className={`mt-2 text-xs ${msg.ok ? "text-emerald-400" : "text-red-400"}`}>{msg.ok ? "✓" : "✗"} {msg.text}</div>
                )}
                {labeling === it.key && (
                  <div className="mt-3 rounded-lg border border-zinc-700 bg-zinc-900/60 p-3">
                    <div className="text-xs text-zinc-400 mb-2">
                      왜 이 초안이 부족한가요? (복수 선택) — 같은 원인이 반복되면 다음 초안의
                      생성 규칙과 개선 큐에 자동 반영됩니다.
                    </div>
                    <div className="flex flex-wrap gap-1.5">
                      {causes.map((c) => {
                        const on = (picked[it.key] ?? []).includes(c.code);
                        return (
                          <button key={c.code} title={`${c.hint} · 레버: ${c.lever}`}
                            onClick={() => toggleCause(it.key, c.code)}
                            className={`text-xs px-2 py-1 rounded border transition ${
                              on ? "bg-zinc-100 text-zinc-900 border-zinc-100"
                                 : `bg-transparent hover:bg-zinc-800 ${LEVER_STYLE[c.lever] ?? LEVER_STYLE.other}`}`}>
                            {c.label}
                          </button>
                        );
                      })}
                    </div>
                    <textarea value={note[it.key] ?? ""} rows={2}
                      onChange={(e) => setNote((n) => ({ ...n, [it.key]: e.target.value }))}
                      placeholder="추가 설명(선택) — 분류로 담기 어려운 맥락만 적으세요."
                      className={`${inputCls} mt-2 w-full text-xs`} />
                  </div>
                )}
                <div className="mt-3 flex gap-2">
                  <button onClick={() => setLabeling(labeling === it.key ? "" : it.key)}
                    className="text-sm px-4 py-2 rounded-lg border border-zinc-600 text-zinc-300 hover:bg-zinc-800">
                    🏷 사유 {(picked[it.key] ?? []).length > 0 ? `(${(picked[it.key] ?? []).length})` : ""}
                  </button>
                  <button onClick={() => validate(it)} disabled={!!validating}
                    className="text-sm px-4 py-2 rounded-lg border border-zinc-600 text-sky-400 hover:bg-zinc-800 disabled:opacity-50">
                    {validating === it.key ? "검증 중…" : "🔎 검증"}
                  </button>
                  <button onClick={() => act(it, "approve")} disabled={!!busy}
                    className="rounded-lg bg-emerald-500 px-4 py-2 text-sm font-medium text-emerald-950 transition hover:bg-emerald-400 disabled:opacity-40">
                    {busy === it.key + "approve" ? "게시 중…" : (isEdited(it) ? "✅ 수정본 승인·게시" : "✅ 승인하고 Jira 게시")}
                  </button>
                  <button onClick={() => act(it, "reject")} disabled={!!busy}
                    className="text-sm px-4 py-2 rounded-lg border border-zinc-700 text-zinc-300 hover:bg-zinc-950 disabled:opacity-50">
                    {busy === it.key + "reject" ? "처리 중…" : "거부"}
                  </button>
                </div>
              </section>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
