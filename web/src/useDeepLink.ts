/** URL 해시 ↔ 화면 상태 동기화.
 *
 * 기존에는 선택한 이슈가 URL에 남지 않아 새로고침하면 사라지고 링크 공유도 불가능했다.
 * 해시를 쓰는 이유: 정적 서빙(web/dist)과 Vite dev 서버 모두에서 서버 라우팅 설정 없이
 * 동작하고, 브라우저 뒤로/앞으로가 그대로 먹는다.
 *
 * 경로 형태:
 *   #/                → 분석 화면(선택 없음)
 *   #/issue/LSI-7     → 분석 화면 + LSI-7 선택
 *   #/dashboard       → 지식 현황
 *   #/rca             → RCA 승인 대기
 *   #/reply           → 고객 답변 발송 대기
 *   #/chat            → 이슈 질문(챗봇)
 *   #/guides          → 답변 지침
 *   #/voc             → VOC
 *   #/settings        → 설정
 */

import { useEffect, useState } from "react";

export type Route =
  | { view: "app"; key?: string }
  | { view: "dashboard" }
  | { view: "rca" }
  | { view: "reply" }
  | { view: "chat"; key?: string }
  | { view: "guides" }
  | { view: "voc" }
  | { view: "settings" };

export function parseHash(hash: string): Route {
  const p = hash.replace(/^#\/?/, "").split("?")[0];
  const [head, arg] = p.split("/");
  switch (head) {
    case "dashboard": return { view: "dashboard" };
    case "rca": return { view: "rca" };
    case "reply": return { view: "reply" };
    case "chat": return { view: "chat", key: arg ? decodeURIComponent(arg).toUpperCase() : undefined };
    case "guides": return { view: "guides" };
    case "voc": return { view: "voc" };
    case "settings": return { view: "settings" };
    case "issue": return { view: "app", key: arg ? decodeURIComponent(arg).toUpperCase() : undefined };
    default: return { view: "app" };
  }
}

export function routeToHash(r: Route): string {
  switch (r.view) {
    case "dashboard": return "#/dashboard";
    case "rca": return "#/rca";
    case "reply": return "#/reply";
    case "chat": return r.key ? `#/chat/${encodeURIComponent(r.key)}` : "#/chat";
    case "guides": return "#/guides";
    case "voc": return "#/voc";
    case "settings": return "#/settings";
    default: return r.key ? `#/issue/${encodeURIComponent(r.key)}` : "#/";
  }
}

/** 현재 라우트 + 이동 함수. push=false 면 히스토리에 항목을 남기지 않는다(치환). */
export function useRoute(): [Route, (r: Route, push?: boolean) => void] {
  const [route, setRoute] = useState<Route>(() => parseHash(window.location.hash));
  useEffect(() => {
    const onHash = () => setRoute(parseHash(window.location.hash));
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  const go = (r: Route, push = true) => {
    const h = routeToHash(r);
    if (h === (window.location.hash || "#/")) { setRoute(r); return; }
    if (push) window.location.hash = h;
    else window.history.replaceState(null, "", h);
    setRoute(r);
  };
  return [route, go];
}
