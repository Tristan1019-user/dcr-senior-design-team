#include "environment.h"
#include <math.h>
#include <string.h>

static bool deadline_reached(uint32_t now_ms, uint32_t deadline_ms)
{
    /* All supported periods are far below half the uint32 millisecond range. */
    return (uint32_t)(now_ms - deadline_ms) < UINT32_C(0x80000000);
}

void env_sampling_start(env_sampling_t *sampling, uint32_t now_ms, uint32_t period_ms)
{
    if (!sampling)
        return;
    sampling->period_ms = period_ms;
    sampling->deadline_ms = now_ms;
    sampling->armed = true;
}

bool env_sampling_due(const env_sampling_t *sampling, uint32_t now_ms)
{
    return sampling && sampling->armed && deadline_reached(now_ms, sampling->deadline_ms);
}

uint32_t env_sampling_wait_ms(const env_sampling_t *sampling, uint32_t now_ms)
{
    if (!sampling || !sampling->armed || env_sampling_due(sampling, now_ms))
        return 0;
    return sampling->deadline_ms - now_ms;
}

void env_sampling_after_sample(env_sampling_t *sampling, uint32_t now_ms)
{
    if (!sampling || !sampling->armed)
        return;
    uint32_t next_deadline = sampling->deadline_ms + sampling->period_ms;
    if (deadline_reached(now_ms, next_deadline))
        /* Skip missed periods so an overrun cannot create a catch-up burst. */
        sampling->deadline_ms = now_ms + sampling->period_ms;
    else
        sampling->deadline_ms = next_deadline;
}

uint8_t sht4x_crc(const uint8_t *data, size_t size)
{
    uint8_t crc = 0xff;
    for (size_t n = 0; n < size; ++n) {
        crc ^= data[n];
        for (unsigned bit = 0; bit < 8; ++bit)
            crc = (uint8_t)((crc & 0x80) ? (crc << 1) ^ 0x31 : crc << 1);
    }
    return crc;
}

env_error_t sht45_sample(const env_io_t *io, uint8_t address, float *t, float *h)
{
    const uint8_t command = 0xfd; /* High precision, heater disabled. */
    uint8_t bytes[6];
    if (address != 0x44 && address != 0x45)
        return ENV_ID;
    if (!io->write(io->ctx, address, &command, 1))
        return ENV_IO;
    io->delay_ms(io->ctx, 10);
    if (!io->read(io->ctx, address, bytes, sizeof(bytes)))
        return ENV_IO;
    if (sht4x_crc(bytes, 2) != bytes[2] || sht4x_crc(bytes + 3, 2) != bytes[5])
        return ENV_CRC;
    *t = -45.0f + 175.0f * (float)((bytes[0] << 8) | bytes[1]) / 65535.0f;
    float humidity = -6.0f + 125.0f * (float)((bytes[3] << 8) | bytes[4]) / 65535.0f;
    *h = humidity < 0 ? 0 : humidity > 100 ? 100 : humidity;
    return ENV_OK;
}

static uint16_t u16(const uint8_t *p)
{
    return (uint16_t)(p[0] | (p[1] << 8));
}
static int16_t s16(const uint8_t *p)
{
    uint16_t value = u16(p);
    return (int16_t)(value < 32768 ? (int32_t)value : (int32_t)value - 65536);
}
static env_error_t wait_ready(const env_io_t *io, uint8_t address)
{
    for (unsigned n = 0; n < 20; ++n) {
        uint8_t status;
        if (!io->register_read(io->ctx, address, 0xf3, &status, 1))
            return ENV_IO;
        if (!(status & 9))
            return ENV_OK;
        io->delay_ms(io->ctx, 2);
    }
    return ENV_TIMEOUT;
}
env_error_t bmp280_prepare(const env_io_t *io, uint8_t address, bmp280_t *b)
{
    memset(b, 0, sizeof(*b));
    b->address = address;
    if (address != 0x76 && address != 0x77)
        return ENV_ID;
    uint8_t id, bytes[24];
    if (!io->register_read(io->ctx, address, 0xd0, &id, 1))
        return ENV_IO;
    if (id != 0x58)
        return ENV_ID; /* Reject BME280 (0x60), never mislabel it. */
    const uint8_t reset[] = {0xe0, 0xb6};
    if (!io->write(io->ctx, address, reset, sizeof(reset)))
        return ENV_IO;
    io->delay_ms(io->ctx, 5);
    env_error_t e = wait_ready(io, address);
    if (e != ENV_OK)
        return e;
    if (!io->register_read(io->ctx, address, 0x88, bytes, sizeof(bytes)))
        return ENV_IO;
    bool all_ff = true;
    for (size_t n = 0; n < sizeof(bytes); ++n)
        all_ff = all_ff && bytes[n] == 0xff;
    b->t1 = u16(bytes);
    b->t2 = s16(bytes + 2);
    b->t3 = s16(bytes + 4);
    b->p1 = u16(bytes + 6);
    b->p2 = s16(bytes + 8);
    b->p3 = s16(bytes + 10);
    b->p4 = s16(bytes + 12);
    b->p5 = s16(bytes + 14);
    b->p6 = s16(bytes + 16);
    b->p7 = s16(bytes + 18);
    b->p8 = s16(bytes + 20);
    b->p9 = s16(bytes + 22);
    if (all_ff || !b->t1 || !b->p1)
        return ENV_CALIBRATION;
    b->ready = true;
    return ENV_OK;
}

env_error_t bmp280_sample(const env_io_t *io, bmp280_t *b, float *t, float *p)
{
    if (!b->ready)
        return ENV_CALIBRATION;
    const uint8_t forced[] = {0xf4, 0x25}; /* Temperature x1, pressure x1, forced mode. */
    if (!io->write(io->ctx, b->address, forced, sizeof(forced)))
        return ENV_IO;
    io->delay_ms(io->ctx, 10); /* Exceeds 6.4 ms maximum at x1/x1. */
    env_error_t e = wait_ready(io, b->address);
    if (e != ENV_OK)
        return e;
    uint8_t bytes[6];
    if (!io->register_read(io->ctx, b->address, 0xf7, bytes, sizeof(bytes)))
        return ENV_IO;
    uint32_t raw_p = ((uint32_t)bytes[0] << 12) | ((uint32_t)bytes[1] << 4) | (bytes[2] >> 4);
    uint32_t raw_t = ((uint32_t)bytes[3] << 12) | ((uint32_t)bytes[4] << 4) | (bytes[5] >> 4);
    if (raw_p == 0x80000 || raw_t == 0x80000)
        return ENV_RANGE;
    /* Bosch compensation equations, DS001 revision 1.26 section 8.1.
     * Double intermediates avoid signed-shift/overflow traps in integer variants. */
    double dt = raw_t / 16384.0 - b->t1 / 1024.0;
    double residual = raw_t / 131072.0 - b->t1 / 8192.0;
    double fine = dt * b->t2 + residual * residual * b->t3;
    double temperature = fine / 5120.0;
    double x = fine / 2.0 - 64000.0;
    double offset = (x * x * b->p6 / 32768.0 + x * b->p5 * 2.0) / 4.0 + b->p4 * 65536.0;
    double gain = (1.0 + (b->p3 * x * x / 524288.0 + b->p2 * x) / 524288.0 / 32768.0) * b->p1;
    if (gain == 0)
        return ENV_CALIBRATION;
    double pressure = (1048576.0 - raw_p - offset / 4096.0) * 6250.0 / gain;
    pressure +=
        (b->p9 * pressure * pressure / 2147483648.0 + b->p8 * pressure / 32768.0 + b->p7) / 16.0;
    if (!isfinite(temperature) || !isfinite(pressure) || temperature < -40 || temperature > 85 ||
        pressure < 30000 || pressure > 110000)
        return ENV_RANGE;
    *t = (float)temperature;
    *p = (float)pressure;
    return ENV_OK;
}
const char *env_error_name(env_error_t e)
{
    static const char *const names[] = {"ok",          "i2c_io",  "crc",  "wrong_chip_id",
                                        "calibration", "timeout", "range"};
    return (unsigned)e < sizeof(names) / sizeof(names[0]) ? names[e] : "unknown";
}
