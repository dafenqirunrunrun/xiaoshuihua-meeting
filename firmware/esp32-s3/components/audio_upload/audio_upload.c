#include "audio_upload.h"

#include <stdio.h>

#include "esp_log.h"
#include "esp_websocket_client.h"
#include "sdkconfig.h"

#define PCM_BYTES_PER_SAMPLE 2

static const char *TAG = "audio_upload";
static esp_websocket_client_handle_t s_client;

static void websocket_event_handler(void *handler_args, esp_event_base_t base,
                                    int32_t event_id, void *event_data)
{
    (void)handler_args;
    (void)base;
    (void)event_data;

    if (event_id == WEBSOCKET_EVENT_CONNECTED) {
        ESP_LOGI(TAG, "[AUDIO] websocket connected");
    } else if (event_id == WEBSOCKET_EVENT_DISCONNECTED) {
        ESP_LOGW(TAG, "[AUDIO] websocket disconnected");
    } else if (event_id == WEBSOCKET_EVENT_ERROR) {
        ESP_LOGW(TAG, "WebSocket error");
    }
}

esp_err_t audio_upload_init(void)
{
    if (s_client != NULL) {
        return ESP_OK;
    }

    char uri[256];
    int written = snprintf(uri, sizeof(uri), "ws://%s:%s%s/%s",
                           CONFIG_SHUIHUA_SERVER_HOST,
                           CONFIG_SHUIHUA_SERVER_PORT,
                           CONFIG_SHUIHUA_AUDIO_WS_PATH,
                           CONFIG_SHUIHUA_DEVICE_ID);
    if (written < 0 || (size_t)written >= sizeof(uri)) {
        return ESP_ERR_INVALID_SIZE;
    }

    esp_websocket_client_config_t config = {
        .uri = uri,
        .network_timeout_ms = 5000,
        .reconnect_timeout_ms = 5000,
    };
    s_client = esp_websocket_client_init(&config);
    if (s_client == NULL) {
        return ESP_ERR_NO_MEM;
    }
    ESP_ERROR_CHECK(esp_websocket_register_events(s_client, WEBSOCKET_EVENT_ANY,
                                                  websocket_event_handler, NULL));
    ESP_LOGI(TAG, "[AUDIO] websocket connecting uri=%s", uri);
    return esp_websocket_client_start(s_client);
}

esp_err_t audio_upload_send_pcm(const int16_t *samples, size_t sample_count)
{
    if (s_client == NULL || samples == NULL || sample_count == 0) {
        return ESP_ERR_INVALID_STATE;
    }
    if (!esp_websocket_client_is_connected(s_client)) {
        return ESP_ERR_INVALID_STATE;
    }

    int bytes = esp_websocket_client_send_bin(s_client,
                                              (const char *)samples,
                                              sample_count * PCM_BYTES_PER_SAMPLE,
                                              5000);
    if (bytes < 0) {
        return ESP_FAIL;
    }
    ESP_LOGI(TAG, "[AUDIO] sent frame bytes=%d", bytes);
    return ESP_OK;
}