#pragma once

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

esp_err_t wifi_init(void);
esp_err_t wifi_wait_connected(void);

#ifdef __cplusplus
}
#endif