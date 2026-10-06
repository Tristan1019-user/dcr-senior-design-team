#include "i2c_bus.h"
#include "driver/i2c_master.h"
#include "esp_err.h"
#include <string.h>

typedef struct {
    i2c_master_bus_handle_t bus;
    uint32_t frequency_hz;
    uint8_t port;
    int sda_pin;
    int scl_pin;
    i2c_master_dev_handle_t devices[16];
    uint8_t addresses[16];
    uint8_t device_count;
} esp_i2c_bus_t;

/* IDF reserves the controller, but GPIO matrix routing does not by itself
 * prevent two controllers from claiming the same physical wire. Keep this
 * control-plane reservation symmetric with the host backend. */
static bool controller_owned[2];
static int pin_owners[49];

dcr_err_t dcr_i2c_platform_open(const dcr_i2c_bus_config_t *config, void **handle)
{
    if (!config || !handle || config->port > 1 || config->sda_pin < 0 ||
        config->sda_pin >= 49 || config->scl_pin < 0 || config->scl_pin >= 49 ||
        config->sda_pin == config->scl_pin)
        return DCR_ERR_ARGUMENT;
    int owner = (int)config->port + 1;
    if (controller_owned[config->port] || pin_owners[config->sda_pin] != 0 ||
        pin_owners[config->scl_pin] != 0)
        return DCR_ERR_INIT;
    controller_owned[config->port] = true;
    pin_owners[config->sda_pin] = owner;
    pin_owners[config->scl_pin] = owner;
    esp_i2c_bus_t *bus = pal_malloc(sizeof(*bus));
    if (!bus) {
        controller_owned[config->port] = false;
        pin_owners[config->sda_pin] = 0;
        pin_owners[config->scl_pin] = 0;
        return DCR_ERR_OOM;
    }
    bus->port = config->port;
    bus->sda_pin = config->sda_pin;
    bus->scl_pin = config->scl_pin;
    i2c_master_bus_config_t settings = {
        .i2c_port = config->port ? I2C_NUM_1 : I2C_NUM_0,
        .sda_io_num = config->sda_pin,
        .scl_io_num = config->scl_pin,
        .clk_source = I2C_CLK_SRC_DEFAULT,
        .glitch_ignore_cnt = 7,
        .flags.enable_internal_pullup = false,
    };
    if (i2c_new_master_bus(&settings, &bus->bus) != ESP_OK) {
        controller_owned[config->port] = false;
        pin_owners[config->sda_pin] = 0;
        pin_owners[config->scl_pin] = 0;
        pal_free(bus);
        return DCR_ERR_INIT;
    }
    bus->frequency_hz = config->frequency_hz;
    *handle = bus;
    return DCR_OK;
}

void dcr_i2c_platform_close(void *handle)
{
    esp_i2c_bus_t *bus = handle;
    if (!bus)
        return;
    for (uint8_t n = 0; n < bus->device_count; ++n)
        i2c_master_bus_rm_device(bus->devices[n]);
    i2c_del_master_bus(bus->bus);
    controller_owned[bus->port] = false;
    if (pin_owners[bus->sda_pin] == (int)bus->port + 1)
        pin_owners[bus->sda_pin] = 0;
    if (pin_owners[bus->scl_pin] == (int)bus->port + 1)
        pin_owners[bus->scl_pin] = 0;
    pal_free(bus);
}

static i2c_master_dev_handle_t device(esp_i2c_bus_t *bus, uint8_t address)
{
    for (uint8_t n = 0; n < bus->device_count; ++n)
        if (bus->addresses[n] == address)
            return bus->devices[n];
    if (bus->device_count == sizeof(bus->devices) / sizeof(bus->devices[0]))
        return NULL;
    i2c_device_config_t config = {.dev_addr_length = I2C_ADDR_BIT_LEN_7,
                                   .device_address = address,
                                   .scl_speed_hz = bus->frequency_hz};
    i2c_master_dev_handle_t handle = NULL;
    if (i2c_master_bus_add_device(bus->bus, &config, &handle) != ESP_OK)
        return NULL;
    bus->addresses[bus->device_count] = address;
    bus->devices[bus->device_count++] = handle;
    return handle;
}

dcr_bus_status_t dcr_i2c_platform_transact(void *handle, const dcr_bus_request_t *request,
                                            dcr_bus_response_t *response)
{
    esp_i2c_bus_t *bus = handle;
    if (!bus || !request || !response || request->address > 0x7f || !request->length ||
        request->length > DCR_BUS_MAX_DATA)
        return DCR_BUS_STATUS_INVALID;
    i2c_master_dev_handle_t target = device(bus, request->address);
    if (!target)
        return DCR_BUS_STATUS_IO;
    esp_err_t error;
    switch (request->operation) {
    case DCR_BUS_I2C_WRITE:
        error = i2c_master_transmit(target, request->data, request->length, 50);
        break;
    case DCR_BUS_I2C_READ:
        error = i2c_master_receive(target, response->data, request->length, 50);
        if (error == ESP_OK)
            response->length = request->length;
        break;
    case DCR_BUS_I2C_REGISTER_READ: {
        uint8_t reg = request->register_address;
        error = i2c_master_transmit_receive(target, &reg, 1, response->data, request->length, 50);
        if (error == ESP_OK)
            response->length = request->length;
        break;
    }
    default:
        return DCR_BUS_STATUS_INVALID;
    }
    if (error == ESP_OK)
        return DCR_BUS_STATUS_OK;
    return error == ESP_ERR_TIMEOUT ? DCR_BUS_STATUS_TIMEOUT : DCR_BUS_STATUS_IO;
}
