/*
 * Optimized CSI Receiver — WIRELESS-BACKHAUL Rx  (ESP32-C6 / HE20)
 * - 링버퍼 + 별도 출력 태스크로 패킷 손실 최소화
 * - 콜백에서는 큐에 넣기만 하고 즉시 리턴
 * - 기본 모드(CONFIG_BACKHAUL_WIRELESS=1): 캡처한 CSI(meta+원시 바이트)를
 *   ESP-NOW 유니캐스트로 게이트웨이(1a:..:01)에 되쏜다. 데이터 케이블 불필요.
 *   4대가 같은 프로브에 동시에 응답하며 충돌하지 않도록, 프로브 수신 시각
 *   기준의 고정 슬롯(TDMA-lite: 2ms + slot*3ms)에서 송신한다.
 * - 디버그 모드(=0): 기존처럼 USB-Serial/JTAG로 바이너리 프레임 출력.
 *
 * ===================== CSI 포맷 계약 (변경 금지) =====================
 * 기존 수집 데이터(246차원 진폭)와의 호환을 위해 아래를 고정한다.
 * 하나라도 바꾸면 과거 데이터셋과 차원이 어긋나 재학습이 필요해진다.
 *
 *   PHY          : 2.4 GHz HE20 (802.11ax SU), MCS0 LGI, ch11
 *   CSI 취득     : acquire_csi_su 만 true (L-LTF/HT/MU/DCM/BFM 전부 false)
 *   서브캐리어   : 256개 = 512 B (int8 I/Q 쌍)
 *   null 인덱스  : 0-4, 128(DC), 252-255  → 유효 진폭 246차원
 *   게인         : 프리즈 없음(CONFIG_FORCE_GAIN=0). 원본 int8 전송 +
 *                  compensate_gain(float)을 meta에 실어 PC에서 곱한다.
 *
 * su만 켜는 이유: HT20/HT40까지 켜면 PPDU 종류에 따라 info->len이 달라져
 * 프레임마다 차원이 흔들린다. su 전용이면 레이트 폴백이 captured=0으로
 * 즉시 드러난다(진단 가능성 > 관대함).
 * ====================================================================
 */

#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <unistd.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"

#include "nvs_flash.h"
#include "esp_mac.h"
#include "rom/ets_sys.h"
#include "esp_log.h"
#include "esp_wifi.h"
#include "esp_netif.h"
#include "esp_now.h"
#include "esp_timer.h"
#include "soc/soc_caps.h"
#if SOC_USB_SERIAL_JTAG_SUPPORTED
#include "driver/usb_serial_jtag.h"
#include "driver/usb_serial_jtag_vfs.h"
#endif

// 타겟에 따라 gain control 헤더 포함
#if CONFIG_IDF_TARGET_ESP32S3 || CONFIG_IDF_TARGET_ESP32C3 || CONFIG_IDF_TARGET_ESP32C5 || CONFIG_IDF_TARGET_ESP32C6 || CONFIG_IDF_TARGET_ESP32C61
#include "esp_csi_gain_ctrl.h"
#define CONFIG_GAIN_CONTROL 1
#else
#define CONFIG_GAIN_CONTROL 0
#endif

// ==================== 설정 ====================
#define CONFIG_LESS_INTERFERENCE_CHANNEL    11
#define CONFIG_ESP_NOW_PHYMODE              WIFI_PHY_MODE_HE20
#define CONFIG_ESP_NOW_RATE                 WIFI_PHY_RATE_MCS0_LGI
#define CONFIG_FORCE_GAIN                   0

// 게인 baseline을 확정하기까지 표본할 프레임 수 (순정과 동일한 100).
// 33Hz 기준 약 3초. 이 구간의 리포트는 compensate_gain=1.0(미보상)으로 나간다.
#define GAIN_BASELINE_FRAMES                100

// 노드 송신 출력 (0.25 dBm 단위). 40 = 10 dBm.
// 비대칭 출력: 게이트웨이 21 dBm(무ARQ 브로드캐스트라 최대 출력 필요) /
// 노드 10 dBm(리포트는 유니캐스트+ARQ라 충분하고 전류 부담이 절반).
#define CONFIG_CSI_TX_POWER_QDBM            40

// HE20 SU의 CSI 길이. 256 서브캐리어 × (I,Q) int8 = 512 B.
// HT40 시절 384 B에서 늘어난 값 — 384로 두면 뒤쪽 서브캐리어가 잘려 나간다.
#define CSI_BUF_MAX                         512

// ==================== 무선 백홀 설정 ====================
// 1 = CSI 리포트를 ESP-NOW로 게이트웨이에 무선 전송 (기본, 데이터 케이블 불필요)
// 0 = 기존 USB-Serial/JTAG 바이너리 프레임 출력 (개별 Rx 유선 디버그용)
#define CONFIG_BACKHAUL_WIRELESS            1

// 이 기기의 Rx 번호. MAC 마지막 바이트(1e:00:00:00:00:XX)이자 슬롯 순서를 정한다.
// 포트 배치(2026-08-04): COM13=게이트웨이, COM14=RX1(0x01), COM15=RX2(0x02),
// COM16=RX3(0x03), COM17=RX4(0x04). 값을 바꿔가며 각각 빌드/플래시할 것.
// 플래시 후에는 소스를 0x01로 되돌려 두는 것이 규약.
#define CONFIG_CSI_RX_ID                    0x04

// 백홀 송신 슬롯 = 프로브 수신 시각 + SLOT_BASE + slot순서*SLOT_SPACING.
// 33Hz 주기 30ms 안에 4슬롯(2/5/8/11ms)이 들어가고, 다음 프로브와 겹치지 않는다.
#define SLOT_BASE_US                        2000
#define SLOT_SPACING_US                     3000

#if CONFIG_BACKHAUL_WIRELESS
// 리포트(meta 46B + CSI 512B = 558B)는 250B를 넘으므로 ESP-NOW v2가 필수 (IDF 5.4+).
// v2 한도 1470B의 38%라 단편화 없이 단일 프레임으로 나간다.
#ifndef ESP_NOW_MAX_DATA_LEN_V2
#error "ESP-NOW v2 (>=IDF 5.4) required: report payload exceeds 250 bytes"
#endif
#endif

// 큐 설정
// csi_data_t가 HE20에서 ~570B로 커졌다(HT40 시절 ~440B). C6는 SRAM이 512KB뿐이라
// HT40 시절의 400개(≈228KB)를 그대로 두면 Wi-Fi 힙과 충돌해 xQueueCreate가 실패한다.
// 33Hz 단일 스트림에 전용 태스크가 붙어 있으므로 64개(≈36KB)면 2초치 버퍼로 충분하다.
#define CSI_QUEUE_SIZE                      64      // 큐 크기 (버퍼 개수)
#define CSI_PRINT_TASK_STACK                8192    // 출력 태스크 스택 크기
#define CSI_PRINT_TASK_PRIORITY             5       // 출력 태스크 우선순위

// USB-Serial/JTAG 드라이버 TX 링버퍼.
// 다중 Tx 스트림 출력은 문자 단위 ROM putc(가득 차면 문자 유실)로는 감당이 안 되므로
// 인터럽트 구동 드라이버(가득 차면 대기 = 무손실)를 쓴다. 콘솔은 sdkconfig에서
// CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG=y 로 설정되어 있어야 한다.
#define USJ_TX_RING_SIZE                    32768

// CSI는 텍스트 CSV 대신 바이너리 프레임으로 내보낸다 (~437B/패킷, 텍스트의 약 1/4).
// USB-Serial/JTAG의 실질 처리량(~100KB/s)으로는 3Tx x 33Hz 텍스트(~150KB/s)가 안 들어가기 때문.
// 프레임: [0xA5][0x5A][ver=0x01][payload_len u16 LE][csi_bin_meta_t + csi bytes][xor u8]
// 디코딩은 tools/csi_sync_engine.py 의 CsiBinaryFramer — 구조 변경 시 반드시 함께 수정할 것.
#define CSI_BIN_MAGIC0                      0xA5
#define CSI_BIN_MAGIC1                      0x5A
#define CSI_BIN_VER                         0x01

// 대역폭/프로토콜 설정
// C6는 802.11ax가 20MHz 전용이다 → HE20이 상한이고, 대역폭은 반드시 HT20.
// 프로토콜에 11AX를 광고하지 않으면 HE 디코드 자체가 안 된다(captured=0).
// 하위 규격(11B/G/N)도 같이 켜 두는 것이 관례다 — 비콘/관리 프레임용.
#if CONFIG_IDF_TARGET_ESP32C5 || CONFIG_IDF_TARGET_ESP32C61 || (CONFIG_IDF_TARGET_ESP32C6 && ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(5, 4, 0))
#define CONFIG_WIFI_BAND_MODE               WIFI_BAND_MODE_2G_ONLY
#define CONFIG_WIFI_2G_BANDWIDTHS           WIFI_BW_HT20
#define CONFIG_WIFI_5G_BANDWIDTHS           WIFI_BW_HT20
#define CONFIG_WIFI_2G_PROTOCOL             (WIFI_PROTOCOL_11B | WIFI_PROTOCOL_11G | WIFI_PROTOCOL_11N | WIFI_PROTOCOL_11AX)
#define CONFIG_WIFI_5G_PROTOCOL             (WIFI_PROTOCOL_11A | WIFI_PROTOCOL_11N | WIFI_PROTOCOL_11AC | WIFI_PROTOCOL_11AX)
#else
#define CONFIG_WIFI_BANDWIDTH               WIFI_BW_HT20
#endif

#if CONFIG_IDF_TARGET_ESP32C5 || CONFIG_IDF_TARGET_ESP32C61
#define CSI_FORCE_LLTF                      0
#endif

#if ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(6, 0, 0)
#define ESP_IF_WIFI_STA ESP_MAC_WIFI_STA
#endif

// ==================== 전역 변수 ====================
// 게이트웨이(프로브 송신원이자 백홀 목적지) MAC — CSI 필터도 이 주소와
// 6바이트 정확 일치로 본다. prefix(5바이트) 비교를 쓰면 예전 4Tx 펌웨어가
// 남아 도는 기기(1a:..:03 등)의 프레임까지 잡혀 백홀이 오염된다 (실측 확인).
static const uint8_t CONFIG_GATEWAY_MAC[] = {0x1a, 0x00, 0x00, 0x00, 0x00, 0x01};
// 이 Rx의 MAC. prefix 1e — 백홀 프레임이 다른 Rx의 CSI 필터(1a)에 걸리지 않게 한다.
// 주의: 첫 바이트의 bit0(멀티캐스트 비트)이 1이면 esp_wifi_set_mac이 거부한다
// (0x1b 불가). 0x1e = 로컬 관리(bit1=1) + 유니캐스트(bit0=0).
static const uint8_t CONFIG_CSI_RX_MAC[] = {0x1e, 0x00, 0x00, 0x00, 0x00, CONFIG_CSI_RX_ID};
static const char *TAG = "csi_recv";
static uint8_t s_my_mac[6] = {0};

// CSI 데이터 저장 구조체
typedef struct {
    wifi_pkt_rx_ctrl_t rx_ctrl;
    uint8_t mac[6];
    int8_t buf[CSI_BUF_MAX];
    uint16_t len;
    uint8_t first_word_invalid;
    uint32_t rx_id;
    float compensate_gain;
    int64_t capture_us;          // 콜백 시각(esp_timer) — 백홀 슬롯 계산 기준
} csi_data_t;

// 바이너리 프레임 메타 (필드 순서·크기를 CsiBinaryFramer의 struct 포맷과 1:1로 맞춰야 함)
typedef struct __attribute__((packed)) {
    uint8_t  recv_mac[6];
    uint8_t  src_mac[6];
    uint32_t seq;                // Tx가 payload에 실어 보낸 카운터
    uint32_t timestamp;          // Rx 하드웨어 수신 타임스탬프 (us) — 동기화 기준
    int8_t   rssi;
    uint8_t  rate;
    uint8_t  sig_mode;
    uint8_t  mcs;
    uint8_t  cwb;
    uint8_t  smoothing;
    uint8_t  not_sounding;
    uint8_t  aggregation;
    uint8_t  stbc;
    uint8_t  fec_coding;
    uint8_t  sgi;
    int8_t   noise_floor;
    uint8_t  ampdu_cnt;
    uint8_t  channel;
    uint8_t  secondary_channel;
    uint8_t  ant;
    uint16_t sig_len;
    uint8_t  rx_state;
    uint8_t  first_word_invalid;
    float    compensate_gain;
    uint16_t csi_len;
} csi_bin_meta_t;

// 큐 핸들
static QueueHandle_t s_csi_queue = NULL;

// 통계
static volatile uint32_t s_total_received = 0;
static volatile uint32_t s_total_dropped = 0;
static volatile uint32_t s_backhaul_ok = 0;      // 게이트웨이 ACK 확인된 리포트
static volatile uint32_t s_backhaul_fail = 0;    // 송신 실패(ACK 미수신 포함)

#if CONFIG_BACKHAUL_WIRELESS
// Rx 번호 → 슬롯 오프셋. ID 1~4가 그대로 슬롯 0~3 (2/5/8/11 ms).
static uint32_t slot_offset_us(void)
{
    switch (CONFIG_CSI_RX_ID) {
    case 0x01: return SLOT_BASE_US + 0 * SLOT_SPACING_US;
    case 0x02: return SLOT_BASE_US + 1 * SLOT_SPACING_US;
    case 0x03: return SLOT_BASE_US + 2 * SLOT_SPACING_US;
    case 0x04: return SLOT_BASE_US + 3 * SLOT_SPACING_US;
    default:   return SLOT_BASE_US + 4 * SLOT_SPACING_US;   // 미지정 ID는 맨 뒤 슬롯
    }
}

static void espnow_send_cb(const esp_now_send_info_t *tx_info, esp_now_send_status_t status)
{
    if (status == ESP_NOW_SEND_SUCCESS) {
        s_backhaul_ok++;
    } else {
        s_backhaul_fail++;
    }
}
#endif

// ==================== Wi-Fi 초기화 ====================
static void wifi_init(void)
{
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    ESP_ERROR_CHECK(esp_netif_init());
    
    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));

#if CONFIG_IDF_TARGET_ESP32C5
    ESP_ERROR_CHECK(esp_wifi_start());
    esp_wifi_set_band_mode(CONFIG_WIFI_BAND_MODE);
    wifi_protocols_t protocols = {
        .ghz_2g = CONFIG_WIFI_2G_PROTOCOL,
        .ghz_5g = CONFIG_WIFI_5G_PROTOCOL
    };
    ESP_ERROR_CHECK(esp_wifi_set_protocols(ESP_IF_WIFI_STA, &protocols));
    wifi_bandwidths_t bandwidth = {
        .ghz_2g = CONFIG_WIFI_2G_BANDWIDTHS,
        .ghz_5g = CONFIG_WIFI_5G_BANDWIDTHS
    };
    ESP_ERROR_CHECK(esp_wifi_set_bandwidths(ESP_IF_WIFI_STA, &bandwidth));
#elif (CONFIG_IDF_TARGET_ESP32C6 && ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(5, 4, 0)) || CONFIG_IDF_TARGET_ESP32C61
    ESP_ERROR_CHECK(esp_wifi_start());
    esp_wifi_set_band_mode(CONFIG_WIFI_BAND_MODE);
    wifi_protocols_t protocols = {
        .ghz_2g = CONFIG_WIFI_2G_PROTOCOL,
    };
    ESP_ERROR_CHECK(esp_wifi_set_protocols(ESP_IF_WIFI_STA, &protocols));
    wifi_bandwidths_t bandwidth = {
        .ghz_2g = CONFIG_WIFI_2G_BANDWIDTHS,
    };
    ESP_ERROR_CHECK(esp_wifi_set_bandwidths(ESP_IF_WIFI_STA, &bandwidth));
#else
    ESP_ERROR_CHECK(esp_wifi_set_bandwidth(ESP_IF_WIFI_STA, CONFIG_WIFI_BANDWIDTH));
    ESP_ERROR_CHECK(esp_wifi_start());
#endif

    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
    
#if CONFIG_IDF_TARGET_ESP32C5
    if ((CONFIG_WIFI_BAND_MODE == WIFI_BAND_MODE_2G_ONLY && CONFIG_WIFI_2G_BANDWIDTHS == WIFI_BW_HT20)
            || (CONFIG_WIFI_BAND_MODE == WIFI_BAND_MODE_5G_ONLY && CONFIG_WIFI_5G_BANDWIDTHS == WIFI_BW_HT20)) {
        ESP_ERROR_CHECK(esp_wifi_set_channel(CONFIG_LESS_INTERFERENCE_CHANNEL, WIFI_SECOND_CHAN_NONE));
    } else {
        ESP_ERROR_CHECK(esp_wifi_set_channel(CONFIG_LESS_INTERFERENCE_CHANNEL, WIFI_SECOND_CHAN_BELOW));
    }
#elif (CONFIG_IDF_TARGET_ESP32C6 && ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(5, 4, 0)) || CONFIG_IDF_TARGET_ESP32C61
    if (CONFIG_WIFI_BAND_MODE == WIFI_BAND_MODE_2G_ONLY && CONFIG_WIFI_2G_BANDWIDTHS == WIFI_BW_HT20) {
        ESP_ERROR_CHECK(esp_wifi_set_channel(CONFIG_LESS_INTERFERENCE_CHANNEL, WIFI_SECOND_CHAN_NONE));
    } else {
        ESP_ERROR_CHECK(esp_wifi_set_channel(CONFIG_LESS_INTERFERENCE_CHANNEL, WIFI_SECOND_CHAN_BELOW));
    }
#else
    if (CONFIG_WIFI_BANDWIDTH == WIFI_BW_HT20) {
        ESP_ERROR_CHECK(esp_wifi_set_channel(CONFIG_LESS_INTERFERENCE_CHANNEL, WIFI_SECOND_CHAN_NONE));
    } else {
        ESP_ERROR_CHECK(esp_wifi_set_channel(CONFIG_LESS_INTERFERENCE_CHANNEL, WIFI_SECOND_CHAN_BELOW));
    }
#endif

    // 이 Rx의 정체성: 1e:00:00:00:00:{RX_ID} (recv_mac으로 PC에서 스트림 구분)
    ESP_ERROR_CHECK(esp_wifi_set_mac(WIFI_IF_STA, CONFIG_CSI_RX_MAC));

    // 노드 출력 10 dBm. 21 dBm로 올리면 일부 노드가 송신 실패한다(전원 버스트).
    ESP_ERROR_CHECK(esp_wifi_set_max_tx_power(CONFIG_CSI_TX_POWER_QDBM));
}

// ==================== ESP-NOW 초기화 ====================
static void wifi_esp_now_init(esp_now_peer_info_t peer)
{
    ESP_ERROR_CHECK(esp_now_init());
    ESP_ERROR_CHECK(esp_now_set_pmk((uint8_t *)"pmk1234567890123"));
    
    esp_now_rate_config_t rate_config = {
        .phymode = CONFIG_ESP_NOW_PHYMODE,
        .rate = CONFIG_ESP_NOW_RATE,
        .ersu = false,
        .dcm = false
    };
    
    ESP_ERROR_CHECK(esp_now_add_peer(&peer));
    ESP_ERROR_CHECK(esp_now_set_peer_rate_config(peer.peer_addr, &rate_config));
}

// ==================== CSI 콜백 (최소 작업만!) ====================
static void IRAM_ATTR wifi_csi_rx_cb(void *ctx, wifi_csi_info_t *info)
{
    if (!info || !info->buf) {
        return;
    }

    // MAC 필터링: 게이트웨이 프로브만 (6바이트 정확 일치)
    if (memcmp(info->mac, CONFIG_GATEWAY_MAC, 6)) {
        return;
    }

    s_total_received++;

    // CSI 데이터 복사
    csi_data_t csi_data;
    memcpy(&csi_data.rx_ctrl, &info->rx_ctrl, sizeof(wifi_pkt_rx_ctrl_t));
    memcpy(csi_data.mac, info->mac, 6);
    
    // CSI 버퍼 복사 (길이 제한). HE20 SU면 info->len == 512가 나와야 정상이다.
    uint16_t copy_len = (info->len > CSI_BUF_MAX) ? CSI_BUF_MAX : info->len;
    memcpy(csi_data.buf, info->buf, copy_len);
    csi_data.len = copy_len;
    csi_data.first_word_invalid = info->first_word_invalid;

    // 프로브가 payload에 실은 seq 카운터(4B). 오프셋 15는 ESP-NOW 프레임의
    // 실측값이다. 짧은/변형 프레임에서 버퍼 밖을 읽지 않도록 경계를 확인한다.
    if (info->payload && info->payload_len >= 15 + sizeof(uint32_t)) {
        memcpy(&csi_data.rx_id, info->payload + 15, sizeof(uint32_t));
    } else {
        csi_data.rx_id = 0;
    }
    csi_data.compensate_gain = 1.0f;
    csi_data.capture_us = esp_timer_get_time();

#if CONFIG_GAIN_CONTROL
    // Gain 보상 계수 계산 (CSI 값 자체는 원본 int8 그대로 보내고 PC에서 곱한다)
    //
    // 주의: 보상 계수는 baseline(초기 N프레임 게인의 중앙값)이 잡힌 뒤에만 나온다.
    // record_rx_gain() 단계를 빼먹으면 get_gain_compensation()이 계속
    // ESP_ERR_INVALID_STATE를 반환해서 보상이 영원히 1.0에 머문다 — 즉 게인 보상이
    // 조용히 꺼진 채로 수집된다. 순정 csi_recv의 관용구를 그대로 따른다.
    uint8_t agc_gain = 0;
    int8_t fft_gain = 0;
    esp_csi_gain_ctrl_get_rx_gain(&info->rx_ctrl, &agc_gain, &fft_gain);

    static uint32_t s_gain_count = 0;
    if (s_gain_count < GAIN_BASELINE_FRAMES) {
        esp_csi_gain_ctrl_record_rx_gain(agc_gain, fft_gain);
        s_gain_count++;
    } else if (s_gain_count == GAIN_BASELINE_FRAMES) {
        uint8_t agc_baseline = 0;
        int8_t fft_baseline = 0;
        esp_csi_gain_ctrl_get_rx_gain_baseline(&agc_baseline, &fft_baseline);
        s_gain_count++;      // 1회만 수행
#if CONFIG_FORCE_GAIN
        // v1의 게인 프리즈 — 포화 영역에서 표본되면 복조가 무너지므로 기본 비활성.
        esp_csi_gain_ctrl_set_rx_force_gain(agc_baseline, fft_baseline);
#endif
    }

    // baseline 확정 전에는 ESP_OK가 아니므로 1.0(보상 미적용)을 유지한다.
    // PC 파서는 gain == 1.0이면 곱셈을 건너뛰므로 이 값이 곧 "미보상" 표식이다.
    float comp = 1.0f;
    if (esp_csi_gain_ctrl_get_gain_compensation(&comp, agc_gain, fft_gain) == ESP_OK) {
        csi_data.compensate_gain = comp;
    }
#endif

    // 큐에 넣기 (블로킹 없이)
    BaseType_t ret = xQueueSendFromISR(s_csi_queue, &csi_data, NULL);
    if (ret != pdTRUE) {
        s_total_dropped++;
    }
}

// ==================== CSI 리포트 태스크 ====================
// 무선 모드: meta+CSI를 슬롯 시간에 ESP-NOW로 게이트웨이에 송신
//            (프레이밍/CRC/USB 출력은 게이트웨이가 담당).
// 유선 모드: 기존처럼 바이너리 프레임을 usb_serial_jtag 드라이버로 출력.
// CSI 값은 gain을 곱하지 않은 원본 int8로 보내고 compensate_gain을 함께 실어,
// PC(CsiBinaryFramer)가 기존 텍스트 포맷과 동일한 값으로 복원한다.
static uint8_t s_frame_buf[6 + sizeof(csi_bin_meta_t) + sizeof(((csi_data_t *)0)->buf)];

static void csi_print_task(void *arg)
{
    csi_data_t csi_data;
    static uint32_t s_print_count = 0;

#if CONFIG_BACKHAUL_WIRELESS
    ESP_LOGI(TAG, "CSI report task started (wireless backhaul, slot +%luus)",
             (unsigned long)slot_offset_us());
#else
    ESP_LOGI(TAG, "CSI print task started (binary frame output)");
#endif

    while (1) {
        // 큐에서 데이터 대기
        if (xQueueReceive(s_csi_queue, &csi_data, portMAX_DELAY) == pdTRUE) {
            const wifi_pkt_rx_ctrl_t *rx_ctrl = &csi_data.rx_ctrl;

            // 첫 캡처의 실측 길이를 1회만 보고한다. HE20 SU면 512여야 한다.
            // 384나 다른 값이면 PHY 협상이 HE20이 아니라는 뜻 → 여기서 멈출 것.
            if (s_print_count == 0) {
                ESP_LOGI(TAG, "first capture: csi_len=%u (expect 512), rssi=%d, fwi=%u",
                         (unsigned)csi_data.len, (int)rx_ctrl->rssi,
                         (unsigned)csi_data.first_word_invalid);
                if (csi_data.len != 512) {
                    ESP_LOGW(TAG, "csi_len != 512 — HE20 SU가 아닐 수 있다. "
                                  "246차원 계약이 깨진 상태로 수집하지 말 것");
                }
            }

            csi_bin_meta_t meta = {0};
            memcpy(meta.recv_mac, s_my_mac, 6);
            memcpy(meta.src_mac, csi_data.mac, 6);
            meta.seq                = csi_data.rx_id;
            meta.timestamp          = rx_ctrl->timestamp;
            meta.rssi               = rx_ctrl->rssi;
            meta.rate               = rx_ctrl->rate;
            meta.noise_floor        = rx_ctrl->noise_floor;
            meta.channel            = rx_ctrl->channel;
#if !(CONFIG_IDF_TARGET_ESP32C5 || CONFIG_IDF_TARGET_ESP32C6 || CONFIG_IDF_TARGET_ESP32C61)
            meta.sig_mode           = rx_ctrl->sig_mode;
            meta.mcs                = rx_ctrl->mcs;
            meta.cwb                = rx_ctrl->cwb;
            meta.smoothing          = rx_ctrl->smoothing;
            meta.not_sounding       = rx_ctrl->not_sounding;
            meta.aggregation        = rx_ctrl->aggregation;
            meta.stbc               = rx_ctrl->stbc;
            meta.fec_coding         = rx_ctrl->fec_coding;
            meta.sgi                = rx_ctrl->sgi;
            meta.ampdu_cnt          = rx_ctrl->ampdu_cnt;
            meta.secondary_channel  = rx_ctrl->secondary_channel;
            meta.ant                = rx_ctrl->ant;
#endif
            meta.sig_len            = rx_ctrl->sig_len;
            meta.rx_state           = rx_ctrl->rx_state;
            meta.first_word_invalid = csi_data.first_word_invalid;
            meta.compensate_gain    = csi_data.compensate_gain;
            meta.csi_len            = csi_data.len;

#if CONFIG_BACKHAUL_WIRELESS
            // 리포트 = meta + CSI 원시 바이트 (매직/길이/CRC 프레이밍은 게이트웨이 몫)
            memcpy(s_frame_buf, &meta, sizeof(meta));
            memcpy(s_frame_buf + sizeof(meta), csi_data.buf, csi_data.len);

            // TDMA-lite: 프로브 수신 시각 + 자기 슬롯 오프셋까지 대기 후 송신.
            // (4대가 같은 프로브에 동시에 응답하는 충돌을 시간 분할로 회피)
            int64_t wait_us = csi_data.capture_us + slot_offset_us() - esp_timer_get_time();
            if (wait_us > 0) {
                vTaskDelay(pdMS_TO_TICKS((wait_us + 999) / 1000));
            }
            if (esp_now_send(CONFIG_GATEWAY_MAC, s_frame_buf,
                             sizeof(meta) + csi_data.len) != ESP_OK) {
                s_backhaul_fail++;
            }
#else
            uint16_t plen = sizeof(meta) + csi_data.len;
            s_frame_buf[0] = CSI_BIN_MAGIC0;
            s_frame_buf[1] = CSI_BIN_MAGIC1;
            s_frame_buf[2] = CSI_BIN_VER;
            s_frame_buf[3] = plen & 0xFF;
            s_frame_buf[4] = plen >> 8;
            memcpy(s_frame_buf + 5, &meta, sizeof(meta));
            memcpy(s_frame_buf + 5 + sizeof(meta), csi_data.buf, csi_data.len);

            uint8_t crc = 0;
            for (int i = 0; i < plen; i++) {
                crc ^= s_frame_buf[5 + i];
            }
            s_frame_buf[5 + plen] = crc;

            // 프레임 통째로 출력 (드라이버 링버퍼가 차면 대기 — 바이트 유실 없음).
            // stdout(fwrite)은 개행 변환(\n → \r\n)으로 바이너리를 오염시키므로
            // 반드시 드라이버에 직접 쓴다.
#if SOC_USB_SERIAL_JTAG_SUPPORTED
            usb_serial_jtag_write_bytes(s_frame_buf, 5 + plen + 1, portMAX_DELAY);
#else
            fwrite(s_frame_buf, 1, 5 + plen + 1, stdout);
#endif
#endif

            s_print_count++;

            // 1000개마다 통계 출력
            if (s_print_count % 1000 == 0) {
                ESP_LOGI(TAG, "Stats: printed=%lu, received=%lu, dropped=%lu, bh_ok=%lu, bh_fail=%lu, queue=%d/%d",
                         (unsigned long)s_print_count,
                         (unsigned long)s_total_received,
                         (unsigned long)s_total_dropped,
                         (unsigned long)s_backhaul_ok,
                         (unsigned long)s_backhaul_fail,
                         (int)uxQueueMessagesWaiting(s_csi_queue),
                         CSI_QUEUE_SIZE);
            }
        }
    }
}

// ==================== CSI 초기화 ====================
static void wifi_csi_init(void)
{
    ESP_ERROR_CHECK(esp_wifi_set_promiscuous(true));

#if CONFIG_IDF_TARGET_ESP32C5 || CONFIG_IDF_TARGET_ESP32C61
    wifi_csi_config_t csi_config = {
        .enable                   = true,
        .acquire_csi_legacy       = false,
        .acquire_csi_force_lltf   = CSI_FORCE_LLTF,
        .acquire_csi_ht20         = true,
        .acquire_csi_ht40         = true,
        .acquire_csi_vht          = false,
        .acquire_csi_su           = false,
        .acquire_csi_mu           = false,
        .acquire_csi_dcm          = false,
        .acquire_csi_beamformed   = false,
        .acquire_csi_he_stbc_mode = 2,
        .val_scale_cfg            = 0,
        .dump_ack_en              = false,
        .reserved                 = false
    };
#elif CONFIG_IDF_TARGET_ESP32C6
    // C6는 SOC_WIFI_MAC_VERSION_NUM==2 브랜치의 구조체를 쓴다
    // (lltf_bit_mode 없음, val_scale_cfg는 2비트=0~3).
    // HE20 SU 전용 — 다른 PPDU 종류를 켜면 info->len이 프레임마다 달라져
    // 256서브캐리어/246차원 계약이 깨진다. 상단 "CSI 포맷 계약" 참조.
    wifi_csi_config_t csi_config = {
        .enable                 = true,
        .acquire_csi_legacy     = false,   // L-LTF 끔 (위상 사용 불가 + 프레임만 길어짐)
        .acquire_csi_ht20       = false,
        .acquire_csi_ht40       = false,
        .acquire_csi_su         = true,    // ← 유일하게 켜는 것
        .acquire_csi_mu         = false,
        .acquire_csi_dcm        = false,
        .acquire_csi_beamformed = false,
        .acquire_csi_he_stbc    = 2,
        .val_scale_cfg          = 0,
        .dump_ack_en            = false,
        .reserved               = false
    };
#else
    wifi_csi_config_t csi_config = {
        .lltf_en           = true,
        .htltf_en          = true,
        .stbc_htltf2_en    = true,
        .ltf_merge_en      = true,
        .channel_filter_en = true,
        .manu_scale        = false,
        .shift             = false,
    };
#endif

    ESP_ERROR_CHECK(esp_wifi_set_csi_config(&csi_config));
    ESP_ERROR_CHECK(esp_wifi_set_csi_rx_cb(wifi_csi_rx_cb, NULL));
    ESP_ERROR_CHECK(esp_wifi_set_csi(true));
}

// ==================== 메인 ====================
void app_main(void)
{
    // NVS 초기화
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

#if SOC_USB_SERIAL_JTAG_SUPPORTED
    // USB-Serial/JTAG 드라이버 설치: 인터럽트 구동 + 대용량 TX 링버퍼.
    // stdout이 ROM putc(가득 차면 문자 유실) 대신 드라이버(가득 차면 대기)를 쓰게 한다.
    usb_serial_jtag_driver_config_t usj_cfg = {
        .tx_buffer_size = USJ_TX_RING_SIZE,
        .rx_buffer_size = 256,
    };
    ESP_ERROR_CHECK(usb_serial_jtag_driver_install(&usj_cfg));
    usb_serial_jtag_vfs_use_driver();
    setvbuf(stdout, NULL, _IONBF, 0);   // stdio 중간 버퍼 제거 → fwrite가 곧장 드라이버로
#endif

    // Wi-Fi 초기화
    wifi_init();

    // ESP-NOW 초기화
    esp_now_peer_info_t peer = {
        .channel   = CONFIG_LESS_INTERFERENCE_CHANNEL,
        .ifidx     = WIFI_IF_STA,
        .encrypt   = false,
        .peer_addr = {0xff, 0xff, 0xff, 0xff, 0xff, 0xff},
    };
    wifi_esp_now_init(peer);

#if CONFIG_BACKHAUL_WIRELESS
    // 백홀 유니캐스트 대상(게이트웨이) peer + 전송률 고정 + 전송 결과 통계
    esp_now_peer_info_t gw_peer = {
        .channel   = CONFIG_LESS_INTERFERENCE_CHANNEL,
        .ifidx     = WIFI_IF_STA,
        .encrypt   = false,
    };
    memcpy(gw_peer.peer_addr, CONFIG_GATEWAY_MAC, 6);
    ESP_ERROR_CHECK(esp_now_add_peer(&gw_peer));
    esp_now_rate_config_t gw_rate = {
        .phymode = CONFIG_ESP_NOW_PHYMODE,
        .rate    = CONFIG_ESP_NOW_RATE,
        .ersu    = false,
        .dcm     = false
    };
    ESP_ERROR_CHECK(esp_now_set_peer_rate_config(gw_peer.peer_addr, &gw_rate));
    ESP_ERROR_CHECK(esp_now_register_send_cb(espnow_send_cb));
#endif

    // MAC 주소 가져오기
    esp_wifi_get_mac(WIFI_IF_STA, s_my_mac);
    ESP_LOGI(TAG, "Receiver MAC: " MACSTR, MAC2STR(s_my_mac));

    // 포맷 계약 자기보고 — 이 줄이 기대와 다르면 그 자리에서 멈추고 원인을 찾을 것.
    uint32_t now_ver = 0;
    esp_now_get_version(&now_ver);
    int8_t cur_power = 0;
    esp_wifi_get_max_tx_power(&cur_power);
    ESP_LOGI(TAG, "rx_id=0x%02x slot=+%luus | HE20 SU-only, ch%d, csi_buf=%dB | "
                  "esp-now v%lu | tx_power=%d qdBm",
             CONFIG_CSI_RX_ID,
#if CONFIG_BACKHAUL_WIRELESS
             (unsigned long)slot_offset_us(),
#else
             0UL,
#endif
             CONFIG_LESS_INTERFERENCE_CHANNEL, CSI_BUF_MAX,
             (unsigned long)now_ver, (int)cur_power);
    if (now_ver < 2) {
        ESP_LOGE(TAG, "ESP-NOW v%lu — 558B 리포트를 보낼 수 없다 (v2 필요)",
                 (unsigned long)now_ver);
    }

    // CSI 큐 생성
    s_csi_queue = xQueueCreate(CSI_QUEUE_SIZE, sizeof(csi_data_t));
    if (s_csi_queue == NULL) {
        ESP_LOGE(TAG, "Failed to create CSI queue!");
        return;
    }
    ESP_LOGI(TAG, "CSI queue created (size: %d)", CSI_QUEUE_SIZE);

    // 출력 태스크 생성.
    // 주의: S3 시절의 xTaskCreatePinnedToCore(..., 1)은 C6(싱글코어)에서
    // FreeRTOS assert → 부팅 루프를 일으킨다 (실측). 코어 고정 없이 생성한다.
    BaseType_t task_ret = xTaskCreate(
        csi_print_task,
        "csi_print",
        CSI_PRINT_TASK_STACK,
        NULL,
        CSI_PRINT_TASK_PRIORITY,
        NULL
    );
    
    if (task_ret != pdPASS) {
        ESP_LOGE(TAG, "Failed to create CSI print task!");
        return;
    }

    // CSI 수신 시작
    wifi_csi_init();

    ESP_LOGI(TAG, "CSI receiver initialized. Waiting for data...");
}
