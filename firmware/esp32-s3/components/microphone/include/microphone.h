#pragma once

#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

esp_err_t microphone_init(void);
esp_err_t microphone_start(void);
esp_err_t microphone_read(int16_t *buffer, size_t samples, size_t *samples_read);
void microphone_stop(void);

#ifdef __cplusplus
}
#endif
