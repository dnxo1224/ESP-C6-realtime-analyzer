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
선택적 토큰을 설정한다. 비밀값이 든 생성 `sdkconfig`는 Git에서 제외된다.

Rx 4대는 기존 C6 펌웨어의 RX ID 설정 방식에 따라 1~4로 각각 빌드/플래시한다.
