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

전체 A단계 샘플 검증은 다음 한 명령으로 실행한다.

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\verify-a.ps1
```

샘플 형식만 확인하려면:

```powershell
python .\scripts\replay_c6_sample.py --validate-only --slots 240
```

운영 시 Admin에서 **빈방 보정 시작** 후 10분(기본 19,800 슬롯)을 유지하고, 보정이
VALID가 된 뒤 **추론 시작**을 누른다. `CALIBRATION_SLOTS=240`은 샘플 시험 전용이다.

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
