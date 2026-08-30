import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 백엔드 API 경로의 최상위 접두사. 개발 서버에서 이 경로들을 백엔드로 프록시해
// **프런트와 API 를 같은 오리진**으로 맞춘다.
//
// 왜 필요한가: 세션이 HttpOnly + SameSite=Lax 쿠키다. 5173(프런트)과 8011(API)은
// 서로 다른 사이트라, 교차 사이트 fetch 에는 Lax 쿠키가 실리지 않아 로그인이 유지되지
// 않는다. SameSite=None 은 Secure(https)를 요구하므로 로컬에서 쓸 수 없다.
// 프로덕션은 FastAPI 가 web/dist 를 같은 오리진에서 서빙하므로 원래 같은 사이트다 —
// 즉 이 프록시는 개발 환경을 프로덕션과 같은 모양으로 만드는 것이다.
// 백엔드의 **모든 최상위 경로**가 여기 있어야 한다. 빠지면 개발 서버가 그 요청에
// index.html 을 돌려주고, 프런트에서는 "JSON 이 아닌 응답" → null 역참조로 나타난다
// (실제로 guides·metrics 가 빠져 화면이 깨졌다). tests/web/test_api_prefixes.mjs 가
// 서버 라우트와 이 목록을 대조한다 — 사람이 기억하는 것에 맡기지 않는다.
const API_PREFIXES = [
  'auth', 'health', 'config', 'webhook', 'reco', 'voc', 'issues', 'graph',
  'recommend', 'rca', 'chat', 'guides', 'knowledge', 'metrics', 'eval', 'improve',
  'selfcheck', 'jira', 'explain',
]

const API_TARGET = process.env.VITE_PROXY_TARGET ?? 'http://127.0.0.1:8011'

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      // (?:[/?]|$) — 쿼리스트링이 붙는 경우까지 포함해야 한다. (/|$) 로 두면
      // `/graph?key=LSI-7` 처럼 최상위 경로 + 쿼리인 요청이 매칭되지 않아 Vite 가
      // index.html 을 돌려주고, 프런트의 r.json() 이 조용히 실패한다(실측으로 확인).
      [`^/(${API_PREFIXES.join('|')})(?:[/?]|$)`]: {
        target: API_TARGET,
        changeOrigin: false,   // Host 를 유지해 쿠키 도메인이 갈라지지 않게 한다
        ws: false,
      },
    },
  },
})
