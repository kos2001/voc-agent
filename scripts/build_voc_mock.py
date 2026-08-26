"""VOC 성격 목(mock) Jira 데이터 생성기 — 초안 개선 loop 검증용.

왜 별도 목 데이터인가:
  기존 `data/all_raw_issues.json` 은 **엔지니어가 쓴 고장 보고서**다. 반면 이 서비스가
  실제로 답해야 하는 것은 **고객이 Jira 로 올린 VOC**다 — 문장이 짧고, 증상 대신
  체감을 말하고, "왜 이러냐/언제 고쳐지냐/우리 일정은 어떻게 하냐" 같은 질문이 섞인다.
  같은 고장이라도 표현이 다르면 검색·생성이 다르게 실패한다. loop 를 검증하려면
  **loop 가 실제로 마주칠 입력**으로 재야 한다.

출력(기본 data/voc_mock_issues.json) 은 `data/all_raw_issues.json` 과 **동일 스키마**라
`preprocess.parse_issue` 를 그대로 통과한다. 프로젝트 키는 VOC- 로 분리해 실제
LSI 데이터와 섞이지 않게 한다.

구성:
  · 해결됨(완료) — 시니어 분석 코멘트 포함. 추천기의 KB(근거 사례)가 된다.
  · 미해결(해야 할 일) — 고객 문의 그대로. 초안 생성의 질의가 된다.
  두 쪽을 **같은 고장 템플릿**에서 뽑되 문장을 다르게 써서, 검색이 표현 차이를
  넘어 같은 고장을 찾아내는지 볼 수 있게 한다.

실행:
    .venv/bin/python scripts/build_voc_mock.py            # data/voc_mock_issues.json
    .venv/bin/python scripts/build_voc_mock.py --count 60 --seed 7
"""
from __future__ import annotations

import argparse
import json
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DEFAULT = ROOT / "data" / "voc_mock_issues.json"


@dataclass
class VocTemplate:
    """고장 1종 + 그 고장이 VOC 로 들어올 때의 말투."""
    tid: str
    chip: str
    component: str
    category: str          # 고장 분류(엔티티 패턴과 일치해야 검색이 걸린다)
    fw_prefix: str
    host: str
    title: str             # 엔지니어 표현(해결 사례 제목)
    voc_title: str         # 고객 표현(VOC 제목) — 같은 고장, 다른 말투
    symptom: str           # 엔지니어 증상 기술
    voc_symptom: str       # 고객 체감 기술
    voc_ask: str           # 고객이 실제로 묻는 것 — 답변이 여기에 답해야 한다
    log: str
    root_cause: str
    resolution: str
    workaround: str
    debug: str
    entities: list[str] = field(default_factory=list)


TEMPLATES: list[VocTemplate] = [
    VocTemplate(
        tid="ufs-bkops-latency", chip="UFS4-Controller", component="UFS Controller",
        category="Firmware", fw_prefix="UF40", host="Exynos 2400",
        title="[UFS4-Controller] 버스트 쓰기 중 write latency spike(>500ms)",
        voc_title="[문의] 사진 연사로 찍으면 앱이 잠깐씩 멈춥니다",
        symptom="짧은 시간 대량 쓰기 시 간헐적으로 write latency가 500ms를 초과합니다.",
        voc_symptom="카메라로 연사 촬영을 하면 저장 중에 화면이 0.5초 정도 멈췄다가 돌아옵니다. "
                    "매번은 아니고 저장 공간이 찼을 때 더 자주 그러는 것 같습니다.",
        voc_ask="이게 저희 앱 문제인지 스토리지 문제인지 알고 싶습니다. 양산 일정이 3주 남았는데 "
                "펌웨어로 해결 가능한지 알려주세요.",
        log="ufs: bkops level 3 (critical)\nblk_mq: request latency 612ms on /dev/sda",
        root_cause="호스트가 bkops(백그라운드 오퍼레이션)를 허용하지 않아 device 내부 GC가 지연되고, "
                   "임계 초과 후 강제 GC가 전면화되며 write latency spike가 발생합니다.",
        resolution="호스트 드라이버에서 bkops_en을 활성화하고, 유휴 구간에 bkops를 유도하도록 "
                   "UF40.3.4.150 이상 펌웨어로 업데이트합니다.",
        workaround="사용률 80% 미만을 유지하고 유휴 시간에 수동 TRIM을 주기적으로 실행합니다.",
        debug="ufs bkops 레벨 로그와 blk_mq latency 히스토그램을 같은 타임라인에 겹쳐 확인했습니다.",
        entities=["bkops", "GC", "TRIM", "UFS Controller"],
    ),
    VocTemplate(
        tid="nvme-thermal-throttle", chip="PM9C3-NVMe", component="SSD Controller",
        category="Thermal", fw_prefix="GDC7", host="AMD Genoa",
        title="[PM9C3-NVMe] 지속 쓰기 중 thermal throttle로 대역폭 40% 하락",
        voc_title="[불만] 벤치마크는 잘 나오는데 실사용에서 속도가 반토막 납니다",
        symptom="지속 쓰기 10분 경과 후 온도 상승으로 throttle이 걸려 대역폭이 급락합니다.",
        voc_symptom="처음 1~2분은 광고된 속도가 나오는데, 대용량 파일을 계속 쓰면 속도가 절반 이하로 "
                    "떨어집니다. 케이스를 열어두면 조금 낫습니다.",
        voc_ask="스펙 미달인 건지 정상 동작인지 명확히 답변 부탁드립니다. 고객사에 설명해야 합니다.",
        log="nvme: thermal throttle engaged, composite temp 85°C\nnvme: bandwidth 6.8GB/s -> 4.0GB/s",
        root_cause="컨트롤러 composite 온도가 85°C 임계에 도달해 펌웨어가 thermal throttle을 "
                   "적용한 것으로, 방열 설계가 지속 쓰기 부하를 감당하지 못한 것이 근본원인입니다.",
        resolution="히트싱크 접촉 면적을 늘리고 GDC7.4.2.700에서 throttle 진입 곡선을 완만하게 "
                   "조정한 펌웨어를 적용합니다.",
        workaround="지속 쓰기 작업을 분할하고 케이스 흡기 팬 RPM을 상향합니다.",
        debug="온도 텔레메트리와 대역폭 로그를 초 단위로 정렬해 throttle 진입 시점을 특정했습니다.",
        entities=["throttle", "throttling", "Thermal"],
    ),
    VocTemplate(
        tid="pcie-ltssm-linkdown", chip="PM9C3-NVMe", component="SSD Controller",
        category="Signal Integrity", fw_prefix="GDC7", host="Intel Raptor Lake",
        title="[PM9C3-NVMe] 콜드부트 시 PCIe LTSSM link down / Gen5 → Gen3 강등",
        voc_title="[문의] 재부팅하면 가끔 디스크가 안 잡히거나 느려집니다",
        symptom="콜드부트 중 LTSSM 이 Recovery 를 반복하며 링크가 Gen3 로 강등되거나 down 됩니다.",
        voc_symptom="컴퓨터를 껐다 켜면 열 번에 한 번 정도 디스크가 아예 안 보입니다. "
                    "보일 때도 속도가 평소보다 많이 느릴 때가 있습니다. 따뜻할 때는 괜찮습니다.",
        voc_ask="불량 교체 대상인지, 보드 문제인지 판단 기준을 알려주세요.",
        log="pcieport: LTSSM state Recovery.RcvrLock timeout\npcieport: link down, retraining to Gen3",
        root_cause="레퍼런스 클럭 지터와 저온에서의 CDR 락 지연이 겹쳐 Gen5 링크 트레이닝이 "
                   "실패하고 LTSSM 이 Recovery 를 반복하는 것이 근본원인입니다.",
        resolution="보드 클럭 버퍼를 저지터 부품으로 교체하고 GDC7.4.2.684에서 저온 구간 CDR "
                   "ADAPT 파라미터를 재조정합니다.",
        workaround="BIOS 에서 링크 속도를 Gen4 로 고정하면 증상이 사라집니다.",
        debug="저온 챔버에서 콜드부트를 반복하며 LTSSM 상태 전이와 클럭 지터를 동시 측정했습니다.",
        entities=["LTSSM", "CDR", "ADAPT", "Gen5", "Gen3", "Signal Integrity"],
    ),
    VocTemplate(
        tid="isp-flicker-banding", chip="ISOCELL-HP9", component="Image Sensor",
        category="Firmware", fw_prefix="ISP3", host="Snapdragon 8 Gen3 ISP",
        title="[ISOCELL-HP9] 50Hz 조명 환경에서 anti-flicker 미동작으로 밴딩 발생",
        voc_title="[불만] 실내에서 찍으면 가로줄 무늬가 생깁니다",
        symptom="50Hz 조명에서 anti-flicker 검출이 실패해 프레임에 밴딩이 남습니다.",
        voc_symptom="사무실 형광등 아래에서 영상을 찍으면 화면에 옅은 가로줄이 흐릅니다. "
                    "밖에서는 멀쩡하고, 실내에서도 어떤 건물은 괜찮습니다.",
        voc_ask="촬영 설정으로 피할 수 있는 문제인지, 아니면 수정이 필요한 결함인지 알려주세요.",
        log="isp3: flicker detect confidence 0.31 (< 0.5 threshold)\nisp3: anti-flicker bypass",
        root_cause="저조도에서 flicker 검출 신뢰도가 임계 미만으로 떨어져 anti-flicker 보정이 "
                   "우회되는 것이 근본원인입니다.",
        resolution="ISP3.2.1.90 에서 저조도 flicker 검출 윈도를 늘리고 임계를 0.35 로 낮춥니다.",
        workaround="수동으로 셔터를 1/100s 로 고정하면 밴딩이 사라집니다.",
        debug="조도별 flicker 검출 신뢰도를 스윕해 임계 미달 구간을 특정했습니다.",
        entities=["flicker", "banding", "anti-flicker", "Firmware"],
    ),
    VocTemplate(
        tid="modem-handover-drop", chip="MDM-5400", component="5G Modem",
        category="Timing", fw_prefix="MDM5", host="Reference RFFE",
        title="[MDM-5400] NSA→SA 핸드오버 중 RRC 재설정 타임아웃으로 호 단절",
        voc_title="[장애] 이동 중에 통화가 끊어진다는 신고가 반복 접수됩니다",
        symptom="NSA 에서 SA 로 전환하는 핸드오버 구간에서 RRC reconfiguration 이 타임아웃됩니다.",
        voc_symptom="차량으로 이동하면서 통화하면 특정 구간에서 통화가 끊깁니다. "
                    "같은 장소에서 반복 재현되고, 서 있을 때는 문제가 없습니다.",
        voc_ask="기지국 문제인지 단말 문제인지 절대적인 판단 근거가 필요합니다. 통신사와 회의가 잡혀 있습니다.",
        log="rrc: reconfiguration timer T304 expired\nnr: beam failure recovery attempt 3 failed",
        root_cause="핸드오버 시 beam failure recovery(BFR) 재시도와 T304 타이머가 경합해 "
                   "RRC 재설정이 완료되기 전에 타이머가 만료되는 것이 근본원인입니다.",
        resolution="MDM5.7.0.220 에서 BFR 재시도 상한을 낮추고 T304 를 규격 상한으로 조정합니다.",
        workaround="해당 구간에서 SA 전용 모드를 비활성화하면 단절이 사라집니다.",
        debug="RRC 시그널링 로그와 beam 측정 리포트를 시간축으로 정렬해 경합 구간을 찾았습니다.",
        entities=["RRC", "NSA", "SA", "handover", "beam", "BFR", "Timing"],
    ),
    VocTemplate(
        tid="nfc-wlc-polling", chip="NFC3-Controller", component="NFC Controller",
        category="Power", fw_prefix="NFC3", host="Reference NFC frontend",
        title="[NFC3-Controller] WLC 충전 중 polling loop 지연으로 태그 인식 실패",
        voc_title="[문의] 무선충전 거치대에 올려두면 교통카드가 안 찍힙니다",
        symptom="WLC 충전이 활성일 때 polling loop 주기가 늘어나 태그 검출이 실패합니다.",
        voc_symptom="무선충전 중일 때만 교통카드 태그가 인식이 안 됩니다. 충전을 빼면 바로 됩니다.",
        voc_ask="충전과 태그를 동시에 쓰는 게 원래 안 되는 건가요? 사용자 안내 문구를 바꿔야 할지 판단이 필요합니다.",
        log="nfc3: polling loop period 1200ms (expected 300ms)\nnfc3: LPCD suppressed by WLC field",
        root_cause="WLC RF field 가 LPCD(저전력 카드 검출)를 억제해 polling loop 주기가 4배로 "
                   "늘어나면서 태그가 검출 윈도를 벗어나는 것이 근본원인입니다.",
        resolution="NFC3.1.4.55 에서 WLC 활성 구간에 polling 우선순위를 올리고 LPCD 억제를 해제합니다.",
        workaround="충전 거치대에서 잠시 떼고 태그하면 정상 인식됩니다.",
        debug="WLC on/off 조건에서 polling loop 주기와 LPCD 이벤트를 로거로 비교했습니다.",
        entities=["WLC", "LPCD", "polling loop", "RF field", "NFC", "Power"],
    ),
    VocTemplate(
        tid="ddr-zq-training", chip="DDR5-PHY", component="DRAM PHY",
        category="Hardware", fw_prefix="DDIT", host="Server reference board",
        title="[DDR5-PHY] 고온 구간 ZQ 캘리브레이션 편차로 DQS 트레이닝 실패",
        voc_title="[장애] 여름철에만 서버가 간헐적으로 리부팅됩니다",
        symptom="고온에서 ZQ 캘리브레이션 편차가 커져 DQS training 이 실패하고 부팅이 중단됩니다.",
        voc_symptom="장비실 온도가 올라가는 오후에만 서버가 재시작됩니다. 겨울에는 한 번도 없었습니다.",
        voc_ask="온도 스펙 내인데도 발생합니다. 하드웨어 교체가 필요한지 확답을 주세요.",
        log="ddr: ZQ calibration delta 14 (limit 8)\nddr: DQS training failed on rank1",
        root_cause="고온에서 ZQ 저항 캘리브레이션 편차가 한계를 초과해 DQS training 마진이 "
                   "사라지는 것이 근본원인입니다.",
        resolution="DDIT.2.0.310 에서 온도별 ZQ 재캘리브레이션 주기를 단축하고 training 마진을 "
                   "재산출합니다.",
        workaround="장비실 온도를 25°C 이하로 유지하면 재현되지 않습니다.",
        debug="온도를 단계적으로 올리며 ZQ 편차와 DQS training 결과를 기록했습니다.",
        entities=["ZQ", "DQS", "training", "Hardware"],
    ),
    VocTemplate(
        tid="se-attestation-fail", chip="SEC7-eSE", component="Secure Element",
        category="Security", fw_prefix="SEC7", host="Reference secure host",
        title="[SEC7-eSE] secure boot 후 attestation PCR 불일치로 결제 앱 실행 실패",
        voc_title="[장애] 업데이트 후 결제 앱이 기기를 인증하지 못한다고 나옵니다",
        symptom="secure boot 이후 PCR 값이 기대와 달라 attestation 검증이 실패합니다.",
        voc_symptom="펌웨어 업데이트를 하고 나서부터 결제 앱이 '기기를 확인할 수 없습니다' 라고 뜹니다. "
                    "초기화해도 동일합니다.",
        voc_ask="롤백하면 되는지, 아니면 앱 쪽에서 대응해야 하는지 알려주세요. 서비스가 중단된 상태입니다.",
        log="ese: attestation failed, PCR[4] mismatch\nese: secure boot measurement changed",
        root_cause="펌웨어 업데이트로 부트 측정값이 바뀌었는데 attestation 기준 PCR 값이 함께 "
                   "갱신되지 않은 것이 근본원인입니다.",
        resolution="SEC7.5.0.12 배포와 함께 attestation 기준 PCR 목록을 갱신 배포합니다.",
        workaround="이전 펌웨어로 롤백하면 즉시 정상화됩니다.",
        debug="업데이트 전후 PCR 측정값을 덤프해 변경된 인덱스를 특정했습니다.",
        entities=["attestation", "secure boot", "PCR", "eSE", "Security"],
    ),
]

# 시니어 RCA 코멘트 헤더 — scripts/lsi_failure_data.py 와 동일해야 파서가 인식한다.
ANALYSIS_HEADER = "🔍 시니어 근본원인 분석"

CUSTOMERS = ["Cobalt Wearables", "Northwind Mobility", "Helio Devices", "Aster Robotics",
             "Kestrel Auto", "Lumen Imaging", "Vertex Cloud", "Marin Payments"]
REPORTERS = ["T. Seo (Customer Eng)", "J. Park (Field App)", "H. Kim (QA)",
             "R. Lee (Program Mgr)", "M. Choi (Support)"]
SENIORS = ["senior.han", "senior.yoon", "senior.jo", "senior.baek"]
SEVERITIES = ["Blocker", "Critical", "Major", "Minor"]
PRIORITY = {"Blocker": "Highest", "Critical": "High", "Major": "Medium", "Minor": "Low"}
# 고객이 VOC 를 올릴 때 붙는 요구 — 답변이 여기에 답하지 않으면 'wrong_scope' 로 거부된다.
ASK_SUFFIX = [
    "회신 기한은 이번 주 금요일입니다.",
    "동일 증상이 다른 고객사에서도 보고되었는지 알려주세요.",
    "재발 방지 대책까지 포함해 답변 부탁드립니다.",
    "임시로라도 넘길 방법이 있으면 먼저 알려주세요.",
]


def _fw(prefix: str, rng: random.Random) -> str:
    return f"{prefix}.{rng.randint(1, 7)}.{rng.randint(0, 9)}.{rng.randint(10, 999)}"


def _resolved_description(t: VocTemplate, chip_fw: str, customer: str,
                          reporter: str, sev: str, found: str) -> str:
    return (
        f"h2. 고객 고장 보고\n\n"
        f"*고객사*: {customer}\n*보고자*: {reporter}\n*칩 모델*: {t.chip}\n"
        f"*펌웨어 버전*: {chip_fw}\n*호스트/플랫폼*: {t.host}\n*발견일*: {found}\n"
        f"*고장 분류*: {t.category}\n*심각도*: {sev}\n\n"
        f"h2. 증상 (Symptom)\n\n{t.symptom}\n\n"
        f"h2. 로그 발췌 (Log Excerpt)\n\n{{code}}\n{t.log}\n{{code}}\n\n"
        f"h2. 영향 (Impact)\n\n해당 고장은 *{sev}* 등급으로 분류됩니다.\n")


def _voc_description(t: VocTemplate, chip_fw: str, customer: str, reporter: str,
                     sev: str, found: str, ask_extra: str) -> str:
    """고객이 직접 쓴 문의 — 엔지니어 보고서보다 짧고, 요구가 앞에 온다."""
    return (
        f"h2. 고객 문의 (VOC)\n\n"
        f"*고객사*: {customer}\n*보고자*: {reporter}\n*칩 모델*: {t.chip}\n"
        f"*펌웨어 버전*: {chip_fw}\n*호스트/플랫폼*: {t.host}\n*접수일*: {found}\n"
        f"*고장 분류*: {t.category}\n*심각도*: {sev}\n\n"
        f"h2. 증상 (Symptom)\n\n{t.voc_symptom}\n\n"
        f"h2. 고객 요청 (Ask)\n\n{t.voc_ask} {ask_extra}\n\n"
        f"h2. 로그 발췌 (Log Excerpt)\n\n{{code}}\n{t.log}\n{{code}}\n")


def _senior_comment(t: VocTemplate, engineer: str) -> str:
    """시니어 RCA 코멘트 — 라벨은 `preprocess._comment_block` 규약을 **정확히** 따른다.

    처음에 '*근본 원인*:' 으로 썼더니 파서가 '근본 원인 (Root Cause)' 를 못 찾아
    root_cause/resolution 이 전부 빈 문자열이 됐다. 목 데이터가 파이프라인을 통과
    하는 것처럼 보이면서 KB 는 사실상 비어 있는 상태 — 검증에 쓰면 가장 위험한 형태다.
    """
    return (
        f"{ANALYSIS_HEADER} (작성: {engineer})\n\n"
        f"*디버깅 접근*: {t.debug}\n\n"
        f"*근본 원인 (Root Cause)*:\n{t.root_cause}\n\n"
        f"*적용 해결책 (Resolution)*:\n{t.resolution}\n\n"
        f"*임시 우회책 (Workaround)*:\n{t.workaround}\n")


def _verify_comments(t: VocTemplate, engineer: str, reporter: str) -> list[str]:
    """해결·검증 + 고객 검증 코멘트 — 둘 다 있어야 `verified=True` 가 된다.

    verified 는 추천 신뢰도 근거로 쓰이므로, 목 데이터에 검증된 사례와 아닌 사례가
    섞여 있어야 실제와 같은 조건에서 검증할 수 있다.
    """
    return [
        f"h3. ✅ 해결 및 검증 (담당: {engineer})\n\n"
        f"*적용 내용*: {t.resolution}\n"
        f"*검증 결과*: 재현 시나리오 100회 반복에서 증상이 재현되지 않았습니다.\n",
        f"h3. 🙌 고객 검증 완료 (보고자: {reporter})\n\n"
        f"*고객 확인*: 현장 단말에서 1주간 모니터링한 결과 재발하지 않았습니다.\n",
    ]


def _voc_followup(t: VocTemplate, reporter: str) -> str:
    return (f"h3. 📩 고객 추가 정보 (보고자: {reporter})\n\n"
            f"*추가 관찰*: 동일 증상이 다른 단말 2대에서도 재현됩니다.\n"
            f"*요청*: {t.voc_ask}\n")


def _resolved_comments(t: VocTemplate, rng: random.Random) -> list[str]:
    """해결 사례 코멘트 스레드. 약 2/3만 고객 검증까지 완료 — verified 가 전부 True 면
    신뢰도 신호가 상수가 되어 아무것도 구분하지 못한다."""
    eng, rep = rng.choice(SENIORS), rng.choice(REPORTERS)
    out = [_senior_comment(t, eng)]
    if rng.random() < 0.66:
        out += _verify_comments(t, eng, rep)
    return out


def build(count: int = 48, seed: int = 20260826) -> list[dict]:
    """해결/미해결 VOC 이슈 생성. 같은 템플릿을 양쪽에 배치해 검색 검증이 가능하게 한다."""
    rng = random.Random(seed)
    base = datetime(2026, 5, 1, 9, 0, 0)
    issues: list[dict] = []
    n = 0
    # 템플릿을 라운드로빈으로 돌려 클래스별 표본이 고르게 쌓이게 한다 —
    # 한 클래스에 몰리면 by_class() 집계가 한 칸만 채워져 loop 검증이 안 된다.
    while len(issues) < count:
        t = TEMPLATES[len(issues) % len(TEMPLATES)]
        n += 1
        # 앞쪽 2/3 은 해결 사례(KB), 뒤쪽 1/3 은 미해결 VOC(질의).
        resolved = len(issues) < int(count * 2 / 3)
        key = f"VOC-{n}"
        customer, reporter = rng.choice(CUSTOMERS), rng.choice(REPORTERS)
        sev = rng.choice(SEVERITIES)
        fw = _fw(t.fw_prefix, rng)
        created = base + timedelta(days=rng.randint(0, 110), hours=rng.randint(0, 8))
        found = (created - timedelta(days=rng.randint(1, 20))).strftime("%Y-%m-%d")
        labels = [t.category, t.chip.split("-")[0], "voc", f"fw-{t.fw_prefix}"]
        if resolved:
            issues.append({
                "key": key, "summary": t.title,
                "description": _resolved_description(t, fw, customer, reporter, sev, found),
                "labels": labels + ["resolved"], "priority": PRIORITY[sev],
                "components": [t.component], "status": "완료",
                "created": created.strftime("%Y-%m-%dT%H:%M:%S.000+0900"),
                "comments": _resolved_comments(t, rng),
            })
        else:
            issues.append({
                "key": key, "summary": t.voc_title,
                "description": _voc_description(t, fw, customer, reporter, sev, found,
                                                rng.choice(ASK_SUFFIX)),
                "labels": labels + ["customer-report"], "priority": PRIORITY[sev],
                "components": [t.component], "status": "해야 할 일",
                "created": created.strftime("%Y-%m-%dT%H:%M:%S.000+0900"),
                "comments": [_voc_followup(t, reporter)],
            })
    return issues


def main() -> int:
    ap = argparse.ArgumentParser(description="VOC 목 Jira 데이터 생성")
    ap.add_argument("--count", type=int, default=48)
    ap.add_argument("--seed", type=int, default=20260826)
    ap.add_argument("--out", type=Path, default=OUT_DEFAULT)
    a = ap.parse_args()
    issues = build(a.count, a.seed)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(issues, ensure_ascii=False, indent=2), encoding="utf-8")
    res = sum(1 for i in issues if i["status"] == "완료")
    print(f"[voc-mock] {len(issues)}건 → {a.out.relative_to(ROOT)} "
          f"(해결 {res} / 미해결 {len(issues) - res}, 템플릿 {len(TEMPLATES)}종)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
