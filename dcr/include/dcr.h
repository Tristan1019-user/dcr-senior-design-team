#ifndef DCR_H
#define DCR_H
#include "pal.h"
#include "cJSON.h"

#define DCR_MAX_INSTANCES 24
#define DCR_MAX_CONNECTIONS 64
#define DCR_MAX_PORTS 12
#define DCR_NAME_SIZE 48
#define DCR_DEFAULT_TIMEOUT UINT32_MAX
#define DCR_BUS_MAX_DATA 32
#define DCR_BUS_RESPONSE_DEPTH 4
typedef enum {
    DCR_OK,
    DCR_ERR_ARGUMENT,
    DCR_ERR_OOM,
    DCR_ERR_SCHEMA,
    DCR_ERR_DUPLICATE,
    DCR_ERR_TYPE,
    DCR_ERR_ENDPOINT,
    DCR_ERR_DIRECTION,
    DCR_ERR_MULTIPLE_DRIVERS,
    DCR_ERR_REQUIRED,
    DCR_ERR_STATE,
    DCR_ERR_INIT,
    DCR_ERR_TIMEOUT,
    DCR_ERR_STOPPED,
    DCR_ERR_LIMIT,
    DCR_ERR_BUSY,
    DCR_ERR_EMPTY,
    DCR_ERR_WATCHDOG,
    DCR_ERR_SAFETY
} dcr_err_t;
typedef enum { DCR_UNLOADED, DCR_INITIALIZED, DCR_RUNNING, DCR_STOPPED, DCR_FAULTED } dcr_state_t;
typedef enum { DCR_BOOL, DCR_INT32, DCR_UINT32, DCR_FLOAT, DCR_BUS_REQUEST } dcr_type_t;
typedef enum { DCR_INPUT, DCR_OUTPUT } dcr_direction_t;
typedef enum { DCR_SIGNAL, DCR_QUEUE } dcr_delivery_t;
typedef enum {
    DCR_BUS_I2C_WRITE = 1,
    DCR_BUS_I2C_READ = 2,
    DCR_BUS_I2C_REGISTER_READ = 3
} dcr_bus_operation_t;
typedef enum {
    DCR_BUS_STATUS_OK,
    DCR_BUS_STATUS_IO,
    DCR_BUS_STATUS_TIMEOUT,
    DCR_BUS_STATUS_INVALID
} dcr_bus_status_t;
typedef struct dcr_response_queue dcr_response_queue_t;
typedef struct {
    uint8_t operation;
    uint8_t address;
    uint8_t register_address;
    uint8_t length;
    uint8_t data[DCR_BUS_MAX_DATA];
    dcr_response_queue_t *response_queue;
    uint32_t transaction_id;
} dcr_bus_request_t;
typedef struct {
    dcr_bus_status_t status;
    uint8_t length;
    uint8_t data[DCR_BUS_MAX_DATA];
    uint32_t transaction_id;
} dcr_bus_response_t;
typedef union {
    bool boolean;
    int32_t i32;
    uint32_t u32;
    float real;
    dcr_bus_request_t bus_request;
} dcr_value_t;
typedef struct {
    const char *name;
    dcr_direction_t direction;
    dcr_type_t type;
    dcr_delivery_t delivery;
    bool required;
    /* Approved R07 exception: QUEUE INPUT only; dedicated resource owner. */
    bool multi_producer;
} dcr_port_decl_t;
typedef struct dcr_instance dcr_instance_t;
typedef struct dcr_runtime dcr_runtime_t;
typedef struct dcr_port dcr_port_t;
typedef struct {
    const char *type_name;
    const dcr_port_decl_t *ports;
    uint8_t port_count;
    uint32_t default_stack_size;
    uint8_t default_priority;
    dcr_err_t (*init)(const cJSON *config, void **user_data);
    void (*run)(void *user_data, dcr_instance_t *instance);
    void (*stop)(void *user_data);
    dcr_err_t (*on_event)(void *user_data, const cJSON *event);
} dcr_descriptor_t;
typedef struct {
    dcr_err_t code;
    char context[192];
    unsigned warnings;
} dcr_diagnostic_t;
typedef struct {
    uint32_t watchdog_timeout_ms;
    uint32_t default_stack_size;
    uint32_t signal_wait_timeout_ms;
    unsigned log_level;
} dcr_settings_t;
typedef struct {
    bool autostart;
    unsigned priority;
    uint32_t stack_size;
    int core;
} dcr_options_t;
typedef struct {
    char id[DCR_NAME_SIZE];
    char type[DCR_NAME_SIZE];
    dcr_state_t state;
    dcr_options_t options;
    dcr_err_t fault;
} dcr_instance_info_t;
typedef struct {
    char name[DCR_NAME_SIZE];
    dcr_type_t type;
    bool published;
    dcr_value_t value;
    uint64_t sequence;
    uint32_t timestamp_ms;
    uint32_t age_ms;
} dcr_output_snapshot_t;
typedef struct {
    char id[DCR_NAME_SIZE];
    char type[DCR_NAME_SIZE];
    dcr_state_t state;
    dcr_err_t fault;
    /* Caller owns config and releases it with dcr_component_snapshot_clear(). */
    cJSON *config;
    dcr_output_snapshot_t outputs[DCR_MAX_PORTS];
    size_t output_count;
} dcr_component_snapshot_t;

/* Registry and descriptors are immutable and must outlive the runtime.
 * One control-plane caller owns all lifecycle/topology calls. Task port APIs
 * are thread-safe. Topology changes require every component task to be joined. */
dcr_runtime_t *dcr_runtime_create(const dcr_descriptor_t *const *registry, size_t count);
dcr_err_t dcr_runtime_destroy(dcr_runtime_t *rt);
dcr_err_t dcr_create_component(dcr_runtime_t *rt, const char *id, const char *type,
                               const cJSON *config, const dcr_options_t *options,
                               dcr_diagnostic_t *diag);
dcr_err_t dcr_destroy_component(dcr_runtime_t *rt, const char *id);
dcr_err_t dcr_init_component(dcr_runtime_t *rt, const char *id);
dcr_err_t dcr_start_component(dcr_runtime_t *rt, const char *id);
dcr_err_t dcr_stop_component(dcr_runtime_t *rt, const char *id, uint32_t timeout_ms);
dcr_err_t dcr_stop_all(dcr_runtime_t *rt, uint32_t timeout_ms);
dcr_err_t dcr_update_config(dcr_runtime_t *rt, const char *id, const cJSON *config);
dcr_err_t dcr_send_event(dcr_runtime_t *rt, const char *id, const cJSON *event);
dcr_err_t dcr_get_component(dcr_runtime_t *rt, const char *id, dcr_instance_info_t *info);
size_t dcr_component_count(dcr_runtime_t *rt);
/* Snapshot is copied while holding the runtime lock. Config ownership transfers to the caller. */
dcr_err_t dcr_get_component_snapshot(dcr_runtime_t *rt, const char *id,
                                     dcr_component_snapshot_t *snapshot);
dcr_err_t dcr_get_component_snapshot_at(dcr_runtime_t *rt, size_t index,
                                         dcr_component_snapshot_t *snapshot);
void dcr_component_snapshot_clear(dcr_component_snapshot_t *snapshot);
/* Caller owns returned JSON and releases it with cJSON_Delete. */
cJSON *dcr_get_topology(dcr_runtime_t *rt);
/* Immutable boot policy snapshot. Currently supports connection_locked only. */
cJSON *dcr_get_safety_rules(dcr_runtime_t *rt);
dcr_err_t dcr_connect(dcr_runtime_t *rt, const char *from, const char *to, const char *signal_name,
                      dcr_diagnostic_t *diag);
dcr_err_t dcr_disconnect(dcr_runtime_t *rt, const char *to);
dcr_err_t dcr_validate(dcr_runtime_t *rt, dcr_diagnostic_t *diag);
/* Build validates all structure BEFORE invoking any component init/task.
 * On a component init failure, returns a retained inspectable runtime with
 * FAULTED instances and no started tasks. On schema failure, *out stays NULL. */
dcr_err_t dcr_build_json(const char *json, const dcr_descriptor_t *const *registry, size_t count,
                         dcr_runtime_t **out, dcr_diagnostic_t *diag);
dcr_err_t dcr_start_autostart(dcr_runtime_t *rt);
dcr_err_t dcr_get_settings(dcr_runtime_t *rt, dcr_settings_t *out);
const char *dcr_error_name(dcr_err_t error);
const char *dcr_state_name(dcr_state_t state);

/* Private component-to-bus reply queues. The owner closes its queue during
 * stop; retained request references keep the object alive until the bus has
 * replied or the runtime drains a pending request. */
dcr_response_queue_t *dcr_response_queue_create(size_t depth);
void dcr_response_queue_destroy(dcr_response_queue_t *queue);
bool dcr_response_queue_send(dcr_response_queue_t *queue, const dcr_bus_response_t *response,
                             uint32_t timeout_ms);
bool dcr_response_queue_receive(dcr_response_queue_t *queue, dcr_bus_response_t *response,
                                uint32_t timeout_ms);
void dcr_bus_request_release(const dcr_bus_request_t *request);

/* Resolve once in run(), retain handles only until run returns. Values are
 * interpreted using the compiled port type, never a JSON-supplied type. */
dcr_port_t *dcr_port(dcr_instance_t *instance, const char *name);
dcr_err_t dcr_port_write(dcr_port_t *port, const dcr_value_t *value, uint32_t timeout_ms);
dcr_err_t dcr_port_wait(dcr_port_t *port, dcr_value_t *value, uint32_t timeout_ms);
dcr_err_t dcr_port_read(dcr_port_t *port, dcr_value_t *value);
bool dcr_should_stop(dcr_instance_t *instance);
void dcr_heartbeat(dcr_instance_t *instance);
void dcr_sleep(dcr_instance_t *instance, uint32_t ms);
void dcr_log(dcr_instance_t *instance, const char *level, const char *fmt, ...);
const char *dcr_instance_id(const dcr_instance_t *instance);
#endif
