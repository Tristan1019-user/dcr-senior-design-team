#include "control.h"
#include "i2c_bus.h"
#include "sensor_components.h"
#include "software.h"
#include <stdio.h>

static bool stdout_writer(void *context, const char *line, size_t length)
{
    (void)context;
    return fwrite(line, 1, length, stdout) == length && fflush(stdout) == 0;
}

int main(void)
{
    const dcr_descriptor_t *registry[8];
    size_t count = dcr_software_registry_count;
    for (size_t n = 0; n < count; ++n)
        registry[n] = dcr_software_registry[n];
    registry[count++] = &dcr_i2c_bus_descriptor;
    registry[count++] = &dcr_sht45_descriptor;
    registry[count++] = &dcr_bmp280_descriptor;
    const char *configuration =
        "{\"version\":\"1.0\",\"components\":["
        "{\"id\":\"i2c_bus_0\",\"type\":\"i2c_bus\",\"priority\":7,\"config\":{"
        "\"port\":0,\"sda_pin\":8,\"scl_pin\":9,\"freq_hz\":100000}},"
        "{\"id\":\"i2c_bus_1\",\"type\":\"i2c_bus\",\"priority\":7,\"config\":{"
        "\"port\":1,\"sda_pin\":10,\"scl_pin\":11,\"freq_hz\":100000}},"
        "{\"id\":\"sht45\",\"type\":\"sht45_sensor\",\"config\":{"
        "\"address\":68,\"period_ms\":1000}},"
        "{\"id\":\"bmp280\",\"type\":\"bmp280_sensor\",\"config\":{"
        "\"address\":118,\"period_ms\":1000}},"
        "{\"id\":\"sht_temperature_c\",\"type\":\"logger\"},"
        "{\"id\":\"humidity_pct\",\"type\":\"logger\"},"
        "{\"id\":\"bmp_temperature_c\",\"type\":\"logger\"},"
        "{\"id\":\"pressure_pa\",\"type\":\"logger\"}],\"connections\":["
        "{\"from\":\"sht45.bus\",\"to\":\"i2c_bus_0.request\"},"
        "{\"from\":\"bmp280.bus\",\"to\":\"i2c_bus_1.request\"},"
        "{\"from\":\"sht45.temperature_c\",\"to\":\"sht_temperature_c.input\"},"
        "{\"from\":\"sht45.humidity_pct\",\"to\":\"humidity_pct.input\"},"
        "{\"from\":\"bmp280.temperature_c\",\"to\":\"bmp_temperature_c.input\"},"
        "{\"from\":\"bmp280.pressure_pa\",\"to\":\"pressure_pa.input\"}],"
        "\"settings\":{\"watchdog_timeout_ms\":5000,\"signal_wait_timeout_ms\":100}}";
    dcr_runtime_t *runtime = NULL;
    dcr_diagnostic_t diagnostic;
    if (dcr_build_json(configuration, registry, count, &runtime, &diagnostic) != DCR_OK)
        return 2;
    if (dcr_start_autostart(runtime) != DCR_OK) {
        dcr_runtime_destroy(runtime);
        return 3;
    }

    dcr_control_t *control = dcr_control_create(runtime, "host-control", stdout_writer, NULL);
    char *line = pal_malloc(DCR_CONTROL_MAX_FRAME + 1U);
    if (!control || !line) {
        dcr_control_destroy(control);
        pal_free(line);
        dcr_runtime_destroy(runtime);
        return 4;
    }
    size_t used = 0;
    bool oversized = false;
    int ch;
    while ((ch = getchar()) != EOF) {
        if (ch == '\n') {
            if (oversized)
                dcr_control_handle_line(control, NULL, DCR_CONTROL_MAX_FRAME + 1U);
            else {
                dcr_control_handle_line(control, line, used);
                dcr_control_emit_snapshot(control);
            }
            used = 0;
            oversized = false;
        } else if (ch == '\r') {
            continue;
        } else if (!oversized) {
            if (used < DCR_CONTROL_MAX_FRAME)
                line[used++] = (char)ch;
            else
                oversized = true;
        }
    }
    if (used || oversized)
        dcr_control_handle_line(control, oversized ? NULL : line,
                                oversized ? DCR_CONTROL_MAX_FRAME + 1U : used);
    dcr_control_destroy(control);
    pal_free(line);
    dcr_runtime_destroy(runtime);
    return 0;
}
