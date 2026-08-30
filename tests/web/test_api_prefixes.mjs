/**
 * 개발 서버 프록시 목록(API_PREFIXES)이 백엔드의 최상위 경로를 **전부** 덮는가.
 *
 * 빠지면 Vite 가 그 요청에 index.html 을 돌려주고, 프런트는 "JSON 이 아닌 응답" 을
 * 받는다. 예전에는 그게 호출부에서 `Cannot read properties of null` 로 터졌다 —
 * 원인(프록시 설정)과 한참 떨어진 자리에서. 이 저장소가 두 번 당한 실수라
 * 사람이 기억하는 것에 맡기지 않는다.
 *
 * 서버 경로는 파이썬을 띄우지 않고 **소스에서** 읽는다 — 이 테스트 하나 때문에
 * KB 로딩·임베딩이 도는 것은 과하다.
 *
 * 실행:  node tests/web/test_api_prefixes.mjs
 */
import { readFileSync } from "node:fs";

const ROOT = new URL("../../", import.meta.url);
const vite = readFileSync(new URL("web/vite.config.ts", ROOT), "utf8");
const server = readFileSync(new URL("backend/server.py", ROOT), "utf8");

const prefixes = new Set(
  (vite.match(/const API_PREFIXES = \[([\s\S]*?)\]/)?.[1] ?? "")
    .match(/'([^']+)'/g)?.map((s) => s.slice(1, -1)) ?? []);

// @app.get("/guides/search", ...) / @app.post('/chat') / app.mount("/mcp", ...)
const routes = [...server.matchAll(/@app\.(?:get|post|put|delete|patch)\(\s*["']\/([^"'/?]*)/g)]
  .map((m) => m[1]).filter(Boolean);

// FastAPI 가 자동으로 붙이는 문서 경로와 SPA 루트는 프록시 대상이 아니다.
const IGNORE = new Set(["docs", "redoc", "openapi.json", "mcp"]);
const needed = [...new Set(routes)].filter((r) => !IGNORE.has(r)).sort();
const missing = needed.filter((r) => !prefixes.has(r));
const extra = [...prefixes].filter((p) => !needed.includes(p) && !IGNORE.has(p)).sort();

console.log(`서버 최상위 경로 ${needed.length}개 · 프록시 목록 ${prefixes.size}개`);
for (const r of needed) {
  if (!prefixes.has(r)) console.log(`✗ ${r} — API_PREFIXES 에 없음 (개발 서버에서 index.html 이 돌아온다)`);
}
if (missing.length === 0) console.log("✓ 모든 서버 경로가 프록시에 있다");
// 목록에만 있고 서버에 없는 항목은 오류가 아니다 — 지운 엔드포인트의 잔재일 뿐이라 알리기만 한다.
if (extra.length) console.log(`· 참고: 서버에 없는 프록시 항목 ${JSON.stringify(extra)}`);

process.exit(missing.length ? 1 : 0);
