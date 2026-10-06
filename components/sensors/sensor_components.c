#include "sensor_components.h"
#include "environment.h"
#include <math.h>
#include <string.h>

typedef struct {
    uint8_t addresses[2], selected;
    bool bmp;
    uint32_t period_ms, successes, errors;
    env_error_t last_error;
    bmp280_t calibration;
    dcr_instance_t *instance;
    dcr_port_t *bus_port;
    dcr_response_queue_t *responses;
    uint32_t transaction_id;
} sensor_state_t;

static bool wait_response(sensor_state_t *s, uint32_t transaction_id, dcr_bus_response_t *response)
{
    uint32_t start = pal_millis();
    for (;;) {
        uint32_t elapsed = pal_millis() - start;
        if (elapsed >= 100)
            return false;
        dcr_bus_response_t candidate;
        if (!dcr_response_queue_receive(s->responses, &candidate, 100 - elapsed))
            return false;
        if (candidate.transaction_id == transaction_id) {
            *response = candidate;
            return true;
        }
        /* A response from a timed-out request can arrive late. It is never
         * allowed to satisfy the next transaction. */
    }
}

static bool transact(sensor_state_t *s, dcr_bus_request_t *request, dcr_bus_response_t *response)
{
    while (dcr_response_queue_receive(s->responses, response, 0)) {
    }
    request->response_queue = s->responses;
    request->transaction_id = ++s->transaction_id;
    dcr_value_t value = {.bus_request = *request};
    if (dcr_port_write(s->bus_port, &value, 100) != DCR_OK)
        return false;
    return wait_response(s, request->transaction_id, response) &&
           response->status == DCR_BUS_STATUS_OK;
}

static bool write_bytes(void *ctx, uint8_t addr, const uint8_t *data, size_t n)
{
    sensor_state_t *s = ctx;
    if (n > DCR_BUS_MAX_DATA)
        return false;
    dcr_bus_request_t request = {.operation = DCR_BUS_I2C_WRITE, .address = addr,
                                 .length = (uint8_t)n};
    memcpy(request.data, data, n);
    dcr_bus_response_t response;
    return transact(s, &request, &response);
}

static bool read_bytes(void *ctx, uint8_t addr, uint8_t *data, size_t n)
{
    sensor_state_t *s = ctx;
    if (n > DCR_BUS_MAX_DATA)
        return false;
    dcr_bus_request_t request = {.operation = DCR_BUS_I2C_READ, .address = addr,
                                 .length = (uint8_t)n};
    dcr_bus_response_t response;
    if (!transact(s, &request, &response) || response.length != n)
        return false;
    memcpy(data, response.data, n);
    return true;
}

static bool register_read(void *ctx, uint8_t addr, uint8_t reg, uint8_t *data, size_t n)
{
    sensor_state_t *s = ctx;
    if (n > DCR_BUS_MAX_DATA)
        return false;
    dcr_bus_request_t request = {.operation = DCR_BUS_I2C_REGISTER_READ,
                                 .address = addr,
                                 .register_address = reg,
                                 .length = (uint8_t)n};
    dcr_bus_response_t response;
    if (!transact(s, &request, &response) || response.length != n)
        return false;
    memcpy(data, response.data, n);
    return true;
}
static void delay_ms(void *ctx, uint32_t ms)
{
    sensor_state_t *s = ctx;
    dcr_sleep(s->instance, ms);
}
static void stop_sensor(void *data)
{
    sensor_state_t *s = data;
    if (!s)
        return;
    dcr_response_queue_destroy(s->responses);
    pal_free(s);
}
static bool integer(const cJSON *config, const char *key, int lo, int hi, int *out)
{
    const cJSON *v = cJSON_GetObjectItemCaseSensitive(config, key);
    if (!cJSON_IsNumber(v) || v->valuedouble < lo || v->valuedouble > hi ||
        floor(v->valuedouble) != v->valuedouble)
        return false;
    *out = (int)v->valuedouble;
    return true;
}
static dcr_err_t init_sensor(const cJSON *config, void **data, bool bmp)
{
    int address, period;
    if (!integer(config, "address", bmp ? 0x76 : 0x44, bmp ? 0x77 : 0x45, &address) ||
        !integer(config, "period_ms", 250, 2000, &period))
        return DCR_ERR_INIT;
    sensor_state_t *s = pal_malloc(sizeof(*s));
    if (!s)
        return DCR_ERR_OOM;
    s->bmp = bmp;
    s->period_ms = (uint32_t)period;
    s->addresses[0] = (uint8_t)address;
    s->addresses[1] = (uint8_t)(bmp ? (address == 0x76 ? 0x77 : 0x76)
                                    : (address == 0x44 ? 0x45 : 0x44));
    s->last_error = ENV_OK;
    s->responses = dcr_response_queue_create(DCR_BUS_RESPONSE_DEPTH);
    if (!s->responses) {
        pal_free(s);
        *data = NULL;
        return DCR_ERR_OOM;
    }
    *data = s;
    return DCR_OK;
}
static dcr_err_t init_sht(const cJSON *c, void **d)
{
    return init_sensor(c, d, false);
}
static dcr_err_t init_bmp(const cJSON *c, void **d)
{
    return init_sensor(c, d, true);
}
static void run_sensor(void *data, dcr_instance_t *instance)
{
    sensor_state_t *s = data;
    s->instance = instance;
    s->bus_port = dcr_port(instance, "bus");
    env_io_t io = {s, write_bytes, read_bytes, register_read, delay_ms};
    env_sampling_t sampling;
    env_sampling_start(&sampling, pal_millis(), s->period_ms);
    dcr_port_t *temperature = dcr_port(instance, "temperature_c");
    dcr_port_t *second = dcr_port(instance, s->bmp ? "pressure_pa" : "humidity_pct");
    dcr_port_t *status = dcr_port(instance, "status");
    while (!dcr_should_stop(instance)) {
        uint32_t wait_ms = env_sampling_wait_ms(&sampling, pal_millis());
        if (wait_ms) {
            dcr_sleep(instance, wait_ms);
            continue;
        }
        float t = 0, value = 0;
        env_error_t error = ENV_IO;
        if (!s->selected) {
            for (unsigned n = 0; n < 2; ++n) {
                uint8_t address = s->addresses[n];
                error = s->bmp ? bmp280_prepare(&io, address, &s->calibration)
                               : sht45_sample(&io, address, &t, &value);
                if (error == ENV_OK) {
                    s->selected = address;
                    dcr_log(instance, "info", "sensor detected address=0x%02x", address);
                    break;
                }
            }
        }
        if (s->selected)
            error = s->bmp ? bmp280_sample(&io, &s->calibration, &t, &value)
                           : sht45_sample(&io, s->selected, &t, &value);
        if (dcr_should_stop(instance))
            break;
        if (error == ENV_OK) {
            dcr_value_t a = {.real = t}, b = {.real = value};
            dcr_err_t ea = dcr_port_write(temperature, &a, 50);
            dcr_err_t eb = dcr_port_write(second, &b, 50);
            if (ea || eb)
                dcr_log(instance, "warn", "output backpressure; t=%d second=%d", ea, eb);
            ++s->successes;
            dcr_log(instance, "info", "sample=%u temperature_c=%.3f %s=%.3f",
                    (unsigned)s->successes, (double)t, s->bmp ? "pressure_pa" : "humidity_pct",
                    (double)value);
        } else {
            ++s->errors;
            s->selected = 0;
            s->calibration.ready = false;
            if (error != s->last_error || s->errors % 10 == 0)
                dcr_log(instance, "warn", "sensor error=%s count=%u; no new numeric reading",
                        env_error_name(error), (unsigned)s->errors);
        }
        s->last_error = error;
        dcr_value_t state = {.u32 = (uint32_t)error};
        if (dcr_port_write(status, &state, 50) != DCR_OK)
            dcr_log(instance, "warn", "status output backpressure");
        env_sampling_after_sample(&sampling, pal_millis());
    }
    dcr_log(instance, "info", "sensor stopped samples=%u errors=%u", (unsigned)s->successes,
            (unsigned)s->errors);
}
static const dcr_port_decl_t sht_ports[] = {
    {"bus", DCR_OUTPUT, DCR_BUS_REQUEST, DCR_QUEUE, true, false},
    {"temperature_c", DCR_OUTPUT, DCR_FLOAT, DCR_SIGNAL, false, false},
    {"humidity_pct", DCR_OUTPUT, DCR_FLOAT, DCR_SIGNAL, false, false},
    {"status", DCR_OUTPUT, DCR_UINT32, DCR_SIGNAL, false, false}};
static const dcr_port_decl_t bmp_ports[] = {
    {"bus", DCR_OUTPUT, DCR_BUS_REQUEST, DCR_QUEUE, true, false},
    {"temperature_c", DCR_OUTPUT, DCR_FLOAT, DCR_SIGNAL, false, false},
    {"pressure_pa", DCR_OUTPUT, DCR_FLOAT, DCR_SIGNAL, false, false},
    {"status", DCR_OUTPUT, DCR_UINT32, DCR_SIGNAL, false, false}};
const dcr_descriptor_t dcr_sht45_descriptor = {"sht45_sensor", sht_ports,  4,           4096, 5,
                                               init_sht,       run_sensor, stop_sensor, NULL};
const dcr_descriptor_t dcr_bmp280_descriptor = {"bmp280_sensor", bmp_ports,  4,           4096, 5,
                                                init_bmp,        run_sensor, stop_sensor, NULL};
