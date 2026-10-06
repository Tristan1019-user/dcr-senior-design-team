#include "control_uart.h"
#include "control.h"
#include "driver/uart.h"
#include "freertos/FreeRTOS.h"
#include <string.h>

#define CONTROL_UART UART_NUM_0
#define CONTROL_UART_BAUD 115200
#define CONTROL_UART_RX_BUFFER 8192
#define CONTROL_UART_READ_CHUNK 128

static bool uart_writer(void *context, const char *line, size_t length)
{
    (void)context;
    return uart_write_bytes(CONTROL_UART, line, length) == (int)length;
}

void dcr_control_uart_run(dcr_runtime_t *runtime, const char *boot_id)
{
    uart_config_t config = {.baud_rate = CONTROL_UART_BAUD,
                             .data_bits = UART_DATA_8_BITS,
                             .parity = UART_PARITY_DISABLE,
                             .stop_bits = UART_STOP_BITS_1,
                             .flow_ctrl = UART_HW_FLOWCTRL_DISABLE,
                             .source_clk = UART_SCLK_DEFAULT};
    if (uart_driver_install(CONTROL_UART, CONTROL_UART_RX_BUFFER, 0, 0, NULL, 0) != ESP_OK ||
        uart_param_config(CONTROL_UART, &config) != ESP_OK)
        return;

    dcr_control_t *control = dcr_control_create(runtime, boot_id, uart_writer, NULL);
    char *line = pal_malloc(DCR_CONTROL_MAX_FRAME + 1U);
    if (!control || !line) {
        dcr_control_destroy(control);
        pal_free(line);
        return;
    }
    uint8_t input[CONTROL_UART_READ_CHUNK];
    size_t used = 0;
    bool oversized = false;
    for (;;) {
        int count = uart_read_bytes(CONTROL_UART, input, sizeof(input), pdMS_TO_TICKS(50));
        for (int n = 0; n < count; ++n) {
            uint8_t byte = input[n];
            if (byte == '\n') {
                if (oversized)
                    dcr_control_handle_line(control, NULL, DCR_CONTROL_MAX_FRAME + 1U);
                else
                    dcr_control_handle_line(control, line, used);
                used = 0;
                oversized = false;
            } else if (byte == '\r') {
                continue;
            } else if (!oversized) {
                if (used < DCR_CONTROL_MAX_FRAME)
                    line[used++] = (char)byte;
                else
                    oversized = true;
            }
        }
        dcr_control_emit_snapshot(control);
    }
}
