const API = (import.meta as any).env?.VITE_API ?? "";

/** 401 을 화면 전체가 알아야 한다 — 버튼마다 제각기 다른 오류를 보여주면 사용자는
 *  "이 기능이 고장났다" 로 읽는다. 실제로는 로그인만 다시 하면 되는 상황이다. */
export const UNAUTHORIZED_EVENT = "rvp:unauthorized";

/** 응답이 JSON 이 아니면 그렇게 말한다.
 *
 *  개발 서버가 API 경로를 프록시에 넣지 않으면 그 요청에 index.html 을 돌려준다.
 *  그러면 res.ok 는 true 인데 본문은 HTML 이라, 예전에는 조용히 null 이 반환돼
 *  호출부에서 "Cannot read properties of null" 로 터졌다 — 원인과 한참 떨어진 곳에서.
 */
function notJson(): Error {
  return new Error("API 응답이 JSON 이 아닙니다 — 개발 서버라면 "
    + "web/vite.config.ts 의 API_PREFIXES 에 이 경로가 빠졌을 수 있습니다.");
}

function messageFor(status: number, detail: string): string {
  if (status === 401) return "세션이 만료되었습니다 — 다시 로그인하세요.";
  if (status === 403) return detail || "권한이 없습니다 (관리자 기능일 수 있습니다).";
  if (status >= 500) return `서버 오류 (${status})${detail ? ` — ${detail}` : ""}`;
  return detail || `요청 실패 (HTTP ${status})`;
}

/**
 * JSON POST. 실패 사유를 **그대로 드러낸다.**
 *
 * 예전에는 호출부가 `d.reason || d.error` 만 읽어서, 서버가 401 `{detail:...}` 을
 * 돌려주면 "큐에 추가하지 못했습니다" 같은 무의미한 문구만 남았다. 사용자는 기능이
 * 망가진 줄 알고 원인을 찾을 방법이 없었다 — 실제로 그렇게 신고가 들어왔다.
 */
export async function postJson<T = any>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${API}${path}`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  let data: any = null;
  let parsed = false;
  try { data = await res.json(); parsed = true; } catch { /* 본문 없음/HTML */ }
  if (!res.ok) {
    if (res.status === 401) window.dispatchEvent(new CustomEvent(UNAUTHORIZED_EVENT));
    throw new Error(messageFor(res.status, data?.detail ?? ""));
  }
  if (!parsed) throw notJson();
  return data as T;
}

export async function getJson<T = any>(path: string): Promise<T> {
  const res = await fetch(`${API}${path}`);
  let data: any = null;
  let parsed = false;
  try { data = await res.json(); parsed = true; } catch { /* 본문 없음/HTML */ }
  if (!res.ok) {
    if (res.status === 401) window.dispatchEvent(new CustomEvent(UNAUTHORIZED_EVENT));
    throw new Error(messageFor(res.status, data?.detail ?? ""));
  }
  if (!parsed) throw notJson();
  return data as T;
}
