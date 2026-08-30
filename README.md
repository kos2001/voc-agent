# VOC Agent

**Jira 로 들어오는 고객 문의(VOC)에 답하는 어시스턴트.** 과거에 해결된 이슈를
지식베이스로, 미해결 문의의 근본원인·해결책 초안을 만들어 **사람이 승인할 때만**
Jira 에 게시한다. 도메인은 LSI 칩/펌웨어 고장 분석이다.

목표는 그 **답변의 성능을 계속 올리는 것**이다. 그래서 초안이 거부·수정될 때마다
원인을 분류해 쌓고(`src/draft_feedback.py`), 그 원인이 다음 초안의 생성 규칙과
개선 큐로 되돌아가는 닫힌 루프를 돈다.

## 무엇을 하나

1. **Jira 적재(ingest)** — 고장 이슈·고객 문의를 REST로 가져온다.
2. **전처리(preprocess)** — 칩/분류/증상/근본원인/해결책/엔티티를 추출하고,
   엔티티↔이슈 bipartite **지식 그래프**(networkx)를 만든다.
3. **탐색·추천(explorer / recommender)** — 미해결 문의의 관찰 가능한 정보(요약·증상·칩·분류)로
   유사한 *해결된* 이슈를 검색하고, 그 근본원인/해결책을 제안한다. (옵션: LLM 종합 설명)
4. **초안 → HITL 승인 → 게시** — 사람이 승인·수정·거부하고, 그 판정이 신호로 쌓인다.
   산출물은 둘이다 — 엔지니어가 읽는 **RCA 분석**과 고객이 받는 **답변**(아래 절).
5. **자기개선 loop** — 쌓인 판정으로 측정(L1)·파라미터 검증(L2)·지식 변경 제안(L3).
6. **프론트엔드** — 미해결 문의를 고르면 과거 해결 사례 + 제안 근본원인/해결책/신뢰도.

## 검색 성능 (eval)

각 이슈의 잠재 '고장 템플릿'을 ground-truth로, 미해결 이슈에 대해 같은 템플릿의 해결 사례를
검색하는 정확도를 측정한다 (`src/eval_recommender.py`):

| method | P@1 | P@3 | MRR |
|--------|-----|-----|-----|
| graph (엔티티 중첩, baseline) | 0.96 | 1.0 | 0.973 |
| **bm25** | **1.0** | **1.0** | **1.0** |
| hybrid (bm25+graph+boost) | 1.0 | 1.0 | 1.0 |

> graph baseline 대비 bm25/hybrid로 P@1을 0.96 → 1.0 으로 끌어올림. (합성 데이터 기준)

## 파이프라인 실행

```sh
set -a && source .env && set +a            # Jira/OpenRouter 자격증명
.venv/bin/python scripts/run_pipeline.py    # ingest → preprocess → explorer
# 개별 단계
.venv/bin/python src/ingest.py
.venv/bin/python src/preprocess.py
.venv/bin/python src/explorer.py "PM9C3 thermal throttle link down"
.venv/bin/python src/explorer.py --viz       # tmp_db/lsi_solution_graph.html
# 성능 평가
.venv/bin/python src/eval_recommender.py --methods graph,bm25,hybrid
```

## 웹 앱 실행

```sh
bash scripts/dev.sh        # 백엔드(:8001) + 프론트(:5173)
```
- Jira 이슈 번호 입력(예: LSI-7) 또는 목록 선택 → 유사 해결 사례 + 제안 root-cause/해결책 + LLM 종합 분석

### 유사도 검색 (recommender)

- 랭킹: BM25 + 엔티티 그래프 (+ 다국어 임베딩) RRF 융합, 기본 `hybrid_embed`
  (`RVP_RECO_METHOD`로 변경). KB 문서는 **이슈 제기(요약/증상) + 문제 분석(디버깅
  접근/근본 원인)** 단계 내용으로 구성 — 해결 단계는 질의에 존재할 수 없어 제외.
- coverage 게이트: 임베딩 코사인 ≥0.5 또는 기술 엔티티 겹침 ≥1 미달 시
  "유사 사례 없음" 처리(LLM 설명도 생성 안 함). 매치별 강도 신호
  (`embed_cos`/`entity_overlap`/`bm25_raw`)를 API로 노출.
- 평가: `src/eval_recommender.py --sets confusable,generated --paraphrase --rerank --both-gates`
  — `--both-gates` 는 rerank ON/OFF 두 경로를, `--wrong-chip` 은 신고자가 칩을
  잘못 적은 상황을 함께 본다. rerank 를 켠 수치만 보면
  embed_cos 게이트의 결함이 가려진다(2026-08-02 실측).
  변별 셋 `eval_confusable` 은 `scripts/build_eval_confusable.py --paraphrase` 로 만든다
  — 같은 칩·분류의 다른 고장모드를 혼동 후보로 붙이고 증상을 현장 말투로 재서술해,
  포화된 기존 셋과 달리 1.0 이 아닌 수치가 나온다(rerank OFF 시 P@1 0.941).
- 이전 평가: `src/eval_recommender.py --paraphrase --doc-stages both` —
  264건 코퍼스(LSI+NFC) 기준 LOO·unresolved P@1 1.0, paraphrase 49문항
  P@1 .898 / P@3 .939 / 게이트 통과 .939 / 무관 질의 차단 .95 (hybrid_embed).

### 화면은 VOC 대응 순서를 따른다

이 서비스의 목적은 불량 분석이 아니라 **VOC 대응**이다. 그런데 화면은 오래도록 분석
도구의 순서를 따르고 있었다 — 제목이 "불량 분석", 첫 카드가 `🤖 AI 제안(근본원인·해결책)`,
고객 답변은 그 아래 부록. **순서가 일의 순서를 정한다.** 담당자는 화면이 먼저 보여주는
것부터 읽고, 거기서 판단을 시작한다.

바꾼 것:

| 자리 | 이전 | 이후 |
|---|---|---|
| 화면 제목 | 불량 분석 | **VOC 대응** — "답변을 만들고, 검토한 뒤 발송합니다" |
| 지표 줄 | 해결 KB · 고장 템플릿 · 미해결 · 검색정확도 | **미답변 문의 · 발송 대기 · 발송됨** · 근거 KB |
| 목록 | 미해결 이슈 | **미답변 문의** |
| 첫 카드 | 🤖 AI 제안(근본원인·해결책) | **📮 고객 응대 답변** (생성 전에도 자리를 지킨다) |
| 근본원인·해결책 | 화면의 주인공 | `🔎 근거` — 접힌 채로, 답변을 뒷받침하는 재료 |

문의를 고르면 **고객이 요청한 것**이 답변을 만들기 전에 먼저 보인다(`/recommend` 가
`asks`·`intent_label` 을 함께 준다 — 규칙 기반이라 비용이 없다). 증상만 읽고 답을
판단하면 요지를 빗나가는데, 그게 초안 거부 사유 1순위다.

**이미 답장한 문의에 또 초안을 만들지 않는다** — 선택 즉시 `/voc/reply/status` 로
발송 대기·발송 이력을 배지로 보여주고, 발송된 건이면 버튼이 "후속 답변" 으로 바뀐다.

**근거가 없어도 고객을 기다리게 두지 않는다.** coverage 게이트에 막히면 예전에는
"AI 제안·심층 분석을 생성하지 않았습니다" 로 끝났다 — 분석 도구로는 옳지만 대응
도구로는 고객을 방치하는 것이다. 이제 그 자리에 **📮 접수 답변 만들기** 가 있다
(원인을 단정하지 않는 골격, 지식 공백은 그대로 집계).

### 심층 분석 = 고객 응대 답변

화면의 **✨ AI 고객 응대 답변**(예전 "AI 심층 분석") 자리는 이제 에이전트가 만드는
**고객에게 보낼 글**이다. 예전에는 여기서 엔지니어용 RCA(근본원인·증상→원인 인과·
검증 절차·사례 키 인라인 인용)를 만들었다. 그러면 사람이 그걸 읽고 **고객 답변을 다시
쓰는 단계가 통째로 남는다** — 에이전트가 하라고 만든 일을 사람이 하게 된다.

생성기(`_generate_explain_md`)를 바꿨을 뿐, 스트리밍·캐시·예열·지연 계측 경로는 그대로
재사용한다. 새 화면을 하나 더 만들면 같은 것을 두 곳에서 만들게 되고, 두 곳은 갈라진다.

- 완료(`done`) 이벤트가 **최종본을 다시 내려준다.** 스트리밍 중에는 모델이 쓴 원문이
  스쳐 지나가는데, 내부 키를 지우고 정책을 매긴 판본은 완료 시점에만 있다. 화면은
  그 판본으로 갈아끼운다.
- 함께 오는 것: 요청 유형(`intent_label`) · 고객이 요청한 문장(`asks`) ·
  답변 언어(`lang`) · 발송 전 정책 판정(`policy`).
- 화면의 **📮 이 답변을 발송 대기로** 는 그 본문을 **그대로** 큐에 넣는다
  (`/voc/reply/draft-from-text`). 서버가 다시 생성하면 사람이 읽고 판단한 글과 큐에
  들어가는 글이 달라진다 — 검토의 의미가 사라지는 종류의 실수다.
- 프롬프트가 바뀌었으므로 `llm_cache.PROMPT_VERSION` 을 올렸다. 옛 캐시본(엔지니어용
  분석)은 자연히 무효가 된다.
- MCP `get_cached_analysis` 도 같은 캐시를 본다 → 이제 고객 답변을 돌려준다. 본문에
  사례 번호가 없는 것이 정상이고(저장 전에 지운다), 근거는 `evidence_keys` 로 온다.

엔지니어용 RCA 경로가 사라진 것은 아니다 — `/rca/draft` 가 근거 기반 RCA 댓글 초안을
만들고 그쪽은 여전히 승인 대기 큐로 간다. 없어진 것은 `/rca/draft-from-analysis`
(고객 답변을 RCA 댓글로 게시하는 경로) 하나다.

### LLM 설명 엔진

`/recommend?explain=true`·`/recommend/explain/stream` 의 종합 설명은 **agno(OpenRouter
HTTP 직접 호출)** 단일 엔진으로 생성한다. 모델·엔드포인트는 `.env`의 `OPENROUTER_*`로 설정
(`OPENROUTER_MODEL`/`OPENROUTER_BASE_URL`/`OPENROUTER_API_KEY`). 스트리밍은 SSE.

### 고객 대응 답변 — 고객이 받는 글

**RCA 분석을 고객에게 그대로 보낼 수는 없다.** 지금까지의 산출물은 엔지니어가 읽는
글이다 — 근본원인·검증 절차·사례 키 인라인 인용. 그대로 나가면 세 가지가 동시에 깨진다.

- 내부 이슈 키(`LSI-42`)와 내부 용어가 고객에게 노출된다
- 고객이 실제로 물은 것("언제 고쳐지나요")에 답하지 않는다 — 요청 유형이 다르면
  답의 **골격 자체**가 달라야 한다
- 확정 일정·환불 같은 **약속**이 검토 없이 외부로 나갈 수 있다

그래서 "RCA 를 고객 말투로 다듬는 후처리" 가 아니라, 요청 유형에서 시작하는 별도
파이프라인을 둔다(`src/voc_agents.py`). 에이전트는 셋이다 — 하나로 뭉치면 답이
이상할 때 유형을 잘못 잡은 건지, 근거가 없던 건지, 문체가 문제인지 분리할 수 없다.

| 에이전트 | 하는 일 | LLM |
|---|---|:--:|
| 의도(intent) | 요청 유형 8종 분류 + **고객이 요청한 문장** 추출 | 불필요 |
| 답변(reply) | 유형별 골격 + 근거를 고객 언어로 | 사용(없으면 결정적 템플릿) |
| 정책(policy) | 발송 전 검사 — **차단은 여기서만** | 불필요 |

**요청 유형이 답의 골격을 정한다.** `defect_report` `status_inquiry`
`workaround_request` `howto` `spec_inquiry` `rma_request` `complaint` `other`.
한 문의에 신호가 섞이면(대개 섞인다) 우선순위는 **고객이 가장 원하는 것** 순이다 —
"안 됩니다. 언제 고쳐지나요?" 에서 기다리는 답은 증상 접수가 아니라 일정이다.

**정책 위반은 두 단계다.** 전부 차단하면 검토자가 검사 자체를 무시한다.

| 심각도 | 코드 | 뜻 |
|---|---|---|
| 차단 | `internal_key` `han_char` `date_promise` `compensation_promise` `plain_speech` `third_party` `wrong_language` | 고치기 전에는 발송 불가 |
| 경고 | `unanswered_ask` `suspect_spelling` `internal_jargon` `missing_next_step` `too_long` `unsupported_certainty` | 사람이 보고 판단 |

시점 표현만으로는 걸리지 않는다 — "다음 주에 확인해 보겠습니다" 는 약속이 아니다.
시점 + 완료 약속이 한 문장에 같이 있을 때만 `date_promise` 다. 오탐이 잦으면 경고가
소음이 되고, 소음이 된 경고는 아무도 안 본다.

`unanswered_ask` 는 **고객이 물은 것에 답하지 않은 것으로 보이는** 경우다. 형식·안전
검사만으로는 이 실패가 전혀 안 보이는데, 답변이 요지를 빗나가는 것이 초안 거부 사유
1순위다. 판정은 요청 문장의 핵심어가 답변에 **하나도** 없을 때로 느슨하게 잡는다 —
"2개 이상 겹쳐야 답한 것" 으로 조이면 말만 바꿔 제대로 답한 문장이 줄줄이 걸린다.
품질 측정(검증 하네스)은 같은 추출기에 **더 엄한 임계**를 쓴다 — 경고는 소음을 피해야
하고 측정은 후하면 안 된다.

`suspect_spelling` 은 **표기 이상**이다. 생성 모델이 한국어 오타를 낸다 — 실측에서
"송괘하게"(송구하게)·"콘트볼러"(컨트롤러)·"작엄"(작업)이 그대로 나왔다. 고객이 읽는
글이라 오타 하나가 답변 전체의 신뢰를 깎는다. 다만 사전 없이 잡을 수 있는 것은
단독 자모·같은 글자 반복·문장부호 앞 공백뿐이라 **경고**다. 진짜 오타는 아래 교정
단계가 맡는다 — 못 잡는 것을 잡는 척하지 않는다.

`third_party` 는 **다른 고객사 이름**이다. 근거 사례는 남의 고장 이력이라 "다른 고객사
○○ 에서도" 는 사실이어도 비밀유지 문제가 된다. 내부 키와 달리 **자동으로 지우지
않는다** — 지우면 문장 뜻이 바뀌므로 사람이 고쳐야 한다.

**고객이 쓴 언어로 답한다.** 문의가 영어면 답변도 영어다 — 내용이 정확해도 언어가
다르면 대응 실패다. 판정은 한글 비율 하나(`detect_lang`)로 결정적으로 한다. 한국어
문의에 영어 로그·제품명이 섞이는 것은 흔하고 영어 문의에 한글이 섞이는 일은 드물어,
임계는 낮게 잡는다(오판 비용이 비대칭이다). 규칙은 언어별로 나눠 관리하지 않는다 —
나누면 한쪽만 갱신돼 **영어 답변에서 약속 금지가 통째로 빠진다**. 검증 하네스가 실제로
그런 구멍을 두 개 잡았다(영어는 시점이 동사 뒤에 와서 순서 고정 정규식이 새고,
`will deploy` 처럼 어간 원형이 샜다). 생성물의 언어가 어긋나면 그 초안은 버리고
같은 언어의 템플릿으로 떨어진다.

> 범위는 **한국어·영어 둘**이다. 일본어·중국어 문의는 한글이 없으므로 `en` 으로 판정돼
> 영어 답변이 나간다 — 무응답보다는 낫지만 옳지는 않다. 그 언어의 VOC 가 실제로 들어오면
> `detect_lang` 에 판정을 추가하고 `_STYLE_RULES_*` 를 같은 규칙으로 하나 더 두면 된다.

#### 교정(proofread) — 오타는 고치되 내용은 못 바꾸게

생성 뒤 오타·띄어쓰기 교정을 LLM 이 한 번 더 본다(`voc_agents.PROOFREAD_PROMPT`).
문제는 교정 모델이 문장을 다시 쓰면서 **수치·조건·약속을 조용히 바꾼다**는 것이다 —
고객 답변에서 그건 오타보다 훨씬 큰 사고다. 그래서 교정은 LLM 이 하고, **그 교정이
내용을 바꾸지 않았는지는 규칙이 검사한다**(`safe_replace`).

하나라도 어긋나면 교정을 버리고 원문을 쓴다: 없던 이슈 키가 생김 · 마크다운 제목이
바뀜 · 숫자가 바뀜 · 제품명·영문 용어가 바뀜 · 언어가 바뀜 · 길이 15% 초과 변동.
사유는 화면에 "교정 폐기" 배지로 남는다. 교정은 있으면 좋은 것이고, 내용 보존은
양보할 수 없는 것이다.

`RVP_PROOFREAD=0` 으로 끈다 — LLM 호출이 1회 늘어난다(실측 48초, 캐시·예열 뒤라
사용자가 기다리는 시간은 아니다).

**RCA 와 다른 세 가지 동작**

- **근거가 없어도 초안을 만든다.** RCA 는 근거가 없으면 만들지 않는 것이 옳지만
  (틀린 원인 단정), 고객 문의는 **답을 안 하는 것이 가장 나쁜 실패**다. 대신 원인을
  단정하지 않는 골격으로 가고, 지식 공백은 `reply_no_evidence` 로 따로 집계한다.
- **해결된 이슈도 대상이다.** "해결됐습니다" 를 알리는 것도 고객 대응이다.
- **LLM 이 없어도 초안이 나온다.** 생성 실패가 무응답이 되면 안 되므로 결정적 템플릿으로
  떨어지고, 어느 경로였는지(`engine`)를 큐에 남긴다.

**우리가 보낸 답변은 KB 근거가 아니다.** Jira 에 게시된 답변에는 `고객 안내 답변`
표식이 붙고 `preprocess` 가 제외한다 — 다시 읽어 들이면 자기 출력을 근거로 삼는
되먹임이 생긴다(RCA 봇 댓글과 같은 이유).

**후속 답변.** 고객이 답장하면 같은 티켓에 **다시** 답해야 한다. 그런데 나간 판본을
조용히 덮으면 발송 이력이 거짓이 된다 — 무엇이 고객에게 갔는지 알 수 없게 된다. 그래서
기본은 덮지 않고(`already_sent`), 명시적 요청(`again`)일 때만 이전 판본을 `history` 로
옮기고 새 초안으로 대체한다. 지표도 이력을 포함해 센다 — 빼면 발송 건수가 줄어
무수정 발송률이 실제보다 좋아 보인다.

**큐가 둘인 이유.** 같은 이슈에 RCA 초안과 고객 답변이 동시에 존재할 수 있어 한
파일에 담으면 서로를 덮어쓴다. 승인 권한도 다르다(`rca.approve` vs `reply.send`).
상태 전이·영속화만 `hitl_queue.Queue` 로 공유한다.

이 작업에서 실제 결함 하나를 잡았다 — 고객 문의 본문의 `h2. 고객 요청 (Ask)` 절이
파싱되지 않아 **목 VOC 32건 전부 요청 문장이 비어 있었다**. 증상만 보고 답을 쓰면
"양산 일정이 3주 남았는데 펌웨어로 해결 가능한지" 같은 진짜 질문이 답변에서 통째로
빠진다. `parse_issue` 에 `customer_ask` 를 추가해 고정했다(회귀 테스트 포함).

#### 검증 — 고객에게 나가도 되는 글인가

`scripts/validate_reply_loop.py` 가 세 가지를 잰다. "돌아간다" 만 확인하는 검증은
무의미하다 — 위반을 **알고** 주입해야 검출률을 말할 수 있고, 정상 문장을 같이 넣어야
오탐률을 말할 수 있다. 둘 중 하나만 재면 "전부 차단" 하는 검사기가 만점을 받는다.

```sh
.venv/bin/python scripts/validate_reply_loop.py            # 정책 + 템플릿 전수 (LLM 0원)
.venv/bin/python scripts/validate_reply_loop.py --llm      # 실제 생성 경로
.venv/bin/python scripts/validate_reply_loop.py --llm --judge   # + LLM 판정
.venv/bin/python scripts/validate_reply_loop.py --judge --reuse  # 생성 결과 재사용(판정만)
.venv/bin/python tests/test_voc_agents.py                  # 118개
```

실측(2026-08-30, VOC 목 32건):

| 항목 | 템플릿 경로 | LLM 경로 |
|---|---|---|
| 정책 검출 복원율(한국어 11 + 영어 6 주입) | **17/17 (1.000)** | 〃 |
| 오탐(정상 문장 차단, 한국어 5 + 영어 3) | **0/8** | 〃 |
| 발송 차단 잔존 · 내부 키 유출 | **0/32 · 0/32** | **0/32 · 0/32** |
| 유형 골격 준수 | **32/32 (1.000)** | **32/32 (1.000)** |
| 요청 반영(어휘 일치, 하한) | 3/32 (0.094) | **31/32 (0.969)** |
| 영어 문의 경로 | 영어로 답함 · 차단 0 | 영어로 답함 · 차단 0 |
| LLM 판정 — 요청에 답함 | — | **28/32 (0.875)** |
| LLM 판정 — 이대로 발송 가능 | — | **28/32 (0.875)** |

**템플릿의 요청 반영이 0.094 인 것은 결함이 아니라 사실이다** — 그 폴백은 접수 사실과
진행 계획만 말하고 고객의 질문에 답하지 않는다. 그래서 템플릿 초안은 **항상**
`needs_review` 다. (요청을 그대로 되짚은 줄은 측정에서 뺀다 — 안 그러면 "요청을
복사했다" 가 "요청에 답했다" 로 둔갑해 템플릿이 1.000 을 받는다. 실제로 그렇게 나왔다.)

요청 반영률은 **어휘 일치라 상한이 아니라 하한**이다(뜻은 맞는데 말이 다르면 놓친다).
어미가 달라 오탐이 나던 것은 고쳤다 — 요청의 `롤백하면` 이 본문의 `롤백` 과 맞지 않아
제대로 답한 답변 6건이 '미응답' 으로 잡혔다. 형태소 분석기를 들이지 않고 어미·조사를
**어미 → 조사 순서로** 벗긴다(순서를 바꾸면 `임시로라도` 가 `임시로라` 라는 없는 말이
된다). `--judge` 는 LLM 이 "요청에 답했는가 / 이대로 보내도 되는가" 를 판정한다.

생성은 LLM 32회라 비싸다 — 결과를 `tmp_db/reply_validation_rows.json` 에 남겨 `--reuse`
로 판정만 다시 돌릴 수 있다. 판정이 구조화 출력 파싱에 실패하면 그 건은 **분모에서
뺀다**(실패를 통과로 세면 수치가 조용히 좋아진다).

**LLM 판정은 근거지 정답이 아니다.** 같은 급의 모델이 매기는 점수라 흔들린다 — 실측에서
"다른 고객사 정보는 공유할 수 없습니다" 라고 **올바르게 거절한** 답변을 발송 불가로
매긴 건이 있었다. 그래도 유지하는 이유는, 어휘 일치가 못 보는 실패(요청을 통째로
건너뜀)를 이것만 잡아내기 때문이다.

판정이 남긴 지적은 두 갈래로 모인다 — ① "동일 증상이 다른 고객사에서도 보고되었는지"
② "불량 교체 대상인지 판단 기준을 달라". 둘 다 **답을 알아도 그대로는 못 주는 질문**이다
(전자는 제3자 정보, 후자는 사례가 없으면 기준을 세울 수 없다). 지금은 프롬프트 규칙으로
"공유할 수 없다는 사실 + 대신 줄 수 있는 것 + 언제 답하는지" 를 쓰게 했고, 여전히
건너뛰는 경우가 남는다. 다음 레버는 프롬프트가 아니라 **지식**이다 — 판단 기준 문서를
KB 에 넣어야 답할 수 있다.

0건을 검증하고 초록을 내는 일이 없도록, 미해결 VOC 를 하나도 못 찾으면 **실패**로
끝난다 — `.env` 를 안 읽어 KB 보조 원천이 빠진 채로 통과한 적이 실제로 있다.

```sh
curl -X POST localhost:8011/voc/reply/draft -d '{"key":"VOC-66"}' -H 'Content-Type: application/json'
curl localhost:8011/voc/reply/pending      # 발송 대기
curl localhost:8011/voc/reply/stats        # 무수정 발송률·발송률·유형 분포·차단 원인
```

초안을 만드는 경로는 하나다 — 분석 화면의 **✨ AI 고객 응대 답변 생성** → 읽고 →
**📮 이 답변을 발송 대기로**. (API 로는 `/voc/reply/draft` 가 생성까지 한 번에 한다.
검증 하네스와 배치 처리가 쓴다.)

화면은 상단바 **📮 고객 답변** — 고객이 요청한 문장, 정책 위반(차단/경고), 편집과
"고객이 보는 모습" 미리보기, 발송·거부. 차단이 남아 있으면 발송 버튼이 잠기고,
본문을 고치면 즉시 재검사한다(옛 판정으로 버튼이 열려 있으면 방금 넣은 위반을 못 본 채
나간다).

대시보드 첫 카드는 **고객 답변**이다 — 유일하게 고객이 직접 받는 산출물이라 맨 앞이다.
RCA 초안 품질은 그 다음 카드로 분리했다.

**지표는 RCA 초안 지표와 섞지 않는다** — 읽는 사람도 실패의 의미도 다르므로 한 지표로
합치면 어느 쪽이 나빠졌는지 알 수 없다. `clean_rate`(무수정 발송) ·
`send_rate`(발송률) · 유형 분포 · 생성 엔진 분포 · 정책 위반 원인 · 근거 없이 쓴 건수.

### Jira 변경 반영 (KB 최신 유지)

두 경로가 있고 **폴링이 기본**이다 — Jira Cloud가 로컬 서버에 도달할 수 없기 때문.

- **폴링(기본)**: 서버가 `RVP_JIRA_POLL_SEC`(기본 5초, 평균 반영 지연 2.5초) 주기로 "마지막 동기화 이후
  변경된 이슈"를 물어 해당 이슈만 재적재하고, **변경이 있을 때만** 추천 캐시를
  무효화한다(빈 폴은 재빌드 비용을 물지 않는다). 삭제는 `updated` JQL로 잡히지
  않으므로 10회마다 전체 키를 대조해 제거한다.
  `RVP_JIRA_POLL_SEC=0` 이면 끈다.
  - `GET /jira/sync/status` — 폴러 상태·마지막 결과
  - `POST /jira/sync[?full=true][&reconcile=true]` — 주기를 기다리지 않고 즉시 동기화
  - CLI: `.venv/bin/python src/jira_sync.py [--full|--reconcile|--watch 30]`
  - 종단 실측(2026-08-01, 주기 5초): Jira 제목 수정 → KB 반영 4.5초 → API 반영 +0.2초,
    변경 이슈 1건만 재조회(`upserted:1`). 원복도 4.9초에 자동 반영.
    무변경 폴 1회 = JQL 1건 240ms(중앙) — 주기를 줄여도 부담은 거의 없다.
- **웹훅(선택)**: 서버가 공개 https URL로 노출된 경우 초 단위 반영.
  수신부 `POST /webhook/jira`는 구현돼 있고, 등록은
  `scripts/jira_webhook_register.py {list|register <공개URL>|delete <id>}`.
  `JIRA_WEBHOOK_SECRET` 설정 시 쿼리로 대조한다. 폴링과 동시 사용해도 무해하다.

### MCP 서버 — Claude Code 등에서 이 지식베이스 쓰기

백엔드의 조회 API 를 MCP 도구로 노출한다(`src/mcp_server.py`). **얇은 포워더**이고
권한은 전부 백엔드가 토큰으로 판정하므로, 인가 규칙이 두 곳으로 갈라지지 않는다.

도구 9개 — `find_similar` · `analyze_issue` · `list_unresolved` ·
`get_cached_analysis` · `knowledge_overview` · `find_duplicate_clusters` ·
`find_contradictions` · `draft_rca` · `whoami`

**의도적으로 뺀 것**
- Jira 게시(`/rca/approve`·`reject`) — 되돌리기 어렵고 외부로 나간다. 사람이 웹에서 승인.
  MCP 로는 `draft_rca`(승인 대기 큐 투입)까지만.
- **고객 답변**(`/voc/reply/*`) 전부 — 초안 생성조차 넣지 않았다. 고객에게 나가는 글은
  발송 전 정책 검사 결과를 **사람이 화면에서 보고** 고치는 것이 전제인데, 도구로
  뽑아가면 그 화면을 건너뛴 사용이 자연스러워진다.
- 설정·사용자관리·동기화·캐시·자기점검 — 에이전트가 만질 이유가 없다.
- **심층 분석 생성** — 캐시본만 돌려준다(`get_cached_analysis`). MCP 클라이언트가
  이미 추론 주체라 백엔드에서 LLM 을 또 부르면 추론이 중첩되고 비용·지연이 두 배가
  된다. 근거는 도구로 충분히 주므로 결론은 클라이언트가 낸다.

전송 방식 두 가지 (같은 도구 정의):
- **streamable-HTTP** — 서버가 `/mcp` 를 직접 제공한다. 클라이언트는 URL + 토큰만
  있으면 되므로 **배포에 적합**. 백엔드 호출은 인프로세스(ASGITransport)라
  자기 자신에게 소켓을 다시 열지 않는다. `RVP_MCP=0` 으로 끈다.
- **stdio** — `python src/mcp_server.py`. 클라이언트가 프로세스를 띄운다(로컬용).
  `LSI_API` · `LSI_MCP_TOKEN` 환경변수.

토큰: `POST /auth/token` (로그인 상태에서 본인 신원으로 발급, 기본 30일).
`Authorization: Bearer <token>` 또는 `X-RVP-Token` 헤더로 쓴다. 웹 세션 쿠키와
**같은 서명 토큰**이라 검증 경로가 하나다. 폐기는 사용자 회수(그 신원의 모든 토큰
무효) 또는 `RVP_SESSION_SECRET` 교체.

원격 배포 시 `LSI_MCP_ALLOWED_HOSTS` 를 지정한다 — 미지정이면 로컬 Host 만 허용해
원격 클라이언트가 421 로 거부된다(DNS 리바인딩 보호).

클라이언트 설정 예시: `scripts/mcp_client_config.md`
검증: `.venv/bin/python tests/test_mcp_server.py` (28개 — 도구 집합 고정, 게시·운영
도구 미노출, 토큰 없음/위조/사용자/관리자 인가 분리, 캐시 전용 동작, 전송 계층)

### 인증(SSO) · 권한(RBAC)

역할은 둘이다 — **관리자(admin)** / **사용자(user)**. 권한은 역할이 아니라 **기능
(capability)** 단위로 검사한다: 엔드포인트가 `require("rca.approve")` 처럼 필요한
기능을 선언하고, 역할→기능 표는 `src/auth.py` 한 곳에만 둔다.

| 기능 | user | admin | 예 |
|---|:--:|:--:|---|
| `issue.read` `reco.read` `knowledge.read` | ✓ | ✓ | 이슈·추천·심층 분석·지식 현황 조회 |
| `rca.draft` `rca.read` | ✓ | ✓ | RCA 초안 → **승인 대기 큐까지만** |
| `reply.draft` `reply.read` | ✓ | ✓ | 고객 답변 초안 → **발송 대기 큐까지만** |
| `feedback.write` | ✓ | ✓ | 추천 피드백·VOC 제출 |
| `rca.approve` | | ✓ | **Jira 실제 게시**·거부 |
| `reply.send` | | ✓ | **고객에게 실제 발송**·거부 |
| `knowledge.write` | | ✓ | 고장모드 기사·수명주기·온톨로지·부정지식 편집 |
| `config.write` | | ✓ | LLM/Jira 접속 설정 |
| `ops.sync` `ops.cache` `ops.eval` | | ✓ | 동기화·재적재·캐시·예열·평가·자기점검 |
| `voc.manage` `improve.manage` | | ✓ | VOC 열람·상태, 개선 큐 처리 |

**로그인 경로 3가지** (설정된 것만 로그인 화면에 나타난다):

- **OIDC SSO** — 인증 코드 플로우 + PKCE. 코드 교환·`id_token` 검증을 **백엔드가**
  한다(프런트에서 하면 IdP 토큰이 JS 가 읽는 곳에 남는다). 결과는 이메일만
  HttpOnly 서명 쿠키에 남기고 IdP 토큰은 저장하지 않는다.
  `RVP_OIDC_DISCOVERY_URL` `RVP_OIDC_CLIENT_ID` `RVP_OIDC_REDIRECT_URI`
  (+ `_CLIENT_SECRET` `_SCOPES` `_EMAIL_CLAIM` `_AUDIENCE` `_POST_LOGIN_URL`)
- **프록시 헤더** — 앞단 SSO 프록시가 검증한 이메일을 신뢰. `RVP_SSO_EMAIL_HEADER`
  를 **명시해야만** 켜진다(기본값을 두면 아무나 그 헤더를 보내 신원을 가로챈다).
- **개발용 로그인** — `RVP_AUTH_DEV_LOGIN=1`. IdP 없이 역할 분리를 확인하는 통로로,
  운영에서는 끈다.

**사용자 등록은 화면에서 한다** — 설정 → 사용자 관리 (관리자만). 등록·역할 변경·회수를
하면 서버가 `users.yaml` 을 원자적으로 다시 쓰고 즉시 재적용한다(재기동 불필요).
잠금 방지 규칙: 활성 관리자를 0명으로 만들 수 없고(마지막 관리자 회수·강등 거부),
자기 자신은 회수할 수 없고, `RVP_ADMIN_EMAILS` 로 지정된 관리자는 화면에서 못 고친다
(그쪽이 탈출구여야 하므로). 회수는 삭제가 아니라 `revoked: true` 로 남긴다.
목록 파일이 없는 상태에서 첫 관리자를 등록하면 그 시점에 **인증이 켜진다**.

인가 목록은 `data/users.yaml`(예시: `data/users.example.yaml`, git 미추적) 또는
`RVP_ADMIN_EMAILS`. **둘 다 없으면 인증 비활성 = 전체 권한**이고, 그 상태는 화면의
"인증 비활성" 배지와 `GET /auth/config` 로 드러난다.

**ID 는 이메일 또는 아이디**다. 사내 SSO 계정은 이메일(사내 형식 `xxx.samsung.com`,
서브도메인 포함)을 쓰고, `admin` 같은 아이디는 IdP 를 거치지 않는 로컬 운영 계정이다
— IdP 가 그 값을 이메일 클레임으로 주지 않으므로 OIDC 로는 로그인되지 않고,
개발용 로그인·프록시 헤더 경로에서 쓴다.

목록 밖 계정은 `RVP_SSO_DEFAULT_ROLE`(기본 `user`, 빈 값이면 거부)로 들어온다.
`RVP_ALLOWED_EMAIL_DOMAINS=samsung.com` 을 두면 **사내 도메인만 자동 등록**된다
(서브도메인 포함 — `sec.samsung.com` 통과, `evil-samsung.com` 차단). IdP 가 외부·게스트
계정을 인증해 주는 구성에서 필요하다.

세션: HttpOnly + SameSite=Lax 서명 쿠키. `RVP_SESSION_SECRET` 을 고정해야 재기동 후
세션이 유지된다. https 배포에서는 `RVP_COOKIE_SECURE=1`.

개발 서버는 Vite 프록시로 프런트와 API 를 **같은 오리진**으로 맞춘다
(`VITE_PROXY_TARGET`, 기본 `http://127.0.0.1:8011`) — 교차 사이트에서는 Lax 쿠키가
실리지 않기 때문이고, 프로덕션은 FastAPI 가 `web/dist` 를 같은 오리진에서 서빙한다.
백엔드에 새 최상위 경로를 추가하면 `web/vite.config.ts` 의 `API_PREFIXES` 에도
넣어야 한다 — 빠지면 개발 서버가 그 요청에 index.html 을 돌려주고, 프런트에서는
"JSON 이 아닌 응답" 으로 나타난다.

검증:
- `.venv/bin/python tests/test_auth_rbac.py` (45개 — 역할별 허용·차단, 세션 위조·만료,
  401/403 구분, 프록시 헤더 신뢰 조건, 인증 비활성 폴백)
- `.venv/bin/python tests/test_user_admin.py` (34개 — 등록·역할변경·회수·복구,
  잠금 방지 4종, 회수된 계정 로그인 거부, 목록 없는 상태에서 인증 켜기)

### 서빙 지연

`GET /metrics` 로 최근 500건의 단계별 분포(p50·p90·max)를 본다. 대시보드 "서빙 지연"
카드에도 있다. 실측(2026-08-02): `/recommend` total p50 **642ms** =
질의 임베딩 API 317ms + rerank API 324ms + 나머지 3ms. 캐시 히트는 0.0ms 로 분리 집계.

`RVP_LAZY_EMBED=1` 로 1차 검색의 질의 임베딩을 생략하면 **363ms** 까지 내려가지만,
변별 셋에서 P@1 이 1.000 → 0.971 로 떨어지고 `embed_cos` 표시도 사라진다.
근본원인을 잘못 짚는 비용이 280ms 보다 크다고 보아 **기본은 끈다** — 지연이 더
중요한 배포에서만 켠다.

### AI 심층 분석 캐시 · 예열

같은 이슈를 다시 열 때마다 LLM을 새로 돌리지 않는다.

- **캐시 키는 내용 주소** — 질의 이슈 내용 + 근거 사례 내용 + 모델 + 프롬프트 버전.
  무관한 이슈가 바뀌어도 캐시가 유지되고, 근거 사례의 근본원인이 수정되면 그 항목만
  자연히 재생성된다. 저장 위치 `tmp_db/llm_cache/`(git 미추적).
- **예열** — 서버 기동 3초 후와 Jira 변경 감지 후 백그라운드로 미해결 이슈의 분석을
  미리 만들어 둔다. 이미 캐시에 있으면 건너뛴다.
- 실측: 심층 분석 캐시 히트 시 첫 토큰 9.9초 → **0.01초**, `/recommend` 0.65초 → 0.00초.
- 화면에 "저장된 분석 재사용" 배지와 "다시 생성"(캐시 무시) 버튼이 있다.
- `GET /explain/cache` 현황 · `POST /explain/prewarm` 수동 예열 · `DELETE /explain/cache` 비우기.
- 프롬프트 문구를 바꾸면 `src/llm_cache.py` 의 `PROMPT_VERSION` 을 올린다(안 올리면 옛 형식이 계속 나간다).

환경변수: `RVP_PREWARM`(0=끔) · `RVP_PREWARM_LIMIT`(기본 20) ·
`RVP_PREWARM_GAP_SEC`(기본 1.0) · `RVP_LLM_CACHE_TTL`(0=무기한)

### 초안 거부·수정 원인 분류 → 자기개선 loop

**VOC 답변 성능을 올리는 유일한 직접 신호는 "사람이 그 초안을 어떻게 판정했는가" 다.**
예전에는 그 신호가 두 곳에서 버려졌다 — `/rca/reject` 는 상태만 뒤집었고,
`/rca/approve` 는 `edited: bool` 만 남겼다. 무엇이 왜 틀렸는지 몰라 다음 초안이 같은
실수를 반복했고, 자기개선 loop 의 신호원(군집·모순·공백·비유용) 중 **최종 산출물의
실패를 보는 것이 하나도 없었다**.

`src/draft_feedback.py` 가 판정을 **닫힌 분류 체계**로 축적한다. 자유 서술만 쌓으면
집계가 안 되고, 집계가 안 되면 loop 가 돌지 않는다.

원인 13종은 전부 **레버**에 매핑된다 — 이게 핵심이다. "무엇이 틀렸나"만 세면
대시보드에서 끝나지만, "어느 손잡이를 돌려야 하나"까지 알면 액션이 나온다.

| 레버 | 원인(예) | loop 가 만드는 액션 |
|---|---|---|
| `retrieval` | 근본원인이 틀림, 근거 무관, 질문 요지 빗나감 | 게이트·랭킹 파라미터 shadow 평가(L2) 후 적용 |
| `generation` | 환각, 인용 키 오류, 얕음, 조치 없음 | 프롬프트 규칙 **자동 주입** + 평가셋 보강 |
| `knowledge` | 사례 자체가 없음, 폐기 지식 참조 | RCA 작성·폐기 (사람, 기존 HITL 엔드포인트) |
| `presentation` | 문체·형식·용어(한자 등) | 검증기 규칙으로 승인 전 차단 |

**사람이 라벨을 안 달아도 신호를 잃지 않는다.** `classify_diff()` 가 (원본→최종) 차이에서
원인을 추정한다 — 인용 삭제→`bad_citation`, 근본원인 섹션 재작성→`wrong_root_cause`,
번호 단계 추가→`missing_action`, `(추정)` 추가→`unsupported_claim`. 추정은
`origin: "auto"` 로 표시해 사람 라벨(`human`)과 **절대 섞지 않는다** — 섞으면 근거의
신뢰도를 알 수 없다.

환류는 두 방향이다:
- **생성**: 같은 고장 클래스에서 **2회 이상** 반복된 지적이 다음 초안의 프롬프트 규칙이
  된다(`prompt_guidance`). 1회는 규칙이 되지 않는다 — 노이즈가 규칙이 되면 프롬프트가
  한 사람의 취향으로 흘러간다. 전역 폴백도 없다(무관 클래스 지적 주입 방지).
- **거버넌스**: 원인이 모이면 `improve_queue` 에 레버별 제안이 올라간다. 최다 원인이
  1회뿐이면 레버를 지목하지 않고 "원인이 흩어져 있으니 사람이 라벨을 달아라" 로 낸다.

측정 지표는 `clean_rate`(손대지 않고 게시된 비율)와 `accept_rate`(거부되지 않은 비율).
무수정 승인도 기록한다 — **분모가 없으면 개선율을 계산할 수 없다**. 두 지표는
`self_improve` 의 핵심지표·드리프트·리포트에 들어가 회차별 추세로 보인다.

```sh
# 대시보드(원인 분포·클래스별 실패율·추세·분류 체계)
curl localhost:8011/rca/draft-feedback
.venv/bin/python tests/test_draft_feedback.py     # 41개
```

### VOC 목 데이터로 loop 검증

기존 `data/all_raw_issues.json` 은 **엔지니어가 쓴 고장 보고서**다. 이 서비스가 실제로
답해야 하는 건 **고객이 Jira 로 올린 VOC** — 짧고, 증상 대신 체감을 말하고, "왜 이러냐/
언제 고쳐지냐" 가 섞인다. loop 는 loop 가 마주칠 입력으로 재야 한다.

`scripts/build_voc_mock.py` 가 고장 템플릿 8종을 **해결 사례(엔지니어 표현)** 와
**미해결 VOC(고객 표현)** 양쪽으로 뽑아 `data/all_raw_issues.json` 과 동일 스키마로
낸다. 프로젝트 키는 `VOC-` 로 분리해 실제 데이터와 섞이지 않는다.

`scripts/validate_draft_loop.py` 가 초안에 **알려진 결함을 주입**하고 사람이 할 법한
수정을 적용해, 자동 분류가 **주입한 원인을 되찾는지** 잰다. 정답 없이 "돌아간다" 만
확인하는 검증은 무의미하기 때문이다.

```sh
.venv/bin/python scripts/build_voc_mock.py --count 96
.venv/bin/python scripts/validate_draft_loop.py     # --write 로 실제 저장소 적재
```

실측(2026-08-26, 96건 / 질의 32건, 로컬 임베딩 fastembed):

| 단계 | 결과 |
|---|---|
| 검색 P@1 · coverage | **32/32** · **32/32** (`hybrid_embed`) |
| 자동 분류 복원율 | **24/24 (1.0)**, 오분류 0 |
| 환류 | 프롬프트 가이던스 2클래스 · 개선 제안 7건(4유형) |

방법별 P@1 (같은 목 데이터, rerank OFF):

| method | P@1 (KB 32) | P@1 (KB 64) |
|---|---|---|
| bm25 | 0.875 | 0.875 |
| graph | 1.0 | 1.0 |
| hybrid · hybrid_embed | **1.0** | **1.0** |

> BM25 가 놓치는 2건은 전부 같은 유형이다 — "사진 연사로 찍으면 앱이 잠깐씩 멈춥니다"
> (KB: "버스트 쓰기 중 write latency spike"), "무선충전 거치대에 올려두면 교통카드가
> 안 찍힙니다"(KB: "WLC 충전 중 polling loop 지연"). 고객이 **증상 대신 체감**을
> 말하면 어휘가 겹치지 않아 어휘 매칭이 무너진다. KB 를 2배로 늘려도 0.875 그대로였다
> — 표본 부족이 아니라 표현 격차다.

이 하네스가 실제 결함 하나를 잡았다: 제목의 한자만 고쳐도(`예상 근본원인` →
`예상 根本原因`) 섹션 추출이 한쪽에서 실패해 `similarity 0.0` 이 나왔고, 형식 손질이
**`wrong_root_cause`(P1 retrieval 신호)** 로 오진됐다. 그대로 뒀으면 loop 가 오타
하나 때문에 검색 파라미터를 튜닝하러 갔을 것이다. 회귀 테스트로 고정했다
(`test_heading_change_is_not_a_content_defect`).

### KB 원천 — 지식 현황

이 서비스가 답해야 하는 대상이 VOC 로 옮겨가면서 KB 에 Jira 미러 말고 다른 원천이
들어와야 했다. 그런데 원천 경로가 `backend/server.py`·`src/self_improve.py`·평가
스크립트에 각각 하드코딩돼 있어, 한 곳만 바꾸면 **서버와 자기개선 loop 가 다른 KB 를
본다** — 이 저장소가 이미 한 번 당한 실패다(대시보드 "모순 없음" vs 개선 큐 "모순 1건").
`src/kb_source.py` 가 단일 소스다.

**Jira 미러에 직접 섞으면 안 된다.** `src/jira_sync.py` 는 `removed = known - live` 로
삭제를 대조하는데 `known` 이 `all_raw_issues.json` 의 전체 키다. Jira 에 없는 키
(`VOC-1` 등)를 미러에 넣으면 **다음 대조 회차에 전부 삭제된다**. 미러는 jira_sync 가
소유하는 파일이고, 보조 지식은 별도 파일로 두고 읽을 때만 합친다.

```sh
RVP_KB_EXTRA=data/voc_mock_issues.json    # 콤마 구분, ROOT 상대 또는 절대 경로
curl localhost:8011/knowledge/sources      # 원천별 현황
```

- 키가 겹치면 **미러가 이긴다**(Jira 가 정본, 보조는 보강). 보조끼리 겹치면 목록 순서.
- 합계는 **중복 제거 후 실제 적재 기준**이다. 파일별 건수를 더한 값과 다르면 키가 겹친
  것이고, 지식 '현황' 이 실제 적재량과 다르면 그 화면은 신뢰를 잃는다.
- 없는 경로는 조용히 빠지지 않고 `missing_sources` 로 드러난다.
- `live` 는 서빙 중인 KB 와의 대조다 — 설정만 바꾸고 캐시를 무효화하지 않으면 갈라진다.

현재 적재(VOC 목 데이터 연결 시):

| 원천 | 건수 | 해결(근거) |
|---|---|---|
| `data/all_raw_issues.json` (Jira 미러) | 264 | 137 |
| `data/voc_mock_issues.json` (보조) | 96 | 64 |
| **합계**(중복 제거) | **360** | **201** (+ 큐레이션 6) |

**VOC 지식이 실제로 근거로 쓰이는가** — VOC 질의 32건 기준 top-1 근거의 24건, top-3
근거의 76/96 이 VOC 사례다. 나머지는 LSI 미러에 더 맞는 실제 사례가 있는 경우로
(UFS latency·thermal throttle 처럼 겹치는 고장모드), 원천을 가리지 않고 더 나은 근거를
고른 결과다 — 원천별 분리가 아니라 통합 검색이 맞는 동작이다.

```sh
.venv/bin/python tests/test_kb_source.py   # 16개
```

### 대시보드 — VOC 답변 현황

대시보드의 첫 카드는 **초안 품질**이다. 다른 카드는 전부 '중간 과정'(KB 구성·품질·중복·
모순·공백)을 보지만, 이것만 **최종 산출물** — 고객에게 나가는 답변 — 의 성패를 본다.

- 헤드라인: `무수정 게시`(clean_rate) · `게시율`(accept_rate) · `사람 수정` · `추세`
- 결함 원인은 **레버 태그**와 함께 보여준다. 원인만 세면 대시보드에서 끝나고, 레버까지
  알아야 무엇을 할지가 정해진다(검색/생성/지식/표현 — 각각 고치는 방법이 다르다).
- `실패율 높은 고장군` 은 어디부터 손댈지를 가리킨다. 원인이 2회 이상 모이면 다음
  초안의 생성 규칙에 자동 반영되고, 개선 큐에 레버별 조치가 올라간다.

**KB 원천** 카드는 지식베이스를 이루는 파일과 각각의 근거 기여량을 보여준다
(`/knowledge/sources`). 적재 총계는 중복 제거 후 실제 기준이라 파일별 합과 다를 수 있다.

판정 이력이 없으면 빈 상태를 보여준다 — 0% 로 그리면 "품질이 0" 이라는 거짓말이 된다.

> 시연용 데이터는 `scripts/validate_draft_loop.py --write` 로 넣는다. 그 데이터는
> **모든 초안에 결함을 주입한 시뮬레이션**이라 실패율이 100% 로 나온다. 실제 성능이
> 아니므로 저장소에 커밋하지 않는다.

### 서빙 성능 — 파생 임베딩 캐시

`/knowledge/contradictions` 가 **7.27초**를 쓰고 있었다(다른 카드는 전부 수 ms). 원인은
캐시 키가 `md5(모든 텍스트를 이어붙인 것)` 이었던 것 — 코퍼스 전체가 한 덩어리라
**레코드가 하나만 바뀌어도**(RCA 승인 1건, Jira 폴링이 물어온 변경 1건) 전체가 무효가
되고 KB 전량(201건)을 다시 임베딩했다. 대시보드는 KB 가 바뀔 때마다 그 값을 냈다.

텍스트별 내용 주소로 바꿨다(`Recommender.embed_cached`):

| 상황 | 이전 | 이후 |
|---|---|---|
| 최초(캐시 없음) | 7.27s | 1.77s |
| 재기동 후 | 7.27s | **0.010s** |
| KB 1건 변경 후 | 7.27s (전량 재임베딩) | **0.031s** (그 1건만) |

캐시는 디스크에 남아 재기동과 KB 변경을 견딘다. 상한은 `RVP_EMB_CACHE_MAX`(기본 5만)이며
이번 요청분을 먼저 지키고 남는 자리에 과거 항목을 채운다.

```sh
.venv/bin/python tests/test_embed_cache.py   # 14개
```

## 구조

```
src/
  ingest.py             1) Jira 적재
  jira_sync.py          Jira 폴링 증분 동기화 (변경분만 재적재 + 삭제 대조)
  auth.py               역할→기능 표 + 인가 목록 (관리자/사용자)
  oidc_sso.py           OIDC 인증 코드 + PKCE (백엔드 코드 교환·id_token 검증)
  session.py            HttpOnly 서명 세션 쿠키
  user_store.py         인가 목록 쓰기 (등록·역할변경·회수 + 잠금 방지)
  llm_cache.py          LLM 생성물 콘텐츠 주소 캐시
  mcp_server.py         MCP 서버 (stdio + streamable-HTTP, 얇은 포워더)
  preprocess.py         2) 전처리 + 엔티티/그래프 (엔티티 패턴 단일 소스)
  explorer.py           3) 탐색/검색/시각화
  recommender.py        해결책 추천기 (graph/bm25/hybrid/embed)
  eval_recommender.py   P@1/P@3/MRR 평가 하네스
  jira_commenter.py     Jira 댓글 조회/게시 (사람 검토 승인 후 사용)
  kb_source.py          KB 원천 단일 소스 (Jira 미러 + RVP_KB_EXTRA 보조 원천)
  draft_feedback.py     초안 거부·수정 원인 분류 축적 → 프롬프트/개선 큐 환류
  voc_agents.py         고객 대응 에이전트 3종 (의도 분류 / 답변 생성 / 발송 정책 검사)
                        + 답변 언어 판정(ko/en)
  hitl_queue.py         HITL 큐 공용 (상태 전이·원자적 영속화)
  rca_queue.py          RCA 승인 큐 (tmp_db/rca_pending.json)
  reply_queue.py        고객 답변 발송 큐 (tmp_db/reply_pending.json)
  self_improve.py       측정(L1)·파라미터 shadow 평가(L2)·지식 변경 제안(L3)
  agent.py, retrievers.py, lang_validator.py, ...  (평가/실험용 유틸)
scripts/
  run_pipeline.py       ingest→preprocess→explorer 오케스트레이션
  jira_webhook_register.py  Jira 웹훅 등록/목록/해제 (공개 URL 필요, 폴링이 기본)
  jira_seed.py          가짜 고장 이슈 Jira 시드 생성기 (--set lsi|nfc|nfc2)
  build_voc_mock.py     VOC 성격 목 Jira 데이터 (고객 표현 질의 + 해결 사례)
  validate_draft_loop.py  초안 개선 loop 엔드투엔드 검증 (결함 주입 → 복원율)
  validate_reply_loop.py  고객 답변 검증 (정책 주입/오탐 + 전수 생성 + 영어 경로 + LLM 판정)
  lsi_failure_data.py   칩 11라인 × (LSI 24종 + NFC Forum 프로토콜 14종) 고장 시나리오
                        NFC 배치: NCI 2.3/Digital 2.4/LLCP 1.4/SNEP/Type 2·3·4·5 Tag/
                        TNEP/WLC 2.0/Smart Poster RTD/NFC Auth Protocol/Connection Handover
                        (https://nfc-forum.org/build/specifications 참조)
backend/server.py       FastAPI (/recommend, /issues/unresolved, /chat, ...)
web/                    Vite + React + TS + Tailwind
  src/ReplyQueue.tsx    고객 답변 발송 대기 화면 (요청·정책 위반·미리보기·발송)
```

## 설정 (.env)

```
JIRA_BASE_URL=...        JIRA_PROJECT_KEY=LSI
JIRA_EMAIL=...           JIRA_API_TOKEN=...      # 또는 JIRA_PAT
RVP_SESSION_SECRET=...   RVP_ADMIN_EMAILS=...    # 인증·권한 (위 절 참조)
OPENROUTER_API_KEY=...   OPENROUTER_MODEL=...    # LLM 엔진=agno(OpenRouter)
```

## Stack

Python 3.11 · networkx · rank-bm25 · fastembed(옵션) · FastAPI · Agno · OpenRouter ·
React + Vite + TypeScript + Tailwind · vis-network
