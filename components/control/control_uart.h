#ifndef DCR_CONTROL_UART_H
#define DCR_CONTROL_UART_H

#include "dcr.h"

/* ESP-IDF 5.5.3 UART0 transport. This function owns the control loop forever. */
void dcr_control_uart_run(dcr_runtime_t *runtime, const char *boot_id);

#endif
