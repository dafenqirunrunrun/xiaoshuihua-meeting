#pragma once

#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

esp_err_t audio_upload_init(void);
esp_err_t audio_upload_send_pcm(const int16_t *samples, size_t sample_count);

#ifdef __cplusplus
}
#endif