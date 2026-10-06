#ifndef DCR_I2C_BUS_HOST_TEST_H
#define DCR_I2C_BUS_HOST_TEST_H

#include "dcr.h"

void dcr_i2c_host_test_reset(void);
void dcr_i2c_host_test_set_delay(uint32_t delay_ms);
void dcr_i2c_host_test_set_status(dcr_bus_status_t status);
unsigned dcr_i2c_host_test_max_inflight(void);
unsigned dcr_i2c_host_test_transactions(void);

#endif
