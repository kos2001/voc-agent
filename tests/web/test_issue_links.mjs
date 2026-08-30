/**
 * 이슈 키 → Jira 링크 변환 계약.
 *
 * 정규식을 **소스에서 읽어 온다.** 복사본을 두면 소스만 고쳤을 때 테스트가 옛 규칙으로
 * 초록을 낸다 — 실제로 그 일이 있었다(괄호 안 인용이 링크되지 않는 결함을 테스트는
 * 통과시켰다).
 *
 * 실행:  node tests/web/test_issue_links.mjs
 */
import { readFileSync } from "node:fs";

const SRC = readFileSync(new URL("../../web/src/issueLinks.tsx", import.meta.url), "utf8");
const KEY_RE_SRC = SRC.match(/const KEY_RE = \/(.+)\/g;/)[1];
const KEY_RE = new RegExp(KEY_RE_SRC, "g");
const browseUrl = (base, key) => `${(base||"").replace(/\/$/,"")}/browse/${encodeURIComponent(key)}`;
function linkifyKeys(md, base) {
  if (!md || !base) return md || "";
  return md.split(/(```[\s\S]*?```|`[^`\n]*`)/g)
    .map((seg,i)=> i%2===1 ? seg : seg.replace(KEY_RE,(_m,k)=>`[${k}](${browseUrl(base,k)})`)).join("");
}
const B = "https://x.atlassian.net";
const cases = [
  ["평문 키", "원인은 LSI-100 참고", "[LSI-100](https://x.atlassian.net/browse/LSI-100)"],
  ["문장 중간", "(LSI-7) 사례", "[LSI-7]("],
  ["코드블록 보호", "```\nlog LSI-100\n```", "log LSI-100"],
  ["인라인코드 보호", "`LSI-100`", "`LSI-100`"],
  ["이미 링크", "[LSI-7](http://a)", "[LSI-7](http://a)"],
  ["도메인 토큰 무시", "PM9C3-NVMe 와 HS-G4", "PM9C3-NVMe 와 HS-G4"],
  ["base 없으면 그대로", "LSI-100", "LSI-100"],
  ["URL 안의 키는 다시 링크하지 않음", "보기 https://x.atlassian.net/browse/LSI-100", "보기 https://x.atlassian.net/browse/LSI-100"],
  ["이미 링크된 키의 대상도 건드리지 않음", "[LSI-7](https://x.atlassian.net/browse/LSI-7)", "[LSI-7](https://x.atlassian.net/browse/LSI-7)"],
];
let fail = 0;
for (const [name, input, expect] of cases) {
  const base = name === "base 없으면 그대로" ? "" : B;
  const out = linkifyKeys(input, base);
  const identity = ["이미 링크", "도메인 토큰 무시", "코드블록 보호", "인라인코드 보호",
                    "base 없으면 그대로", "URL 안의 키는 다시 링크하지 않음",
                    "이미 링크된 키의 대상도 건드리지 않음"];
  const ok = identity.includes(name) ? out === input : out.includes(expect);
  console.log(`${ok ? "✓" : "✗"} ${name}${ok ? "" : `  → ${out}`}`);
  if (!ok) fail++;
}
process.exit(fail ? 1 : 0);
