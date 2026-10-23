#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include "sdkconfig.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/task.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "lwip/inet.h"
#include "lwip/sockets.h"
#include "nvs_flash.h"
#include "frame.h"

#define WIFI_READY BIT0
#define WIFI_DISCONNECTED BIT1
#define NETWORK_CHANGED BIT2
#define BACKOFF_MIN_MS 1000U
#define BACKOFF_MAX_MS 30000U

static const char *TAG = "edge_device";
static EventGroupHandle_t network_events;

static TickType_t ticks_ms(uint32_t ms)
{
    const TickType_t ticks = pdMS_TO_TICKS(ms);
    return ticks > 0 ? ticks : 1;
}

static void retry_delay(uint32_t *delay_ms, const char *reason)
{
    ESP_LOGW(TAG, "%s; retry in %" PRIu32 " ms", reason, *delay_ms);
    vTaskDelay(ticks_ms(*delay_ms));
    *delay_ms = *delay_ms < BACKOFF_MAX_MS / 2U ? *delay_ms * 2U : BACKOFF_MAX_MS;
}

/* Callbacks only publish state. The application task owns all socket lifetimes. */
static void wifi_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    (void)arg;
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        const wifi_event_sta_disconnected_t *event = data;
        xEventGroupClearBits(network_events, WIFI_READY);
        xEventGroupSetBits(network_events, WIFI_DISCONNECTED | NETWORK_CHANGED);
        ESP_LOGW(TAG, "Wi-Fi disconnected, reason=%u", (unsigned)event->reason);
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        const ip_event_got_ip_t *event = data;
        xEventGroupClearBits(network_events, WIFI_DISCONNECTED);
        /* Also invalidates the old socket after DHCP address changes. */
        xEventGroupSetBits(network_events, WIFI_READY | NETWORK_CHANGED);
        ESP_LOGI(TAG, "Wi-Fi IPv4 " IPSTR, IP2STR(&event->ip_info.ip));
    } else if (base == IP_EVENT && id == IP_EVENT_STA_LOST_IP) {
        xEventGroupClearBits(network_events, WIFI_READY);
        xEventGroupSetBits(network_events, WIFI_DISCONNECTED | NETWORK_CHANGED);
    }
}

static bool credentials_valid(void)
{
    const size_t ssid_len = strlen(CONFIG_EDGE_WIFI_SSID);
    const size_t pass_len = strlen(CONFIG_EDGE_WIFI_PASSWORD);
    if (ssid_len == 0 || ssid_len > 32 || pass_len < 8 || pass_len > 64) {
        return false;
    }
    if (pass_len == 64) {
        for (size_t i = 0; i < pass_len; ++i) {
            const char c = CONFIG_EDGE_WIFI_PASSWORD[i];
            if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') ||
                  (c >= 'A' && c <= 'F'))) {
                return false;
            }
        }
    }
    return true;
}

static void wifi_init(void)
{
    network_events = xEventGroupCreate();
    if (network_events == NULL) {
        ESP_LOGE(TAG, "Cannot allocate Wi-Fi event group");
        abort();
    }
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    if (esp_netif_create_default_wifi_sta() == NULL) {
        ESP_LOGE(TAG, "Cannot allocate Wi-Fi network interface");
        abort();
    }
    wifi_init_config_t init = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&init));
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, wifi_event, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, wifi_event, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_LOST_IP, wifi_event, NULL));
    wifi_config_t config = {0};
    memcpy(config.sta.ssid, CONFIG_EDGE_WIFI_SSID, strlen(CONFIG_EDGE_WIFI_SSID));
    memcpy(config.sta.password, CONFIG_EDGE_WIFI_PASSWORD, strlen(CONFIG_EDGE_WIFI_PASSWORD));
    config.sta.threshold.authmode = WIFI_AUTH_WPA2_PSK;
    config.sta.pmf_cfg.capable = true;
    config.sta.pmf_cfg.required = false;
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &config));
    ESP_ERROR_CHECK(esp_wifi_start());
    /* Avoid modem-sleep adding uncontrolled latency to the demo. */
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
}

static void ensure_wifi_connected(void)
{
    uint32_t backoff_ms = BACKOFF_MIN_MS;
    while (!(xEventGroupGetBits(network_events) & WIFI_READY)) {
        xEventGroupClearBits(network_events, WIFI_DISCONNECTED);
        const esp_err_t err = esp_wifi_connect();
        if (err == ESP_OK) {
            const EventBits_t bits = xEventGroupWaitBits(
                network_events, WIFI_READY | WIFI_DISCONNECTED, pdFALSE, pdFALSE,
                ticks_ms(CONFIG_EDGE_WIFI_TIMEOUT_MS));
            if (bits & WIFI_READY) {
                return;
            }
        } else {
            ESP_LOGW(TAG, "Wi-Fi connect returned %s", esp_err_to_name(err));
        }
        /* Cancel association/DHCP attempt; callbacks never start their own retry. */
        (void)esp_wifi_disconnect();
        retry_delay(&backoff_ms, "Wi-Fi not ready");
    }
}

static bool link_usable(void)
{
    const EventBits_t bits = xEventGroupGetBits(network_events);
    return (bits & WIFI_READY) && !(bits & NETWORK_CHANGED);
}

/* Absolute monotonic deadlines bound the complete operation, including retries. */
static int wait_writable(int fd, int64_t deadline_us)
{
    for (;;) {
        if (!link_usable()) {
            errno = ENETDOWN;
            return -1;
        }
        int64_t remaining_us = deadline_us - esp_timer_get_time();
        if (remaining_us <= 0) {
            errno = ETIMEDOUT;
            return -1;
        }
        if (remaining_us > 250000) {
            remaining_us = 250000; /* Recheck Wi-Fi even if select stays idle. */
        }
        struct timeval timeout = {
            .tv_sec = (long)(remaining_us / 1000000),
            .tv_usec = (long)(remaining_us % 1000000),
        };
        fd_set writable, errors;
        FD_ZERO(&writable);
        FD_ZERO(&errors);
        FD_SET(fd, &writable);
        FD_SET(fd, &errors);
        const int result = select(fd + 1, NULL, &writable, &errors, &timeout);
        if (result < 0) {
            if (errno == EINTR) {
                continue;
            }
            return -1;
        }
        if (result > 0) {
            int socket_error = 0;
            socklen_t size = sizeof(socket_error);
            if (getsockopt(fd, SOL_SOCKET, SO_ERROR, &socket_error, &size) < 0) {
                return -1;
            }
            if (socket_error != 0 || FD_ISSET(fd, &errors)) {
                errno = socket_error != 0 ? socket_error : EIO;
                return -1;
            }
            return 0;
        }
    }
}

static void close_socket(int *fd)
{
    if (*fd >= 0) {
        /* A partially transmitted NDJSON frame must never continue on another stream. */
        shutdown(*fd, SHUT_RDWR);
        close(*fd);
        *fd = -1;
    }
}

static int connect_server(const struct sockaddr_in *server)
{
    int fd = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (fd < 0) {
        return -1;
    }
    const int flags = fcntl(fd, F_GETFL, 0);
    if (flags < 0 || fcntl(fd, F_SETFL, flags | O_NONBLOCK) < 0) {
        goto failed;
    }
    const int keepalive = 1;
    if (setsockopt(fd, SOL_SOCKET, SO_KEEPALIVE, &keepalive, sizeof(keepalive)) < 0) {
        goto failed;
    }
    const int64_t deadline = esp_timer_get_time() + (int64_t)CONFIG_EDGE_SOCKET_TIMEOUT_MS * 1000;
    if (!link_usable()) {
        errno = ENETDOWN;
        goto failed;
    }
    if (connect(fd, (const struct sockaddr *)server, sizeof(*server)) != 0) {
        if (errno != EINPROGRESS || wait_writable(fd, deadline) < 0) {
            goto failed;
        }
    }
    ESP_LOGI(TAG, "TCP connected to %s:%d", CONFIG_EDGE_SERVER_IPV4, CONFIG_EDGE_SERVER_PORT);
    return fd;

failed:
    {
        const int saved_errno = errno;
        close_socket(&fd);
        errno = saved_errno;
        return -1;
    }
}

static int send_all(int fd, const char *frame, size_t length)
{
    const int64_t deadline = esp_timer_get_time() + (int64_t)CONFIG_EDGE_SOCKET_TIMEOUT_MS * 1000;
    size_t offset = 0;
    while (offset < length) {
        if (!link_usable()) {
            errno = ENETDOWN;
            return -1;
        }
        if (esp_timer_get_time() >= deadline) {
            errno = ETIMEDOUT;
            return -1;
        }
        const ssize_t sent = send(fd, frame + offset, length - offset, 0);
        if (sent > 0) {
            offset += (size_t)sent;
        } else if (sent == 0) {
            errno = ECONNRESET;
            return -1;
        } else if (errno == EINTR) {
            continue;
        } else if (errno == EAGAIN || errno == EWOULDBLOCK) {
            if (wait_writable(fd, deadline) < 0) {
                return -1;
            }
        } else {
            return -1;
        }
    }
    return 0;
}

void app_main(void)
{
    struct sockaddr_in server = {
        .sin_family = AF_INET,
        .sin_port = htons(CONFIG_EDGE_SERVER_PORT),
    };
    if (!credentials_valid() || !edge_device_id_valid(CONFIG_EDGE_DEVICE_ID) ||
        inet_pton(AF_INET, CONFIG_EDGE_SERVER_IPV4, &server.sin_addr) != 1 ||
        server.sin_addr.s_addr == htonl(INADDR_ANY) ||
        server.sin_addr.s_addr == htonl(INADDR_BROADCAST)) {
        ESP_LOGE(TAG, "Invalid configuration. Set Edge scheduler device in menuconfig.");
        return;
    }
    if (CONFIG_EDGE_URGENT_SAMPLES > CONFIG_EDGE_NORMAL_SAMPLES) {
        ESP_LOGW(TAG, "Urgent batch is larger than normal; small-task demo may not apply");
    }
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }
    ESP_ERROR_CHECK(err);
    const size_t maximum_samples = CONFIG_EDGE_NORMAL_SAMPLES > CONFIG_EDGE_URGENT_SAMPLES
        ? CONFIG_EDGE_NORMAL_SAMPLES : CONFIG_EDGE_URGENT_SAMPLES;
    const size_t capacity = edge_frame_capacity(maximum_samples);
    char *frame = malloc(capacity);
    if (frame == NULL) {
        ESP_LOGE(TAG, "Cannot allocate %u-byte frame buffer", (unsigned)capacity);
        return;
    }
    wifi_init();
    int fd = -1;
    uint64_t sequence = 1;
    uint32_t tcp_backoff_ms = BACKOFF_MIN_MS;
    ESP_LOGI(TAG, "Source=synthetic. TCP send success is NOT server completion; no ACK/replay.");
    for (;;) {
        if (!link_usable()) {
            close_socket(&fd);
        }
        ensure_wifi_connected();
        if (xEventGroupClearBits(network_events, NETWORK_CHANGED) & NETWORK_CHANGED) {
            close_socket(&fd);
        }
        if (fd < 0) {
            fd = connect_server(&server);
            if (fd < 0) {
                ESP_LOGW(TAG, "TCP connect failed, errno=%d", errno);
                retry_delay(&tcp_backoff_ms, "TCP unavailable");
                continue;
            }
        }
        /* Avoid a compile-time modulo-by-zero warning when urgent is disabled. */
        const uint64_t urgent_every = CONFIG_EDGE_URGENT_EVERY;
        const bool urgent = urgent_every != 0 && sequence % urgent_every == 0;
        const size_t samples = urgent ? CONFIG_EDGE_URGENT_SAMPLES : CONFIG_EDGE_NORMAL_SAMPLES;
        const size_t length = edge_frame_build(frame, capacity, CONFIG_EDGE_DEVICE_ID,
                                               sequence, urgent, samples);
        if (length == 0) {
            ESP_LOGE(TAG, "Frame encoding failed; producer stopped");
            break;
        }
        const int result = send_all(fd, frame, length);
        if (result < 0) {
            ESP_LOGW(TAG, "seq=%" PRIu64 " send failed errno=%d; delivery unknown, dropping frame",
                     sequence, errno);
            close_socket(&fd);
        } else {
            ESP_LOGI(TAG, "seq=%" PRIu64 " %s samples=%u bytes=%u accepted by local TCP",
                     sequence, urgent ? "urgent" : "normal", (unsigned)samples, (unsigned)length);
            tcp_backoff_ms = BACKOFF_MIN_MS;
        }
        if (sequence == EDGE_MAX_SEQ) {
            ESP_LOGE(TAG, "Protocol sequence limit reached; producer stopped (no wrap)");
            break;
        }
        ++sequence; /* Failed attempts are not replayed; sequence gaps are expected. */
        if (result < 0) {
            retry_delay(&tcp_backoff_ms, "TCP send failure");
        } else {
            /* Wi-Fi events interrupt the configured interval for faster recovery. */
            (void)xEventGroupWaitBits(network_events, NETWORK_CHANGED, pdFALSE, pdFALSE,
                                     ticks_ms(CONFIG_EDGE_SEND_INTERVAL_MS));
        }
    }
    close_socket(&fd);
    free(frame);
}
