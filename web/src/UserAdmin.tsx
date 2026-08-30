/** 사용자 관리 — 관리자가 다른 관리자·사용자를 등록·변경·회수한다.
 *
 * 서버가 실제 관문이다(모든 조작에 config.write 권한 + 잠금 방지 규칙). 여기서는
 * 그 규칙을 화면에 그대로 드러내는 것이 목적이다 — 왜 어떤 항목은 못 고치는지
 * (환경변수 관리자), 왜 회수가 막히는지(마지막 관리자·자기 자신)를 눌러 보기 전에
 * 알 수 있어야 한다.
 */

import { useCallback, useEffect, useState } from "react";
import { Badge, Button, ErrorNote, Notice, SectionTitle, inputCls, selectCls } from "./ui";

const API = (import.meta as any).env?.VITE_API ?? "";

type Row = { email: string; name: string; role: string; revoked: boolean; locked: boolean };
type Listing = {
  users: Row[]; file: string; file_present: boolean;
  env_admins: string[]; active_admins: number; roles: string[];
};

export default function UserAdmin({ myEmail, onChanged }: {
  /** 자기 자신은 회수 버튼을 그리지 않는다(서버도 막지만 눌러 볼 이유가 없다). */
  myEmail: string;
  onChanged?: () => void;
}) {
  const [data, setData] = useState<Listing | null>(null);
  const [err, setErr] = useState("");
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [role, setRole] = useState("user");

  const load = useCallback(() => {
    setErr("");
    fetch(`${API}/auth/users`)
      .then((r) => (r.ok ? r.json() : r.json().then((d) => Promise.reject(new Error(d.detail || `HTTP ${r.status}`)))))
      .then(setData)
      .catch((e) => setErr(e.message));
  }, []);

  useEffect(() => { load(); }, [load]);

  const send = async (path: string, body: any, ok: string) => {
    setBusy(true); setErr(""); setMsg("");
    try {
      const r = await fetch(`${API}${path}`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) { setErr(d.detail || `실패 (HTTP ${r.status})`); return false; }
      setData(d);
      setMsg(ok);
      onChanged?.();
      return true;
    } catch (e: any) { setErr(e.message); return false; } finally { setBusy(false); }
  };

  const add = async () => {
    if (await send("/auth/users", { email, name, role },
                   `${email} 을 ${role === "admin" ? "관리자" : "사용자"}로 등록했습니다`)) {
      setEmail(""); setName("");
    }
  };

  const changeRole = (u: Row, next: string) =>
    send("/auth/users", { email: u.email, name: u.name, role: next },
         `${u.email} 역할을 ${next === "admin" ? "관리자" : "사용자"}로 변경했습니다`);

  const setRevoked = (u: Row, revoked: boolean) =>
    send("/auth/users/revoke", { email: u.email, revoked },
         `${u.email} 권한을 ${revoked ? "회수" : "복구"}했습니다`);

  const active = (data?.users ?? []).filter((u) => !u.revoked);
  const revoked = (data?.users ?? []).filter((u) => u.revoked);
  const lastAdmin = (u: Row) =>
    u.role === "admin" && !u.revoked && (data?.active_admins ?? 0) <= 1;

  return (
    <section className="rounded-xl border border-zinc-800 bg-zinc-900/60 p-5">
      <SectionTitle hint="관리자만 볼 수 있습니다">사용자 관리</SectionTitle>

      {!data ? (
        err ? <ErrorNote message={err} action={<button onClick={load} className="underline">다시 시도</button>} />
            : <div className="space-y-2">{[0, 1, 2].map((i) => <div key={i} className="h-3 animate-pulse rounded bg-zinc-800" />)}</div>
      ) : (
        <>
          {!data.file_present && (
            <div className="mb-3">
              <Notice tone="warn">
                인가 목록 파일이 아직 없습니다. 여기서 첫 사용자를 등록하면 파일이 생기고
                <b> 인증이 켜집니다</b> — 자기 계정을 관리자로 먼저 등록하세요.
              </Notice>
            </div>
          )}

          {/* 등록 폼 */}
          <form onSubmit={(e) => { e.preventDefault(); add(); }}
            className="mb-4 rounded-lg border border-zinc-800 bg-zinc-950/60 p-3">
            <div className="mb-2 text-[11px] font-medium uppercase tracking-wider text-zinc-400">
              새 사용자 등록
            </div>
            <div className="flex flex-wrap gap-2">
              <input value={email} onChange={(e) => setEmail(e.target.value)}
                placeholder="ID — 이메일 또는 아이디" autoComplete="off"
                className={`${inputCls} min-w-[200px] flex-1`} />
              <input value={name} onChange={(e) => setName(e.target.value)}
                placeholder="이름 (선택)" autoComplete="off"
                className={`${inputCls} min-w-[120px] flex-1`} />
              <select value={role} onChange={(e) => setRole(e.target.value)} aria-label="역할"
                className={`${selectCls} py-2`}>
                <option value="user">사용자</option>
                <option value="admin">관리자</option>
              </select>
              <Button type="submit" disabled={busy || !email} className="shrink-0">
                {busy ? "저장 중…" : "등록"}
              </Button>
            </div>
            <p className="mt-2 text-[11px] leading-relaxed text-zinc-400">
              ID 는 <b>이메일</b>(SSO 계정) 또는 <b>아이디</b>(<span className="font-mono">admin</span> 같은
              로컬 계정)입니다. 아이디 계정은 IdP 가 그 값을 주지 않으므로 SSO 로는 들어오지
              못하고, 개발용 로그인·프록시 헤더 경로에서 쓰입니다.<br />
              이미 있는 ID 면 이름·역할이 갱신되고 회수된 계정이면 함께 복구됩니다.
              사용자는 조회·분석·초안 제출까지 하고, <b>Jira 게시·설정·지식 편집은 관리자만</b> 합니다.
            </p>
          </form>

          {msg && <div className="mb-3"><Notice tone="ok">{msg}</Notice></div>}
          {err && <div className="mb-3"><ErrorNote message={err} /></div>}

          {/* 활성 목록 */}
          <div className="overflow-x-auto rounded-lg border border-zinc-800">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-zinc-800">
                  {["ID", "이름", "역할", ""].map((h, i) => (
                    <th key={i} className="px-3 py-2 text-left text-[11px] font-medium uppercase tracking-wider text-zinc-400">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {active.map((u) => (
                  <tr key={u.email} className="border-b border-zinc-800/60 last:border-0">
                    <td className="px-3 py-2 align-middle font-mono text-[12px] text-zinc-300">
                      {u.email}
                      {u.email === myEmail && <span className="ml-1.5 text-[10px] text-sky-400">(나)</span>}
                    </td>
                    <td className="px-3 py-2 align-middle text-zinc-300">{u.name}</td>
                    <td className="px-3 py-2 align-middle">
                      {u.locked ? (
                        <Badge tone="violet" title="RVP_ADMIN_EMAILS 환경변수로 지정 — 화면에서 변경할 수 없습니다">
                          관리자 · 환경변수
                        </Badge>
                      ) : (
                        <select value={u.role} disabled={busy || lastAdmin(u)}
                          onChange={(e) => changeRole(u, e.target.value)}
                          aria-label={`${u.email} 역할`}
                          title={lastAdmin(u) ? "마지막 관리자는 역할을 바꿀 수 없습니다" : ""}
                          className={`${selectCls} disabled:opacity-50`}>
                          <option value="user">사용자</option>
                          <option value="admin">관리자</option>
                        </select>
                      )}
                    </td>
                    <td className="px-3 py-2 text-right align-middle">
                      {!u.locked && u.email !== myEmail && (
                        <button onClick={() => setRevoked(u, true)} disabled={busy || lastAdmin(u)}
                          title={lastAdmin(u) ? "마지막 관리자는 회수할 수 없습니다" : "권한 회수 (목록에는 남습니다)"}
                          className="rounded border border-red-800/60 px-2 py-0.5 text-[11px] text-red-300 hover:bg-red-950/40 disabled:opacity-40">
                          회수
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
                {!active.length && (
                  <tr><td colSpan={4} className="px-3 py-4 text-center text-xs text-zinc-400">등록된 사용자가 없습니다</td></tr>
                )}
              </tbody>
            </table>
          </div>

          {revoked.length > 0 && (
            <details className="mt-3">
              <summary className="cursor-pointer text-[11px] text-zinc-400 hover:text-sky-400">
                회수된 계정 {revoked.length}건 (이력 보관 — 지우지 않습니다)
              </summary>
              <div className="mt-2 space-y-1">
                {revoked.map((u) => (
                  <div key={u.email} className="flex items-center gap-2 text-[11px]">
                    <span className="font-mono text-zinc-400">{u.email}</span>
                    <span className="text-zinc-500">{u.name}</span>
                    <Badge tone="neutral">{u.role === "admin" ? "관리자" : "사용자"}</Badge>
                    <button onClick={() => setRevoked(u, false)} disabled={busy}
                      className="ml-auto rounded border border-zinc-600 px-2 py-0.5 text-zinc-300 hover:bg-zinc-800 disabled:opacity-40">
                      복구
                    </button>
                  </div>
                ))}
              </div>
            </details>
          )}

          <div className="mt-3 text-[11px] leading-relaxed text-zinc-400">
            활성 관리자 {data.active_admins}명 · 목록 파일 <span className="font-mono">{data.file}</span>
            {data.env_admins.length > 0 && (
              <> · 환경변수 관리자 {data.env_admins.length}명(변경은 RVP_ADMIN_EMAILS 에서)</>
            )}
          </div>
        </>
      )}
    </section>
  );
}
