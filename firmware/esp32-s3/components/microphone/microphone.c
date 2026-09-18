#include "microphone.h"

#include "driver/i2s_pdm.h"
#include "esp_log.h"
#include "hal/gpio_types.h"

#define MIC_I2S_PORT I2S_NUM_0
#define MIC_PDM_CLK_GPIO GPIO_NUM_4
#define MIC_PDM_DATA_GPIO GPIO_NUM_5
#define MIC_SAMPLE_RATE_HZ 16000
#define MIC_READ_TIMEOUT_MS 1000

static const char *TAG = "microphone";
static i2s_chan_handle_t s_rx_chan;
static bool s_started;

esp_err_t microphone_init(void)
{
    if (s_rx_chan != NULL) {
        return ESP_OK;
    }

    i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(MIC_I2S_PORT, I2S_ROLE_MASTER);
    chan_cfg.dma_desc_num = 6;
    chan_cfg.dma_frame_num = 256;

    esp_err_t ret = i2s_new_channel(&chan_cfg, NULL, &s_rx_chan);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "i2s_new_channel failed: %s", esp_err_to_name(ret));
        s_rx_chan = NULL;
        return ret;
    }

    i2s_pdm_rx_config_t pdm_rx_cfg = {
        .clk_cfg = I2S_PDM_RX_CLK_DEFAULT_CONFIG(MIC_SAMPLE_RATE_HZ),
        .slot_cfg = I2S_PDM_RX_SLOT_PCM_FMT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_MONO),
        .gpio_cfg = {
            .clk = MIC_PDM_CLK_GPIO,
            .din = MIC_PDM_DATA_GPIO,
            .invert_flags = {
                .clk_inv = false,
            },
        },
    };

    pdm_rx_cfg.slot_cfg.slot_mask = I2S_PDM_SLOT_LEFT;

    ret = i2s_channel_init_pdm_rx_mode(s_rx_chan, &pdm_rx_cfg);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "PDM RX init failed: %s", esp_err_to_name(ret));
        i2s_del_channel(s_rx_chan);
        s_rx_chan = NULL;
        return ret;
    }

    ESP_LOGI(TAG, "PDM RX configured: I2S0 master, CLK GPIO4, DATA GPIO5, 16000 Hz PCM16 mono, LEFT slot");
    return ESP_OK;
}

esp_err_t microphone_start(void)
{
    if (s_rx_chan == NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    if (s_started) {
        return ESP_OK;
    }

    esp_err_t ret = i2s_channel_enable(s_rx_chan);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "channel enable failed: %s", esp_err_to_name(ret));
        return ret;
    }

    s_started = true;
    return ESP_OK;
}

esp_err_t microphone_read(int16_t *buffer, size_t samples, size_t *samples_read)
{
    if (samples_read != NULL) {
        *samples_read = 0;
    }

    if (s_rx_chan == NULL || !s_started) {
        return ESP_ERR_INVALID_STATE;
    }
    if (buffer == NULL || samples == 0) {
        return ESP_ERR_INVALID_ARG;
    }

    size_t bytes_read = 0;
    const size_t bytes_to_read = samples * sizeof(int16_t);
    esp_err_t ret = i2s_channel_read(s_rx_chan, buffer, bytes_to_read, &bytes_read, MIC_READ_TIMEOUT_MS);

    if (ret == ESP_ERR_TIMEOUT) {
        ESP_LOGW(TAG, "read timeout after %u ms", MIC_READ_TIMEOUT_MS);
        return ret;
    }
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "i2s_channel_read failed: %s", esp_err_to_name(ret));
        return ret;
    }
    if (bytes_read == 0) {
        ESP_LOGW(TAG, "i2s_channel_read returned 0 bytes");
        return ESP_OK;
    }

    if (samples_read != NULL) {
        *samples_read = bytes_read / sizeof(int16_t);
    }

    return ESP_OK;
}

void microphone_stop(void)
{
    if (s_rx_chan != NULL && s_started) {
        esp_err_t ret = i2s_channel_disable(s_rx_chan);
        if (ret != ESP_OK) {
            ESP_LOGW(TAG, "channel disable failed: %s", esp_err_to_name(ret));
        }
        s_started = false;
    }
}
