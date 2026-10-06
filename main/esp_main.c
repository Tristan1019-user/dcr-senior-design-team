#include "software.h"
#include "esp_spiffs.h"
#include "esp_log.h"
#ifdef DCR_CONTROL
#include "control_uart.h"
#include "i2c_bus.h"
#include "sensor_components.h"
#include "esp_random.h"
#include "esp_system.h"
#include <stdio.h>
#define CONFIG_FILE "/spiffs/environment.json"
#elif defined(DCR_SENSOR_DEMO)
#include "i2c_bus.h"
#include "sensor_components.h"
#define CONFIG_FILE "/spiffs/environment.json"
#ifndef DCR_SENSOR_DURATION_MS
#define DCR_SENSOR_DURATION_MS 60000
#endif
#define DEMO_DURATION_MS DCR_SENSOR_DURATION_MS
#else
#define CONFIG_FILE "/spiffs/fanout.json"
#define DEMO_DURATION_MS 15000
#endif
void app_main(void)
{
    esp_vfs_spiffs_conf_t fs = {.base_path = "/spiffs",
                                .partition_label = "storage",
                                .max_files = 3,
                                .format_if_mount_failed = false};
    if (esp_vfs_spiffs_register(&fs) != ESP_OK) {
        ESP_LOGE("dcr", "Config filesystem missing; refusing fallback topology");
        return;
    }
    char *text = pal_malloc(65537);
    if (!text)
        return;
    if (pal_read_file(CONFIG_FILE, text, 65537) < 0) {
        pal_free(text);
        ESP_LOGE("dcr", "Cannot read config");
        return;
    }
    dcr_runtime_t *r = NULL;
    dcr_diagnostic_t diag;
    const dcr_descriptor_t *registry[16];
    size_t count = dcr_software_registry_count;
    size_t extra = 0;
#if defined(DCR_CONTROL) || defined(DCR_SENSOR_DEMO)
    extra = 3;
#endif
    if (count + extra > sizeof(registry) / sizeof(registry[0])) {
        pal_free(text);
        ESP_LOGE("dcr", "Registry capacity exceeded");
        return;
    }
    for (size_t n = 0; n < count; ++n)
        registry[n] = dcr_software_registry[n];
#if defined(DCR_CONTROL) || defined(DCR_SENSOR_DEMO)
    registry[count++] = &dcr_i2c_bus_descriptor;
    registry[count++] = &dcr_sht45_descriptor;
    registry[count++] = &dcr_bmp280_descriptor;
#endif
    dcr_err_t e = dcr_build_json(text, registry, count, &r, &diag);
    pal_free(text);
    if (e) {
        ESP_LOGE("dcr", "%s: %s", dcr_error_name(e), diag.context);
        dcr_runtime_destroy(r);
        return;
    }
#ifdef DCR_CONTROL
    /* UART0 is the sole machine writer after startup; application logs stay disabled. */
    esp_log_level_set("*", ESP_LOG_NONE);
    e = dcr_start_autostart(r);
    if (!e) {
        char boot_id[32];
        snprintf(boot_id, sizeof(boot_id), "esp32s3-%08lx%08lx", (unsigned long)esp_random(),
                 (unsigned long)esp_random());
        dcr_control_uart_run(r, boot_id);
    }
    dcr_err_t stopped = dcr_runtime_destroy(r);
    (void)stopped;
#elif defined(DCR_SENSOR_DEMO)
    e = dcr_start_autostart(r);
    if (!e)
        pal_sleep(DEMO_DURATION_MS);
    dcr_err_t stopped = dcr_runtime_destroy(r);
    ESP_LOGI("dcr", "DCR_SENSOR_DEMO %s PAL_allocations=%u; verify sample/error logs separately",
             !e && !stopped && !pal_allocations() ? "STOPPED" : "FAIL",
             (unsigned)pal_allocations());
#else
    e = dcr_start_autostart(r);
    if (!e)
        pal_sleep(DEMO_DURATION_MS);
    dcr_err_t stopped = dcr_runtime_destroy(r);
    ESP_LOGI("dcr", "DCR_DEVICE_DEMO %s PAL_allocations=%u",
             !e && !stopped && !pal_allocations() ? "PASS" : "FAIL", (unsigned)pal_allocations());
#endif
}
