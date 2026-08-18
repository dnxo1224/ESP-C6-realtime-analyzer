# C6 ↔ S3 배선과 펌웨어

## 배선

LILYGO T7-C6의 일반 헤더에 노출된 GPIO3을 UART1 송신으로 사용한다. 보드 정면 기준
오른쪽 헤더의 `RST` 바로 아래 `IO3` 핀이다.

```text
T7-C6 GPIO3 (UART1 TX) ── 470~499 Ω 직렬 저항 ── ESP32-S3 GPIO1 (UART1 RX)
T7-C6 GND             ─────────────────────────── ESP32-S3 GND
```

- 921600 baud, 8 data bits, no parity, 1 stop bit, flow control 없음
- 다른 GPIO, 3V3, 5V는 연결하지 않는다. 두 보드는 각자 USB로 전원을 공급한다.
- C6 USB Serial/JTAG 콘솔은 그대로 유지된다.
- GPIO3은 strapping 핀이 아니며 이 구성에서 다른 주변장치에 사용하지 않는다.
- GPIO4/5/8/9/15는 strapping 용도이고 GPIO12/13은 USB-JTAG이므로 이 연결에 쓰지 않았다.

## 빌드

ESP-IDF 5.5.3 PowerShell 환경에서 각 폴더별로 실행한다.

```powershell
$env:IDF_TOOLS_PATH='C:\Espressif'
. C:\esp\v5.5.3\esp-idf\export.ps1
cd firmware\csi_send; idf.py build
cd ..\csi_recv; idf.py build
cd ..\csi_relay; idf.py menuconfig; idf.py build
```

S3의 `menuconfig > C6 CSI relay`에서 2.4 GHz SSID/비밀번호, 서버 IP, 포트 9600,
선택적 토큰을 설정한다. 토큰은 서버 `.env`의 `CSI_TOKEN`과 반드시 같아야 한다.
비밀값이 든 생성 `sdkconfig`는 Git에서 제외된다.

> **빌드 메모리 주의**: Docker 스택이 떠 있는 상태에서 IDF 병렬 빌드를 돌리면
> 메모리 부족으로 컴파일러가 내부 오류(Segmentation fault)를 내며 죽는다.
> 컨테이너를 잠시 내리고 `ninja -C build -j 1`로 순차 빌드하면 통과한다.

## 실측 기준값 (2026-08-18, 책상 밀집 배치)

정상 동작 시 Relay 콘솔의 통계 한 줄이 판정 기준이다:

```text
frames=7933 (132.2/s) bad=1 rx1=2003 rx2=1956 rx3=2011 rx4=1963 | wifi=up(drop 0) fwd=on sock=54
```

- `132/s` = 4 Rx × 33 Hz. 네 카운터가 고르게 올라야 정상
- `wifi=down`이면 SSID/비밀번호, `sock=-1`이면 서버 IP·포트·방화벽을 본다
- DB 확인: `SELECT rx_id, COUNT(*) FROM reports WHERE recv_ts > UNIX_TIMESTAMP()-60 GROUP BY rx_id`
  → Rx당 약 2,000행(33 Hz)

Rx 4대는 기존 C6 펌웨어의 RX ID 설정 방식에 따라 1~4로 각각 빌드/플래시한다.
