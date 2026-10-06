#include "environment.h"
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
static unsigned checks;
#define CHECK(x)                                                                                   \
    do {                                                                                           \
        ++checks;                                                                                  \
        if (!(x)) {                                                                                \
            fprintf(stderr, "FAIL line %d: %s\n", __LINE__, #x);                                   \
            exit(1);                                                                               \
        }                                                                                          \
    } while (0)
typedef struct {
    uint8_t frame[6], calib[24], raw[6], id, status;
    bool fail_write, fail_read, fail_reg;
    unsigned delay, writes;
} fake_t;
static bool write_fake(void *ctx, uint8_t addr, const uint8_t *bytes, size_t n)
{
    fake_t *f = ctx;
    CHECK((addr == 0x44 || addr == 0x45) ? n == 1 && bytes[0] == 0xfd
                                         : n == 2 && ((bytes[0] == 0xe0 && bytes[1] == 0xb6) ||
                                                      (bytes[0] == 0xf4 && bytes[1] == 0x25)));
    ++f->writes;
    return !f->fail_write;
}
static bool read_fake(void *ctx, uint8_t addr, uint8_t *bytes, size_t n)
{
    fake_t *f = ctx;
    CHECK((addr == 0x44 || addr == 0x45) && n == 6 && f->delay >= 10);
    memcpy(bytes, f->frame, n);
    return !f->fail_read;
}
static bool reg_fake(void *ctx, uint8_t addr, uint8_t reg, uint8_t *bytes, size_t n)
{
    fake_t *f = ctx;
    CHECK(addr == 0x76 || addr == 0x77);
    if (f->fail_reg)
        return false;
    switch (reg) {
    case 0xd0:
        CHECK(n == 1);
        bytes[0] = f->id;
        break;
    case 0xf3:
        CHECK(n == 1);
        bytes[0] = f->status;
        break;
    case 0x88:
        CHECK(n == 24);
        memcpy(bytes, f->calib, n);
        break;
    case 0xf7:
        CHECK(n == 6 && f->delay >= 15);
        memcpy(bytes, f->raw, n);
        break;
    default:
        CHECK(false);
    }
    return true;
}
static void delay_fake(void *ctx, uint32_t ms)
{
    ((fake_t *)ctx)->delay += ms;
}
static void setup(fake_t *f)
{
    memset(f, 0, sizeof(*f));
    f->id = 0x58;
    const int coefficients[] = {27504, 26435, -1000, 36477, -10685, 3024,
                                2855,  140,   -7,    15500, -14600, 6000};
    for (size_t n = 0; n < 12; ++n) {
        uint16_t v = (uint16_t)coefficients[n];
        f->calib[n * 2] = (uint8_t)v;
        f->calib[n * 2 + 1] = (uint8_t)(v >> 8);
    }
    uint32_t raw[] = {415148, 519888};
    for (size_t n = 0; n < 2; ++n) {
        f->raw[n * 3] = (uint8_t)(raw[n] >> 12);
        f->raw[n * 3 + 1] = (uint8_t)(raw[n] >> 4);
        f->raw[n * 3 + 2] = (uint8_t)(raw[n] << 4);
    }
    f->frame[0] = 0x66;
    f->frame[1] = 0x66;
    f->frame[3] = 0x80;
    f->frame[4] = 0;
    f->frame[2] = sht4x_crc(f->frame, 2);
    f->frame[5] = sht4x_crc(f->frame + 3, 2);
}
static void test_sampling(void)
{
    env_sampling_t sampling;
    env_sampling_start(&sampling, 1000, 100);
    CHECK(env_sampling_due(&sampling, 1000));
    env_sampling_after_sample(&sampling, 1020);
    CHECK(!env_sampling_due(&sampling, 1099));
    CHECK(env_sampling_wait_ms(&sampling, 1099) == 1);
    CHECK(env_sampling_due(&sampling, 1100));
    env_sampling_after_sample(&sampling, 1120);
    CHECK(env_sampling_wait_ms(&sampling, 1120) == 80);
    env_sampling_after_sample(&sampling, 1300);
    CHECK(!env_sampling_due(&sampling, 1300));
    CHECK(env_sampling_wait_ms(&sampling, 1300) == 100);

    env_sampling_start(&sampling, UINT32_MAX - 20, 100);
    env_sampling_after_sample(&sampling, UINT32_MAX - 10);
    CHECK(!env_sampling_due(&sampling, 20));
    CHECK(env_sampling_wait_ms(&sampling, 20) == 59);
    CHECK(env_sampling_due(&sampling, 79));
    env_sampling_after_sample(&sampling, 200);
    CHECK(!env_sampling_due(&sampling, 200));
    CHECK(env_sampling_wait_ms(&sampling, 200) == 100);
}
int main(void)
{
    test_sampling();
    fake_t f;
    setup(&f);
    env_io_t io = {&f, write_fake, read_fake, reg_fake, delay_fake};
    const uint8_t known_crc[] = {0xbe, 0xef};
    CHECK(sht4x_crc(known_crc, 2) == 0x92);
    float t = -999, h = -999, p = -999;
    CHECK(sht45_sample(&io, 0x44, &t, &h) == ENV_OK);
    CHECK(fabs(t - 25) < 0.01 && fabs(h - 56.501) < 0.01);
    CHECK(sht45_sample(&io, 0x45, &t, &h) == ENV_OK);
    CHECK(sht45_sample(&io, 0x46, &t, &h) == ENV_ID);
    f.frame[2] ^= 1;
    t = -999;
    CHECK(sht45_sample(&io, 0x44, &t, &h) == ENV_CRC && t == -999);
    setup(&f);
    f.frame[5] ^= 1;
    CHECK(sht45_sample(&io, 0x44, &t, &h) == ENV_CRC);
    setup(&f);
    f.fail_write = true;
    CHECK(sht45_sample(&io, 0x44, &t, &h) == ENV_IO);
    setup(&f);
    f.fail_read = true;
    CHECK(sht45_sample(&io, 0x44, &t, &h) == ENV_IO);
    setup(&f);
    f.frame[3] = f.frame[4] = 0;
    f.frame[5] = sht4x_crc(f.frame + 3, 2);
    CHECK(sht45_sample(&io, 0x44, &t, &h) == ENV_OK && h == 0);
    f.frame[3] = f.frame[4] = 0xff;
    f.frame[5] = sht4x_crc(f.frame + 3, 2);
    CHECK(sht45_sample(&io, 0x44, &t, &h) == ENV_OK && h == 100);
    bmp280_t b;
    setup(&f);
    CHECK(bmp280_prepare(&io, 0x76, &b) == ENV_OK && b.ready);
    CHECK(bmp280_sample(&io, &b, &t, &p) == ENV_OK);
    CHECK(fabs(t - 25.082) < 0.01 && fabs(p - 100653.27) < 0.2);
    CHECK(b.p2 == -10685 && b.t3 == -1000);
    CHECK(bmp280_prepare(&io, 0x77, &b) == ENV_OK);
    CHECK(bmp280_prepare(&io, 0x75, &b) == ENV_ID && !b.ready);
    f.id = 0x60;
    CHECK(bmp280_prepare(&io, 0x76, &b) == ENV_ID && !b.ready);
    setup(&f);
    f.fail_reg = true;
    CHECK(bmp280_prepare(&io, 0x76, &b) == ENV_IO);
    setup(&f);
    f.fail_write = true;
    CHECK(bmp280_prepare(&io, 0x76, &b) == ENV_IO);
    setup(&f);
    f.status = 1;
    CHECK(bmp280_prepare(&io, 0x76, &b) == ENV_TIMEOUT && f.delay == 45);
    setup(&f);
    memset(f.calib, 0, 24);
    CHECK(bmp280_prepare(&io, 0x76, &b) == ENV_CALIBRATION);
    setup(&f);
    memset(f.calib, 0xff, 24);
    CHECK(bmp280_prepare(&io, 0x76, &b) == ENV_CALIBRATION);
    setup(&f);
    CHECK(bmp280_prepare(&io, 0x76, &b) == ENV_OK);
    f.status = 8;
    CHECK(bmp280_sample(&io, &b, &t, &p) == ENV_TIMEOUT);
    f.status = 0;
    f.fail_write = true;
    CHECK(bmp280_sample(&io, &b, &t, &p) == ENV_IO);
    f.fail_write = false;
    f.fail_reg = true;
    CHECK(bmp280_sample(&io, &b, &t, &p) == ENV_IO);
    f.fail_reg = false;
    f.raw[0] = 0x80;
    f.raw[1] = f.raw[2] = 0;
    p = -999;
    CHECK(bmp280_sample(&io, &b, &t, &p) == ENV_RANGE && p == -999);
    b.ready = false;
    CHECK(bmp280_sample(&io, &b, &t, &p) == ENV_CALIBRATION);
    printf("SENSOR_PROTOCOL_TESTS PASS checks=%u\n", checks);
    return 0;
}
