#ifndef DCR_INTERNAL_H
#define DCR_INTERNAL_H
#include "dcr.h"
#define DCR_QUEUE_DEPTH 8
#define DCR_MAX_LOCK_RULES 24
struct dcr_port {
    dcr_instance_t *owner;
    const dcr_port_decl_t *decl;
    struct dcr_port *source;
    pal_queue_t *queue;
    pal_event_t *update;
    dcr_value_t latest;
    uint64_t sequence, seen;
    uint32_t published_at_ms;
    char signal_name[2 * DCR_NAME_SIZE + 1];
};
struct dcr_instance {
    dcr_runtime_t *runtime;
    char id[DCR_NAME_SIZE];
    const dcr_descriptor_t *descriptor;
    cJSON *config;
    void *user_data;
    dcr_options_t options;
    dcr_state_t state;
    dcr_err_t fault;
    bool stop_requested;
    bool needs_cleanup;
    uint32_t heartbeat;
    pal_task_t *task;
    dcr_port_t ports[DCR_MAX_PORTS];
};
typedef struct {
    dcr_port_t *from, *to;
} dcr_connection_t;
typedef struct {
    char id[DCR_NAME_SIZE];
    dcr_port_t *from, *to;
} dcr_connection_lock_t;
struct dcr_runtime {
    const dcr_descriptor_t *const *registry;
    size_t registry_count;
    dcr_instance_t *instances[DCR_MAX_INSTANCES];
    dcr_connection_t connections[DCR_MAX_CONNECTIONS];
    size_t instance_count, connection_count;
    dcr_connection_lock_t connection_locks[DCR_MAX_LOCK_RULES];
    size_t connection_lock_count;
    dcr_settings_t settings;
    pal_mutex_t *lock;
    pal_event_t *space;
    pal_task_t *watchdog;
    bool watchdog_stop;
};
dcr_instance_t *dcr_find(dcr_runtime_t *rt, const char *id);
const dcr_descriptor_t *dcr_lookup(dcr_runtime_t *rt, const char *type);
bool dcr_has_tasks(dcr_runtime_t *rt);
bool dcr_has_active_bus_clients(dcr_runtime_t *rt, dcr_instance_t *bus);
bool dcr_valid_name(const char *name);
dcr_err_t dcr_fail(dcr_diagnostic_t *diag, dcr_err_t code, const char *fmt, ...);
dcr_err_t dcr_required(dcr_instance_t *instance);
dcr_err_t dcr_watchdog_start(dcr_runtime_t *rt);
void dcr_task_entry(void *arg);
bool dcr_retain_queued_value(dcr_type_t type, const dcr_value_t *value);
void dcr_release_queued_value(dcr_type_t type, const dcr_value_t *value);
dcr_port_t *dcr_resolve_port(dcr_runtime_t *rt, const char *endpoint);
const char *dcr_connection_lock_id(dcr_runtime_t *rt, dcr_port_t *destination);
dcr_err_t dcr_disconnect_for_teardown(dcr_runtime_t *rt, const char *to);
#endif
