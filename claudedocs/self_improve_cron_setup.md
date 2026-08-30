# 자기 개선 loop 주기 실행 (hermes cron)

매일 1회 **측정·진단(L1) + 지식 변경 제안(L3) 큐 갱신**을 자동 수행하고, 주 1회
**에이전트가 그 결과를 해석**한다. 지식은 불변 — L2 파라미터 적용·L3 제안 실행은
사람이 검토 후.

## 설치

```sh
bash scripts/setup_self_improve_cron.sh
```

두 작업이 등록된다.

| 작업 | 주기 | 방식 |
|---|---|---|
| `voc-selfimprove-daily` | 매일 09:00 | 스크립트가 곧 작업(`--no-agent`) — LLM 0원, 결정적 |
| `voc-selfimprove-weekly` | 매주 월 09:30 | 같은 스크립트 + **에이전트가 결과 해석** |

주간 작업은 "지난주 대비 나빠진 지표 / 지금 손대야 할 제안 하나 / 실행이 실패했으면
그 사실 먼저" 만 한국어로 정리하게 한다 — 숫자를 지어내지 말고 출력에 있는 값만 쓰도록.

되돌리기:
```sh
hermes -p <프로파일> cron rm voc-selfimprove-daily
hermes -p <프로파일> cron rm voc-selfimprove-weekly
```

## 프로파일

이 loop 은 **`voc-agent` 전용 프로파일**에서 돈다. hermes 는 프로파일마다 cron
저장소와 게이트웨이가 따로라, 다른 프로젝트 프로파일(`lsi` 등)에 얹으면 이 loop 의
수명이 그쪽에 묶인다. 설치 스크립트가 프로파일이 없으면 만들고 게이트웨이까지 올린다.

다른 프로파일에 두려면 `HERMES_PROFILE=<이름> bash scripts/setup_self_improve_cron.sh`.

## 왜 launchd 에서 옮겼나

plist 에 저장소 **절대경로**가 박혀 있었다. `lsi_error_analyzer` → `voc-agent` 리네임
후 작업이 **조용히 죽었다** — 나흘치 실행이 통째로 빠졌는데 로그도 화면도 아무 말을
하지 않았다. hermes cron 은 실행 이력(`cron runs`)을 자체 보관해 "돌았는데 실패" 와
"아예 안 돌았다" 를 구분할 수 있다.

옮기면서 같은 종류의 함정을 세 번 더 밟았고, 셋 다 스크립트가 막는다.

1. **프로파일이 갈렸다.** hermes 는 프로파일마다 cron 저장소가 따로다. 맨 `hermes` 를
   부르면 sticky 기본 프로파일로 가는데, 게이트웨이는 다른 프로파일로 돌고 있었다 —
   **영원히 발화하지 않는 작업**이 만들어졌다. 스크립트가 프로파일을 명시한다.
2. **스크립트 경로가 전역이 아니다.** `~/.hermes/profiles/<프로파일>/scripts/` 에
   있어야 한다. 전역 `~/.hermes/scripts` 에 두면 등록은 되고 실행에서 실패한다.
3. **게이트웨이가 떠 있어야 발화한다.** 등록만으로는 아무 일도 없다. 스크립트가
   등록 후 `cron status` 를 확인하고, 안 떠 있으면 **실패로 끝낸다**(exit 2).

## 무엇이 도는가

`scripts/self_improve_cron.sh` → `src/self_improve.py`:
- 측정: 유용성·KB품질·지식공백·자산통계 → 날짜별 리포트(`claudedocs/self_improve/`)
- 진단: 신호→우선순위 권고
- 제안: 미승격 군집 승격·공백 RCA 작성·비유용 사례 폐기검토·온톨로지 정규화
  → `data/improve_queue.json` 에 병합(거부/완료 보존)
- 로그: `logs/self_improve_cron.log`

## 돌고 있는지 어떻게 아나

대시보드 **"자기개선 loop"** 카드 — 마지막 실행·경과 시간·누적 실행·열린 제안.
`RVP_SELFCHECK_MAX_AGE_H`(기본 36시간)를 넘으면 빨간 경고와 함께 복구 명령을 띄운다.

판정은 **산출물**(리포트 파일·이력)로 한다. 스케줄러에게 묻지 않는다 — "등록됨" 이라고
답해도 실제로 안 돌 수 있고, 이번 결함이 정확히 그것이었다.

```sh
curl localhost:8011/selfcheck/status
hermes -p <프로파일> cron list      # 등록 상태
hermes -p <프로파일> cron runs      # 실행 이력(성공/실패)
```

## 주의
- 리랭커는 cron 에서 off(외부 API 비용 0). 군집화는 로컬 fastembed.
- `data/improve_queue.json`·`self_improve_history.json` 은 누적 — git 추적(버전·공유).
  다중 머신에서 동시 실행 시 충돌 가능 → 단일 운영 노드 권장.
