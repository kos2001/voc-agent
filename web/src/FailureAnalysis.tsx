import { useEffect, useMemo, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { browseUrl, linkifyKeys } from "./issueLinks";
import { postJson } from "./api";
import RelationGraph, { type GraphData } from "./RelationGraph";
import FreshnessBadge from "./FreshnessBadge";
import { Button, inputCls, selectCls } from "./ui";

const API = (import.meta as any).env?.VITE_API ?? "";   // 빈 값 = 같은 오리진(개발은 vite 프록시)

type Issue = {
  key: string; summary: string; status: string; chip: string;
  category: string; priority: string; severity: string; symptom: string;
};
type Match = {
  key: string; score: number; summary: string; chip: string; category: string;
  root_cause: string; resolution: string; workaround: string; debug_approach: string;
  embed_cos?: number; entity_overlap?: number; bm25_raw?: number; rerank_score?: number; verified?: boolean;
  known_issue?: { id: string; title: string };   // 소속 반복 문의 유형(Known-Issue)
  lifecycle?: { state: string; superseded_by: string; freshness: number | null; fw_version: string; warnings: string[] };  // 수명주기(P2-5)
};
type Proposal = { root_cause: string; resolution: string; workaround: string; based_on: string; confidence: number };
type Gate = {
  signal: string; passed: boolean;
  available?: boolean; reason?: string; candidates?: number;   // signal="none"(판정 불가)
  rerank_top?: number; threshold?: number;
  max_cos?: number; cos_threshold?: number; top_entity_overlap?: number;
};
type RecoResp = { query: any; matches: Match[]; proposal: Proposal | null; coverage: boolean; gate?: Gate | null; explanation?: string; explanation_citations?: string[]; explanation_dropped_citations?: string[]; explanation_cached?: boolean;
  reply_policy?: { ok: boolean; blocked: boolean; violations: { code: string; severity: string; detail: string }[] } | null;
  reply_intent?: string; reply_asks?: string[]; reply_lang?: string;
  reply_proofread?: { ran?: boolean; applied?: boolean; rejected?: string; error?: string } | null;
  reply_policy_docs?: { title: string; section: string; url: string }[];
  intent?: string; intent_label?: string; asks?: string[]; customer_ask?: string;
  profile?: string };

// 분류 칩 — 다크 배경에서 읽히도록 -950/60 배경 + -400 글자(하네스 배지 규칙).
const CAT_COLOR: Record<string, string> = {
  Firmware: "bg-sky-950/60 text-sky-400", Thermal: "bg-red-950/60 text-red-400",
  "Signal Integrity": "bg-lime-950/60 text-lime-400", Timing: "bg-violet-950/60 text-violet-400",
  Hardware: "bg-orange-950/60 text-orange-400", Power: "bg-amber-950/60 text-amber-400",
  Security: "bg-cyan-950/60 text-cyan-400",
};
const CAT_FALLBACK = "bg-zinc-800 text-zinc-300";
// 점수 → 색상. **게이트 임계를 기준으로** 칠한다.
//
// 예전에는 80/60/40 이라는 고정 백분율 띠를 썼다. 그런데 이 숫자들은 모델 원점수라
// 의미 있는 범위가 0~100 이 아니다 — 실측(정답 83건):
//   rerank  정답 최소 0.384 / 중앙 0.731  ·  무관 최대 0.154   (게이트 0.17)
//   코사인   정답 최소 0.472 / 중앙 0.634  ·  무관 최대 0.541   (게이트 0.57)
// 코사인은 정답 p25(0.591)와 무관 최대(0.541)가 붙어 있어 **둘 다 amber** 로 칠해졌다 —
// 색이 통과/차단을 전혀 구분하지 못했다. 시스템은 통과라는데 화면은 위험색인 경우도
// 생긴다(rerank 0.20 은 통과인데 빨강). 판정과 표시가 어긋나면 신뢰를 잃는다.
//
// 그래서 임계(t)를 0 점으로 두고 [t, 1] 을 다시 편다: 임계 미만은 빨강(실제로 차단),
// 임계 위는 여유에 따라 amber → lime → emerald.
const scoreText = (v: number, threshold?: number) => {
  const t = threshold ?? 0.5;
  if (v < t) return "text-red-400";
  const room = 1 - t;
  const rel = room > 0 ? (v - t) / room : 1;      // 임계 대비 여유 0~1
  return rel >= 0.6 ? "text-emerald-400" : rel >= 0.3 ? "text-lime-400" : "text-amber-400";
};

const statusBadge = (s: string) =>
  s === "진행 중" ? "bg-emerald-400" : s === "해야 할 일" ? "bg-zinc-500" : "bg-sky-400";

/** Jira 원본으로 나가는 링크. base_url 은 /config/status 에서 받아 하드코딩하지 않는다. */
function JiraLink({ base, issueKey, className = "", tabIndex }: {
  base: string; issueKey: string; className?: string; tabIndex?: number;
}) {
  if (!base || !issueKey) return null;
  return (
    <a href={`${base.replace(/\/$/, "")}/browse/${encodeURIComponent(issueKey)}`}
      target="_blank" rel="noreferrer" tabIndex={tabIndex}
      onClick={(e) => e.stopPropagation()}
      title={`Jira에서 ${issueKey} 열기 (새 탭)`}
      className={`text-zinc-400 hover:text-sky-400 ${className}`}>↗</a>
  );
}

/** 검색 대기 중 자리 표시 — /recommend 가 실측 700ms대라 텍스트 한 줄로는 체감이 나쁘다. */
function MatchSkeleton() {
  return (
    <div className="space-y-3" aria-busy="true" aria-label="유사 사례 검색 중">
      {[0, 1, 2].map((i) => (
        <div key={i} className="rounded-xl border border-zinc-800 bg-zinc-900/60 p-4">
          <div className="flex items-center gap-2">
            <div className="h-3 w-16 rounded bg-zinc-700 animate-pulse" />
            <div className="h-3 w-20 rounded bg-zinc-800 animate-pulse" />
            <div className="h-3 w-14 rounded bg-zinc-800 animate-pulse ml-auto" />
          </div>
          <div className="h-4 w-3/4 rounded bg-zinc-800 animate-pulse mt-2.5" />
          <div className="h-3 w-1/3 rounded bg-zinc-800 animate-pulse mt-2" />
        </div>
      ))}
    </div>
  );
}

/** 게이트가 왜 막았는지를 수치로 보여준다 — "사례 없음"만 띄우면 신뢰가 안 생긴다. */
function GateDetail({ gate }: { gate: Gate }) {
  // 판정 신호가 없을 때는 보여줄 수치가 없다 — 수치 자리에 이유를 적는다.
  if (gate.signal === "none") {
    return (
      <div className="mt-2 text-[11px] text-amber-400/90">
        <span className="font-semibold">판정 근거:</span> 없음 — 재순위·임베딩 서비스를
        모두 사용할 수 없습니다. 후보 {gate.candidates ?? 0}건은 아래에 있지만
        관련도를 검증하지 못했습니다.
      </div>
    );
  }
  const rows = gate.signal === "rerank"
    ? [["재순위 최고 관련도", gate.rerank_top], ["통과 임계", gate.threshold]]
    : [["임베딩 최고 유사도", gate.max_cos], ["통과 임계", gate.cos_threshold],
       ["기술 엔티티 겹침", gate.top_entity_overlap]];
  return (
    <div className="mt-2 text-[11px] text-amber-400/90">
      <span className="font-semibold">판정 근거({gate.signal}):</span>{" "}
      {rows.filter(([, v]) => v != null).map(([k, v]) => `${k} ${v}`).join(" · ")}
    </div>
  );
}

function Bar({ value }: { value: number }) {
  const pct = Math.round(value * 100);
  const color = pct >= 80 ? "bg-emerald-500" : pct >= 50 ? "bg-amber-500" : "bg-red-500";
  return (
    <div className="flex items-center gap-2">
      <div className="flex-1 h-2 rounded-full bg-zinc-800 overflow-hidden">
        <div className={`h-full ${color}`} style={{ width: `${pct}%` }} />
      </div>
      <span className="text-xs font-medium text-zinc-300 w-10 text-right">{pct}%</span>
    </div>
  );
}

// 큐 진입 결과 알림 — 심각도(ok/info/warn)별 색상으로 '왜 안 들어갔는지'를 또렷이.
function QNotice({ m }: { m: { sev: "ok" | "info" | "warn"; text: string } | null }) {
  if (!m) return null;
  const style = m.sev === "ok" ? "border-emerald-900/60 bg-emerald-950/40 text-emerald-400"
    : m.sev === "info" ? "border-sky-900/60 bg-sky-950/40 text-sky-400"
    : "border-amber-900/60 bg-amber-950/40 text-amber-400";
  const icon = m.sev === "ok" ? "✓" : m.sev === "info" ? "ℹ" : "⚠";
  return <div className={`mt-2 text-xs rounded-lg border px-2.5 py-1.5 leading-relaxed ${style}`}>{icon} {m.text}</div>;
}

export default function FailureAnalysis({ onQueueChange, routeKey, onSelectKey, jiraBase = "" }: {
  onQueueChange?: () => void;
  /** URL(#/issue/LSI-7)에서 온 이슈 키 — 이 값이 바뀌면 해당 이슈를 자동으로 분석한다. */
  routeKey?: string;
  /** 선택이 바뀔 때 URL을 갱신하도록 부모에 알린다. */
  onSelectKey?: (key: string) => void;
  /** Jira base URL (/config/status) — 원본 링크 생성용. */
  jiraBase?: string;
} = {}) {
  const [stats, setStats] = useState<any>(null);
  const [issues, setIssues] = useState<Issue[]>([]);
  const [q, setQ] = useState("");
  const [cat, setCat] = useState<string>("");
  const [chip, setChip] = useState<string>("");
  const [statusF, setStatusF] = useState<string>("");
  const searchRef = useRef<HTMLInputElement | null>(null);
  const [showKeys, setShowKeys] = useState(false);
  const [sel, setSel] = useState<Issue | null>(null);
  const [reco, setReco] = useState<RecoResp | null>(null);
  const [loading, setLoading] = useState(false);
  const [explaining, setExplaining] = useState(false);
  const [keyInput, setKeyInput] = useState("");
  const [err, setErr] = useState("");
  const [graph, setGraph] = useState<GraphData | null>(null);
  const [graphErr, setGraphErr] = useState("");
  const [graphLoading, setGraphLoading] = useState(false);
  // 사례가 없을 때의 조사 계획 — 근거 기반 분석과 **다른 산출물**이라 상태를 따로 둔다.
  const [invMd, setInvMd] = useState("");
  const [invBusy, setInvBusy] = useState(false);
  const [invErr, setInvErr] = useState("");
  // 사이드바 너비 조정 + 접기
  const [leftW, setLeftW] = useState(320);
  const [rightW, setRightW] = useState(340);
  const [leftOpen, setLeftOpen] = useState(true);
  const [rightOpen, setRightOpen] = useState(true);
  const startDrag = (side: "left" | "right", e: ReactPointerEvent) => {
    e.preventDefault();
    const startX = e.clientX;
    const startW = side === "left" ? leftW : rightW;
    const onMove = (ev: PointerEvent) => {
      const dx = ev.clientX - startX;
      const raw = side === "left" ? startW + dx : startW - dx;
      const w = Math.max(side === "left" ? 200 : 260, Math.min(640, raw));
      side === "left" ? setLeftW(w) : setRightW(w);
    };
    const onUp = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      document.body.style.userSelect = "";
    };
    document.body.style.userSelect = "none";
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  };

  // 목록 로드 실패를 삼키면 issues=[] 가 되어 "미해결 이슈 0건" 으로 보인다 —
  // 서버가 죽었는지 정말 0건인지 구분할 수 없다. 사용자는 아무것도 할 수 없는데
  // 화면은 정상처럼 보이는 것이 가장 나쁘다.
  const [listErr, setListErr] = useState("");
  useEffect(() => {
    fetch(`${API}/reco/stats`).then((r) => r.json()).then(setStats).catch(() => {});
    fetch(`${API}/issues/unresolved`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((d) => { setIssues(d.issues ?? []); setListErr(""); })
      .catch((e) => setListErr(e.message || "불러오기 실패"));
  }, []);

  // 선택 이슈가 바뀌면 관계 그래프 로드 (우측 사이드바).
  // 실패를 삼키지 않는다 — 예전에는 catch 에서 null 로만 되돌려, 응답이 JSON 이
  // 아닌 경우(dev 프록시 미스매치 등)가 영구히 "로딩 중" 으로 보였다.
  useEffect(() => {
    if (!sel?.key) { setGraph(null); setGraphErr(""); setGraphLoading(false); return; }
    let alive = true;
    setGraph(null); setGraphErr(""); setGraphLoading(true);
    fetch(`${API}/graph?key=${encodeURIComponent(sel.key)}&k=12`)
      .then(async (r) => {
        const ct = r.headers.get("content-type") || "";
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        if (!ct.includes("application/json")) throw new Error(`JSON 이 아닌 응답 (${ct.split(";")[0] || "unknown"})`);
        return r.json();
      })
      .then((d) => { if (alive) { if (d?.error) throw new Error(d.error); setGraph(d); } })
      .catch((e) => { if (alive) setGraphErr(e.message || "불러오기 실패"); })
      .finally(() => { if (alive) setGraphLoading(false); });
    return () => { alive = false; };
  }, [sel?.key]);

  const cats = useMemo(() => Array.from(new Set(issues.map((i) => i.category))).sort(), [issues]);
  const chips = useMemo(() => Array.from(new Set(issues.map((i) => i.chip).filter(Boolean))).sort(), [issues]);
  const statuses = useMemo(() => Array.from(new Set(issues.map((i) => i.status).filter(Boolean))).sort(), [issues]);
  const filtered = useMemo(
    () => issues.filter((i) =>
      (!cat || i.category === cat) &&
      (!chip || i.chip === chip) &&
      (!statusF || i.status === statusF) &&
      (!q || (i.key + i.summary + i.chip + i.symptom).toLowerCase().includes(q.toLowerCase()))),
    [issues, q, cat, chip, statusF]);
  const filterOn = !!(cat || chip || statusF || q);

  // 이슈를 옮길 때 이전 결과가 새 이슈에 섞이지 않게 하는 두 개의 문지기.
  //  · reqSeq: /recommend 응답이 늦게 도착해도 최신 요청이 아니면 버린다.
  //  · esRef : 진행 중이던 SSE 를 반드시 닫는다. 닫지 않으면 이전 이슈의 델타가
  //            새 이슈의 explanation 뒤에 계속 붙는다(실제로 그랬다).
  const reqSeq = useRef(0);
  // 이전 /recommend 를 실제로 **취소**한다. reqSeq 는 늦은 응답을 안 그릴 뿐이라
  // 목록에서 ↑/↓ 를 누르고 있으면 임베딩+rerank 요청이 반복 횟수만큼 나간다.
  const abortRef = useRef<AbortController | null>(null);
  const esRef = useRef<EventSource | null>(null);
  const activeKey = useRef<string>("");

  const closeStream = () => {
    if (esRef.current) { esRef.current.close(); esRef.current = null; }
  };

  // 화면을 떠날 때(다른 페이지로 이동 등) 스트림을 닫는다. 서버는 연결이 끊겨도
  // 생성을 끝내 캐시에 넣으므로, 돌아오면 캐시본이 즉시 뜬다.
  useEffect(() => () => { closeStream(); abortRef.current?.abort(); }, []);

  const select = async (issue: Issue) => {
    const seq = ++reqSeq.current;
    abortRef.current?.abort();         // 진행 중이던 이전 검색 요청 취소
    const ac = new AbortController();
    abortRef.current = ac;
    closeStream();                     // 이전 이슈의 스트리밍 중단
    activeKey.current = issue.key;
    setSel(issue); setReco(null); setErr(""); setLoading(true); setExplaining(false);
    onSelectKey?.(issue.key);          // URL 동기화 — 새로고침·공유 가능하게
    try {
      const r = await fetch(`${API}/recommend`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key: issue.key, k: 4 }), signal: ac.signal,
      });
      const d = await r.json();
      if (seq !== reqSeq.current) return;   // 그 사이 다른 이슈로 옮겼다 — 버린다
      setReco(d);
    } catch (e: any) {
      if (e?.name === "AbortError" || seq !== reqSeq.current) return;
      setReco({ query: {}, matches: [], proposal: null, coverage: false, explanation: "[error] " + e.message });
    } finally { if (seq === reqSeq.current) setLoading(false); }
  };

  // LLM 종합 분석을 SSE로 스트리밍 — 본문은 토큰 단위, 인용은 완료 시 검증본 반영
  const runExplain = (key: string, refresh = false) => {
    closeStream();
    activeKey.current = key;
    setExplaining(true);
    setReco((prev) => (prev ? { ...prev, explanation: "", explanation_cached: undefined,
                                reply_policy: null, reply_intent: "", reply_asks: [] } : prev));
    setInvMd(""); setInvErr(""); setInvBusy(false);
    // withCredentials: SSE 도 쿠키 세션을 실어야 한다(전역 fetch 래퍼가 못 덮는 경로).
    const es = new EventSource(
      `${API}/recommend/explain/stream?key=${encodeURIComponent(key)}&k=4${refresh ? "&refresh=true" : ""}`,
      { withCredentials: true });
    esRef.current = es;
    // 이 스트림이 아직 화면의 주인인가 — 늦게 온 이벤트가 다른 이슈를 덮어쓰지 않게.
    const mine = () => esRef.current === es && activeKey.current === key;
    const finish = () => {
      if (esRef.current === es) { es.close(); esRef.current = null; setExplaining(false); }
      else es.close();
    };
    es.onmessage = (e) => {
      if (!mine()) { es.close(); return; }
      let d: any; try { d = JSON.parse(e.data); } catch { return; }
      if (d.type === "delta") {
        setReco((prev) => (prev ? { ...prev, explanation: (prev.explanation || "") + d.text } : prev));
      } else if (d.type === "done") {
        // 최종본으로 갈아끼운다 — 스트리밍 중에는 모델이 쓴 원문이 스쳐 지나가고,
        // 서버가 내부 키를 지우고 정책까지 매긴 판본은 done 에만 있다.
        setReco((prev) => (prev ? { ...prev,
                                    explanation: d.text || prev.explanation,
                                    explanation_cached: d.cached,
                                    reply_policy: d.policy ?? null,
                                    reply_intent: d.intent_label || "",
                                    reply_asks: d.asks || [],
                                    reply_lang: d.lang || "ko",
                                    reply_proofread: d.proofread ?? null,
                                    reply_policy_docs: d.policy_docs ?? [] } : prev));
        finish();
      } else if (d.type === "error") {
        setReco((prev) => (prev ? { ...prev, explanation: (prev.explanation || "") + `\n\n_(생성 오류: ${d.message})_` } : prev));
        finish();
      }
    };
    es.onerror = finish;
  };

  const explain = () => { if (sel) runExplain(sel.key); };
  const reExplain = () => { if (sel) runExplain(sel.key, true); };

  // 분석 코멘트 초안 → HITL 게시 대기 큐 (Jira 게시는 승인 시에만).
  // 고객 답변과 다른 산출물이다 — 이건 이슈에 남기는 **내부 기록**이고, 읽는 사람은
  // 다음에 이 이슈를 여는 엔지니어다.
  // 큐 진입 결과를 심각도(ok/info/warn)로 표준화 — '왜 안 들어갔는지'를 또렷이 표시
  type QMsg = { sev: "ok" | "info" | "warn"; text: string } | null;
  const qmsgOf = (d: any): QMsg => {
    if (d?.queued) return { sev: "ok", text: d.reason || "승인 대기 큐에 추가됨" };
    if (d?.reason_code === "already_approved") return { sev: "info", text: d.reason };
    return { sev: "warn", text: d?.reason || d?.error || "큐에 추가하지 못했습니다" };
  };

  const [drafting, setDrafting] = useState(false);
  const [draftMsg, setDraftMsg] = useState<QMsg>(null);
  const draftRca = async () => {
    if (!sel) return;
    setDrafting(true); setDraftMsg(null);
    try {
      const d = await postJson(`/rca/draft`, { key: sel.key });
      setDraftMsg(qmsgOf(d));
      if (d.queued) onQueueChange?.();
    } catch (e: any) { setDraftMsg({ sev: "warn", text: e.message }); } finally { setDrafting(false); }
  };

  // 화면의 고객 응대 답변 → 발송 대기 큐. 본문을 **그대로** 보낸다 —
  // 서버가 다시 생성하면 사람이 읽고 판단한 글과 큐에 들어가는 글이 달라진다.
  // 이 화면의 목적은 '대응' 이므로, 지표도 대응 진행도를 본다.
  const [replyStats, setReplyStats] = useState<any>(null);
  // 대응 프로파일(외부 고객 / 사내 VOC) — 서버가 정본이다. 화면이 하드코딩하면
  // 규칙과 표시가 갈라진다.
  const [profileLabel, setProfileLabel] = useState("");
  useEffect(() => {
    fetch(`${API}/voc/reply/intents`).then((r) => (r.ok ? r.json() : null))
      .then((d) => d && setProfileLabel(d.profile_label || "")).catch(() => {});
  }, []);
  const loadReplyStats = () => fetch(`${API}/voc/reply/stats`).then((r) => (r.ok ? r.json() : null))
    .then((d) => d && setReplyStats(d)).catch(() => {});
  useEffect(() => { loadReplyStats(); }, []);
  // 선택한 문의의 답변 상태 — 이미 답장한 건에 또 초안을 만드는 것을 막는다.
  const [replyStatus, setReplyStatus] = useState<any>(null);
  useEffect(() => {
    setReplyStatus(null);
    if (!sel?.key) return;
    fetch(`${API}/voc/reply/status?key=${encodeURIComponent(sel.key)}`)
      .then((r) => (r.ok ? r.json() : null)).then((d) => d && setReplyStatus(d)).catch(() => {});
  }, [sel?.key]);

  // 근거가 없을 때의 답변. 원인을 단정하지 않는 골격으로 가되, **답은 나간다** —
  // 고객 대응에서 무응답이 가장 나쁜 실패다. 근거가 없으므로 스트리밍 경로(게이트에
  // 막힌다) 대신 큐 직행 경로를 쓴다.
  const [ackBusy, setAckBusy] = useState(false);
  const [ackMsg, setAckMsg] = useState<QMsg>(null);
  const draftAck = async () => {
    if (!sel) return;
    setAckBusy(true); setAckMsg(null);
    try {
      const d = await postJson(`/voc/reply/draft`,
        { key: sel.key, again: replyStatus?.state === "approved" });
      setAckMsg(d?.queued
        ? { sev: "ok", text: d.reason || "발송 대기 큐에 추가됨" }
        : { sev: d?.reason_code === "already_sent" ? "info" : "warn",
            text: d?.reason || d?.error || "큐에 추가하지 못했습니다" });
      if (d?.queued) { onQueueChange?.(); loadReplyStats(); setReplyStatus({ state: "pending" }); }
    } catch (e: any) { setAckMsg({ sev: "warn", text: e.message }); }
    finally { setAckBusy(false); }
  };

  const [queueing, setQueueing] = useState(false);
  const [queueMsg, setQueueMsg] = useState<QMsg>(null);
  const [replySent, setReplySent] = useState(false);
  useEffect(() => { setQueueMsg(null); setReplySent(false); }, [sel?.key]);
  const queueReply = async () => {
    if (!sel || !reco?.explanation) return;
    setQueueing(true); setQueueMsg(null);
    try {
      const d = await postJson(`/voc/reply/draft-from-text`,
        { key: sel.key, body: reco.explanation, again: replySent });
      setReplySent(d?.reason_code === "already_sent");
      setQueueMsg(d?.queued
        ? { sev: "ok", text: d.reason || "발송 대기 큐에 추가됨" }
        : { sev: d?.reason_code === "already_sent" ? "info" : "warn",
            text: d?.reason || d?.error || "큐에 추가하지 못했습니다" });
      if (d?.queued) { onQueueChange?.(); loadReplyStats(); setReplyStatus({ state: "pending" }); }
    } catch (e: any) { setQueueMsg({ sev: "warn", text: e.message }); }
    finally { setQueueing(false); }
  };

  // P1-3 추천 유용성 피드백 — 매치별 도움됨/아님 + 실제 근본원인 라벨 수집
  const [fb, setFb] = useState<Record<string, { rating?: "helpful" | "not_helpful"; actual?: boolean }>>({});
  const sendFeedback = async (m: Match, rank: number,
                              patch: { rating?: "helpful" | "not_helpful"; actual?: boolean }) => {
    const cur = fb[m.key] ?? {};
    const next = { ...cur, ...patch };
    setFb((s) => ({ ...s, [m.key]: next }));               // 낙관적 갱신
    if (!next.rating) return;                               // rating 없으면 전송 보류
    try {
      await fetch(`${API}/reco/feedback`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          query_key: sel?.key ?? "", query_summary: sel?.summary ?? reco?.query?.summary ?? "",
          match_key: m.key, rating: next.rating, is_actual_root_cause: !!next.actual,
          match_rank: rank, match_score: m.rerank_score ?? m.embed_cos ?? m.score,
        }),
      });
    } catch { /* 피드백 실패는 조용히 무시(분석 흐름 방해 금지) */ }
  };

  // 반복 문의 유형(Known-Issue)으로 묶기 — 아직 유형에 속하지 않은 매치들을 승격.
  // VOC 대응에서 이 묶음이 갖는 뜻: 같은 문의가 반복되면 **답변도 하나여야 한다.**
  const [promoting, setPromoting] = useState(false);
  const [promoteMsg, setPromoteMsg] = useState("");
  const promoteMatches = async () => {
    const ms = reco?.matches ?? [];
    const free = ms.filter((m) => !m.known_issue).map((m) => m.key);
    if (free.length < 2) return;
    // 제목에서 고객사·호스트 꼬리표를 뗀다. 기사는 **유형**의 이름이지 한 고객의
    // 티켓 이름이 아니다 — 실제로 "…(Orion Telecom / Cust" 처럼 고객사가 박혔고,
    // 그 제목이 화면 곳곳에 그대로 나왔다.
    const rawTitle = (sel?.summary ?? reco?.query?.summary ?? "반복 문의 유형");
    const title = rawTitle.replace(/\s*\([^)]*\)\s*$/, "").replace(/\s*\(.*$/, "").slice(0, 80).trim()
      || rawTitle.slice(0, 80);
    setPromoting(true); setPromoteMsg("");
    try {
      const d = await fetch(`${API}/knowledge/known-issue`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title, members: free }),
      }).then((r) => r.json());
      if (d.ok) {
        setPromoteMsg(`✓ 반복 문의 유형 ${d.article.id} 생성 — ${free.length}건 묶음`);
        if (sel) select(sel);                // 재조회로 기사 배지 반영
      } else setPromoteMsg(`⚠ ${d.error || "승격 실패"}`);
    } catch (e: any) { setPromoteMsg(`⚠ ${e.message}`); } finally { setPromoting(false); }
  };

  // Jira 번호(또는 그래프 노드 클릭) → 유사 사례 검색 + 에이전트(LLM) 종합 분석
  const goKey = async (raw: string) => {
    const t = raw.trim().toUpperCase();
    if (!t) return;
    const key = /^\d+$/.test(t) ? `LSI-${t}` : t;
    setErr("");
    const found = issues.find((i) => i.key === key);
    if (found) {
      await select(found);
      runExplain(key);
      return;
    }
    // 미해결 목록에 없는 키(해결 이슈 등)는 백엔드 by_key로 직접 조회
    setSel(null); setReco(null); setLoading(true);
    onSelectKey?.(key);
    try {
      const r = await fetch(`${API}/recommend`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key, k: 4 }),
      });
      const d = await r.json();
      if (d.error) { setErr(d.error); return; }
      const q = d.query ?? {};
      setSel({
        key, summary: q.summary ?? "", status: q.status ?? "", chip: q.chip ?? "",
        category: q.category ?? "기타", priority: "", severity: "", symptom: q.symptom ?? "",
      });
      setReco(d);
      runExplain(key);
    } catch (e: any) {
      setErr(e.message);
    } finally { setLoading(false); }
  };

  // URL(#/issue/LSI-7)에서 온 키를 반영 — 새로고침·뒤로가기·대시보드 드릴다운의 진입점.
  // goKeyRef 로 최신 클로저를 참조해, issues 로드 전에 들어온 키도 목록이 준비되면 처리한다.
  const goKeyRef = useRef(goKey);
  useEffect(() => { goKeyRef.current = goKey; });
  const lastRouteKey = useRef<string | undefined>(undefined);
  useEffect(() => {
    if (!routeKey || routeKey === lastRouteKey.current) return;
    lastRouteKey.current = routeKey;
    goKeyRef.current(routeKey);
  }, [routeKey, issues.length]);

  // 전역 단축키는 '/'(검색)와 '?'(도움말)·Esc 만. ↑/↓ 는 전역으로 잡지 않는다 —
  // 전역으로 가로채면 본문을 스크롤하려고 누른 화살표가 이슈 선택을 바꾸고
  // /recommend 요청까지 발사한다(실측으로 확인). 목록 이동은 아래 listKeyDown 담당.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      const typing = !!t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable);
      if (e.key === "Escape") {
        if (typing) t.blur();
        setShowKeys(false);
        return;
      }
      if (typing) return;
      if (e.key === "/") { e.preventDefault(); searchRef.current?.focus(); }
      else if (e.key === "?") { e.preventDefault(); setShowKeys((v) => !v); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // 이슈 목록(또는 검색창) 안에서의 ↑/↓ — 여기서만 선택을 옮긴다.
  // roving tabindex 라 선택이 바뀌면 **포커스도 같이 옮겨야 한다.** 안 그러면 방금
  // tabIndex=-1 이 된 항목에 포커스가 남아 다음 Tab 이 문서 처음으로 튄다.
  const activeRowRef = useRef<HTMLButtonElement | null>(null);
  const [followFocus, setFollowFocus] = useState(false);
  useEffect(() => {
    if (!followFocus) return;                 // 마우스 클릭 선택까지 포커스를 뺏지 않는다
    activeRowRef.current?.focus();
    activeRowRef.current?.scrollIntoView({ block: "nearest" });
    setFollowFocus(false);
  }, [sel?.key, followFocus]);

  const runInvestigate = async () => {
    if (!sel) return;
    setInvBusy(true); setInvErr(""); setInvMd("");
    try {
      const d = await postJson(`/recommend/investigate`, { key: sel.key, k: 5 });
      if (d.available) setInvMd(d.markdown || "");
      // 검증에 걸려 막힌 경우도 조용히 비우지 않는다 — 장애와 구분이 안 된다.
      else setInvErr(d.message || "조사 계획을 만들지 못했습니다.");
    } catch (e: any) {
      setInvErr(e?.message || "요청 실패");
    } finally { setInvBusy(false); }
  };

  const listKeyDown = (e: React.KeyboardEvent) => {
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
    if (!filtered.length) return;
    e.preventDefault();
    const cur = filtered.findIndex((i) => i.key === sel?.key);
    const next = e.key === "ArrowDown"
      ? Math.min(filtered.length - 1, cur < 0 ? 0 : cur + 1)
      : Math.max(0, cur < 0 ? 0 : cur - 1);
    select(filtered[next]);
    setFollowFocus(true);
  };

  // 점수 색상의 기준선 — 서버가 실제로 쓰는 게이트 임계를 응답에서 가져온다.
  // 프런트에 상수로 박으면 서버 재보정 때 조용히 어긋난다(판정과 표시의 불일치).
  const rrThr = reco?.gate?.signal === "rerank" ? reco.gate.threshold : undefined;
  const cosThr = reco?.gate?.signal === "embed_cos" ? reco.gate.cos_threshold : undefined;

  return (
    <div className="h-full flex bg-zinc-950 text-zinc-200">
      {showKeys && (
        <div className="fixed inset-0 z-50 bg-black/60 flex items-center justify-center"
          onClick={() => setShowKeys(false)}>
          <div className="w-80 rounded-xl border border-zinc-800 bg-zinc-900 p-5 shadow-2xl" onClick={(e) => e.stopPropagation()}>
            <div className="mb-3 font-semibold tracking-tight text-zinc-50">키보드 단축키</div>
            <dl className="text-sm space-y-1.5">
              {[["/", "이슈 검색으로 이동"], ["↑ / ↓", "이슈 목록 안에서 위·아래 선택"],
                ["Enter", "입력한 Jira 번호 분석"], ["Esc", "입력 해제 / 창 닫기"],
                ["?", "이 도움말 열고 닫기"]].map(([k, v]) => (
                <div key={k} className="flex gap-3">
                  <dt className="w-16 shrink-0 rounded border border-zinc-700 bg-zinc-950 px-1.5 py-0.5 text-center font-mono text-xs text-zinc-300">{k}</dt>
                  <dd className="text-zinc-300">{v}</dd>
                </div>
              ))}
            </dl>
          </div>
        </div>
      )}
      {/* 좌: 미해결 이슈 목록 (너비 조정 + 접기) */}
      {leftOpen ? (
      <aside style={{ width: leftW }} onKeyDown={listKeyDown}
        className="shrink-0 border-r border-zinc-800 bg-zinc-950 flex flex-col">
        <div className="p-4 border-b border-zinc-800">
          <div className="flex items-center justify-between mb-2">
            <div className="text-xs font-medium uppercase tracking-wider text-zinc-400">
              미답변 문의 {filterOn ? `${filtered.length} / ${issues.length}` : `${issues.length}`}건
            </div>
            {/* 22×16px 이던 것을 28×28 로 — 마우스로도 집기 어려운 크기였다.
                글자는 그대로 두고 클릭 영역만 넓힌다. */}
            <button onClick={() => setLeftOpen(false)} title="목록 접기" aria-label="이슈 목록 접기"
              className="grid h-7 w-7 place-items-center rounded leading-none text-zinc-400
                         outline-none hover:bg-zinc-900 hover:text-sky-400
                         focus-visible:ring-1 focus-visible:ring-sky-500">◀</button>
          </div>
          <input
            ref={searchRef}
            value={q} onChange={(e) => setQ(e.target.value)}
            placeholder="이슈 검색 (키/칩/증상)  —  '/' 키"
            className={inputCls}
          />
          <div className="flex flex-wrap gap-1 mt-2">
            <button onClick={() => setCat("")}
              className={`text-xs px-2 py-0.5 rounded-full ${!cat ? "bg-zinc-100 text-zinc-900" : "bg-zinc-800 text-zinc-300 hover:text-zinc-200"}`}>전체</button>
            {cats.map((c) => (
              <button key={c} onClick={() => setCat(c === cat ? "" : c)}
                className={`text-xs px-2 py-0.5 rounded-full ${c === cat ? "bg-zinc-100 text-zinc-900" : "bg-zinc-800 text-zinc-300 hover:text-zinc-200"}`}>{c}</button>
            ))}
          </div>
          {/* 칩·상태 필터 — 264건 코퍼스에서 분류만으로는 좁혀지지 않는다 */}
          <div className="flex gap-1.5 mt-2">
            <select value={chip} onChange={(e) => setChip(e.target.value)} aria-label="칩 필터"
              className={`flex-1 min-w-0 ${selectCls}`}>
              <option value="">모든 칩</option>
              {chips.map((c) => <option key={c} value={c}>{c}</option>)}
            </select>
            <select value={statusF} onChange={(e) => setStatusF(e.target.value)} aria-label="상태 필터"
              className={`flex-1 min-w-0 ${selectCls}`}>
              <option value="">모든 상태</option>
              {statuses.map((s) => <option key={s} value={s}>{s}</option>)}
            </select>
          </div>
          {filterOn && (
            <button onClick={() => { setCat(""); setChip(""); setStatusF(""); setQ(""); }}
              className="mt-1.5 text-[11px] text-zinc-400 underline hover:text-sky-400">필터 초기화</button>
          )}
        </div>
        <div className="flex-1 overflow-y-auto" role="listbox"
          aria-label={`미답변 고객 문의 ${filtered.length}건 — 위아래 방향키로 이동`}>
          {listErr && (
            <div className="m-3 rounded-lg border border-red-900/60 bg-red-950/40 px-3 py-2 text-xs text-red-400">
              이슈 목록을 불러오지 못했습니다 — {listErr}
              <button onClick={() => window.location.reload()}
                className="ml-2 underline hover:text-red-300">새로고침</button>
            </div>
          )}
          {!listErr && filtered.length === 0 && (
            <div className="p-6 text-center text-xs text-zinc-400">
              조건에 맞는 이슈가 없습니다.
              {filterOn && (
                <button onClick={() => { setCat(""); setChip(""); setStatusF(""); setQ(""); }}
                  className="mx-auto mt-2 block text-sky-400 hover:underline">필터 초기화</button>
              )}
            </div>
          )}
          {/* 목록은 **탭 정지점 하나**로 묶는다(roving tabindex). 예전에는 137개 항목이
              전부 탭 순서에 들어가 가운데 본문까지 Tab 을 273번 눌러야 했다(실측).
              선택된 항목만 tabIndex=0 이고 나머지는 -1 — 목록 안 이동은 이미 있던
              ↑/↓ 가 맡는다. 표준 listbox 규약이라 스크린리더도 "N개 중 M번째" 로 읽는다.
              Jira 링크는 hover/focus 로만 드러나는 보조 동작이라 함께 빼 준다. */}
          {filtered.map((i, idx) => {
            const active = sel ? sel.key === i.key : idx === 0;
            return (
            <div key={i.key} role="option" aria-selected={sel?.key === i.key}
              className={`group flex items-start border-b border-zinc-800/70 transition hover:bg-zinc-900 ${sel?.key === i.key ? "bg-zinc-800/70" : ""}`}>
              <button onClick={() => select(i)} tabIndex={active ? 0 : -1}
                ref={active ? activeRowRef : undefined}
                className="flex-1 min-w-0 px-4 py-3 text-left outline-none focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-sky-500">
                <div className="flex items-center gap-2">
                  <span className={`w-2 h-2 rounded-full ${statusBadge(i.status)}`} />
                  <span className="font-mono text-xs text-zinc-400">{i.key}</span>
                  <span className={`text-[10px] px-1.5 py-0.5 rounded ${CAT_COLOR[i.category] ?? CAT_FALLBACK}`}>{i.category}</span>
                </div>
                <div className="text-sm mt-1 leading-snug line-clamp-2">{i.summary}</div>
              </button>
              {/* 예전에는 opacity-0 이라 hover 전에는 존재조차 보이지 않았다 —
                  "Jira 링크가 없다" 는 신고의 원인이다. 항상 흐리게 두고 hover 에 밝힌다. */}
              <JiraLink base={jiraBase} issueKey={i.key} tabIndex={active ? 0 : -1}
                className="px-2 py-3 opacity-50 group-hover:opacity-100 focus:opacity-100 shrink-0" />
            </div>
            );
          })}
        </div>
      </aside>
      ) : (
        <button onClick={() => setLeftOpen(true)} title="이슈 목록 펼치기"
          className="flex w-7 shrink-0 flex-col items-center justify-center gap-2 border-r border-zinc-800 bg-zinc-950 text-zinc-400 hover:bg-zinc-900 hover:text-sky-400">
          <span>▶</span>
          <span className="text-[10px] [writing-mode:vertical-rl]">이슈 목록</span>
        </button>
      )}
      {leftOpen && (
        <div onPointerDown={(e) => startDrag("left", e)} title="드래그하여 너비 조정"
          className="w-1.5 shrink-0 cursor-col-resize bg-zinc-800 transition-colors hover:bg-sky-600 active:bg-sky-500" />
      )}

      {/* 우: 추천 결과 */}
      <main className="flex-1 min-w-0 overflow-y-auto">
        {/* 그라데이션 배너 대신 제목 + 설명 + 지표 줄 — 하네스의 PageHeader 형식. */}
        <header className="px-6 pt-8 sm:px-8">
          <div className="mb-5 flex items-start justify-between gap-4">
            <div>
              <div className="flex items-center gap-2">
                <h1 className="text-2xl font-semibold tracking-tight text-zinc-50">
                  {profileLabel === "사내 VOC 대응" ? "사내 VOC 대응" : "VOC 대응"}
                </h1>
                {profileLabel && (
                  <span className="rounded-full border border-zinc-700 px-2 py-0.5 text-[11px] text-zinc-300"
                    title="대응 프로파일 — 요청 유형 체계와 발송 정책이 여기서 갈립니다 (RVP_VOC_PROFILE)">
                    {profileLabel}
                  </span>
                )}
              </div>
              <p className="mt-1.5 text-sm text-zinc-300">
                {profileLabel === "사내 VOC 대응"
                  ? "사내 서비스 요청에 보낼 답변을 만들고, 검토한 뒤 발송합니다 — 과거 처리 사례가 근거입니다"
                  : "고객 문의에 보낼 답변을 만들고, 검토한 뒤 발송합니다 — 과거 해결 사례가 근거입니다"}
              </p>
            </div>
            <FreshnessBadge onSynced={() => {
              // Jira에 변경이 있었으면 목록·통계를 다시 읽어 화면을 최신으로 맞춘다.
              fetch(`${API}/issues/unresolved`).then((r) => r.json())
                .then((d) => setIssues(d.issues ?? [])).catch(() => {});
              fetch(`${API}/reco/stats`).then((r) => r.json()).then(setStats).catch(() => {});
            }} />
          </div>
          {stats && (
            <dl className="mb-5 flex flex-wrap gap-x-6 gap-y-2 text-xs">
              {[["미답변 문의", `${stats.unresolved}건`],
                ["발송 대기", `${replyStats?.pending ?? 0}건`],
                ["발송됨", `${replyStats?.sent ?? 0}건`],
                ["근거 KB", `${stats.resolved}건`]].map(([k, v]) => (
                <div key={k} className="flex items-baseline gap-1.5">
                  <dt className="text-zinc-400">{k}</dt>
                  <dd className="font-medium text-zinc-200 tabular-nums">{v}</dd>
                </div>
              ))}
            </dl>
          )}
          <form onSubmit={(e) => { e.preventDefault(); goKey(keyInput); }}
            className="flex max-w-md gap-2">
            <input
              value={keyInput} onChange={(e) => setKeyInput(e.target.value)}
              placeholder="문의 번호 입력 (예: VOC-66 또는 66)"
              className={inputCls}
            />
            <Button type="submit" disabled={loading || explaining} className="shrink-0">
              문의 열기
            </Button>
          </form>
        </header>

        {/* 상태 알림 — 버튼을 누르고 수 초 뒤에 결과가 도착하는데, 화면을 보지 않는
            사용자에게는 아무 일도 일어나지 않은 것과 같았다(라이브 영역이 0개였다).
            polite 라 진행 중인 낭독을 끊지 않는다. 시각적으로는 숨긴다 — 같은 내용을
            아래 본문이 이미 보여준다. */}
        <p className="sr-only" role="status" aria-live="polite">
          {err ? `오류: ${err}`
            : loading ? "유사 사례를 검색하는 중입니다"
            : explaining ? "고객 응대 답변을 생성하는 중입니다"
            : reco && !reco.coverage ? "유사한 과거 해결 사례를 찾지 못했습니다. 시니어 검토가 필요합니다"
            : reco ? `근거 사례 ${reco.matches.length}건을 찾았습니다`
            : ""}
        </p>

        {err && (
          <div className="m-8 rounded-xl border border-red-900/60 bg-red-950/40 p-5 text-sm text-red-400">
            ⚠️ {err}
          </div>
        )}

        {!sel && !err ? (
          <div className="p-16 text-center text-sm text-zinc-400">
            ← 왼쪽에서 고객 문의를 고르거나 위에 문의 번호를 입력하세요.
            에이전트가 과거 해결 사례를 근거로 <b>고객에게 보낼 답변</b>을 만듭니다.
          </div>
        ) : !sel ? null : (
          <div className="p-8 space-y-6 w-full">
            {/* 선택 이슈 */}
            <section className="rounded-xl border border-zinc-800 bg-zinc-900/60 p-5">
              <div className="flex items-center gap-2 mb-2">
                <span className={`w-2.5 h-2.5 rounded-full ${statusBadge(sel.status)}`} />
                {jiraBase ? (
                  <a href={browseUrl(jiraBase, sel.key)} target="_blank" rel="noreferrer"
                    title={`Jira에서 ${sel.key} 원문 열기 (새 탭)`}
                    className="font-mono text-sm text-sky-400 underline decoration-dotted underline-offset-2 hover:text-sky-300">
                    {sel.key}
                  </a>
                ) : (
                  <span className="font-mono text-sm text-zinc-400">{sel.key}</span>
                )}
                <span className="text-xs text-zinc-400">{sel.status}</span>
                <span className={`text-xs px-2 py-0.5 rounded ${CAT_COLOR[sel.category] ?? CAT_FALLBACK}`}>{sel.category}</span>
                <span className="rounded bg-zinc-800 px-2 py-0.5 text-xs text-zinc-300">{sel.chip}</span>
                <JiraLink base={jiraBase} issueKey={sel.key} className="text-base leading-none" />
              </div>
              <h2 className="font-semibold text-lg leading-snug">{sel.summary}</h2>
              <p className="mt-2 text-sm text-zinc-300">{sel.symptom}</p>

              {/* 고객이 무엇을 요청했는지를 답변을 만들기 **전에** 보여준다.
                  증상만 읽고 답을 판단하면 요지를 빗나간다 — 초안 거부 사유 1순위다. */}
              {(reco?.asks?.length ?? 0) > 0 && (
                <div className="mt-3 rounded-lg border border-sky-900/60 bg-sky-950/20 p-3">
                  <div className="text-[11px] text-sky-300">
                    고객이 요청한 것{reco?.intent_label ? ` · ${reco.intent_label}` : ""}
                  </div>
                  <ul className="mt-1 space-y-0.5 text-[13px] text-zinc-200">
                    {reco!.asks!.map((a, i) => <li key={i}>· {a}</li>)}
                  </ul>
                </div>
              )}

              {replyStatus && replyStatus.state !== "none" && (
                <div className={`mt-3 rounded-lg border px-3 py-2 text-[12px] ${
                  replyStatus.state === "approved"
                    ? "border-emerald-800 bg-emerald-950/30 text-emerald-300"
                    : replyStatus.state === "rejected"
                    ? "border-zinc-700 bg-zinc-900 text-zinc-400"
                    : "border-amber-800 bg-amber-950/30 text-amber-300"}`}>
                  {replyStatus.state === "approved"
                    ? `이 문의에는 이미 답변을 발송했습니다${replyStatus.sent_at ? ` (${replyStatus.sent_at})` : ""}. 아래에서 후속 답변을 만들 수 있습니다.`
                    : replyStatus.state === "rejected"
                    ? "이전 초안은 거부되었습니다. 다시 만들 수 있습니다."
                    : "이 문의의 답변이 발송 대기 중입니다 — 상단바 📮 고객 답변에서 검토·발송하세요."}
                </div>
              )}
            </section>

            {loading && (
              <div>
                <div className="mb-3 text-sm text-zinc-400">유사 사례 검색 중…</div>
                <MatchSkeleton />
              </div>
            )}

            {reco && !loading && (
              <>
                {!reco.coverage ? (
                  // 다크 UI 에 홀로 남아 있던 라이트 패널 — 화면에서 이 블록만
                  // 하얗게 튀었다. 위쪽 오류 패널(red-950/40)과 같은 규칙으로 맞춘다.
                  <div className="rounded-xl border border-amber-900/60 bg-amber-950/40 p-5 text-sm text-amber-300">
                    {reco.gate?.signal === "none" ? (
                      <>
                        {/* 사례가 없는 것과 판정을 못 하는 것은 다르다 — 같은 문구로
                            뭉뚱그리면 사용자가 "이 고장은 처음이구나" 로 잘못 읽는다. */}
                        <div className="font-semibold">⚠️ 지금은 관련도를 판정할 수 없습니다.</div>
                        <p className="mt-1 leading-relaxed">
                          재순위·임베딩 서비스가 일시적으로 응답하지 않아, 찾은 후보가
                          실제로 관련 있는지 확인하지 못했습니다. 근거 없는 원인 단정을 막기 위해
                          근거 기반 답변을 생성하지 않았습니다 —
                          <b> 잠시 후 다시 시도</b>하거나 아래 후보를 직접 확인하세요.
                        </p>
                      </>
                    ) : (
                      <>
                        <div className="font-semibold">⚠️ 유사한 과거 해결 사례를 찾지 못했습니다.</div>
                        <p className="mt-1 leading-relaxed">
                          이 유형의 문의는 처음일 수 있습니다. 근거 없는 추측을 막기 위해
                          <b>원인을 담은 답변</b>은 만들지 않았습니다. 그렇다고 고객을 기다리게 둘 수는
                          없으므로, 원인을 단정하지 않는 <b>접수 답변</b>은 아래에서 만들 수 있습니다.
                        </p>
                      </>
                    )}
                    <div className="mt-3">
                      <button onClick={draftAck} disabled={ackBusy}
                        title="원인을 단정하지 않는 접수 답변을 만들어 발송 대기 큐에 넣습니다"
                        className="inline-flex items-center rounded-lg border border-emerald-600/50 bg-emerald-500/10 px-3 py-1.5 text-xs font-medium text-emerald-300 transition hover:bg-emerald-500/20 disabled:opacity-40">
                        {ackBusy ? "작성 중…" : "📮 접수 답변 만들기 → 발송 대기"}
                      </button>
                      <QNotice m={ackMsg} />
                    </div>
                    {reco.gate && <GateDetail gate={reco.gate} />}
                    {/* 사례가 없을 때만 뜨는 조사 계획. 근본원인을 만들지 않고
                        "무엇을 재면 어떤 가설이 죽는지" 를 쓴다 — 게이트를 우회하는
                        것이 아니라 산출물을 바꾸는 것이다. 장애(signal="none")일 때는
                        띄우지 않는다(사례가 없는 게 아니라 판정을 못 한 상황). */}
                    {reco.gate?.signal !== "none" && !invMd && (
                      <button onClick={runInvestigate} disabled={invBusy}
                        className="mt-3 inline-flex items-center rounded-lg border border-amber-600/60
                                   bg-amber-500/10 px-3 py-1.5 text-xs font-medium text-amber-300
                                   transition hover:bg-amber-500/20 disabled:opacity-40">
                        {invBusy ? "조사 계획 작성 중… (1~2분)" : "🧭 1차 원리 조사 계획 만들기"}
                      </button>
                    )}
                    {invErr && <div className="mt-2 text-[11px] text-red-400">⚠ {invErr}</div>}
                    {invMd && (
                      <div className="mt-3 rounded-lg border border-zinc-800 bg-zinc-950/60 p-4">
                        <div className="mb-2 text-[11px] text-amber-400">
                          🧭 <b>조사 계획</b> — 과거 사례에 근거하지 않습니다. 근본원인이 아니라
                          <b> 무엇을 확인해야 하는지</b>를 제시합니다. (참고)·(배경)·(추정) 표시를 확인하세요.
                        </div>
                        <div className="prose prose-invert prose-sm max-w-none">
                          <ReactMarkdown remarkPlugins={[remarkGfm]}>{invMd}</ReactMarkdown>
                        </div>
                      </div>
                    )}
                    <div className="mt-3 border-t border-amber-900/60 pt-2 text-[11px] text-amber-300/80">
                      {reco.gate?.signal === "none"
                        ? "일시 장애로 판정하지 못한 질의라 지식 공백으로 세지 않습니다 — 서비스가 돌아오면 다시 눌러 주세요."
                        : <>이 질의는 <b>지식 공백</b>으로 기록되어 어떤 고장 유형의 사례가 부족한지 집계됩니다 (지식 현황 → 지식 공백).</>}
                      {reco.matches.length > 0 && (
                        <> 참고용 하위 후보 {reco.matches.length}건은 아래에 접어 두었습니다.</>
                      )}
                    </div>
                    {reco.matches.length > 0 && (
                      <details className="mt-2">
                        <summary className="cursor-pointer text-[11px] text-amber-900 hover:underline">
                          참고용 후보 {reco.matches.length}건 보기 (게이트 미통과 — 근거로 쓰지 마세요)
                        </summary>
                        <div className="mt-2 space-y-1">
                          {reco.matches.map((m) => (
                            <div key={m.key} className="text-[11px] flex items-center gap-2">
                              <button onClick={() => goKey(m.key)}
                                className="shrink-0 font-mono text-sky-400 hover:underline">{m.key}</button>
                              <span className="truncate text-zinc-300">{m.summary}</span>
                            </div>
                          ))}
                        </div>
                      </details>
                    )}
                  </div>
                ) : (
                  <>
                    {/* 📮 고객 응대 답변 — 이 화면의 목적이다. 그래서 맨 위에 있고,
                        생성 전에도 자리를 지킨다. 아래 '근거' 는 이 답변을 뒷받침하는
                        재료이지 산출물이 아니다. */}
                    <section className="rounded-xl border border-emerald-900/60 bg-emerald-950/10 p-5">
                      <div className="mb-3 flex flex-wrap items-center gap-2">
                        <span className="text-sm font-semibold text-emerald-300">📮 고객 응대 답변</span>
                        {reco.reply_intent && (
                          <span className="rounded-full border border-zinc-700 px-2 py-0.5 text-[11px] text-zinc-300">
                            {reco.reply_intent}
                          </span>
                        )}
                        {reco.reply_lang && reco.reply_lang !== "ko" && (
                          <span className="rounded-full border border-sky-800 px-2 py-0.5 text-[11px] text-sky-300"
                            title="고객이 쓴 언어로 답합니다">{reco.reply_lang.toUpperCase()}</span>
                        )}
                        {reco.reply_proofread?.applied && (
                          <span className="rounded-full border border-zinc-700 px-2 py-0.5 text-[11px] text-zinc-400"
                            title="오타·띄어쓰기를 교정했습니다. 숫자·제품명·제목이 바뀌면 교정을 버리고 원문을 씁니다">
                            오타 교정됨
                          </span>
                        )}
                        {reco.reply_proofread?.rejected && (
                          <span className="rounded-full border border-amber-700 px-2 py-0.5 text-[11px] text-amber-300"
                            title={`교정본이 내용을 바꿔 폐기했습니다: ${reco.reply_proofread.rejected}`}>
                            교정 폐기
                          </span>
                        )}
                        {!explaining && reco.explanation && reco.explanation_cached !== undefined && (
                          <span title={reco.explanation_cached
                            ? "문의와 근거 사례가 그대로여서 저장된 답변을 재사용했습니다 (LLM 호출 없음)"
                            : "이번에 새로 생성했습니다"}
                            className={`inline-flex items-center whitespace-nowrap rounded-full border px-2.5 py-0.5 text-[11px] font-medium ${
                              reco.explanation_cached
                                ? "border-emerald-900/60 bg-emerald-950/60 text-emerald-400"
                                : "border-sky-900/60 bg-sky-950/60 text-sky-400"}`}>
                            {reco.explanation_cached ? "저장된 답변 재사용" : "새로 생성됨"}
                          </span>
                        )}
                        {reco.explanation && !explaining && (
                          <button onClick={reExplain}
                            title="캐시를 무시하고 지금 다시 생성합니다"
                            className="ml-auto text-[11px] text-zinc-400 underline decoration-dotted underline-offset-2 hover:text-sky-400">
                            다시 생성
                          </button>
                        )}
                      </div>

                      {!reco.explanation && !explaining ? (
                        <div className="rounded-lg border border-dashed border-zinc-700 bg-zinc-950/40 p-6 text-center">
                          <p className="text-sm text-zinc-400">
                            이 문의에 보낼 답변을 아직 만들지 않았습니다.
                          </p>
                          <button onClick={explain}
                            className="mt-3 inline-flex items-center rounded-lg border border-emerald-500/50 bg-emerald-500/10 px-4 py-2 text-sm font-medium text-emerald-300 transition hover:bg-emerald-500/20">
                            ✨ 고객 응대 답변 생성
                          </button>
                          <p className="mt-2 text-[11px] text-zinc-500">
                            과거 해결 사례를 근거로 쓰되, 내부 사례 번호·확정 일정 약속은 발송 전에 차단됩니다.
                          </p>
                        </div>
                      ) : (
                        <>
                          <div className="prose prose-sm prose-invert max-w-none prose-headings:text-emerald-300 prose-headings:my-2 prose-p:my-1">
                            <ReactMarkdown remarkPlugins={[remarkGfm]}
                              components={{ a: ({ node, ...p }) => <a {...p} target="_blank" rel="noreferrer" /> }}>
                              {linkifyKeys(reco.explanation || "생성 중…", jiraBase)}</ReactMarkdown>
                          </div>

                          {(reco.reply_policy_docs?.length ?? 0) > 0 && (
                          // 어떤 지침을 적용했는지 보여준다 — 검토자가 "왜 이렇게 답했나" 를
                          // 확인하려면 그 문단으로 갈 수 있어야 한다.
                          <div className="mt-3 border-t border-zinc-800 pt-3">
                            <div className="text-[11px] text-zinc-500">적용한 사내 지침</div>
                            <div className="mt-1 flex flex-wrap gap-1.5">
                              {reco.reply_policy_docs!.map((g, i) => (
                                g.url ? (
                                  <a key={i} href={g.url} target="_blank" rel="noreferrer"
                                    title={`${g.title} — 원문 열기 (새 탭)`}
                                    className="rounded border border-zinc-700 px-1.5 py-0.5 text-[11px] text-sky-400 hover:border-sky-600">
                                    📘 {g.section || g.title} ↗
                                  </a>
                                ) : (
                                  <span key={i} title={g.title}
                                    className="rounded border border-zinc-700 px-1.5 py-0.5 text-[11px] text-zinc-300">
                                    📘 {g.section || g.title}
                                  </span>
                                )
                              ))}
                            </div>
                          </div>
                        )}

                        {(reco.reply_policy?.violations?.length ?? 0) > 0 && (
                            <div className="mt-3 space-y-1 border-t border-zinc-800 pt-3">
                              {reco.reply_policy!.violations.map((v, i) => (
                                <div key={i} className={`rounded-lg border px-2 py-1 text-[12px] ${
                                  v.severity === "block"
                                    ? "border-rose-700 bg-rose-500/10 text-rose-300"
                                    : "border-amber-700 bg-amber-500/10 text-amber-300"}`}>
                                  <span className="font-medium">{v.severity === "block" ? "차단" : "경고"}</span>
                                  <span className="ml-2 opacity-70">{v.code}</span>
                                  <span className="ml-2">{v.detail}</span>
                                </div>
                              ))}
                            </div>
                          )}

                          {sel && !explaining && (
                            <div className="mt-3 border-t border-zinc-800 pt-3">
                              <button onClick={queueReply} disabled={queueing}
                                title="이 답변을 발송 대기 큐에 넣습니다 (고객에게 나가는 건 승인 시에만)"
                                className="inline-flex items-center rounded-lg border border-emerald-600/50 bg-emerald-500/10 px-4 py-2 text-sm font-medium text-emerald-300 transition hover:bg-emerald-500/20 disabled:opacity-40">
                                {queueing ? "추가 중…"
                                  : replyStatus?.state === "approved" ? "📮 후속 답변을 발송 대기로"
                                  : "📮 이 답변을 발송 대기로"}
                              </button>
                              <QNotice m={queueMsg} />
                            </div>
                          )}
                        </>
                      )}
                    </section>

                    {/* 근거 — 답변을 뒷받침하는 재료다. 예전에는 이게 화면의 주인공
                        이었고(🤖 AI 제안), 고객 답변은 그 아래 부록이었다. 순서가
                        일의 순서를 정한다. */}
                    {reco.proposal && (
                      <details className="rounded-xl border border-zinc-800 bg-zinc-900/60 p-5">
                        <summary className="cursor-pointer text-sm font-semibold text-zinc-200">
                          🔎 근거 — 유사 사례에서 확인된 원인·조치
                          <span className="ml-2 text-[11px] font-normal text-zinc-500">
                            (근거 {reco.proposal.based_on} · 신뢰도 {Math.round((reco.proposal.confidence ?? 0) * 100)}%)
                          </span>
                        </summary>
                        <div className="mt-3 w-40"><Bar value={reco.proposal.confidence} /></div>
                        <div className="mt-3 space-y-3 text-sm">
                          <div><span className="font-semibold text-red-400">🔍 예상 근본원인</span>
                            <div className="mt-1 prose prose-sm prose-invert max-w-none text-zinc-300 prose-p:my-1 prose-li:my-0.5 prose-ol:my-1 prose-ul:my-1">
                              <ReactMarkdown remarkPlugins={[remarkGfm]}>{reco.proposal.root_cause || "—"}</ReactMarkdown></div></div>
                          <div><span className="font-semibold text-emerald-400">✅ 권장 해결책</span>
                            <div className="mt-1 prose prose-sm prose-invert max-w-none text-zinc-300 prose-p:my-1 prose-li:my-0.5 prose-ol:my-1 prose-ul:my-1">
                              <ReactMarkdown remarkPlugins={[remarkGfm]}>{reco.proposal.resolution || "—"}</ReactMarkdown></div></div>
                          <div><span className="font-semibold text-zinc-300">↪ 임시 우회책</span>
                            <div className="mt-1 prose prose-sm prose-invert max-w-none text-zinc-300 prose-p:my-1 prose-li:my-0.5 prose-ol:my-1 prose-ul:my-1">
                              <ReactMarkdown remarkPlugins={[remarkGfm]}>{reco.proposal.workaround || "—"}</ReactMarkdown></div></div>
                        </div>
                        {sel && sel.status !== "완료" && (
                          <div className="mt-4">
                            <button onClick={draftRca} disabled={drafting}
                              title="이슈에 남길 내부 분석 코멘트 초안을 만들어 게시 대기에 추가 (Jira 게시는 승인 시에만)"
                              className="inline-flex items-center rounded-lg border border-zinc-600 px-4 py-2 text-sm font-medium text-zinc-200 transition hover:bg-zinc-800 disabled:opacity-40">
                              {drafting ? "초안 생성 중…" : "🧾 분석 코멘트 초안 (이슈에 기록) → 게시 대기"}
                            </button>
                            <QNotice m={draftMsg} />
                          </div>
                        )}
                      </details>
                    )}

                    {/* 유사 사례 */}
                    <section>
                      {/* 색상 기준이 되는 게이트 임계는 **응답에서** 읽는다. 프런트에
                          박아 두면 서버가 재보정될 때(모델 교체 등) 조용히 어긋난다. */}
                      <div className="flex items-center gap-2 mb-3">
                        <h3 className="text-sm font-semibold tracking-tight text-zinc-200">유사 과거 해결 사례 {reco.matches.length}건</h3>
                        {reco.matches.filter((m) => !m.known_issue).length >= 2 && (
                          <button onClick={promoteMatches} disabled={promoting}
                            title="같은 유형의 사례들을 하나로 묶습니다 — 이 유형에 발송한 답변이 다음 문의의 정본이 됩니다"
                            className="ml-auto rounded border border-zinc-600 px-2 py-0.5 text-[11px] text-zinc-300 hover:bg-zinc-800 disabled:opacity-40">
                            {promoting ? "묶는 중…" : "📚 반복 문의 유형으로 묶기"}
                          </button>
                        )}
                      </div>
                      {promoteMsg && <div className="mb-2 text-[11px] text-zinc-400">{promoteMsg}</div>}
                      {(() => {
                        const arts = Array.from(new Map(reco.matches.filter((m) => m.known_issue)
                          .map((m) => [m.known_issue!.id, m.known_issue!])).values());
                        return arts.length > 0 ? (
                          <div className="mb-3 rounded-lg border border-sky-900/60 bg-sky-950/40 px-3 py-2 text-[11px] text-sky-400">
                            📚 같은 <b>반복 문의 유형</b>입니다: {arts.map((a) => `${a.id} ${a.title}`).join(" · ")}
                          {" "}— 이 유형에 발송한 답변이 다음 문의의 정본이 됩니다.
                          </div>
                        ) : null;
                      })()}
                      <div className="space-y-3">
                        {reco.matches.map((m, mi) => (
                          <div key={m.key} className="rounded-xl border border-zinc-800 bg-zinc-900/60 p-4">
                            <div className="flex items-center gap-2 mb-1">
                              {/* 키를 누르면 **Jira 원문**이 열린다 — 사용자가 기대하는 동작이다.
                                  이 화면에서 열기는 따로 둔다(예전에는 그게 키에 걸려 있었다). */}
                              {jiraBase ? (
                                <a href={browseUrl(jiraBase, m.key)} target="_blank" rel="noreferrer"
                                  title={`Jira에서 ${m.key} 원문 열기 (새 탭)`}
                                  className="font-mono text-xs font-semibold text-sky-400 underline decoration-dotted underline-offset-2 hover:text-sky-300">
                                  {m.key}
                                </a>
                              ) : (
                                <button onClick={() => goKey(m.key)} title="이 사례를 화면에서 열기"
                                  className="font-mono text-xs font-semibold text-sky-400 hover:underline">{m.key}</button>
                              )}
                              <button onClick={() => goKey(m.key)} title="이 사례를 이 화면에서 열기"
                                className="rounded border border-zinc-700 px-1.5 text-[10px] text-zinc-400 hover:border-sky-600 hover:text-sky-400">
                                여기서 열기
                              </button>
                              <span className={`text-[10px] px-1.5 py-0.5 rounded ${CAT_COLOR[m.category] ?? CAT_FALLBACK}`}>{m.category}</span>
                              <span className="rounded bg-zinc-800 px-1.5 py-0.5 text-[10px] text-zinc-300">{m.chip}</span>
                              {m.verified && (
                                <span className="rounded bg-emerald-950/60 px-1.5 py-0.5 text-[10px] text-emerald-400" title="해결 검증 + 고객 확인 완료">✓ 검증됨</span>
                              )}
                              {m.known_issue && (
                                <span className="rounded bg-sky-950/60 px-1.5 py-0.5 text-[10px] text-sky-400" title={`반복 문의 유형: ${m.known_issue.title}`}>📚 {m.known_issue.id}</span>
                              )}
                              {m.lifecycle && m.lifecycle.warnings.length > 0 && (
                                <span className="rounded bg-amber-950/60 px-1.5 py-0.5 text-[10px] text-amber-400"
                                  title={`${m.lifecycle.warnings.join(" · ")}${m.lifecycle.fw_version ? ` · FW ${m.lifecycle.fw_version}` : ""}`}>
                                  ⚠ {m.lifecycle.warnings[0]}
                                </span>
                              )}
                              <span className="ml-auto flex items-center gap-2 text-[10px] text-zinc-400"
                                title={"관련도=reranker 재순위 점수(카드 정렬 기준) · 임베딩=bi-encoder 코사인"
                                  + (rrThr != null ? ` · 통과 임계 관련도 ${Math.round(rrThr * 100)}%` : "")
                                  + (cosThr != null ? ` · 임베딩 ${Math.round(cosThr * 100)}%` : "")}>
                                {m.rerank_score != null ? (
                                  <>
                                    <span>관련도 <b className={`font-bold ${scoreText(m.rerank_score, rrThr)}`}>{Math.round(m.rerank_score * 100)}%</b></span>
                                    {m.embed_cos != null && (
                                      <span>임베딩 <b className={`font-bold ${scoreText(m.embed_cos, cosThr)}`}>{Math.round(m.embed_cos * 100)}%</b></span>
                                    )}
                                  </>
                                ) : m.embed_cos != null ? (
                                  <span>유사도 <b className={`font-bold ${scoreText(m.embed_cos, cosThr)}`}>{Math.round(m.embed_cos * 100)}%</b></span>
                                ) : (
                                  // m.score 는 RRF 융합 점수다 — 순위를 합친 값이라 절댓값에
                                  // 의미가 없다(최상위 매치도 0.040 쯤 나온다). 예전에는 이걸
                                  // "유사도 0.040" 으로 보여줘 4% 유사도로 읽혔다.
                                  <span title="순위 융합 점수 — 카드 정렬에만 쓰는 내부 값이라 절댓값에 의미가 없습니다">
                                    관련도 <b className="text-zinc-400">측정 안 됨</b>
                                  </span>
                                )}
                              </span>
                            </div>
                            <div className="text-sm font-medium leading-snug">{m.summary}</div>
                            <details className="mt-2 text-xs text-zinc-300">
                              <summary className="cursor-pointer text-zinc-400 hover:text-sky-400">근본원인 · 해결책 보기</summary>
                              <div className="mt-2 space-y-1.5 border-l-2 border-zinc-800 pl-2">
                                <p><b className="text-red-400">근본원인:</b> {m.root_cause}</p>
                                <p><b className="text-emerald-400">해결책:</b> {m.resolution}</p>
                                {m.workaround && <p><b className="text-zinc-300">우회책:</b> {m.workaround}</p>}
                              </div>
                            </details>
                            {/* P1-3 유용성 피드백 */}
                            <div className="mt-2 flex items-center gap-2 border-t border-zinc-800 pt-2 text-[11px]">
                              <span className="text-zinc-400">이 사례가</span>
                              <button onClick={() => sendFeedback(m, mi + 1, { rating: "helpful" })}
                                title="이 추천이 도움됨"
                                className={`px-2 py-0.5 rounded-full border ${fb[m.key]?.rating === "helpful" ? "border-emerald-800/60 bg-emerald-950/40 text-emerald-400" : "border-zinc-700 text-zinc-300 hover:border-emerald-700"}`}>
                                👍 도움됨
                              </button>
                              <button onClick={() => sendFeedback(m, mi + 1, { rating: "not_helpful" })}
                                title="이 추천이 도움 안 됨"
                                className={`px-2 py-0.5 rounded-full border ${fb[m.key]?.rating === "not_helpful" ? "border-red-800/60 bg-red-950/40 text-red-400" : "border-zinc-700 text-zinc-300 hover:border-red-700"}`}>
                                👎 아님
                              </button>
                              <label className="ml-auto flex cursor-pointer items-center gap-1 text-zinc-400" title="이 사례가 실제 근본원인이었음(ROI·평가셋 정답)">
                                <input type="checkbox" checked={!!fb[m.key]?.actual}
                                  onChange={(e) => sendFeedback(m, mi + 1, { actual: e.target.checked, rating: fb[m.key]?.rating ?? "helpful" })}
                                  className="accent-sky-500" />
                                실제 근본원인
                              </label>
                            </div>
                          </div>
                        ))}
                      </div>
                    </section>
                  </>
                )}
              </>
            )}
          </div>
        )}
      </main>

      {/* 우: 이슈 관계 그래프 (너비 조정 + 접기) */}
      {rightOpen && (
        <div onPointerDown={(e) => startDrag("right", e)} title="드래그하여 너비 조정"
          className="w-1.5 shrink-0 cursor-col-resize bg-zinc-800 transition-colors hover:bg-sky-600 active:bg-sky-500" />
      )}
      {rightOpen ? (
      <aside style={{ width: rightW }} className="shrink-0 border-l border-zinc-800 bg-zinc-950 flex flex-col">
        <div className="p-4 border-b border-zinc-800">
          <div className="flex items-center justify-between">
            <div className="text-sm font-semibold tracking-tight text-zinc-200">🔗 이슈 관계 그래프</div>
            <button onClick={() => setRightOpen(false)} title="그래프 접기" aria-label="관계 그래프 접기"
              className="grid h-7 w-7 place-items-center rounded leading-none text-zinc-400
                         outline-none hover:bg-zinc-900 hover:text-sky-400
                         focus-visible:ring-1 focus-visible:ring-sky-500">▶</button>
          </div>
          <div className="mt-0.5 text-[11px] text-zinc-400">공유 엔티티(칩·분류·기술용어) 기반 · 노드 클릭 시 이동</div>
        </div>
        <div className="flex-1 overflow-y-auto p-3">
          {!sel ? (
            <div className="p-4 text-center text-xs text-zinc-400">이슈를 선택하면 관련 이슈들의 관계가 그래프로 표시됩니다.</div>
          ) : graphLoading ? (
            <div className="p-4" aria-busy="true">
              <div className="mx-auto h-32 w-32 animate-pulse rounded-full border-4 border-zinc-800" />
              <div className="mt-3 text-center text-xs text-zinc-400">관계 그래프 불러오는 중…</div>
            </div>
          ) : graphErr ? (
            <div className="p-4 text-center">
              <div className="rounded-lg border border-red-900/60 bg-red-950/40 px-3 py-2 text-xs text-red-400">
                관계 그래프를 불러오지 못했습니다 — {graphErr}
              </div>
              <button onClick={() => setSel({ ...sel })}
                className="mt-2 text-[11px] text-sky-400 underline hover:text-sky-300">다시 시도</button>
            </div>
          ) : !graph || graph.nodes.length <= 1 ? (
            <div className="p-4 text-center text-xs text-zinc-400">
              이 이슈와 엔티티(칩·분류·기술용어)를 공유하는 다른 이슈가 없습니다.
            </div>
          ) : (
            <>
              <RelationGraph data={graph} onSelect={goKey} />
              <div className="mt-3 space-y-1.5 text-[11px] text-zinc-400">
                <div className="flex items-center gap-2">
                  <svg width="26" height="8"><line x1="0" y1="4" x2="26" y2="4" stroke="#38bdf8" strokeWidth="5" strokeLinecap="round" /></svg>
                  선 굵기·노드 크기 = <b className="text-sky-400">rerank 관련도</b>
                </div>
                <div className="flex items-center gap-2">
                  <svg width="26" height="10"><circle cx="13" cy="5" r="4" fill="none" stroke="#38bdf8" strokeWidth="1.2" strokeDasharray="2 2" /></svg>
                  점선 테두리 = 같은 근본원인(동일 템플릿)
                </div>
                <div className="flex items-center gap-3 pt-1">
                  <span className="flex items-center gap-1"><span className="w-2.5 h-2.5 rounded-full" style={{ background: "#10b981" }} />진행 중</span>
                  <span className="flex items-center gap-1"><span className="w-2.5 h-2.5 rounded-full" style={{ background: "#94a3b8" }} />해야 할 일</span>
                  <span className="flex items-center gap-1"><span className="w-2.5 h-2.5 rounded-full" style={{ background: "#3b82f6" }} />완료</span>
                </div>
                <div className="pt-1 text-zinc-400">
                  중심 <span className="font-mono">{graph.center}</span> · 관련 {graph.nodes.length - 1}건
                  {graph.has_rerank === false && <span className="text-amber-400"> · (rerank 미적용: 엔티티 기반)</span>}
                </div>
              </div>
            </>
          )}
        </div>
      </aside>
      ) : (
        <button onClick={() => setRightOpen(true)} title="관계 그래프 펼치기"
          className="flex w-7 shrink-0 flex-col items-center justify-center gap-2 border-l border-zinc-800 bg-zinc-950 text-zinc-400 hover:bg-zinc-900 hover:text-sky-400">
          <span>◀</span>
          <span className="text-[10px] [writing-mode:vertical-rl]">관계 그래프</span>
        </button>
      )}
    </div>
  );
}
