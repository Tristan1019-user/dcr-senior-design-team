#ifndef DCR_I2C_BUS_H
#define DCR_I2C_BUS_H

#include "dcr.h"

typedef struct {
    uint8_t port;
    int sda_pin;
    int scl_pin;
    uint32_t frequency_hz;
} dcr_i2c_bus_config_t;

/* Platform implementations own the only physical bus handle. */
dcr_err_t dcr_i2c_platform_open(const dcr_i2c_bus_config_t *config, void **handle);
void dcr_i2c_platform_close(void *handle);
dcr_bus_status_t dcr_i2c_platform_transact(void *handle, const dcr_bus_request_t *request,
                                            dcr_bus_response_t *response);

extern const dcr_descriptor_t dcr_i2c_bus_descriptor;

#endif
