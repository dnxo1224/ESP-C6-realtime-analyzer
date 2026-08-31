# C6 전용 실시간 CSI 시스템

기존 `proj-backhaul-c5`를 수정하지 않고 만든 독립 프로젝트다. ESP32-C6 1 Tx/4 Rx가
512바이트 CSI를 수집하고, ESP32-S3가 UART 스트림을 TCP로 중계하며, MySQL·ingest·RF
worker·Spring Admin이 보정과 실시간 추론을 담당한다.

## 고정 계약

- Wi-Fi: 2.4 GHz ch11, HE20, MCS0, 33 Hz
- 프레임: `A5 5A 01 | payload_len(u16 LE) | meta(46) + CSI(512) | XOR`
- 보상: 각 I/Q에 `int(compensate_gain * raw_int8)`을 먼저 적용
- 입력: 256 I/Q 중 `{0..4, 128, 252..255}` 제거 → 246 서브캐리어
- 창: `(200, 4, 246)`, stride 33
- 특징/모델: N5+N4z, 2168 특징, `models/final_bundle.joblib`
- 490바이트 C5 프레임은 ingest에서 거부

## 빠른 시작

```powershell
Copy-Item .env.example .env
docker compose up -d --build --wait
Start-Process http://127.0.0.1:8180
```

`.env`에 `CSI_TOKEN`·`ADMIN_PASSWORD`를 채우면 ingest 토큰 핸드셰이크와 admin 비밀번호가
활성화된다. **비워두면 둘 다 비활성**이라 로컬은 무마찰이지만, 외부에 노출되는 곳에서는
반드시 채울 것.

## 검증

기능 전 경로(A단계) — 240슬롯 축소판, 몇 분 소요:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\verify-a.ps1
```

`.env`의 비밀값을 자동으로 읽어 **보안이 켜진 구성 그대로** 검증하며, 토큰 없는 스트림과
미인증 API 호출이 실제로 거부되는지까지 확인한다.

실운용 규모(B단계) — 19,800슬롯 보정을 통째로 돌려 소요 시간·메모리를 측정한다.
클라우드 인스턴스 사양을 정하기 전에 통과시킬 것:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\verify-b-scale.ps1
```

샘플 형식만 확인하려면:

```powershell
python .\scripts\replay_c6_sample.py --validate-only --slots 240
```

`--drop-rx 4`로 특정 Rx를 제외해 보정 게이트 실패 경로를 재현할 수 있다.

> PowerShell 5.1은 `.ps1`을 ANSI로 읽으므로 이 스크립트들은 **UTF-8 BOM**으로 저장돼 있다.
> 편집 후 BOM이 사라지면 한글 문자열에서 구문 오류가 난다.

운영 시 Admin에서 **빈방 보정 시작** 후 10분(기본 19,800 슬롯)을 유지하고, 보정이
VALID가 된 뒤 **추론 시작**을 누른다. `CALIBRATION_SLOTS=240`은 샘플 시험 전용이다.

Admin 화면 구성:

| 경로 | 내용 |
|---|---|
| `/` | 실시간 관제 — 라이브 CSI 스펙트로그램·파형, 운용 제어, 낙상/수신끊김 경고, Rx별 수신 |
| `/calibrations` | 보정 이력·임계값 조회, 과거 VALID 보정 재활성화 |
| `/sessions` | 수집 세션 관리(위치/행동/사람 라벨), 세션 상세 분석 뷰 3종 |
| `/inference` | 낙상 확률 추이와 에피소드 판정 |

## 격리된 자원

| 자원 | C6 값 |
|---|---|
| Admin | <http://127.0.0.1:8180> |
| TCP ingest | `9600` |
| MySQL host | `127.0.0.1:13306` |
| DB / volume | `csi_c6` / `csi-c6-db-data` |
| containers | `csi-c6-db`, `csi-c6-ingest`, `csi-c6-worker`, `csi-c6-admin` |

펌웨어 빌드·플래시와 실제 배선은 [docs/HARDWARE.md](docs/HARDWARE.md), 모델/CSV 검증은
[docs/C6_CONTRACT.md](docs/C6_CONTRACT.md)를 따른다. 원본 출처는 [SOURCE_MANIFEST.md](SOURCE_MANIFEST.md)에 고정했다.

## 라이선스와 데이터 안내

펌웨어는 Espressif의 [esp-csi](https://github.com/espressif/esp-csi) 예제에서 파생했으며
Apache-2.0을 따른다([LICENSE](LICENSE)).

`docs/`의 연구 문서에 나오는 **집·인물 라벨은 익명화한 것**이다
(`home_A`~`home_C`, `P1`~`P4`). 실제 참여자 정보는 이 저장소에 포함하지 않는다.
`samples/synced_head.csv`는 형식 검증용 30슬롯 발췌이며 전체 데이터셋은 포함하지 않는다.
