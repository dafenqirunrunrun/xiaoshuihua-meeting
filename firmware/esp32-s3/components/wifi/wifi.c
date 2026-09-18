#include "wifi.h"

#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_wifi.h"
#include "nvs_flash.h"
#include "sdkconfig.h"

#define WIFI_CONNECTED_BIT BIT0
#define WIFI_MAX_RETRY 10

static const char *TAG = "wifi";
static EventGroupHandle_t s_wifi_events;
static int s_retry_count;

static void wifi_event_handler(void *arg, esp_event_base_t event_base,
                               int32_t event_id, void *event_data)
{
    (void)arg;

    if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_START) {
        ESP_LOGI(TAG, "[WIFI] connecting...");
        esp_wifi_connect();
    } else if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_DISCONNECTED) {
        xEventGroupClearBits(s_wifi_events, WIFI_CONNECTED_BIT);
        if (s_retry_count < WIFI_MAX_RETRY) {
            ++s_retry_count;
            esp_wifi_connect();
            ESP_LOGW(TAG, "retrying connection (%d/%d)", s_retry_count, WIFI_MAX_RETRY);
        } else {
            ESP_LOGE(TAG, "Wi-Fi connection failed after %d retries", WIFI_MAX_RETRY);
        }
    } else if (event_base == IP_EVENT && event_id == IP_EVENT_STA_GOT_IP) {
        const ip_event_got_ip_t *event = (const ip_event_got_ip_t *)event_data;
        s_retry_count = 0;
        xEventGroupSetBits(s_wifi_events, WIFI_CONNECTED_BIT);
        ESP_LOGI(TAG, "[WIFI] connected ip=" IPSTR, IP2STR(&event->ip_info.ip));
    }
}

esp_err_t wifi_init(void)
{
    if (s_wifi_events != NULL) {
        return ESP_OK;
    }
    if (CONFIG_SHUIHUA_WIFI_SSID[0] == '\0') {
        ESP_LOGE(TAG, "CONFIG_SHUIHUA_WIFI_SSID is empty; set it with idf.py menuconfig");
        return ESP_ERR_INVALID_ARG;
    }

    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    if (ret != ESP_OK) {
        return ret;
    }

    s_wifi_events = xEventGroupCreate();
    if (s_wifi_events == NULL) {
        return ESP_ERR_NO_MEM;
    }

    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t init_config = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&init_config));
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID,
                                               &wifi_event_handler, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP,
                                               &wifi_event_handler, NULL));

    wifi_config_t wifi_config = {0};
    strlcpy((char *)wifi_config.sta.ssid, CONFIG_SHUIHUA_WIFI_SSID,
            sizeof(wifi_config.sta.ssid));
    strlcpy((char *)wifi_config.sta.password, CONFIG_SHUIHUA_WIFI_PASSWORD,
            sizeof(wifi_config.sta.password));
    wifi_config.sta.pmf_cfg.capable = true;
    wifi_config.sta.pmf_cfg.required = false;

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_config));
    ESP_ERROR_CHECK(esp_wifi_start());
    return ESP_OK;
}

esp_err_t wifi_wait_connected(void)
{
    if (s_wifi_events == NULL) {
        return ESP_ERR_INVALID_STATE;
    }

    EventBits_t bits = xEventGroupWaitBits(s_wifi_events, WIFI_CONNECTED_BIT,
                                           pdFALSE, pdTRUE, portMAX_DELAY);
    return (bits & WIFI_CONNECTED_BIT) != 0 ? ESP_OK : ESP_FAIL;
}