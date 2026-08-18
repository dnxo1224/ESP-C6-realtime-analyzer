/*
 * SPDX-FileCopyrightText: 2025-2026 Espressif Systems (Shanghai) CO LTD
 *
 * SPDX-License-Identifier: Apache-2.0
 */
/* WIRELESS-BACKHAUL Gateway (Tx + Collector) — ESP32-C6 / HE20
 *
 * PHY 계약: 2.4GHz ch11, HE20(802.11ax SU), MCS0 LGI, 게이트웨이 21 dBm.
 * 이 값들은 Rx(csi_recv)의 "CSI 포맷 계약"과 짝을 이룬다. 한쪽만 바꾸면
 * 캡처가 0이 되거나 서브캐리어 수가 달라져 246차원 데이터셋과 어긋난다.
 *
 * 1Tx-4Rx convergecast 구조의 중앙 노드. 두 역할을 동시에 수행한다:
 *   1. 프로브 송신: 33Hz 고정 주기로 4바이트 카운터(seq)를 ESP-NOW 브로드캐스트.
 *      구석의 Rx들이 이 프레임에서 CSI를 캡처한다.
 *   2. 백홀 수집: Rx들이 슬롯 시간에 유니캐스트로 되쏘는 CSI 리포트
 *      (csi_bin_meta_t + CSI 원시 바이트)를 수신해, 기존 csi_recv와 동일한
 *      바이너리 프레임([0xA5][0x5A][ver][len u16][payload][xor])으로 감싸
 *      USB-Serial/JTAG로 노트북에 출력한다.
 *
 * 페이로드는 Rx가 만든 것을 파싱하지 않고 길이 검증만 해서 그대로 통과시킨다.
 * PC측 디코딩: tools/csi_sync_engine.py 의 CsiBinaryFramer (무수정 재사용).
 *
 * MAC 규약: 게이트웨이 = 1a:00:00:00:00:01 (Rx의 CSI 필터 대상),
 *           Rx = 1e:00:00:00:00:{01,02,03,04} (백홀 필터 prefix 1e).
 * 포트 배치(2026-08-04): COM13=게이트웨이, COM14~17=RX1~4.
 */
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <unistd.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "driver/uart.h"

#include "nvs_flash.h"

#include "esp_mac.h"
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

#define CONFIG_LESS_INTERFERENCE_CHANNEL   11

// 대역폭/프로토콜 — Rx(csi_recv)와 반드시 동일해야 한다.
// C6의 802.11ax는 20MHz 전용이므로 HE20 + HT20 대역폭이 상한이다.
// 프로토콜에 11AX를 광고하지 않으면 프로브가 HE20으로 나가지 않는다.
#if CONFIG_IDF_TARGET_ESP32C5 || CONFIG_IDF_TARGET_ESP32C61 || (CONFIG_IDF_TARGET_ESP32C6 && ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(5, 4, 0))
#define CONFIG_WIFI_BAND_MODE   WIFI_BAND_MODE_2G_ONLY
#define CONFIG_WIFI_2G_BANDWIDTHS           WIFI_BW_HT20
#define CONFIG_WIFI_5G_BANDWIDTHS           WIFI_BW_HT20
#define CONFIG_WIFI_2G_PROTOCOL             (WIFI_PROTOCOL_11B | WIFI_PROTOCOL_11G | WIFI_PROTOCOL_11N | WIFI_PROTOCOL_11AX)
#define CONFIG_WIFI_5G_PROTOCOL             (WIFI_PROTOCOL_11A | WIFI_PROTOCOL_11N | WIFI_PROTOCOL_11AC | WIFI_PROTOCOL_11AX)
#else
#define CONFIG_WIFI_BANDWIDTH           WIFI_BW_HT20
#endif

#define CONFIG_ESP_NOW_PHYMODE           WIFI_PHY_MODE_HE20
#define CONFIG_ESP_NOW_RATE             WIFI_PHY_RATE_MCS0_LGI
#define CONFIG_SEND_FREQUENCY               33

// 게이트웨이 출력 (0.25 dBm 단위). 84 = 21 dBm.
// 프로브는 브로드캐스트라 MAC 재전송이 없다 → 캡처 확률을 출력으로 벌어야 한다.
// 단 근접 배치에서 노드 RSSI가 -30을 넘으면 포화로 오히려 캡처가 무너지므로,
// 배치 후 노드 RSSI를 -40~-70에 넣는 값으로 재조정할 것.
#define CONFIG_CSI_TX_POWER_QDBM            84

#if ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(6, 0, 0)
#define ESP_IF_WIFI_STA ESP_MAC_WIFI_STA
#endif

// Rx 리포트(백홀)는 250B를 넘으므로 ESP-NOW v2가 필수다 (IDF 5.4+).
#ifndef ESP_NOW_MAX_DATA_LEN_V2
#error "ESP-NOW v2 (>=IDF 5.4) required: report payload exceeds 250 bytes"
#endif

// 게이트웨이 MAC. Rx들의 CSI 필터가 이 주소(prefix 1a:00:00:00:00)를 본다.
static const uint8_t CONFIG_CSI_SEND_MAC[] = {0x1a, 0x00, 0x00, 0x00, 0x00, 0x01};
// 백홀 리포트를 보내오는 Rx MAC prefix (앞 5바이트 비교).
// 0x1e = 로컬 관리 + 유니캐스트 (0x1b은 멀티캐스트 비트 때문에 set_mac 불가)
static const uint8_t CONFIG_CSI_RX_MAC_PREFIX[] = {0x1e, 0x00, 0x00, 0x00, 0x00, 0x00};
static const char *TAG = "csi_gw";

// ==================== 바이너리 프레임 (csi_recv와 동일 포맷) ====================
#define CSI_BIN_MAGIC0                      0xA5
#define CSI_BIN_MAGIC1                      0x5A
#define CSI_BIN_VER                         0x01
#define USJ_TX_RING_SIZE                    32768
#define RELAY_UART_NUM                      UART_NUM_1
#define RELAY_UART_TX_GPIO                  3
#define RELAY_UART_BAUD                     921600
#define RELAY_UART_RX_RING_SIZE             256
#define RELAY_UART_TX_RING_SIZE             32768

// 리포트 = csi_bin_meta_t(46B) + CSI 원시 바이트(HE20 SU = 512B) = 558B.
// csi_recv의 CSI_BUF_MAX와 반드시 같은 값이어야 한다 — 작으면 게이트웨이가
// 정상 리포트를 "too long"으로 버려서 s_reports_bad만 올라간다.
#define REPORT_META_LEN                     46
#define REPORT_CSI_MAX                      512
#define REPORT_MAX_LEN                      (REPORT_META_LEN + REPORT_CSI_MAX)

typedef struct {
    uint16_t len;
    uint8_t  data[REPORT_MAX_LEN];
} report_t;

// report_t가 560B로 커졌다. C6 SRAM 512KB 기준 128개(≈72KB)는 부담이므로
// 64개(≈36KB)로 낮춘다 — 4노드 × 33Hz에 대해 약 0.5초치 버퍼.
#define REPORT_QUEUE_SIZE                   64
#define REPORT_TASK_STACK                   4096
#define REPORT_TASK_PRIORITY                5

static QueueHandle_t s_report_queue = NULL;
static volatile uint32_t s_reports_received = 0;
static volatile uint32_t s_reports_dropped = 0;
static volatile uint32_t s_reports_bad = 0;
static volatile uint32_t s_probes_sent = 0;
static volatile uint32_t s_usb_frames_dropped = 0;

// ==================== Wi-Fi 초기화 ====================
static void wifi_init()
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
    ESP_ERROR_CHECK(esp_wifi_set_mac(WIFI_IF_STA, CONFIG_CSI_SEND_MAC));
    ESP_ERROR_CHECK(esp_wifi_set_max_tx_power(CONFIG_CSI_TX_POWER_QDBM));
}

static void wifi_esp_now_init(esp_now_peer_info_t peer)
{
    ESP_ERROR_CHECK(esp_now_init());
    ESP_ERROR_CHECK(esp_now_set_pmk((uint8_t *)"pmk1234567890123"));
    ESP_ERROR_CHECK(esp_now_add_peer(&peer));
    esp_now_rate_config_t rate_config = {
        .phymode = CONFIG_ESP_NOW_PHYMODE,
        .rate = CONFIG_ESP_NOW_RATE,
        .ersu = false,
        .dcm = false
    };
    ESP_ERROR_CHECK(esp_now_set_peer_rate_config(peer.peer_addr, &rate_config));
}

// ==================== 백홀 수신 콜백 (Wi-Fi 태스크 컨텍스트 — 복사만) ====================
static void espnow_recv_cb(const esp_now_recv_info_t *info, const uint8_t *data, int len)
{
    if (!info || !info->src_addr || !data) {
        return;
    }
    // Rx(1e:00:00:00:00:xx)가 보낸 리포트만 수집
    if (memcmp(info->src_addr, CONFIG_CSI_RX_MAC_PREFIX, 5)) {
        return;
    }
    if (len < REPORT_META_LEN || len > REPORT_MAX_LEN) {
        s_reports_bad++;
        return;
    }

    report_t report;
    report.len = (uint16_t)len;
    memcpy(report.data, data, len);

    s_reports_received++;
    if (xQueueSend(s_report_queue, &report, 0) != pdTRUE) {
        s_reports_dropped++;
    }
}

// ==================== USB 출력 태스크 ====================
// 리포트를 바이너리 프레임으로 감싸 USB-Serial/JTAG 드라이버로 내보낸다.
// 로그는 이 태스크에서만 찍는다 — 다른 태스크의 로그가 프레임 쓰기 사이에
// 끼어들면 바이너리가 오염된다 (초기화 로그는 스트림 시작 전이라 무해).
static uint8_t s_frame_buf[5 + REPORT_MAX_LEN + 1];

static void report_output_task(void *arg)
{
    report_t report;
    uint32_t out_count = 0;

    ESP_LOGI(TAG, "report output task started (binary frame output)");

    while (1) {
        if (xQueueReceive(s_report_queue, &report, portMAX_DELAY) != pdTRUE) {
            continue;
        }

        s_frame_buf[0] = CSI_BIN_MAGIC0;
        s_frame_buf[1] = CSI_BIN_MAGIC1;
        s_frame_buf[2] = CSI_BIN_VER;
        s_frame_buf[3] = report.len & 0xFF;
        s_frame_buf[4] = report.len >> 8;
        memcpy(s_frame_buf + 5, report.data, report.len);

        uint8_t crc = 0;
        for (int i = 0; i < report.len; i++) {
            crc ^= report.data[i];
        }
        s_frame_buf[5 + report.len] = crc;

        const size_t frame_len = 5 + report.len + 1;

        // UART is the production path to the S3 relay, so enqueue it first and
        // never let the optional USB diagnostic mirror apply backpressure to it.
        uart_write_bytes(RELAY_UART_NUM, s_frame_buf, frame_len);

#if SOC_USB_SERIAL_JTAG_SUPPORTED
        // With no COM monitor attached the USB TX ring eventually fills. A
        // blocking write here used to freeze this task and therefore UART too.
        // xRingbufferSend is all-or-nothing, so a zero-timeout call either
        // mirrors the complete frame or drops it without emitting a fragment.
        if (usb_serial_jtag_write_bytes(s_frame_buf, frame_len, 0) != (int)frame_len) {
            s_usb_frames_dropped++;
        }
#else
        fwrite(s_frame_buf, 1, frame_len, stdout);
#endif

        if (++out_count % 4096 == 0) {
            ESP_LOGI(TAG, "Stats: probes=%lu, reports rx=%lu out=%lu dropped=%lu bad=%lu usb_drop=%lu, queue=%d/%d",
                     (unsigned long)s_probes_sent,
                     (unsigned long)s_reports_received,
                     (unsigned long)out_count,
                     (unsigned long)s_reports_dropped,
                     (unsigned long)s_reports_bad,
                     (unsigned long)s_usb_frames_dropped,
                     (int)uxQueueMessagesWaiting(s_report_queue),
                     REPORT_QUEUE_SIZE);
        }
    }
}

void app_main()
{
    /**
     * @brief Initialize NVS
     */
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    // LILYGO T7-C6: exposed GPIO3(UART1 TX) -> 470~499 ohm -> ESP32-S3 GPIO1(UART1 RX).
    uart_config_t relay_uart_cfg = {
        .baud_rate = RELAY_UART_BAUD,
        .data_bits = UART_DATA_8_BITS,
        .parity = UART_PARITY_DISABLE,
        .stop_bits = UART_STOP_BITS_1,
        .flow_ctrl = UART_HW_FLOWCTRL_DISABLE,
        .source_clk = UART_SCLK_DEFAULT,
    };
    // ESP-IDF 5.5 validates that the RX ring is larger than the UART HW FIFO
    // even when this link is TX-only. Keep a minimal valid RX ring; RX remains
    // disconnected through UART_PIN_NO_CHANGE below.
    ESP_ERROR_CHECK(uart_driver_install(RELAY_UART_NUM, RELAY_UART_RX_RING_SIZE,
                                        RELAY_UART_TX_RING_SIZE, 0, NULL, 0));
    ESP_ERROR_CHECK(uart_param_config(RELAY_UART_NUM, &relay_uart_cfg));
    ESP_ERROR_CHECK(uart_set_pin(RELAY_UART_NUM, RELAY_UART_TX_GPIO, UART_PIN_NO_CHANGE,
                                 UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE));

#if SOC_USB_SERIAL_JTAG_SUPPORTED
    // USB-Serial/JTAG 드라이버 설치: 인터럽트 구동 + 대용량 TX 링버퍼.
    // (가득 차면 대기 = 프레임 바이트 무손실. csi_recv에서 검증된 구성)
    usb_serial_jtag_driver_config_t usj_cfg = {
        .tx_buffer_size = USJ_TX_RING_SIZE,
        .rx_buffer_size = 256,
    };
    ESP_ERROR_CHECK(usb_serial_jtag_driver_install(&usj_cfg));
    usb_serial_jtag_vfs_use_driver();
    setvbuf(stdout, NULL, _IONBF, 0);
#endif

    /**
     * @brief Initialize Wi-Fi
     */
    wifi_init();

    /**
     * @brief Initialize ESP-NOW
     */
    esp_now_peer_info_t peer = {
        .channel   = CONFIG_LESS_INTERFERENCE_CHANNEL,
        .ifidx     = WIFI_IF_STA,
        .encrypt   = false,
        .peer_addr = {0xff, 0xff, 0xff, 0xff, 0xff, 0xff},
    };
    wifi_esp_now_init(peer);
    ESP_ERROR_CHECK(esp_now_register_recv_cb(espnow_recv_cb));

    s_report_queue = xQueueCreate(REPORT_QUEUE_SIZE, sizeof(report_t));
    if (s_report_queue == NULL) {
        ESP_LOGE(TAG, "Failed to create report queue!");
        return;
    }

    // 주의: S3 시절의 xTaskCreatePinnedToCore(..., 1)은 C6(싱글코어)에서
    // FreeRTOS assert → 부팅 루프를 일으킨다 (실측). 코어 고정 없이 생성한다.
    BaseType_t task_ret = xTaskCreate(
        report_output_task,
        "report_out",
        REPORT_TASK_STACK,
        NULL,
        REPORT_TASK_PRIORITY,
        NULL
    );
    if (task_ret != pdPASS) {
        ESP_LOGE(TAG, "Failed to create report output task!");
        return;
    }

    ESP_LOGI(TAG, "================ CSI GATEWAY (Tx + Collector) ================");
    ESP_LOGI(TAG, "wifi_channel: %d, send_frequency: %d, mac: " MACSTR,
             CONFIG_LESS_INTERFERENCE_CHANNEL, CONFIG_SEND_FREQUENCY, MAC2STR(CONFIG_CSI_SEND_MAC));
    ESP_LOGI(TAG, "relay uart: GPIO%d -> S3 GPIO1, %d baud 8N1", RELAY_UART_TX_GPIO, RELAY_UART_BAUD);

    // 포맷 계약 자기보고 — Rx의 부팅 로그와 대조할 것.
    uint32_t now_ver = 0;
    esp_now_get_version(&now_ver);
    int8_t cur_power = 0;
    esp_wifi_get_max_tx_power(&cur_power);
    ESP_LOGI(TAG, "probe=HE20 MCS0_LGI | report_max=%dB (meta %d + csi %d) | "
                  "esp-now v%lu | tx_power=%d qdBm",
             REPORT_MAX_LEN, REPORT_META_LEN, REPORT_CSI_MAX,
             (unsigned long)now_ver, (int)cur_power);
    if (now_ver < 2) {
        ESP_LOGE(TAG, "ESP-NOW v%lu — 558B 리포트를 받을 수 없다 (v2 필요)",
                 (unsigned long)now_ver);
    }

    // 프로브 루프: vTaskDelayUntil로 드리프트 없는 고정 주기.
    // (다중 Tx 시절의 위상 지터는 Tx가 1대뿐이므로 제거 — Rx 슬롯 계산이 깨끗해진다)
    TickType_t last_wake = xTaskGetTickCount();
    const TickType_t period = pdMS_TO_TICKS(1000 / CONFIG_SEND_FREQUENCY);
    ESP_LOGI(TAG, "probe period=%u ticks, tick=%ums (rate=%uHz)",
             (unsigned)period, (unsigned)portTICK_PERIOD_MS, (unsigned)configTICK_RATE_HZ);

    for (uint32_t count = 0; ; ++count) {
        esp_now_send(peer.peer_addr, (const uint8_t *)&count, sizeof(count));
        s_probes_sent++;
        vTaskDelayUntil(&last_wake, period);
    }
}
