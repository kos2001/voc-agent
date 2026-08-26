/** 인증·인가 컨텍스트 + 로그인 화면.
 *
 * 서버가 진짜 관문이다(모든 엔드포인트에 기능 권한이 걸려 있다). 여기서 하는 일은
 * 두 가지뿐 — (1) 로그인 유도, (2) 권한 없는 조작 버튼을 숨겨 헛클릭을 줄이는 것.
 * 화면에서 숨기는 것은 보안이 아니라 사용성이다.
 *
 * 자격증명은 HttpOnly 쿠키에 있으므로 JS 가 토큰을 만지지 않는다. 대신 모든 요청에
 * credentials:"include" 가 필요해서, fetch 를 감싼 api() 를 여기서 제공한다.
 */

import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { Button, ErrorNote, inputCls } from "./ui";

const API = (import.meta as any).env?.VITE_API ?? "";   // 빈 값 = 같은 오리진(개발은 vite 프록시)

export type Me = {
  subject: string; name: string; role: string; email: string; via: string;
  capabilities: string[];
};
export type AuthConfig = {
  enabled: boolean;
  modes: { oidc: boolean; proxy: boolean; dev: boolean };
  default_role: string;
  users_file_present: boolean;
};

/** 쿠키를 실어 보내는 fetch. 401 이면 로그인 화면으로 되돌릴 수 있게 표시한다. */
export async function api(path: string, init: RequestInit = {}): Promise<Response> {
  return fetch(`${API}${path}`, { credentials: "include", ...init });
}

type Ctx = {
  me: Me | null;
  cfg: AuthConfig | null;
  loading: boolean;
  can: (cap: string) => boolean;
  reload: () => void;
  logout: () => Promise<void>;
};

const AuthCtx = createContext<Ctx>({
  me: null, cfg: null, loading: true, can: () => false,
  reload: () => {}, logout: async () => {},
});

export const useAuth = () => useContext(AuthCtx);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [me, setMe] = useState<Me | null>(null);
  const [cfg, setCfg] = useState<AuthConfig | null>(null);
  const [loading, setLoading] = useState(true);

  const reload = useCallback(() => {
    setLoading(true);
    Promise.all([
      api("/auth/config").then((r) => (r.ok ? r.json() : null)).catch(() => null),
      api("/auth/me").then((r) => (r.ok ? r.json() : null)).catch(() => null),
    ]).then(([c, m]) => { setCfg(c); setMe(m); }).finally(() => setLoading(false));
  }, []);

  useEffect(() => { reload(); }, [reload]);

  const logout = useCallback(async () => {
    await api("/auth/logout", { method: "POST" }).catch(() => {});
    setMe(null);
    reload();
  }, [reload]);

  // 인증이 꺼져 있으면(목록 없음) 서버가 전체 권한을 주므로 화면도 막지 않는다.
  const can = useCallback(
    (cap: string) => !!me && (me.via === "disabled" || me.capabilities.includes(cap)),
    [me]);

  return (
    <AuthCtx.Provider value={{ me, cfg, loading, can, reload, logout }}>
      {children}
    </AuthCtx.Provider>
  );
}

/** 역할 배지 — 지금 무슨 권한으로 보고 있는지 항상 드러낸다. */
export function RoleBadge({ me }: { me: Me }) {
  const tone = me.via === "disabled" ? "border-amber-900/60 bg-amber-950/60 text-amber-400"
    : me.role === "admin" ? "border-violet-900/60 bg-violet-950/60 text-violet-400"
    : "border-zinc-700/60 bg-zinc-800/80 text-zinc-300";
  const label = me.via === "disabled" ? "인증 비활성"
    : me.role === "admin" ? "관리자" : "사용자";
  return (
    <span title={me.via === "disabled"
      ? "인가 목록(users.yaml / RVP_ADMIN_EMAILS)이 없어 인증이 꺼져 있습니다 — 전체 권한"
      : `${me.subject} · 인증 경로 ${me.via} · 권한 ${me.capabilities.length}개`}
      className={`inline-flex items-center whitespace-nowrap rounded-full border px-2.5 py-0.5 text-[11px] font-medium ${tone}`}>
      {label}
    </span>
  );
}

/** 로그인 화면 — 설정된 경로만 보여 준다. */
export function LoginScreen({ cfg, onDone }: { cfg: AuthConfig | null; onDone: () => void }) {
  const [email, setEmail] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  const devLogin = async () => {
    setBusy(true); setErr("");
    try {
      const r = await api("/auth/dev-login", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email }),
      });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) { setErr(d.detail || `로그인 실패 (HTTP ${r.status})`); return; }
      onDone();
    } catch (e: any) { setErr(e.message); } finally { setBusy(false); }
  };

  const noPath = cfg && !cfg.modes.oidc && !cfg.modes.proxy && !cfg.modes.dev;

  return (
    <div className="flex h-full items-center justify-center bg-zinc-950 px-4">
      <div className="w-full max-w-sm">
        <h1 className="text-2xl font-semibold tracking-tight text-zinc-50">VOC Agent</h1>
        <p className="mt-1.5 text-sm text-zinc-300">
          계속하려면 로그인이 필요합니다.
        </p>

        <div className="mt-6 space-y-3">
          {cfg?.modes.oidc && (
            <a href={`${API}/auth/login?next=${encodeURIComponent(window.location.href)}`}
              className="flex w-full items-center justify-center gap-1.5 rounded-lg bg-zinc-100 px-5 py-2.5 text-sm font-medium text-zinc-900 transition hover:bg-white">
              사내 SSO 로 로그인
            </a>
          )}

          {cfg?.modes.proxy && !cfg?.modes.oidc && (
            <div className="rounded-lg border border-zinc-800 bg-zinc-900/60 px-4 py-3 text-sm text-zinc-300">
              앞단 SSO 프록시 인증을 사용합니다. 프록시를 통해 접속했는데 이 화면이
              보이면, 전달된 계정이 인가 목록에 없는 것입니다 — 관리자에게 요청하세요.
            </div>
          )}

          {cfg?.modes.dev && (
            <form onSubmit={(e) => { e.preventDefault(); devLogin(); }}
              className="rounded-lg border border-zinc-800 bg-zinc-900/60 p-4">
              <div className="mb-2 text-[11px] font-medium uppercase tracking-wider text-zinc-400">
                개발용 로그인
              </div>
              <input value={email} onChange={(e) => setEmail(e.target.value)}
                placeholder="ID (이메일 또는 아이디)" autoComplete="username" className={inputCls} />
              <Button type="submit" disabled={busy || !email} className="mt-2 w-full">
                {busy ? "확인 중…" : "로그인"}
              </Button>
              <p className="mt-2 text-[11px] leading-relaxed text-zinc-400">
                IdP 없이 역할 분리를 확인하는 통로입니다(RVP_AUTH_DEV_LOGIN=1).
                운영에서는 끄세요 — 이메일만 알면 그 역할이 됩니다.
              </p>
            </form>
          )}

          {noPath && (
            <ErrorNote message="로그인 경로가 설정되지 않았습니다 — RVP_OIDC_* (SSO) 또는 RVP_SSO_EMAIL_HEADER (프록시), 또는 RVP_AUTH_DEV_LOGIN=1 을 설정하세요." />
          )}
          {err && <ErrorNote message={err} />}
        </div>

        {cfg && !cfg.users_file_present && cfg.enabled && (
          <p className="mt-4 text-[11px] leading-relaxed text-zinc-400">
            인가 목록은 RVP_ADMIN_EMAILS 환경변수로 지정돼 있습니다.
            목록에 없는 계정은 기본 역할 “{cfg.default_role}” 로 들어옵니다.
          </p>
        )}
      </div>
    </div>
  );
}
