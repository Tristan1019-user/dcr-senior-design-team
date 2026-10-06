#ifndef DCR_CONTROL_H
#define DCR_CONTROL_H

#include "dcr.h"

#define DCR_CONTROL_MAX_FRAME 8192U
#define DCR_CONTROL_SNAPSHOT_PERIOD_MS 250U
#define DCR_CONTROL_STOP_TIMEOUT_MS 2000U

typedef struct dcr_control dcr_control_t;
typedef bool (*dcr_control_write_fn)(void *context, const char *line, size_t length);

/* The runtime remains owned by the caller. The writer receives one complete @DCR line. */
dcr_control_t *dcr_control_create(dcr_runtime_t *runtime, const char *boot_id,
                                  dcr_control_write_fn writer, void *context);
void dcr_control_destroy(dcr_control_t *control);

/* line excludes its newline. Exactly one response is attempted for every request frame. */
bool dcr_control_handle_line(dcr_control_t *control, const char *line, size_t length);

/* Emits the latest snapshot at most four times per second. */
bool dcr_control_emit_snapshot(dcr_control_t *control);

#endif
