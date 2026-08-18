/*
 * SPDX-FileCopyrightText: 2025-2026 Espressif Systems (Shanghai) CO LTD
 *
 * SPDX-License-Identifier: Apache-2.0
 */
/* Convergecast Relay (Data Forwarder) — ESP32-S3
 *
 * Tx(게이트웨이 C6, csi_send)가 UART1로 내보내는 바이너리 프레임 스트림
 * ([0xA5][0x5A][ver][len u16 LE][meta 46B + CSI][xor])을 수신·검증하고,
 * Wi-Fi(STA)로 인터넷에 접속해 서버로 포워딩한다.
 *
 * 역할 분리 원칙: Tx는 CSI 채널(ch11)에 고정되어 33Hz 프로브 격자를 지키고,
 * AP 접속(채널 스위칭·DHCP·비콘)이 필요한 인터넷 역할은 이 보드가 전담한다.
 * UART는 Tx→Relay 단방향 데이터 흐름 — 서버가 밀려도 Tx로 역압되지 않는다.
 *
 * 배선(2026-08-17): T7-C6 GPIO3(UART1 TX) → 470~499Ω → S3 GPIO1(UART1 RX), GND 공통.
 *       단방향 전용이며 다른 GPIO/전원 레일은 연결하지 않는다.
 *       S3의 TX/RX 라벨 핀(43/44)은 온보드 브리지 공유라 사용 금지.
 *
 * 포워딩: SERVER_HOST가 비어 있으면 통계만 출력한다(현 단계).
 * 서버 주소/포트가 정해지면 SERVER_HOST/PORT만 채워 재빌드 — TCP로
 * 프레임 스트림을 그대로 중계하고, 끊기면 백오프 후 재접속한다.
 * 주의: S3는 2.4GHz 전용 — AP 채널이 CSI ch11과 겹치지 않게 할 것.
 */
#include <stdio.h>
#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"
#include "freertos/queue.h"

#include "nvs_flash.h"
#include "esp_log.h"
#include "esp_wifi.h"
#include "esp_netif.h"
#include "esp_event.h"
#include "esp_timer.h"
#include "driver/uart.h"
#include "driver/gpio.h"
#include "esp_rom_sys.h"
#include "lwip/sockets.h"
#include "lwip/netdb.h"

// ==================== 설정 ====================
#define WIFI_SSID                   CONFIG_C6_RELAY_WIFI_SSID
#define WIFI_PASS                   CONFIG_C6_RELAY_WIFI_PASSWORD

// 주의: 보드의 TX(43)/RX(44) 라벨 핀은 온보드 USB-UART 브리지와 공유라
// 외부 UART 입력으로 쓸 수 없다 (브리지 TX가 라인을 상시 점유 — 실측 확인).
#define RELAY_UART_NUM              UART_NUM_1
#define RELAY_UART_RX_GPIO          1
#define RELAY_UART_TX_GPIO          5
#define RELAY_UART_BAUD             921600
// 백홀 유입 ~60KB/s. Wi-Fi 순단(재접속 수 초) 동안의 완충용으로 크게 잡는다.
#define RELAY_UART_RXBUF            (24 * 1024)

// 서버 포워딩 대상. 비어 있으면 수신·검증·통계만 수행한다.
// 현재: 노트북(csi_ingest_server.py)으로 테스트 — 실서버 확정 시 IP만 교체.
#define SERVER_HOST                 CONFIG_C6_RELAY_SERVER_HOST
#define SERVER_PORT                 CONFIG_C6_RELAY_SERVER_PORT
// 접속 토큰 — ingest가 --token으로 검증. 비워두면 미전송(무인증 로컬).
// 클라우드 배포 시 서버측 --token 값과 반드시 일치시킬 것.
#define SERVER_TOKEN                CONFIG_C6_RELAY_TOKEN

// 프레임 포맷 (csi_send/csi_recv와 동일 — 변경 시 반드시 함께 수정)
#define CSI_BIN_MAGIC0              0xA5
#define CSI_BIN_MAGIC1              0x5A
#define CSI_BIN_VER                 0x01
#define REPORT_META_LEN             46
#define REPORT_CSI_MAX              512
#define FRAME_MAX                   (5 + REPORT_META_LEN + REPORT_CSI_MAX + 1)

#define STATS_PERIOD_MS             5000

static const char *TAG = "csi_relay";

// ==================== Wi-Fi (STA) ====================
static EventGroupHandle_t s_wifi_events;
#define WIFI_CONNECTED_BIT          BIT0
static volatile uint32_t s_wifi_disconnects = 0;

static void wifi_event_handler(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        // 첫 접속은 app_main의 진단 스캔 후에 시작한다
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        wifi_event_sta_disconnected_t *d = (wifi_event_sta_disconnected_t *)data;
        // reason 대표값: 201=AP 못 찾음(SSID/대역), 15=4way 타임아웃(비밀번호), 2=AUTH_EXPIRE
        ESP_LOGW(TAG, "wifi disconnected: reason=%d rssi=%d", d->reason, d->rssi);
        xEventGroupClearBits(s_wifi_events, WIFI_CONNECTED_BIT);
        s_wifi_disconnects++;
        // 무한 재시도 — Relay는 사람이 없는 곳에서 스스로 복구해야 한다.
        vTaskDelay(pdMS_TO_TICKS(1000));
        esp_wifi_connect();
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *ev = (ip_event_got_ip_t *)data;
        ESP_LOGI(TAG, "got ip: " IPSTR, IP2STR(&ev->ip_info.ip));
        xEventGroupSetBits(s_wifi_events, WIFI_CONNECTED_BIT);
    }
}

static void wifi_init_sta(void)
{
    s_wifi_events = xEventGroupCreate();
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, wifi_event_handler, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, wifi_event_handler, NULL));

    wifi_config_t wifi_config = {
        .sta = {
            .ssid = WIFI_SSID,
            .password = WIFI_PASS,
            .threshold.authmode = WIFI_AUTH_WPA2_PSK,
            // WPA3(H2E) AP까지 커버 — WPA2 AP에는 영향 없음
            .sae_pwe_h2e = WPA3_SAE_PWE_BOTH,
        },
    };
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_config));
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_LOGI(TAG, "wifi sta start: ssid=%s", WIFI_SSID);

    // 진단용 1회 스캔: 2.4GHz에서 실제로 보이는 AP를 찍는다.
    // (S3는 2.4GHz 전용 — 대상 SSID가 5GHz 전용이면 여기 안 나온다 = reason 201)
    wifi_scan_config_t scan_cfg = { .show_hidden = true };
    if (esp_wifi_scan_start(&scan_cfg, true) == ESP_OK) {
        uint16_t n = 20;
        static wifi_ap_record_t recs[20];
        if (esp_wifi_scan_get_ap_records(&n, recs) == ESP_OK) {
            for (int i = 0; i < n; i++) {
                ESP_LOGI(TAG, "scan: %-24s ch%-2u rssi=%d auth=%d",
                         (const char *)recs[i].ssid, recs[i].primary,
                         recs[i].rssi, recs[i].authmode);
            }
        }
    }
    ESP_ERROR_CHECK(esp_wifi_connect());
}

// ==================== 서버 포워딩 (TCP) ====================
static volatile int s_sock = -1;
static volatile uint32_t s_fwd_bytes = 0;
static volatile uint32_t s_fwd_reconnects = 0;
static volatile uint32_t s_fwd_dropped = 0;
typedef struct { uint16_t len; uint8_t data[FRAME_MAX]; } forward_item_t;
static QueueHandle_t s_forward_queue;

static bool forward_enabled(void)
{
    return SERVER_HOST[0] != '\0' && SERVER_PORT != 0;
}

// UART 파서는 네트워크를 기다리지 않는다. 검증 프레임만 bounded queue에 복사한다.
static void forward_frame(const uint8_t *buf, int len)
{
    forward_item_t item = { .len = len };
    memcpy(item.data, buf, len);
    if (xQueueSend(s_forward_queue, &item, 0) != pdTRUE) s_fwd_dropped++;
}

static void network_send_task(void *arg)
{
    forward_item_t item;
    while (xQueueReceive(s_forward_queue, &item, portMAX_DELAY) == pdTRUE) {
        while (s_sock < 0) vTaskDelay(pdMS_TO_TICKS(100));
        int off = 0;
        while (off < item.len) {
            int sock = s_sock;
            int n = send(sock, item.data + off, item.len - off, 0);
            if (n <= 0) { close(sock); s_sock = -1; break; }
            off += n;
        }
        if (off == item.len) s_fwd_bytes += item.len;
    }
}

static void forward_task(void *arg)
{
    while (1) {
        xEventGroupWaitBits(s_wifi_events, WIFI_CONNECTED_BIT, pdFALSE, pdTRUE, portMAX_DELAY);
        if (s_sock >= 0) {
            vTaskDelay(pdMS_TO_TICKS(1000));
            continue;
        }

        struct addrinfo hints = { .ai_family = AF_INET, .ai_socktype = SOCK_STREAM };
        struct addrinfo *res = NULL;
        char port_str[8];
        snprintf(port_str, sizeof(port_str), "%d", SERVER_PORT);
        if (getaddrinfo(SERVER_HOST, port_str, &hints, &res) != 0 || !res) {
            ESP_LOGW(TAG, "dns fail: %s", SERVER_HOST);
            vTaskDelay(pdMS_TO_TICKS(5000));
            continue;
        }
        int sock = socket(res->ai_family, res->ai_socktype, 0);
        if (sock >= 0 && connect(sock, res->ai_addr, res->ai_addrlen) == 0) {
            // 토큰 핸드셰이크: 접속 직후 한 줄 전송 (ingest --token 검증용)
            if (strlen(SERVER_TOKEN) > 0) {
                char line[96];
                int n = snprintf(line, sizeof(line), "CSI-TOKEN %s\n", SERVER_TOKEN);
                if (send(sock, line, n, 0) != n) {
                    ESP_LOGW(TAG, "token send fail");
                    close(sock);
                    vTaskDelay(pdMS_TO_TICKS(5000));
                    freeaddrinfo(res);
                    continue;
                }
            }
            ESP_LOGI(TAG, "server connected: %s:%d", SERVER_HOST, SERVER_PORT);
            s_sock = sock;
            s_fwd_reconnects++;
        } else {
            ESP_LOGW(TAG, "connect fail: %s:%d", SERVER_HOST, SERVER_PORT);
            if (sock >= 0) {
                close(sock);
            }
            vTaskDelay(pdMS_TO_TICKS(5000));
        }
        freeaddrinfo(res);
    }
}

// ==================== UART 프레임 파서 ====================
static volatile uint32_t s_frames_ok = 0;
static volatile uint32_t s_frames_bad = 0;      // CRC/길이 불량 (resync 포함)
static volatile uint32_t s_rx_count[5] = {0};   // 인덱스 = recv_mac 끝자리 (1~4)
static volatile uint32_t s_uart_bytes = 0;

static void uart_ingest_task(void *arg)
{
    // 조립 버퍼: 프레임 경계가 UART read 단위와 어긋나므로 이월분을 유지한다.
    // 반드시 (최대 이월분 FRAME_MAX + chunk 크기)보다 커야 한다 — 작으면
    // 리셋 경로가 상시 발동해 멀쩡한 프레임을 ~17%씩 버린다 (실측했던 버그).
    static uint8_t acc[8192];
    static uint8_t chunk[2048];
    int fill = 0;

    ESP_LOGI(TAG, "uart ingest task started (RX=GPIO%d, %d baud)",
             RELAY_UART_RX_GPIO, RELAY_UART_BAUD);

    while (1) {
        int n = uart_read_bytes(RELAY_UART_NUM, chunk, sizeof(chunk), pdMS_TO_TICKS(100));
        if (n <= 0) {
            continue;
        }
        s_uart_bytes += n;
        if (fill + n > (int)sizeof(acc)) {
            // 파서가 밀렸다 — 오래된 이월분을 버리고 재동기화한다.
            fill = 0;
            s_frames_bad++;
        }
        memcpy(acc + fill, chunk, n);
        fill += n;

        int i = 0;
        while (fill - i >= 6) {
            if (acc[i] != CSI_BIN_MAGIC0 || acc[i + 1] != CSI_BIN_MAGIC1 || acc[i + 2] != CSI_BIN_VER) {
                i++;
                continue;
            }
            int plen = acc[i + 3] | (acc[i + 4] << 8);
            if (plen < REPORT_META_LEN || plen > REPORT_META_LEN + REPORT_CSI_MAX) {
                s_frames_bad++;
                i += 2;
                continue;
            }
            int end = i + 5 + plen + 1;
            if (end > fill) {
                break;      // 프레임 미완 — 다음 read에서 이어붙인다
            }
            uint8_t crc = 0;
            for (int k = 0; k < plen; k++) {
                crc ^= acc[i + 5 + k];
            }
            if (crc != acc[i + 5 + plen]) {
                s_frames_bad++;
                i += 2;
                continue;
            }
            // 유효 프레임. meta의 recv_mac(payload 앞 6B) 끝자리로 Rx 구분.
            uint8_t rx_id = acc[i + 5 + 5];
            if (rx_id >= 1 && rx_id <= 4) {
                s_rx_count[rx_id]++;
            }
            s_frames_ok++;
            if (forward_enabled()) {
                forward_frame(acc + i, 5 + plen + 1);
            }
            i = end;
        }
        if (i > 0) {
            memmove(acc, acc + i, fill - i);
            fill -= i;
        }
    }
}

// ==================== 통계 ====================
static void stats_task(void *arg)
{
    uint32_t prev_ok = 0;
    while (1) {
        vTaskDelay(pdMS_TO_TICKS(STATS_PERIOD_MS));
        uint32_t ok = s_frames_ok;
        float rate = (ok - prev_ok) * 1000.0f / STATS_PERIOD_MS;
        prev_ok = ok;
        bool wifi_up = (xEventGroupGetBits(s_wifi_events) & WIFI_CONNECTED_BIT) != 0;
        // rx_low: RX 핀을 5000회 연속 샘플해 low가 잡힌 횟수.
        // 데이터가 흐르면(라인 점유 ~65%) 수천 회, 0이면 엣지 자체가 없음
        // = 단선 또는 다른 드라이버(온보드 USB-UART 브리지 등)와의 경합.
        int rx_low = 0;
        for (int k = 0; k < 5000; k++) {
            if (gpio_get_level(RELAY_UART_RX_GPIO) == 0) {
                rx_low++;
            }
        }
        int rx_lvl = rx_low;
        // 풀다운 판별: 풀다운을 걸었는데도 1이면 외부 드라이버가 high를
        // 강하게 유지(브리지 칩 경합 등), 0이면 핀이 사실상 플로팅(미연결).
        gpio_set_pull_mode(RELAY_UART_RX_GPIO, GPIO_PULLDOWN_ONLY);
        esp_rom_delay_us(50);
        int pd_lvl = gpio_get_level(RELAY_UART_RX_GPIO);
        gpio_set_pull_mode(RELAY_UART_RX_GPIO, GPIO_PULLUP_ONLY);
        ESP_LOGI(TAG, "frames=%lu (%.1f/s) bad=%lu rx1=%lu rx2=%lu rx3=%lu rx4=%lu | "
                      "uart=%luB rx_low=%d/5000 pd_lvl=%d wifi=%s(drop %lu) fwd=%s sock=%d sent=%luB qdrop=%lu",
                 (unsigned long)ok, rate, (unsigned long)s_frames_bad,
                 (unsigned long)s_rx_count[1], (unsigned long)s_rx_count[2],
                 (unsigned long)s_rx_count[3], (unsigned long)s_rx_count[4],
                 (unsigned long)s_uart_bytes, rx_lvl, pd_lvl,
                 wifi_up ? "up" : "down", (unsigned long)s_wifi_disconnects,
                 forward_enabled() ? "on" : "off", (int)s_sock,
                 (unsigned long)s_fwd_bytes, (unsigned long)s_fwd_dropped);
    }
}

// ==================== 메인 ====================
void app_main(void)
{
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    // UART1: T7-C6 GPIO3으로부터 C6 프레임 스트림 수신
    uart_config_t uart_cfg = {
        .baud_rate  = RELAY_UART_BAUD,
        .data_bits  = UART_DATA_8_BITS,
        .parity     = UART_PARITY_DISABLE,
        .stop_bits  = UART_STOP_BITS_1,
        .flow_ctrl  = UART_HW_FLOWCTRL_DISABLE,
        .source_clk = UART_SCLK_DEFAULT,
    };
    ESP_ERROR_CHECK(uart_driver_install(RELAY_UART_NUM, RELAY_UART_RXBUF, 0, 0, NULL, 0));
    ESP_ERROR_CHECK(uart_param_config(RELAY_UART_NUM, &uart_cfg));
    ESP_ERROR_CHECK(uart_set_pin(RELAY_UART_NUM, RELAY_UART_TX_GPIO, RELAY_UART_RX_GPIO,
                                 UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE));

    wifi_init_sta();
    s_forward_queue = xQueueCreate(64, sizeof(forward_item_t));
    ESP_ERROR_CHECK(s_forward_queue ? ESP_OK : ESP_ERR_NO_MEM);

    ESP_LOGI(TAG, "================ CSI RELAY (Data Forwarder, ESP32-S3) ================");
    ESP_LOGI(TAG, "uart: RX=GPIO%d %dbaud | server=%s:%d (%s)",
             RELAY_UART_RX_GPIO, RELAY_UART_BAUD,
             forward_enabled() ? SERVER_HOST : "(미설정)", SERVER_PORT,
             forward_enabled() ? "forwarding" : "stats only");

    xTaskCreate(uart_ingest_task, "uart_ingest", 6144, NULL, 6, NULL);
    xTaskCreate(stats_task, "stats", 4096, NULL, 3, NULL);
    if (forward_enabled()) {
        xTaskCreate(forward_task, "forward", 4096, NULL, 4, NULL);
        xTaskCreate(network_send_task, "network_send", 4096, NULL, 5, NULL);
    }
}
