#!/usr/bin/env bash
# 자기개선 loop 주기 실행을 **hermes cron** 에 등록한다.
#
# 왜 옮기나:
#   launchd plist 에 저장소 절대경로가 박혀 있었다. 저장소를 lsi_error_analyzer →
#   voc-agent 로 리네임하자 작업이 **조용히 죽었다** — 나흘치 실행이 통째로 빠졌는데
#   화면도 로그도 아무 말을 하지 않았다. hermes cron 은 실행 이력(runs/history)을
#   자체 보관하므로 "돌았는데 실패" 와 "아예 안 돌았다" 를 구분할 수 있다.
#
# 두 층으로 등록한다. 성격이 다르기 때문이다.
#   1) 매일 09:00 — 측정·제안 (결정적, LLM 0원). 스크립트가 곧 작업이다(--no-agent).
#   2) 매주 월 09:30 — **에이전트가 해석**. 스크립트 출력을 프롬프트로 받아
#      "무엇이 나빠졌고 무엇부터 해야 하는가" 를 사람 말로 정리한다.
#
# 되돌리기:  hermes -p <프로파일> cron rm voc-selfimprove-daily (weekly 도 동일)
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# 실행 파일 순서: 저장소 설정(HERMES_BIN) → lsi 래퍼 → 맨 hermes.
# 맨 `hermes` 를 먼저 잡으면 **sticky 기본 프로파일**로 간다 — 실제로 그래서 작업이
# ppa-agent 에 등록됐는데 게이트웨이는 lsi 로 돌아 영원히 발화하지 않았다.
# 이 저장소 전용 프로파일을 기본으로 쓴다. 프로파일마다 cron 저장소·게이트웨이가
# 따로라, 다른 프로젝트 프로파일에 얹으면 그쪽 수명에 이 loop 이 묶인다.
HERMES="${HERMES_BIN:-}"
[ -x "${HERMES:-}" ] || HERMES="$HOME/.local/bin/voc-agent"
[ -x "$HERMES" ] || HERMES="$HOME/.local/bin/lsi"
[ -x "$HERMES" ] || HERMES="$(command -v hermes || true)"
[ -x "$HERMES" ] || { echo "hermes 실행 파일을 찾지 못했습니다." >&2; exit 1; }

# **프로파일을 명시한다.** hermes 는 프로파일마다 cron 저장소가 따로 있다. 어느
# 프로파일에 넣는지는 주변 상태가 아니라 이 스크립트가 정해야 한다.
PROFILE="${HERMES_PROFILE:-$("$HERMES" profile 2>/dev/null | awk '/Active profile:/{print $3}')}"

# 프로파일이 없으면 만든다 — 없는 채로 다른 프로파일에 등록되는 것이 이번 사고였다.
if ! "$HERMES" profile list 2>/dev/null | grep -qE "(^|[^a-z-])$PROFILE([^a-z-]|$)"; then
  echo "프로파일 $PROFILE 생성"
  hermes profile create "$PROFILE" --clone-from lsi \
    --description "VOC 대응 에이전트 — 자기개선 loop 주기 실행" >/dev/null

  # 클론은 **충돌하는 것까지 복사한다.** 그대로 두면 게이트웨이가 뜨자마자 죽는다:
  #   · Telegram 봇 토큰이 같아 두 게이트웨이가 같은 봇을 폴링 → Conflict →
  #     "No connected messaging platforms remain" 으로 종료(실측).
  #   · api_server 포트가 같아 바인드 실패.
  # 이 프로파일은 메시징이 아니라 **cron 스케줄러**가 목적이므로 Telegram 은 끈다.
  CFG="$HOME/.hermes/profiles/$PROFILE/config.yaml"
  ENVF="$HOME/.hermes/profiles/$PROFILE/.env"
  PORT=8643
  while lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; do PORT=$((PORT+1)); done
  python3 - "$CFG" "$ENVF" "$PORT" <<'PYFIX'
import re, sys, pathlib
cfg, envf, port = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), sys.argv[3]
s = cfg.read_text()
s = re.sub(r'(platforms:\n(?:.*\n)*?  telegram:\n    enabled: )true', r'\1false', s, count=1)
s = re.sub(r'(platforms:\n(?:.*\n)*?      port: )\d+', r'\g<1>' + port, s, count=1)
cfg.write_text(s)
if envf.exists():
    e = envf.read_text()
    e = re.sub(r'^TELEGRAM_BOT_TOKEN=.*$', 'TELEGRAM_BOT_TOKEN=', e, count=1, flags=re.M)
    envf.write_text(e)
print(f"  Telegram 비활성 · api_server 포트 {port}")
PYFIX

  "$HERMES" gateway install >/dev/null 2>&1 || true
  "$HERMES" gateway start   >/dev/null 2>&1 || true
  sleep 5
fi
[ -n "$PROFILE" ] || { echo "hermes 프로파일을 알 수 없습니다. HERMES_PROFILE 을 지정하세요." >&2; exit 1; }
# 래퍼(예: `lsi` = `hermes -p lsi`)는 이미 프로파일을 고정한다. 거기에 -p 를 또 붙이면
# 인자 파싱이 깨진다. 받는지 **직접 확인해서** 결정한다 — 이름으로 추측하지 않는다.
if "$HERMES" -p "$PROFILE" profile >/dev/null 2>&1; then
  H=("$HERMES" -p "$PROFILE")
else
  H=("$HERMES")            # 래퍼가 이미 프로파일을 고정하고 있다
fi
echo "hermes: ${H[*]} · 프로파일: $PROFILE"

# 스크립트는 **프로파일별** 디렉터리에서 찾는다(~/.hermes/profiles/<프로파일>/scripts).
# 전역 ~/.hermes/scripts 에 두면 등록은 되지만 실행 시 "Script not found" 로 실패한다 —
# 실제로 그렇게 한 번 실패했다.
SCRIPTS="$HOME/.hermes/profiles/$PROFILE/scripts"
mkdir -p "$SCRIPTS"

# 저장소 경로는 이 런처 **한 곳**에만 둔다 — 다시 리네임돼도 고칠 자리가 하나다.
cat > "$SCRIPTS/voc_self_improve.sh" <<LAUNCHER
#!/usr/bin/env bash
# 생성: scripts/setup_self_improve_cron.sh (voc-agent)
set -euo pipefail
exec "$REPO/scripts/self_improve_cron.sh"
LAUNCHER
chmod +x "$SCRIPTS/voc_self_improve.sh"

echo "런처: $SCRIPTS/voc_self_improve.sh → $REPO/scripts/self_improve_cron.sh"

"${H[@]}" cron rm voc-selfimprove-daily  >/dev/null 2>&1 || true
"${H[@]}" cron rm voc-selfimprove-weekly >/dev/null 2>&1 || true

# --script 는 ~/.hermes/scripts 기준 **파일명만** 받는다(절대경로 거부).
# 프롬프트는 schedule 바로 뒤의 위치 인자다 — 옵션 뒤에 두면 파서가 거부한다.
SCRIPT_NAME="voc_self_improve.sh"

# 1) 매일 — 스크립트가 곧 작업(LLM 없음). 출력이 비면 조용하다.
"${H[@]}" cron create '0 9 * * *' \
  --name voc-selfimprove-daily \
  --script "$SCRIPT_NAME" \
  --no-agent \
  --workdir "$REPO"

# 2) 매주 — 같은 스크립트를 돌리되 **에이전트가 결과를 해석**한다.
WEEKLY_PROMPT='위 출력은 VOC 에이전트의 자기개선 loop 주간 실행 결과다. 다음만 한국어로 간단히 정리하라.
1) 지난주 대비 나빠진 지표가 있는가(유용성·KB 품질·지식 공백·큐레이션). 없으면 "변화 없음".
2) 개선 큐의 열린 제안 중 지금 사람이 손대야 할 것 하나를 고르고 이유를 한 줄로.
3) 실행이 실패했거나 수치가 비어 있으면 그 사실을 먼저 말하라 — 추측으로 채우지 말 것.
숫자를 지어내지 말고 출력에 있는 값만 쓴다.'

"${H[@]}" cron create '30 9 * * 1' "$WEEKLY_PROMPT" \
  --name voc-selfimprove-weekly \
  --script "$SCRIPT_NAME" \
  --workdir "$REPO"

echo
"${H[@]}" cron list
echo

# 등록만으로는 아무 일도 일어나지 않는다. 스케줄러(게이트웨이)가 떠 있어야 발화한다 —
# 등록해 놓고 안 도는 상태가 이 저장소를 이미 한 번 속였다(launchd 경로 문제로 나흘 결번).
if "${H[@]}" cron status 2>&1 | grep -q "Gateway is running"; then
  echo "✓ 스케줄러 동작 중 — 다음 실행에 발화합니다."
else
  echo "⚠ 스케줄러가 떠 있지 않습니다. 작업은 등록됐지만 **발화하지 않습니다.**" >&2
  echo "   해결:  $HERMES -p $PROFILE gateway install && $HERMES -p $PROFILE gateway start" >&2
  exit 2
fi
echo "확인:  $HERMES -p $PROFILE cron list · cron runs · 대시보드의 '자기개선 loop' 카드"
