import { StrictMode, useEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import FailureAnalysis from './FailureAnalysis.tsx'
import Onboarding from './Onboarding.tsx'
import RcaQueue from './RcaQueue.tsx'
import VocPage from './VocPage.tsx'
import Dashboard from './Dashboard.tsx'
import { useRoute, type Route } from './useDeepLink'
import { AuthProvider, LoginScreen, RoleBadge, useAuth } from './auth.tsx'

const API = (import.meta as any).env?.VITE_API ?? "";   // 빈 값 = 같은 오리진(개발은 vite 프록시)

// 세션이 HttpOnly 쿠키에 있으므로 API 오리진 요청에는 credentials 가 실려야 한다.
// 호출부를 전부 고치는 대신 여기서 한 번 감싼다(EventSource 는 withCredentials 로 별도 처리).
{
  const orig = window.fetch;
  window.fetch = (input: any, init: any = {}) => {
    const url = typeof input === "string" ? input : (input?.url ?? "");
    if (url.startsWith(API) && init.credentials === undefined) {
      init = { ...init, credentials: "include" };
    }
    return orig(input, init);
  };
}

function Shell() {
  const [status, setStatus] = useState<any | undefined>(undefined); // undefined = 로딩
  const [pending, setPending] = useState(0);
  // 화면 상태를 URL 해시로 옮겼다 — 새로고침·뒤로가기·링크 공유가 동작하고,
  // 대시보드에서 이슈로 바로 들어오는 드릴다운도 같은 경로를 쓴다.
  const [route, go] = useRoute();
  // closeSettings 는 "설정 저장이 끝났으니 설정 화면을 닫아라" 는 뜻이다.
  // 기본값을 false 로 둔다 — 초기 로드에서도 닫으면 #/settings 로 직접 들어올 수 없다.
  const load = (closeSettings = false) => fetch(`${API}/config/status`).then((r) => r.json())
    .then((s) => { setStatus(s); if (closeSettings && route.view === "settings") go({ view: "app" }, false); })
    .catch(() => setStatus({ ready: false, _err: true }));
  const loadPending = () => fetch(`${API}/rca/pending`).then((r) => r.json())
    .then((d) => setPending(d.counts?.pending ?? 0)).catch(() => {});
  useEffect(() => { load(); loadPending(); }, []);

  const { me, cfg, loading: authLoading, can, logout, reload: reloadAuth } = useAuth();
  // 설정 화면·온보딩은 관리자 작업이다 — 권한이 없으면 강제하지 않는다.
  const isAdmin = can("config.write");
  const showOnboarding = status !== undefined && isAdmin
    && (route.view === "settings" || !status.ready);
  const jiraBase = status?.jira?.base_url ?? "";

  // 네비게이션 항목 — 사이드바를 하나 더 두면 3분할(목록/본문/그래프)이 좁아지므로
  // 상단바를 유지하되, 하네스의 활성/비활성 톤(zinc-800 채움 vs zinc-400 글자)을 쓴다.
  const navItem = (view: Route["view"], icon: string, label: string, title: string,
                   onClick?: () => void, badge?: number) => {
    const active = route.view === view && !showOnboarding;
    return (
      <button onClick={onClick ?? (() => go({ view } as Route))} title={title}
        className={`flex items-center gap-2 rounded-lg px-3 py-1.5 text-sm transition-colors outline-none focus-visible:ring-1 focus-visible:ring-sky-500 ${
          active ? "bg-zinc-800 font-medium text-zinc-50" : "text-zinc-300 hover:bg-zinc-900 hover:text-zinc-200"}`}>
        <span className="w-4 text-center text-zinc-400">{icon}</span>
        {label}
        {badge ? (
          <span className="rounded-full bg-sky-500/15 px-1.5 text-[11px] font-medium text-sky-300">{badge}</span>
        ) : null}
      </button>
    );
  };

  return (
    <div className="h-screen flex flex-col bg-zinc-950 text-zinc-200">
      {/* 본문 바로가기 — 이슈 목록이 통째로 탭 순서에 들어가서, 키보드로 본문까지
          가려면 Tab 을 273번 눌러야 했다(실측). 평소엔 화면 밖에 숨고 포커스를
          받으면 나타난다. */}
      <a href="#main-content"
        className="sr-only focus:not-sr-only focus:absolute focus:left-3 focus:top-3 focus:z-50
                   focus:rounded-lg focus:bg-sky-600 focus:px-3 focus:py-2 focus:text-sm
                   focus:font-medium focus:text-white focus:outline-none focus:ring-2 focus:ring-sky-300">
        본문 바로가기
      </a>
      <nav className="flex shrink-0 items-center gap-1 border-b border-zinc-800 bg-zinc-950 px-4 h-14">
        <button onClick={() => go({ view: "app" })} title="홈(분석 화면)으로"
          className="mr-4 text-left outline-none focus-visible:ring-1 focus-visible:ring-sky-500 rounded">
          <span className="block text-sm font-semibold tracking-tight text-zinc-50">VOC Agent</span>
          <span className="block text-[11px] text-zinc-400">고객 문의(VOC) 답변 — 과거 해결 사례 기반</span>
        </button>
        {navItem("app", "◎", "분석", "미해결 이슈의 근본원인·해결책 추천")}
        {status?.ready && navItem("dashboard", "▤", "VOC 답변 현황", "초안 품질(무수정 게시율·결함 원인) + KB 구성·품질·공백")}
        {status?.ready && !showOnboarding && (
          <div className="ml-auto flex items-center gap-1">
            {can("rca.read") && navItem("rca", "↗", "승인 대기", "RCA 댓글 승인 대기 (HITL)",
              () => { go({ view: route.view === "rca" ? "app" : "rca" }); loadPending(); }, pending)}
            {can("voc.manage") && navItem("voc", "✎", "VOC", "서비스 의견 (VOC)",
              () => go({ view: route.view === "voc" ? "app" : "voc" }))}
            {isAdmin && navItem("settings", "⚙", "설정", "설정 변경")}
            {me && (
              <div className="ml-2 flex items-center gap-2 border-l border-zinc-800 pl-3">
                <RoleBadge me={me} />
                <span className="max-w-[140px] truncate text-[11px] text-zinc-400"
                  title={me.subject}>{me.name || me.subject}</span>
                {me.via !== "disabled" && (
                  <button onClick={logout} title="로그아웃"
                    className="text-[11px] text-zinc-400 underline decoration-dotted underline-offset-2 hover:text-sky-400">
                    로그아웃
                  </button>
                )}
              </div>
            )}
          </div>
        )}
      </nav>
      {/* tabIndex=-1 이라야 바로가기 링크로 포커스가 실제로 옮겨간다(클릭만으로는
          스크롤만 되고 탭 순서는 링크 자리에 남는다). 화면별로 <main> 유무가 달라
          공통 컨테이너에 건다. */}
      <div id="main-content" tabIndex={-1} className="flex-1 min-h-0 outline-none">
        {authLoading ? (
          <div className="h-full flex items-center justify-center text-zinc-400 text-sm">인증 확인 중…</div>
        ) : !me ? (
          <LoginScreen cfg={cfg} onDone={() => { reloadAuth(); load(); loadPending(); }} />
        ) : status === undefined ? (
          <div className="h-full flex items-center justify-center text-zinc-400 text-sm">설정 확인 중…</div>
        ) : showOnboarding ? (
          <Onboarding status={status} onDone={() => load(true)} myEmail={me?.subject ?? ""} authReady={!!status?.ready} />
        ) : route.view === "rca" ? (
          <RcaQueue onBack={() => { go({ view: "app" }); loadPending(); }} onChange={loadPending} />
        ) : route.view === "voc" ? (
          <VocPage onBack={() => go({ view: "app" })} />
        ) : route.view === "dashboard" ? (
          <Dashboard onOpenIssue={(key) => go({ view: "app", key })} can={can} />
        ) : (
          <FailureAnalysis
            onQueueChange={loadPending}
            routeKey={route.view === "app" ? route.key : undefined}
            onSelectKey={(key) => go({ view: "app", key }, false)}
            jiraBase={jiraBase}
          />
        )}
      </div>
    </div>
  )
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <AuthProvider>
      <Shell />
    </AuthProvider>
  </StrictMode>,
)
