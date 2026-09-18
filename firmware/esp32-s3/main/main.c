#include <inttypes.h>
#include <stdint.h>
#include <stdlib.h>

#include "esp_err.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "audio_upload.h"
#include "microphone.h"
#include "wifi.h"

#define MIC_SAMPLE_RATE_HZ 16000
#define PCM_FRAME_SAMPLES 640

static const char *TAG = "shuihua_main";

void app_main(void)
{
    ESP_LOGI(TAG, "ShuiHuaMeeting audio upload: PCM_S16LE %d Hz mono", MIC_SAMPLE_RATE_HZ);

    ESP_ERROR_CHECK(wifi_init());
    ESP_ERROR_CHECK(wifi_wait_connected());
    ESP_ERROR_CHECK(microphone_init());
    ESP_ERROR_CHECK(microphone_start());
    ESP_ERROR_CHECK(audio_upload_init());

    int16_t *buffer = (int16_t *)calloc(PCM_FRAME_SAMPLES, sizeof(int16_t));
    if (buffer == NULL) {
        ESP_LOGE(TAG, "Failed to allocate PCM upload buffer");
        return;
    }

    while (true) {
        size_t collected = 0;

        while (collected < PCM_FRAME_SAMPLES) {
            size_t samples_read = 0;
            esp_err_t ret = microphone_read(&buffer[collected],
                                            PCM_FRAME_SAMPLES - collected,
                                            &samples_read);
            if (ret != ESP_OK) {
                ESP_LOGW(TAG, "microphone_read failed: %s", esp_err_to_name(ret));
                vTaskDelay(pdMS_TO_TICKS(20));
                continue;
            }

            if (samples_read == 0) {
                ESP_LOGW(TAG, "read returned 0 samples");
                vTaskDelay(pdMS_TO_TICKS(20));
                continue;
            }

            collected += samples_read;
        }

        esp_err_t ret = audio_upload_send_pcm(buffer, PCM_FRAME_SAMPLES);
        if (ret != ESP_OK) {
            ESP_LOGW(TAG, "PCM frame dropped: %s", esp_err_to_name(ret));
            vTaskDelay(pdMS_TO_TICKS(1000));
        }
    }
}
