#include "i2c_bus.h"
#include "i2c_bus_host_test.h"
#include "pal.h"
#include <stdlib.h>
#include <string.h>

typedef struct {
    uint8_t port;
    int sda_pin;
    int scl_pin;
} host_i2c_bus_t;

static bool owners[2];
static int pin_owners[49];
static unsigned active_transactions;
static unsigned max_inflight;
static unsigned transactions;
static uint32_t delay_ms;
static dcr_bus_status_t forced_status;
static pal_mutex_t *metrics_lock;

static void ensure_lock(void)
{
    if (!metrics_lock)
        metrics_lock = pal_mutex_create();
}

static uint8_t crc(const uint8_t *data, size_t length)
{
    uint8_t value = 0xff;
    for (size_t n = 0; n < length; ++n) {
        value ^= data[n];
        for (unsigned bit = 0; bit < 8; ++bit)
            value = (uint8_t)((value & 0x80) ? (value << 1) ^ 0x31 : value << 1);
    }
    return value;
}

static void begin_transaction(void)
{
    ensure_lock();
    pal_mutex_lock(metrics_lock);
    unsigned active = ++active_transactions;
    if (active > max_inflight)
        max_inflight = active;
    ++transactions;
    pal_mutex_unlock(metrics_lock);
}

static void end_transaction(void)
{
    pal_mutex_lock(metrics_lock);
    --active_transactions;
    pal_mutex_unlock(metrics_lock);
}

void dcr_i2c_host_test_reset(void)
{
    ensure_lock();
    pal_mutex_lock(metrics_lock);
    active_transactions = 0;
    max_inflight = 0;
    transactions = 0;
    delay_ms = 0;
    forced_status = DCR_BUS_STATUS_OK;
    pal_mutex_unlock(metrics_lock);
}

void dcr_i2c_host_test_set_delay(uint32_t value)
{
    ensure_lock();
    pal_mutex_lock(metrics_lock);
    delay_ms = value;
    pal_mutex_unlock(metrics_lock);
}

void dcr_i2c_host_test_set_status(dcr_bus_status_t status)
{
    ensure_lock();
    pal_mutex_lock(metrics_lock);
    forced_status = status;
    pal_mutex_unlock(metrics_lock);
}

unsigned dcr_i2c_host_test_max_inflight(void)
{
    ensure_lock();
    pal_mutex_lock(metrics_lock);
    unsigned value = max_inflight;
    pal_mutex_unlock(metrics_lock);
    return value;
}

unsigned dcr_i2c_host_test_transactions(void)
{
    ensure_lock();
    pal_mutex_lock(metrics_lock);
    unsigned value = transactions;
    pal_mutex_unlock(metrics_lock);
    return value;
}

dcr_err_t dcr_i2c_platform_open(const dcr_i2c_bus_config_t *config, void **handle)
{
    if (!config || !handle || config->port > 1 || config->sda_pin < 0 || config->sda_pin >= 49 ||
        config->scl_pin < 0 || config->scl_pin >= 49 || config->sda_pin == config->scl_pin)
        return DCR_ERR_ARGUMENT;
    ensure_lock();
    pal_mutex_lock(metrics_lock);
    int owner = (int)config->port + 1;
    bool already_owned = owners[config->port] || pin_owners[config->sda_pin] != 0 ||
                         pin_owners[config->scl_pin] != 0;
    if (!already_owned) {
        owners[config->port] = true;
        pin_owners[config->sda_pin] = owner;
        pin_owners[config->scl_pin] = owner;
    }
    pal_mutex_unlock(metrics_lock);
    if (already_owned)
        return DCR_ERR_INIT;
    host_i2c_bus_t *bus = calloc(1, sizeof(*bus));
    if (!bus) {
        pal_mutex_lock(metrics_lock);
        owners[config->port] = false;
        if (pin_owners[config->sda_pin] == owner)
            pin_owners[config->sda_pin] = 0;
        if (pin_owners[config->scl_pin] == owner)
            pin_owners[config->scl_pin] = 0;
        pal_mutex_unlock(metrics_lock);
        return DCR_ERR_OOM;
    }
    bus->port = config->port;
    bus->sda_pin = config->sda_pin;
    bus->scl_pin = config->scl_pin;
    *handle = bus;
    return DCR_OK;
}

void dcr_i2c_platform_close(void *handle)
{
    host_i2c_bus_t *bus = handle;
    if (bus) {
        ensure_lock();
        pal_mutex_lock(metrics_lock);
        owners[bus->port] = false;
        if (pin_owners[bus->sda_pin] == (int)bus->port + 1)
            pin_owners[bus->sda_pin] = 0;
        if (pin_owners[bus->scl_pin] == (int)bus->port + 1)
            pin_owners[bus->scl_pin] = 0;
        pal_mutex_unlock(metrics_lock);
        free(bus);
    }
}

static dcr_bus_status_t fake_sht(const dcr_bus_request_t *request, dcr_bus_response_t *response)
{
    if (request->address != 0x44 && request->address != 0x45)
        return DCR_BUS_STATUS_IO;
    if (request->operation == DCR_BUS_I2C_WRITE && request->length == 1 &&
        request->data[0] == 0xfd)
        return DCR_BUS_STATUS_OK;
    if (request->operation == DCR_BUS_I2C_READ && request->length == 6) {
        response->data[0] = 0x66;
        response->data[1] = 0x66;
        response->data[2] = crc(response->data, 2);
        response->data[3] = 0x80;
        response->data[4] = 0;
        response->data[5] = crc(response->data + 3, 2);
        response->length = 6;
        return DCR_BUS_STATUS_OK;
    }
    return DCR_BUS_STATUS_INVALID;
}

static dcr_bus_status_t fake_bmp(const dcr_bus_request_t *request, dcr_bus_response_t *response)
{
    if (request->address != 0x76 && request->address != 0x77)
        return DCR_BUS_STATUS_IO;
    if (request->operation == DCR_BUS_I2C_WRITE && request->length == 2 &&
        ((request->data[0] == 0xe0 && request->data[1] == 0xb6) ||
         (request->data[0] == 0xf4 && request->data[1] == 0x25)))
        return DCR_BUS_STATUS_OK;
    if (request->operation != DCR_BUS_I2C_REGISTER_READ)
        return DCR_BUS_STATUS_INVALID;
    if (request->register_address == 0xd0 && request->length == 1) {
        response->data[0] = 0x58;
        response->length = 1;
        return DCR_BUS_STATUS_OK;
    }
    if (request->register_address == 0xf3 && request->length == 1) {
        response->data[0] = 0;
        response->length = 1;
        return DCR_BUS_STATUS_OK;
    }
    if (request->register_address == 0x88 && request->length == 24) {
        const int coefficients[] = {27504, 26435, -1000, 36477, -10685, 3024,
                                    2855,  140,   -7,    15500, -14600, 6000};
        for (size_t n = 0; n < 12; ++n) {
            uint16_t value = (uint16_t)coefficients[n];
            response->data[n * 2] = (uint8_t)value;
            response->data[n * 2 + 1] = (uint8_t)(value >> 8);
        }
        response->length = 24;
        return DCR_BUS_STATUS_OK;
    }
    if (request->register_address == 0xf7 && request->length == 6) {
        const uint32_t raw[] = {415148, 519888};
        for (size_t n = 0; n < 2; ++n) {
            response->data[n * 3] = (uint8_t)(raw[n] >> 12);
            response->data[n * 3 + 1] = (uint8_t)(raw[n] >> 4);
            response->data[n * 3 + 2] = (uint8_t)(raw[n] << 4);
        }
        response->length = 6;
        return DCR_BUS_STATUS_OK;
    }
    return DCR_BUS_STATUS_INVALID;
}

dcr_bus_status_t dcr_i2c_platform_transact(void *handle, const dcr_bus_request_t *request,
                                            dcr_bus_response_t *response)
{
    host_i2c_bus_t *bus = handle;
    if (!bus || !request || !response)
        return DCR_BUS_STATUS_INVALID;
    begin_transaction();
    ensure_lock();
    pal_mutex_lock(metrics_lock);
    uint32_t delay = delay_ms;
    dcr_bus_status_t forced = forced_status;
    pal_mutex_unlock(metrics_lock);
    if (delay)
        pal_sleep(delay);
    dcr_bus_status_t status = forced != DCR_BUS_STATUS_OK
                                  ? forced
                                  : (bus->port == 0 ? fake_sht(request, response)
                                                    : fake_bmp(request, response));
    end_transaction();
    return status;
}
