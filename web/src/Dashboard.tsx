/** 지식 현황 대시보드.
 *
 * 백엔드가 이미 계산하지만 화면이 없던 신호들을 한곳에 모은다 — KB 구성, 인입 품질,
 * 중복 클러스터, 지식 모순, 지식 공백, 추천 효능, 수명주기, 개선 큐, 기여자, 반복 문의 유형.
 *
 * 카드마다 독립적으로 fetch 한다: 엔드포인트 하나가 실패해도 나머지는 보이고, 실패한
 * 카드만 재시도 버튼을 띄운다(전체 화면이 백지가 되는 것을 막는다).
 */

import { useCallback, useEffect, useState } from "react";
import { BarList, Donut, RateBar, StatTile, type BarItem } from "./charts";
import { EmptyState, ErrorNote, PageHeader, SectionTitle, Tag } from "./ui";

const API = (import.meta as any).env?.VITE_API ?? "";   // 빈 값 = 같은 오리진(개발은 vite 프록시)

/** 카드 하나의 로딩/실패/성공 상태를 담는 훅.
 *
 * rev 를 올리면 다시 읽는다 — 목록을 바꾸는 조작(개선 큐 상태 변경) 후 갱신용.
 */
function useEndpoint<T>(path: string, rev = 0) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);
  useEffect(() => {
    let alive = true;
    setLoading(true); setError("");
    fetch(`${API}${path}`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((d) => { if (alive) setData(d); })
      .catch((e) => { if (alive) setError(e.message || "불러오기 실패"); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [path, rev, tick]);
  const reload = useCallback(() => setTick((n) => n + 1), []);
  return { data, error, loading, reload };
}


/** 레버 = 그 원인을 고칠 수 있는 손잡이. 원인만 세면 대시보드에서 끝나고,
 *  레버까지 알아야 무엇을 할지가 정해진다. 서버(draft_feedback.LEVERS)와 같은 값. */
const LEVER_KO: Record<string, string> = {
  retrieval: "검색", generation: "생성", knowledge: "지식",
  presentation: "표현", other: "기타",
};
const LEVER_HINT: Record<string, string> = {
  retrieval: "근거 검색이 틀렸다 → 게이트·랭킹 파라미터를 동결 평가셋에 검증 후 적용",
  generation: "근거는 맞는데 글이 틀렸다 → 프롬프트 규칙 자동 주입 + 평가셋 보강",
  knowledge: "KB 에 답이 없거나 낡았다 → 사람이 RCA 작성·폐기",
  presentation: "내용은 맞고 형식이 틀렸다 → 검증기 규칙으로 승인 전 차단",
  other: "위 분류에 해당하지 않음",
};

/** 비율(0~1)을 퍼센트 문자열로. null 은 표본이 없다는 뜻이라 0% 로 쓰면 거짓말이 된다. */
function pct(v: number | null | undefined): string {
  return typeof v === "number" ? `${Math.round(v * 100)}%` : "—";
}

/** 임계 두 개로 색을 정한다 — 낮으면 빨강, 중간 주황, 높으면 초록. */
function rateTone(v: number | null | undefined, bad: number, good: number):
    "neutral" | "good" | "warn" | "bad" {
  if (typeof v !== "number") return "neutral";
  if (v < bad) return "bad";
  if (v < good) return "warn";
  return "good";
}

function trendLabel(t: any): string {
  if (!t?.enough_data) return "—";
  const d = Math.round((t.delta ?? 0) * 100);
  return `${d > 0 ? "+" : ""}${d}%p`;
}

function trendTone(t: any): "neutral" | "good" | "warn" | "bad" {
  if (!t?.enough_data) return "neutral";
  if ((t.delta ?? 0) <= -0.1) return "bad";
  if ((t.delta ?? 0) > 0) return "good";
  return "neutral";
}

function Card({ title, hint, wide, children, error, loading, onRetry }: {
  title: string; hint?: string; wide?: boolean; children: React.ReactNode;
  error?: string; loading?: boolean; onRetry?: () => void;
}) {
  return (
    <section className={`rounded-xl border border-zinc-800 bg-zinc-900/60 p-5 ${wide ? "xl:col-span-2" : ""}`}>
      <SectionTitle hint={hint}>{title}</SectionTitle>
      {loading ? (
        <div className="space-y-2" aria-busy="true">
          {[0, 1, 2].map((i) => <div key={i} className="h-3 rounded bg-zinc-800 animate-pulse" />)}
        </div>
      ) : error ? (
        <ErrorNote message={error} action={onRetry && (
          <button onClick={onRetry} className="underline hover:text-red-300">다시 시도</button>
        )} />
      ) : children}
    </section>
  );
}

/** 값이 0인 것과 데이터가 없는 것을 구분해서 알린다. */
function Ok({ text }: { text: string }) {
  return <div className="text-xs text-emerald-400">✓ {text}</div>;
}

export default function Dashboard({ onOpenIssue, can }: {
  onOpenIssue: (key: string) => void;
  /** 기능 권한 확인 — 권한 없는 조작 버튼은 아예 그리지 않는다(헛클릭 방지). */
  can: (cap: string) => boolean;
}) {
  // VOC 에이전트의 헤드라인 지표 — 초안이 사람 손을 안 타고 나갔는가.
  // 고객에게 실제로 나간 글의 성패 — 이 서비스의 최종 산출물이다.
  const reply = useEndpoint<any>("/voc/reply/stats");
  const draft = useEndpoint<any>("/rca/draft-feedback");
  const sources = useEndpoint<any>("/knowledge/sources");

  const reco = useEndpoint<any>("/reco/stats");
  const quality = useEndpoint<any>("/knowledge/quality");
  const clusters = useEndpoint<any>("/knowledge/clusters?threshold=0.80&min_size=2");
  const contra = useEndpoint<any>("/knowledge/contradictions");
  const gaps = useEndpoint<any>("/knowledge/gaps?top=8");
  const fb = useEndpoint<any>("/reco/feedback/stats");
  const outcomes = useEndpoint<any>("/knowledge/outcomes");
  const life = useEndpoint<any>("/knowledge/lifecycle/stats");
  const experts = useEndpoint<any>("/knowledge/experts?top=6");
  const articles = useEndpoint<any>("/knowledge/known-issues");
  const kstore = useEndpoint<any>("/knowledge/stats");
  const cache = useEndpoint<any>("/explain/cache");
  const metrics = useEndpoint<any>("/metrics");
  const [queueRev, setQueueRev] = useState(0);
  const queue = useEndpoint<any>("/improve/queue", queueRev);

  const setQueueState = async (id: string, state: string) => {
    try {
      await fetch(`${API}/improve/queue/state`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id, state }),
      });
    } finally { setQueueRev((n) => n + 1); }
  };

  const cats: BarItem[] = Object.entries(reco.data?.by_category ?? {})
    .map(([k, v]) => ({ label: k || "(미분류)", value: v as number }))
    .sort((a, b) => b.value - a.value);

  const fillRows: [string, number][] = (() => {
    const f = quality.data?.report?.fill;
    if (!f) return [];
    const out: [string, number][] = [];
    for (const group of ["all_required", "resolved_critical", "resolved_recommended"] as const) {
      for (const [k, v] of Object.entries(f[group] ?? {})) out.push([k, v as number]);
    }
    if (typeof f.category_classified === "number") out.push(["category_classified", f.category_classified]);
    if (typeof f.resolved_verified === "number") out.push(["resolved_verified", f.resolved_verified]);
    return out;
  })();

  const openQueue = (queue.data?.items ?? []).filter((it: any) => it.state === "open");

  return (
    <div className="h-full overflow-y-auto bg-zinc-950 px-6 pb-10 pt-8 sm:px-8">
      <PageHeader title="VOC 대응 현황"
        description="고객에게 나간 답변의 성패 + 그 답변을 떠받치는 지식 자산의 상태" />

      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
        {/* 고객 답변 — 유일하게 **고객이 직접 받는** 산출물이다. 그래서 맨 앞이다.
            RCA 초안 품질(다음 카드)과 섞지 않는다: 읽는 사람도 실패의 의미도 다르다. */}
        <Card title="고객 답변" wide
          hint="고객에게 나간 답변 — 무수정 발송률·발송률·요청 유형·정책 차단"
          loading={reply.loading} error={reply.error} onRetry={reply.reload}>
          {(reply.data?.total ?? 0) === 0 ? (
            <EmptyState message="아직 작성된 고객 답변이 없습니다. 분석 화면에서 “📮 고객 답변 초안”을 만들면 여기에 쌓입니다." />
          ) : (
            <>
              <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 mb-3">
                <StatTile label="무수정 발송" value={pct(reply.data.clean_rate)}
                  tone={rateTone(reply.data.clean_rate, 0.5, 0.75)}
                  title="사람이 손대지 않고 그대로 나간 비율" />
                <StatTile label="발송률" value={pct(reply.data.send_rate)}
                  tone={rateTone(reply.data.send_rate, 0.6, 0.85)}
                  title="판정된 초안 중 실제로 발송된 비율" />
                <StatTile label="발송 대기" value={reply.data.pending} />
                <StatTile label="근거 없이 작성" value={reply.data.no_evidence}
                  tone={reply.data.no_evidence ? "warn" : "neutral"}
                  title="유사 사례를 못 찾아 원인을 단정하지 않는 골격으로 쓴 건수 — 지식 공백" />
              </div>
              <div className="flex flex-wrap gap-1.5">
                {Object.entries(reply.data.by_intent ?? {}).map(([k, v]) => (
                  <span key={k} className="rounded-full border border-zinc-700 px-2 py-0.5 text-[11px] text-zinc-300">
                    {k} {v as number}
                  </span>
                ))}
              </div>
              {Object.keys(reply.data.policy_violations ?? {}).length > 0 && (
                <div className="mt-2 flex flex-wrap gap-1.5">
                  {Object.entries(reply.data.policy_violations).map(([k, v]) => (
                    <span key={k} className="rounded-full border border-amber-700 px-2 py-0.5 text-[11px] text-amber-300"
                      title="발송 전 정책 검사에 걸린 항목 — 차단은 고쳐야 나가고, 경고는 사람이 판단한다">
                      {k} {v as number}
                    </span>
                  ))}
                </div>
              )}
            </>
          )}
        </Card>

        {/* RCA 초안 품질 — 엔지니어가 읽는 분석의 성패. 고객 답변과 분리해서 본다. */}
        <Card title="RCA 초안 품질" wide
          hint="RCA 분석 초안이 사람 손을 안 타고 게시된 비율 — 엔지니어용 산출물 지표"
          loading={draft.loading} error={draft.error} onRetry={draft.reload}>
          {(draft.data?.stats?.judged ?? 0) === 0 ? (
            <EmptyState message="아직 판정된 초안이 없습니다. 승인 대기 화면에서 초안을 승인·거부하면 여기에 쌓입니다." />
          ) : (
            <>
              <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 mb-3">
                <StatTile label="무수정 게시" value={pct(draft.data.stats.clean_rate)}
                  tone={rateTone(draft.data.stats.clean_rate, 0.5, 0.75)}
                  sub={`${draft.data.stats.accepted_clean}건`}
                  title="초안 그대로 Jira 에 나간 비율 — 진짜 품질 지표" />
                <StatTile label="게시율" value={pct(draft.data.stats.accept_rate)}
                  tone={rateTone(draft.data.stats.accept_rate, 0.7, 0.9)}
                  sub={`거부 ${draft.data.stats.rejected}건`}
                  title="거부되지 않고 게시까지 간 비율" />
                <StatTile label="사람 수정" value={draft.data.stats.edited}
                  sub={`판정 ${draft.data.stats.judged}건`}
                  title="게시는 됐지만 사람이 고친 건수" />
                <StatTile label="추세" value={trendLabel(draft.data.trend)}
                  tone={trendTone(draft.data.trend)}
                  sub={draft.data.trend?.enough_data
                    ? `최근 ${draft.data.trend.window}건 기준` : "표본 부족"}
                  title="최근 구간의 무수정 게시율이 그 이전보다 오르고 있는가" />
              </div>

              <div className="grid gap-4 lg:grid-cols-2">
                <div>
                  <div className="text-[11px] text-zinc-400 mb-1.5">
                    결함 원인 — 레버별로 고치는 방법이 다르다
                  </div>
                  <BarList labelW={132}
                    items={(draft.data.stats.by_cause ?? []).slice(0, 6).map((c: any) => ({
                      label: c.label, value: c.count,
                      hint: `${c.label} — 레버: ${LEVER_KO[c.lever] ?? c.lever}`,
                    }))}
                    emptyText="분류된 원인이 아직 없습니다" />
                  <div className="mt-2 flex flex-wrap gap-1">
                    {Object.entries(draft.data.stats.by_lever ?? {})
                      .sort((a: any, b: any) => b[1] - a[1])
                      .map(([lv, n]: any) => (
                        <Tag key={lv} title={LEVER_HINT[lv] ?? ""}>
                          {LEVER_KO[lv] ?? lv} {n}
                        </Tag>
                      ))}
                  </div>
                </div>

                <div>
                  <div className="text-[11px] text-zinc-400 mb-1.5">
                    실패율 높은 고장군 — 여기부터 손대면 된다
                  </div>
                  <BarList labelW={148} unit="%"
                    items={(draft.data.by_class ?? []).slice(0, 6).map((c: any) => ({
                      label: c.template || "(미분류)",
                      value: Math.round((c.failure_rate ?? 0) * 100),
                      hint: `판정 ${c.judged}건 · 거부 ${c.rejected} / 수정 ${c.edited}`
                        + (c.top_causes?.[0] ? ` · 최다 원인 ${c.top_causes[0].cause}` : ""),
                    }))}
                    max={100} emptyText="클래스별 집계가 아직 없습니다" />
                  <div className="mt-2 text-[11px] text-zinc-500">
                    막대는 거부·수정 비율(%). 원인이 2회 이상 모이면 다음 초안의 생성 규칙에
                    자동 반영되고, 개선 큐에 레버별 조치가 올라간다.
                  </div>
                </div>
              </div>
            </>
          )}
        </Card>

        {/* KB 원천 — 무엇이 지식베이스를 이루는가(Jira 미러 + 보조 원천) */}
        <Card title="KB 원천" hint="지식베이스를 이루는 파일과 각각의 근거 기여량"
          loading={sources.loading} error={sources.error} onRetry={sources.reload}>
          <div className="grid grid-cols-3 gap-2 mb-3">
            <StatTile label="적재 총계" value={sources.data?.total ?? "—"}
              sub="중복 제거 후" title="파일별 건수를 더한 값과 다르면 키가 겹친 것이다" />
            <StatTile label="해결(근거)" value={sources.data?.resolved ?? "—"} sub="검색 대상" />
            <StatTile label="큐레이션" value={sources.data?.curated_in_live ?? "—"}
              sub="승인 RCA 환류" title="사람이 승인·수정해 KB 에 되돌아온 분석" />
          </div>
          <BarList labelW={150}
            items={(sources.data?.sources ?? []).map((x: any) => ({
              label: `${x.key_prefixes?.[0] ?? x.path} (${x.kind === "jira_mirror" ? "미러" : "보조"})`,
              value: x.resolved,
              hint: `${x.path} — 전체 ${x.total}건 / 해결 ${x.resolved}건`,
            }))}
            emptyText="원천 정보 없음" />
          {(sources.data?.missing_sources ?? []).length > 0 && (
            <div className="mt-2 text-[11px] text-amber-400">
              찾을 수 없는 원천: {sources.data.missing_sources.join(", ")}
            </div>
          )}
        </Card>

        {/* KB 구성 */}
        <Card title="KB 구성" hint="분류별 해결 사례 분포" loading={reco.loading} error={reco.error}
          onRetry={reco.reload}>
          <div className="grid grid-cols-3 gap-2 mb-3">
            <StatTile label="해결 KB" value={reco.data?.resolved ?? "—"} sub="검색 대상" />
            <StatTile label="미해결" value={reco.data?.unresolved ?? "—"} sub="분석 대기" />
            <StatTile label="고장 템플릿" value={reco.data?.templates ?? "—"} sub="근본원인 클래스" />
          </div>
          <Donut items={cats} centerLabel="해결 사례" centerValue={reco.data?.resolved} />
          <div className="mt-2 text-[11px] text-zinc-400">검색 방식: {reco.data?.method ?? "—"}</div>
        </Card>

        {/* 인입 품질 */}
        <Card title="인입 품질" hint="필드 추출 성공률 — 낮으면 검색이 약해진다"
          loading={quality.loading} error={quality.error} onRetry={quality.reload}>
          <div className="space-y-1.5">
            {fillRows.length
              ? fillRows.map(([k, v]) => <RateBar key={k} label={k} value={v} />)
              : <div className="text-xs text-zinc-400">데이터 없음</div>}
          </div>
          {quality.data && (
            <div className="mt-3 pt-2 border-t border-zinc-800 text-xs">
              {quality.data.violations?.length ? (
                <div className="text-amber-400">⚠ 위반 {quality.data.violations.length}건: {quality.data.violations.join(" / ")}</div>
              ) : <Ok text="품질 게이트 위반 없음" />}
              {quality.data.report?.deficient_resolved_keys?.length > 0 && (
                <div className="mt-1 text-zinc-400">
                  필드 결손: {quality.data.report.deficient_resolved_keys.map((k: string) => (
                    <button key={k} onClick={() => onOpenIssue(k)}
                      className="font-mono text-sky-400 hover:underline mr-1.5">{k}</button>
                  ))}
                </div>
              )}
            </div>
          )}
        </Card>

        {/* 추천 효능 */}
        <Card title="추천 효능" hint="사람 피드백 + 실제 해결 여부"
          loading={fb.loading || outcomes.loading} error={fb.error || outcomes.error}
          onRetry={() => { fb.reload(); outcomes.reload(); }}>
          <div className="grid grid-cols-2 gap-2">
            <StatTile label="도움됨률" tone={(fb.data?.stats?.helpful_rate ?? 0) >= 0.7 ? "good" : "warn"}
              value={fb.data?.stats?.total ? `${Math.round((fb.data.stats.helpful_rate ?? 0) * 100)}%` : "—"}
              sub={`피드백 ${fb.data?.stats?.total ?? 0}건`}
              title="매치 카드의 👍/👎 집계" />
            <StatTile label="RCA 효능"
              tone={(outcomes.data?.efficacy_rate ?? 0) >= 0.5 ? "good" : "warn"}
              value={outcomes.data?.total_tracked ? `${Math.round((outcomes.data.efficacy_rate ?? 0) * 100)}%` : "—"}
              sub={`추적 ${outcomes.data?.total_tracked ?? 0}건 · 대기 ${outcomes.data?.pending ?? 0}`}
              title="게시된 RCA 이후 이슈가 실제로 해결된 비율" />
          </div>
          {fb.data?.stats?.top_helpful_matches?.length > 0 && (
            <div className="mt-3">
              <div className="text-[11px] text-zinc-400 mb-1">가장 도움된 사례</div>
              <BarList items={fb.data.stats.top_helpful_matches.map((m: any) => ({
                label: m.match_key, value: m.net, hint: `${m.match_key} 순추천 ${m.net}`,
                onClick: () => onOpenIssue(m.match_key),
              }))} unit="점" labelW={72} />
            </div>
          )}
          {fb.data?.stats?.total === 0 && (
            <div className="mt-3 text-[11px] text-zinc-400">
              아직 피드백이 없습니다 — 분석 화면의 매치 카드에서 👍/👎 를 누르면 쌓입니다.
            </div>
          )}
        </Card>

        {/* 중복 지식 */}
        <Card title="중복 지식 (클러스터)" wide
          hint="유사도 0.80 이상으로 뭉친 사례 — 반복 문의 유형으로 승격 대상"
          loading={clusters.loading} error={clusters.error} onRetry={clusters.reload}>
          {clusters.data?.count ? (
            <>
              <div className="text-xs text-zinc-400 mb-2">
                클러스터 {clusters.data.count}개 — 같은 근본원인이 여러 이슈에 흩어져 있다는 신호
              </div>
              <div className="space-y-2 max-h-72 overflow-y-auto pr-1">
                {clusters.data.clusters.slice(0, 12).map((c: any) => (
                  <div key={c.representative} className="border border-zinc-800 rounded-lg p-2">
                    <div className="flex items-center gap-2 text-[11px] mb-1">
                      <span className="font-semibold text-zinc-300">{c.size}건</span>
                      <span className="text-zinc-400">평균 유사도 {c.avg_similarity}</span>
                      {c.chips?.length > 0 && (
                        <span className="px-1.5 rounded bg-zinc-800 text-zinc-300">{c.chips.join(", ")}</span>
                      )}
                      {c.categories?.length > 0 && (
                        <span className="px-1.5 rounded bg-sky-500/10 text-sky-400">{c.categories.join(", ")}</span>
                      )}
                      {c.verified_count > 0 && (
                        <span className="text-emerald-400">✓ 검증 {c.verified_count}</span>
                      )}
                    </div>
                    <div className="text-xs text-zinc-300 line-clamp-2 mb-1">
                      {c.sample_summaries?.[0]?.summary}
                    </div>
                    <div className="flex flex-wrap gap-1">
                      {c.members.map((k: string) => (
                        <button key={k} onClick={() => onOpenIssue(k)}
                          className={`text-[10px] font-mono px-1.5 py-0.5 rounded border hover:bg-zinc-800 ${
                            k === c.representative
                              ? "border-sky-700/60 text-sky-300 font-semibold"
                              : "border-zinc-800 text-zinc-400"}`}
                          title={k === c.representative ? "대표 사례" : "클릭하면 분석 화면에서 엽니다"}>{k}</button>
                      ))}
                    </div>
                  </div>
                ))}
              </div>
              {clusters.data.count > 12 && (
                <div className="mt-2 text-[11px] text-zinc-400">상위 12개만 표시 (전체 {clusters.data.count}개)</div>
              )}
            </>
          ) : <Ok text="중복 클러스터 없음" />}
        </Card>

        {/* 개선 큐 */}
        <Card title="개선 큐" hint="자기개선 루프가 제안한 조치"
          loading={queue.loading} error={queue.error} onRetry={queue.reload}>
          {openQueue.length ? (
            <div className="space-y-2 max-h-72 overflow-y-auto pr-1">
              {openQueue.map((it: any) => (
                <div key={it.id} className="border border-zinc-800 rounded-lg p-2">
                  <div className="flex items-center gap-1.5 text-[11px] mb-1">
                    <span className={`px-1.5 rounded font-semibold ${
                      it.priority === "P1" ? "bg-red-950/40 text-red-400"
                        : it.priority === "P2" ? "bg-amber-50 text-amber-400"
                        : "bg-zinc-800 text-zinc-300"}`}>{it.priority}</span>
                    <span className="text-zinc-400">{it.type}</span>
                    {it.target && (
                      <button onClick={() => onOpenIssue(it.target)}
                        className="font-mono text-sky-400 hover:underline">{it.target}</button>
                    )}
                  </div>
                  <div className="text-xs text-zinc-300">{it.rationale}</div>
                  {can("improve.manage") && (
                  <div className="mt-1.5 flex gap-1.5">
                    <button onClick={() => setQueueState(it.id, "done")}
                      className="text-[10px] px-2 py-0.5 rounded border border-emerald-800/60 text-emerald-300 hover:bg-emerald-950/40">
                      완료 처리
                    </button>
                    <button onClick={() => setQueueState(it.id, "dismissed")}
                      className="text-[10px] px-2 py-0.5 rounded border border-zinc-800 text-zinc-400 hover:bg-zinc-800">
                      보류
                    </button>
                  </div>
                  )}
                </div>
              ))}
            </div>
          ) : <Ok text={`열린 제안 없음 (전체 ${queue.data?.items?.length ?? 0}건)`} />}
        </Card>

        {/* 지식 모순 */}
        <Card title="지식 모순" hint="같은 고장모드인데 근본원인이 엇갈리는 쌍"
          loading={contra.loading} error={contra.error} onRetry={contra.reload}>
          {contra.data?.count ? (
            <div className="space-y-2 max-h-60 overflow-y-auto pr-1">
              {contra.data.contradictions.map((c: any, i: number) => (
                <div key={i} className="border border-amber-900/60 bg-amber-950/40 rounded-lg p-2 text-xs">
                  <div className="flex items-center gap-1.5 mb-1">
                    <span className="text-zinc-400">사례 유사 {c.doc_similarity}</span>
                    <span className="text-amber-400 ml-auto">근본원인 유사 {c.root_cause_similarity}</span>
                  </div>
                  {([["a", c.a, c.summary_a, c.root_cause_a], ["b", c.b, c.summary_b, c.root_cause_b]] as const)
                    .map(([slot, key, summary, rc]) => (
                    <div key={slot} className="mt-1 pl-2 border-l-2 border-amber-300">
                      <button onClick={() => onOpenIssue(key)}
                        className="font-mono text-[11px] text-sky-400 hover:underline">{key}</button>
                      <div className="text-zinc-300 line-clamp-1">{summary}</div>
                      <div className="text-zinc-400 line-clamp-2">근본원인: {rc}</div>
                    </div>
                  ))}
                </div>
              ))}
            </div>
          ) : (
            <>
              <Ok text="모순 없음" />
              <div className="mt-1 text-[11px] text-zinc-400">
                기준: 사례 유사도 ≥ {contra.data?.params?.sim_hi ?? "—"} 이면서 근본원인 유사도 ≤ {contra.data?.params?.rc_lo ?? "—"}
              </div>
            </>
          )}
        </Card>

        {/* 지식 공백 */}
        <Card title="지식 공백" hint="coverage 게이트를 통과 못한 질의 — KB가 비어 있는 영역"
          loading={gaps.loading} error={gaps.error} onRetry={gaps.reload}>
          {gaps.data?.total_gap_events ? (
            <>
              <div className="grid grid-cols-2 gap-2 mb-2">
                <StatTile label="공백 이벤트" value={gaps.data.total_gap_events} tone="warn" />
                <StatTile label="미충족 템플릿" value={gaps.data.top_underserved_templates?.length ?? 0} />
              </div>
              <BarList items={Object.entries(gaps.data.by_category ?? {})
                .map(([k, v]) => ({ label: k || "(미분류)", value: v as number }))
                .sort((a, b) => b.value - a.value)} />
            </>
          ) : (
            <>
              <Ok text="기록된 지식 공백 없음" />
              <div className="mt-1 text-[11px] text-zinc-400">
                게이트를 통과하지 못한 질의가 여기 쌓입니다 — 어떤 고장 유형의 사례가 부족한지 알려줍니다.
              </div>
            </>
          )}
        </Card>

        {/* 수명주기 + 큐레이션 저장소 */}
        <Card title="지식 수명주기" hint="폐기·대체된 사례와 신선도 반감기"
          loading={life.loading || kstore.loading} error={life.error || kstore.error}
          onRetry={() => { life.reload(); kstore.reload(); }}>
          <div className="grid grid-cols-2 gap-2">
            <StatTile label="폐기(deprecated)" value={life.data?.stats?.deprecated ?? "—"}
              tone={(life.data?.stats?.deprecated ?? 0) > 0 ? "warn" : "good"} />
            <StatTile label="대체(superseded)" value={life.data?.stats?.superseded ?? "—"}
              tone={(life.data?.stats?.superseded ?? 0) > 0 ? "warn" : "good"} />
            <StatTile label="신선도 반감기" value={`${life.data?.stats?.halflife_days ?? "—"}일`}
              title="이 기간이 지나면 사례의 신선도 점수가 절반이 된다" />
            <StatTile label="큐레이션 지식" value={kstore.data?.knowledge?.total ?? "—"}
              sub={kstore.data?.knowledge?.tracked_in_git ? "git 추적됨" : "git 미추적"}
              title="사람이 승인·수정해 KB에 환류된 RCA" />
          </div>
        </Card>

        {/* 반복 문의 유형 */}
        <Card title="반복 문의 유형 (Known-Issue)" hint="같은 문의를 하나로 묶은 정본 — 발송한 답변이 다음 문의의 기준이 된다"
          loading={articles.loading} error={articles.error} onRetry={articles.reload}>
          {articles.data?.articles?.length ? (
            <div className="space-y-1.5 max-h-60 overflow-y-auto pr-1">
              {articles.data.articles.map((a: any) => (
                <div key={a.id} className="border border-zinc-800 rounded-lg p-2">
                  <div className="text-[11px] font-semibold text-sky-300">{a.id}</div>
                  <div className="text-xs text-zinc-300 line-clamp-2">{a.title}</div>
                </div>
              ))}
            </div>
          ) : (
            <>
              <Ok text="기사 없음" />
              <div className="mt-1 text-[11px] text-zinc-400">
                VOC 대응 화면에서 "📚 반복 문의 유형으로 묶기"로 만들 수 있습니다.
              </div>
            </>
          )}
        </Card>

        {/* 분석 캐시·예열 */}
        <Card title="분석 캐시 · 예열" hint="같은 입력이면 다시 생성하지 않는다"
          loading={cache.loading} error={cache.error} onRetry={cache.reload}>
          <div className="grid grid-cols-2 gap-2">
            <StatTile label="저장된 분석" value={cache.data?.cache?.entries ?? "—"}
              sub={cache.data?.cache?.bytes != null ? `${Math.round(cache.data.cache.bytes / 1024)} KB` : ""}
              title="질의·근거·모델·프롬프트 버전으로 주소가 정해지는 콘텐츠 캐시" />
            <StatTile label="예열 생성" value={cache.data?.prewarm?.done ?? "—"}
              tone={cache.data?.prewarm?.failed ? "warn" : "neutral"}
              sub={`건너뜀 ${cache.data?.prewarm?.skipped ?? 0} · 실패 ${cache.data?.prewarm?.failed ?? 0}`}
              title="이미 캐시에 있으면 건너뛴다" />
          </div>
          <div className="mt-2 text-[11px] text-zinc-400">
            프롬프트 버전 {cache.data?.cache?.prompt_version ?? "—"}
            {cache.data?.prewarm?.running && <span className="ml-2 text-sky-400">예열 진행 중…</span>}
            {cache.data?.prewarm?.error && <span className="ml-2 text-amber-400">최근 오류: {cache.data.prewarm.error}</span>}
          </div>
          {can("ops.cache") && (
          <div className="mt-3 flex gap-1.5">
            <button onClick={async () => {
                await fetch(`${API}/explain/prewarm`, { method: "POST" }).catch(() => {});
                cache.reload();
              }}
              className="rounded border border-zinc-600 px-2 py-0.5 text-[10px] text-zinc-300 hover:bg-zinc-800">
              지금 예열
            </button>
            <button onClick={async () => {
                await fetch(`${API}/explain/cache`, { method: "DELETE" }).catch(() => {});
                cache.reload();
              }}
              title="프롬프트를 바꿨는데 버전을 안 올렸을 때만 필요합니다"
              className="rounded border border-zinc-700 px-2 py-0.5 text-[10px] text-zinc-400 hover:bg-zinc-800">
              캐시 비우기
            </button>
          </div>
          )}
        </Card>

        {/* 서빙 지연 */}
        <Card title="서빙 지연" hint="최근 요청의 단계별 분포 — 회귀 감시"
          loading={metrics.loading} error={metrics.error} onRetry={metrics.reload}>
          {metrics.data?.recommend?.count ? (
            <>
              <div className="grid grid-cols-2 gap-2 mb-3">
                <StatTile label="/recommend p50"
                  value={`${metrics.data.recommend.stages_ms?.total_ms?.p50 ?? "—"}ms`}
                  sub={`p90 ${metrics.data.recommend.stages_ms?.total_ms?.p90 ?? "—"}ms`}
                  tone={(metrics.data.recommend.stages_ms?.total_ms?.p50 ?? 0) > 1000 ? "warn" : "neutral"}
                  title="캐시 히트를 제외한 실제 검색 경로" />
                <StatTile label="캐시 히트율"
                  value={metrics.data.recommend.cache_hit_rate != null
                    ? `${Math.round(metrics.data.recommend.cache_hit_rate * 100)}%` : "—"}
                  sub={`히트 ${metrics.data.recommend.cache_hits} / ${metrics.data.recommend.count}건`}
                  tone="good" />
              </div>
              <BarList unit="ms" labelW={92}
                items={Object.entries(metrics.data.recommend.stages_ms || {})
                  .filter(([k]) => k !== "total_ms")
                  .map(([k, v]: any) => ({ label: k.replace("_ms", ""), value: v.p50 ?? 0,
                                           hint: `${k} p50 ${v.p50}ms · p90 ${v.p90}ms (n=${v.n})` }))} />
              <div className="mt-2 text-[11px] text-zinc-400">
                최근 {metrics.data.window}건 기준 · 캐시 히트 p50{" "}
                {metrics.data.recommend.cached_total_ms?.p50 ?? "—"}ms
                {metrics.data.rerank_failures > 0 && (
                  <span className="ml-2 text-amber-400">rerank 실패 {metrics.data.rerank_failures}건</span>
                )}
              </div>
            </>
          ) : (
            <div className="text-xs text-zinc-400">아직 요청이 없습니다 — 분석을 몇 번 돌리면 채워집니다.</div>
          )}
        </Card>

        {/* 기여 전문가 */}
        <Card title="기여 전문가" hint="RCA 승인·수정 기여자"
          loading={experts.loading} error={experts.error} onRetry={experts.reload}>
          {experts.data?.experts?.length ? (
            <BarList items={experts.data.experts.map((e: any) => ({
              label: e.author, value: e.contributions, hint: `${e.author}: ${e.keys?.join(", ")}`,
            }))} unit="건" labelW={150} />
          ) : <div className="text-xs text-zinc-400">데이터 없음</div>}
        </Card>
      </div>
    </div>
  );
}
