#include <inttypes.h>
#include <stdint.h>

#include "esp_err.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "audio_upload.h"
#include "microphone.h"
#include "wifi.h"

#define MIC_SAMPLE_RATE_HZ 16000
#define PCM_FRAME_SAMPLES 640
#define PCM_QUEUE_LENGTH 25
#define QUEUE_DROP_LOG_INTERVAL 25
#define UPLOAD_LOG_INTERVAL 25
#define MICROPHONE_CAPTURE_TASK_STACK_SIZE 4096
#define AUDIO_UPLOAD_TASK_STACK_SIZE 4096
#define MICROPHONE_CAPTURE_TASK_PRIORITY 5
#define AUDIO_UPLOAD_TASK_PRIORITY 4

static const char *TAG = "shuihua_main";
static QueueHandle_t s_pcm_queue;

typedef struct {
    int16_t samples[PCM_FRAME_SAMPLES];
} pcm_frame_t;

static void microphone_capture_task(void *arg)
{
    (void)arg;

    pcm_frame_t frame;
    uint32_t queue_drop_count = 0;

    ESP_LOGI(TAG, "[AUDIO] capture task started");

    while (true) {
        size_t collected = 0;

        while (collected < PCM_FRAME_SAMPLES) {
            size_t samples_read = 0;
            esp_err_t ret = microphone_read(&frame.samples[collected],
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

        if (xQueueSend(s_pcm_queue, &frame, 0) != pdPASS) {
            pcm_frame_t oldest_frame;
            if (xQueueReceive(s_pcm_queue, &oldest_frame, 0) == pdPASS) {
                ++queue_drop_count;
                if ((queue_drop_count % QUEUE_DROP_LOG_INTERVAL) == 0) {
                    ESP_LOGW(TAG, "[AUDIO] capture queue overflow dropped=%" PRIu32,
                             queue_drop_count);
                }
            }

            if (xQueueSend(s_pcm_queue, &frame, 0) != pdPASS) {
                ESP_LOGW(TAG, "[AUDIO] failed to enqueue latest PCM frame");
            }
        }
    }
}

static void audio_upload_task(void *arg)
{
    (void)arg;

    pcm_frame_t frame;
    uint32_t uploaded_frame_count = 0;
    uint32_t send_failure_count = 0;
    uint32_t consecutive_send_failures = 0;

    ESP_LOGI(TAG, "[AUDIO] upload task started");

    while (true) {
        if (xQueueReceive(s_pcm_queue, &frame, portMAX_DELAY) != pdPASS) {
            continue;
        }

        esp_err_t ret = audio_upload_send_pcm(frame.samples, PCM_FRAME_SAMPLES);
        if (ret != ESP_OK) {
            ++send_failure_count;
            ++consecutive_send_failures;
            if (consecutive_send_failures == 1 ||
                (consecutive_send_failures % UPLOAD_LOG_INTERVAL) == 0) {
                ESP_LOGW(TAG, "[AUDIO] send failed count=%" PRIu32 ": %s",
                         send_failure_count, esp_err_to_name(ret));
            }
            continue;
        }

        consecutive_send_failures = 0;
        ++uploaded_frame_count;
        if ((uploaded_frame_count % UPLOAD_LOG_INTERVAL) == 0) {
            ESP_LOGI(TAG, "[AUDIO] uploaded frames=%" PRIu32,
                     uploaded_frame_count);
        }
    }
}

void app_main(void)
{
    ESP_LOGI(TAG, "ShuiHuaMeeting audio upload: PCM_S16LE %d Hz mono", MIC_SAMPLE_RATE_HZ);

    ESP_ERROR_CHECK(wifi_init());
    ESP_ERROR_CHECK(wifi_wait_connected());
    ESP_ERROR_CHECK(microphone_init());
    ESP_ERROR_CHECK(microphone_start());
    ESP_ERROR_CHECK(audio_upload_init());

    s_pcm_queue = xQueueCreate(PCM_QUEUE_LENGTH, sizeof(pcm_frame_t));
    if (s_pcm_queue == NULL) {
        ESP_LOGE(TAG, "Failed to create PCM queue");
        return;
    }

    TaskHandle_t upload_task_handle = NULL;
    if (xTaskCreate(audio_upload_task, "audio_upload",
                    AUDIO_UPLOAD_TASK_STACK_SIZE, NULL,
                    AUDIO_UPLOAD_TASK_PRIORITY, &upload_task_handle) != pdPASS) {
        ESP_LOGE(TAG, "Failed to create audio upload task");
        vQueueDelete(s_pcm_queue);
        s_pcm_queue = NULL;
        return;
    }

    if (xTaskCreate(microphone_capture_task, "mic_capture",
                    MICROPHONE_CAPTURE_TASK_STACK_SIZE, NULL,
                    MICROPHONE_CAPTURE_TASK_PRIORITY, NULL) != pdPASS) {
        ESP_LOGE(TAG, "Failed to create microphone capture task");
        vTaskDelete(upload_task_handle);
        vQueueDelete(s_pcm_queue);
        s_pcm_queue = NULL;
    }
}
