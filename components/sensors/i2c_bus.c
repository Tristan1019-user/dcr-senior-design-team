#include "i2c_bus.h"
#include <math.h>
#include <string.h>

typedef struct {
    void *platform_handle;
} i2c_bus_state_t;

static bool integer(const cJSON *config, const char *key, double low, double high, int *out)
{
    const cJSON *value = cJSON_GetObjectItemCaseSensitive(config, key);
    if (!cJSON_IsNumber(value) || !isfinite(value->valuedouble) || value->valuedouble < low ||
        value->valuedouble > high || floor(value->valuedouble) != value->valuedouble)
        return false;
    *out = (int)value->valuedouble;
    return true;
}

static dcr_err_t init_bus(const cJSON *config, void **data)
{
    int port, sda, scl, frequency;
    if (!config || !data || !integer(config, "port", 0, 1, &port) ||
        !integer(config, "sda_pin", 0, 48, &sda) ||
        !integer(config, "scl_pin", 0, 48, &scl) ||
        !integer(config, "freq_hz", 10000, 1000000, &frequency) || sda == scl)
        return DCR_ERR_INIT;
    dcr_i2c_bus_config_t bus = {(uint8_t)port, sda, scl, (uint32_t)frequency};
    i2c_bus_state_t *state = pal_malloc(sizeof(*state));
    if (!state)
        return DCR_ERR_OOM;
    dcr_err_t error = dcr_i2c_platform_open(&bus, &state->platform_handle);
    if (error) {
        pal_free(state);
        return error;
    }
    *data = state;
    return DCR_OK;
}

static void stop_bus(void *data)
{
    i2c_bus_state_t *state = data;
    if (!state)
        return;
    dcr_i2c_platform_close(state->platform_handle);
    pal_free(state);
}

static bool valid_request(const dcr_bus_request_t *request)
{
    if (!request || !request->response_queue || request->address > 0x7f || !request->length ||
        request->length > DCR_BUS_MAX_DATA)
        return false;
    return request->operation == DCR_BUS_I2C_WRITE || request->operation == DCR_BUS_I2C_READ ||
           request->operation == DCR_BUS_I2C_REGISTER_READ;
}

static void publish_status(dcr_port_t *status_port, dcr_bus_status_t status)
{
    dcr_value_t value = {.u32 = (uint32_t)status};
    dcr_port_write(status_port, &value, 0);
}

static void run_bus(void *data, dcr_instance_t *instance)
{
    i2c_bus_state_t *state = data;
    dcr_port_t *request_port = dcr_port(instance, "request");
    dcr_port_t *status_port = dcr_port(instance, "status");
    dcr_bus_status_t status = DCR_BUS_STATUS_OK;
    publish_status(status_port, status);
    while (!dcr_should_stop(instance)) {
        dcr_value_t value = {0};
        dcr_err_t wait = dcr_port_wait(request_port, &value, 250);
        if (wait == DCR_ERR_TIMEOUT) {
            dcr_heartbeat(instance);
            publish_status(status_port, status);
            continue;
        }
        if (wait != DCR_OK)
            break;
        dcr_heartbeat(instance);
        dcr_bus_response_t response = {.status = DCR_BUS_STATUS_INVALID,
                                       .transaction_id = value.bus_request.transaction_id};
        if (valid_request(&value.bus_request))
            response.status = dcr_i2c_platform_transact(state->platform_handle, &value.bus_request,
                                                         &response);
        status = response.status;
        publish_status(status_port, status);
        dcr_response_queue_send(value.bus_request.response_queue, &response, 0);
        dcr_bus_request_release(&value.bus_request);
        dcr_heartbeat(instance);
    }
    dcr_value_t discarded;
    while (dcr_port_read(request_port, &discarded) == DCR_OK)
        dcr_bus_request_release(&discarded.bus_request);
}

static const dcr_port_decl_t ports[] = {
    {"request", DCR_INPUT, DCR_BUS_REQUEST, DCR_QUEUE, true, true},
    {"status", DCR_OUTPUT, DCR_UINT32, DCR_SIGNAL, false, false}};

const dcr_descriptor_t dcr_i2c_bus_descriptor = {"i2c_bus", ports, 2, 4096, 7,
                                                  init_bus, run_bus, stop_bus, NULL};
