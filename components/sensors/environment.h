#ifndef DCR_ENVIRONMENT_H
#define DCR_ENVIRONMENT_H
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

typedef enum {
    ENV_OK,
    ENV_IO,
    ENV_CRC,
    ENV_ID,
    ENV_CALIBRATION,
    ENV_TIMEOUT,
    ENV_RANGE
} env_error_t;
typedef struct {
    void *ctx;
    bool (*write)(void *, uint8_t, const uint8_t *, size_t);
    bool (*read)(void *, uint8_t, uint8_t *, size_t);
    bool (*register_read)(void *, uint8_t, uint8_t, uint8_t *, size_t);
    void (*delay_ms)(void *, uint32_t);
} env_io_t;
typedef struct {
    uint16_t t1, p1;
    int16_t t2, t3, p2, p3, p4, p5, p6, p7, p8, p9;
    bool ready;
    uint8_t address;
} bmp280_t;
typedef struct {
    uint32_t period_ms;
    uint32_t deadline_ms;
    bool armed;
} env_sampling_t;
void env_sampling_start(env_sampling_t *sampling, uint32_t now_ms, uint32_t period_ms);
bool env_sampling_due(const env_sampling_t *sampling, uint32_t now_ms);
uint32_t env_sampling_wait_ms(const env_sampling_t *sampling, uint32_t now_ms);
void env_sampling_after_sample(env_sampling_t *sampling, uint32_t now_ms);
uint8_t sht4x_crc(const uint8_t *data, size_t size);
env_error_t sht45_sample(const env_io_t *io, uint8_t address, float *temperature_c,
                         float *humidity_pct);
env_error_t bmp280_prepare(const env_io_t *io, uint8_t address, bmp280_t *sensor);
env_error_t bmp280_sample(const env_io_t *io, bmp280_t *sensor, float *temperature_c,
                          float *pressure_pa);
const char *env_error_name(env_error_t error);
#endif
