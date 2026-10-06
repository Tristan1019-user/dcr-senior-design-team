#include "../components/sensors/i2c_bus.h"
#include "../components/sensors/i2c_bus_host_test.h"
#include "dcr.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static unsigned checks;
#define CHECK(x)                                                                                   \
    do {                                                                                           \
        ++checks;                                                                                  \
        if (!(x)) {                                                                                \
            fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #x);                         \
            exit(1);                                                                               \
        }                                                                                          \
    } while (0)

typedef struct {
    dcr_response_queue_t *responses;
    unsigned slot;
    unsigned requests;
    unsigned first_timeout_ms;
    unsigned timeout_ms;
    bool close_after_write;
    bool enqueue_only;
    bool ignore_stop;
    dcr_instance_t *instance;
} client_state_t;

static unsigned replies[2], mismatches[2], timeouts[2];
static pal_mutex_t *stats_lock;
static unsigned release_stuck_client, enqueued;

static void stats_reset(void)
{
    if (!stats_lock)
        stats_lock = pal_mutex_create();
    pal_mutex_lock(stats_lock);
    memset(replies, 0, sizeof(replies));
    memset(mismatches, 0, sizeof(mismatches));
    memset(timeouts, 0, sizeof(timeouts));
    release_stuck_client = 0;
    enqueued = 0;
    pal_mutex_unlock(stats_lock);
}

static void stats_add(unsigned *counter)
{
    pal_mutex_lock(stats_lock);
    ++*counter;
    pal_mutex_unlock(stats_lock);
}

static unsigned stats_get(const unsigned *counter)
{
    pal_mutex_lock(stats_lock);
    unsigned value = *counter;
    pal_mutex_unlock(stats_lock);
    return value;
}

static int integer(const cJSON *config, const char *key, int fallback)
{
    const cJSON *value = cJSON_GetObjectItemCaseSensitive(config, key);
    return cJSON_IsNumber(value) ? (int)value->valuedouble : fallback;
}

static dcr_err_t client_init(const cJSON *config, void **data)
{
    client_state_t *state = pal_malloc(sizeof(*state));
    if (!state)
        return DCR_ERR_OOM;
    state->slot = (unsigned)integer(config, "slot", 0);
    state->requests = (unsigned)integer(config, "requests", 4);
    state->first_timeout_ms = (unsigned)integer(config, "first_timeout_ms", 100);
    state->timeout_ms = (unsigned)integer(config, "timeout_ms", 100);
    state->close_after_write = cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(config,
                                                                               "close_after_write"));
    state->enqueue_only = cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(config, "enqueue_only"));
    state->ignore_stop = cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(config, "ignore_stop"));
    state->responses = dcr_response_queue_create(DCR_BUS_RESPONSE_DEPTH);
    if (!state->responses) {
        pal_free(state);
        return DCR_ERR_OOM;
    }
    *data = state;
    return DCR_OK;
}

static void client_stop(void *data)
{
    client_state_t *state = data;
    if (!state)
        return;
    dcr_response_queue_destroy(state->responses);
    pal_free(state);
}

static void client_run(void *data, dcr_instance_t *instance)
{
    client_state_t *state = data;
    state->instance = instance;
    dcr_port_t *bus = dcr_port(instance, "bus");
    for (unsigned n = 0; n < state->requests && !dcr_should_stop(instance); ++n) {
        dcr_bus_request_t request = {.operation = DCR_BUS_I2C_WRITE,
                                     .address = 0x44,
                                     .length = 1,
                                     .data = {0xfd},
                                     .response_queue = state->responses,
                                     .transaction_id = n + 1};
        dcr_value_t value = {.bus_request = request};
        if (dcr_port_write(bus, &value, 100) != DCR_OK)
            break;
        stats_add(&enqueued);
        if (state->close_after_write && !state->enqueue_only)
            break;
        if (state->enqueue_only)
            continue;
        uint32_t timeout = n ? state->timeout_ms : state->first_timeout_ms;
        uint32_t start = pal_millis();
        bool received = false;
        while (!dcr_should_stop(instance) && pal_millis() - start < timeout) {
            dcr_bus_response_t response;
            uint32_t elapsed = pal_millis() - start;
            if (!dcr_response_queue_receive(state->responses, &response,
                                            elapsed < timeout ? timeout - elapsed : 0))
                break;
            if (response.transaction_id != request.transaction_id) {
                stats_add(&mismatches[state->slot & 1]);
                continue;
            }
            received = true;
            if (response.status == DCR_BUS_STATUS_OK)
                stats_add(&replies[state->slot & 1]);
            break;
        }
        if (!received)
            stats_add(&timeouts[state->slot & 1]);
    }
    while (state->ignore_stop ? !stats_get(&release_stuck_client) : !dcr_should_stop(instance))
        dcr_sleep(instance, 10);
}

static const dcr_port_decl_t client_ports[] = {
    {"bus", DCR_OUTPUT, DCR_BUS_REQUEST, DCR_QUEUE, true, false}};
static const dcr_descriptor_t client_descriptor = {"test_bus_client", client_ports, 1, 4096, 5,
                                                   client_init, client_run, client_stop, NULL};
static const dcr_descriptor_t *const registry[] = {&dcr_i2c_bus_descriptor, &client_descriptor};

static cJSON *object(const char *text)
{
    cJSON *value = cJSON_Parse(text);
    CHECK(value);
    return value;
}

static dcr_runtime_t *runtime(void)
{
    dcr_runtime_t *result = dcr_runtime_create(registry, 2);
    CHECK(result);
    return result;
}

static void create(dcr_runtime_t *r, const char *id, const char *type, const char *config,
                   const dcr_options_t *options)
{
    cJSON *value = config ? object(config) : NULL;
    CHECK(dcr_create_component(r, id, type, value, options, NULL) == DCR_OK);
    cJSON_Delete(value);
}

static void connect_client(dcr_runtime_t *r, const char *id)
{
    char endpoint[64];
    snprintf(endpoint, sizeof(endpoint), "%s.bus", id);
    dcr_err_t error = dcr_connect(r, endpoint, "i2c.request", NULL, NULL);
    CHECK(error == DCR_OK);
}

static void fan_in_and_serial(void)
{
    dcr_i2c_host_test_reset();
    dcr_i2c_host_test_set_delay(4);
    stats_reset();
    dcr_runtime_t *r = runtime();
    create(r, "i2c", "i2c_bus", "{\"port\":0,\"sda_pin\":8,\"scl_pin\":9,\"freq_hz\":100000}", NULL);
    create(r, "a", "test_bus_client", "{\"slot\":0,\"requests\":6}", NULL);
    create(r, "b", "test_bus_client", "{\"slot\":1,\"requests\":6}", NULL);
    connect_client(r, "a");
    connect_client(r, "b");
    CHECK(dcr_validate(r, NULL) == DCR_OK);
    CHECK(dcr_init_component(r, "i2c") == DCR_OK);
    CHECK(dcr_init_component(r, "a") == DCR_OK);
    CHECK(dcr_init_component(r, "b") == DCR_OK);
    CHECK(dcr_start_component(r, "i2c") == DCR_OK);
    CHECK(dcr_start_component(r, "a") == DCR_OK);
    CHECK(dcr_start_component(r, "b") == DCR_OK);
    CHECK(dcr_stop_component(r, "i2c", 100) == DCR_ERR_BUSY);
    pal_sleep(150);
    CHECK(dcr_stop_component(r, "a", 500) == DCR_OK);
    CHECK(dcr_stop_component(r, "b", 500) == DCR_OK);
    CHECK(dcr_stop_component(r, "i2c", 500) == DCR_OK);
    CHECK(stats_get(&replies[0]) + stats_get(&replies[1]) >= 8);
    CHECK(dcr_i2c_host_test_transactions() >= 8);
    CHECK(dcr_i2c_host_test_max_inflight() == 1);
    CHECK(dcr_runtime_destroy(r) == DCR_OK);
}

static void late_reply_lifetime(void)
{
    dcr_i2c_host_test_reset();
    dcr_i2c_host_test_set_delay(40);
    dcr_runtime_t *r = runtime();
    create(r, "i2c", "i2c_bus", "{\"port\":0,\"sda_pin\":8,\"scl_pin\":9,\"freq_hz\":100000}", NULL);
    create(r, "late", "test_bus_client",
           "{\"slot\":0,\"requests\":1,\"close_after_write\":true}", NULL);
    connect_client(r, "late");
    CHECK(dcr_init_component(r, "i2c") == DCR_OK);
    CHECK(dcr_init_component(r, "late") == DCR_OK);
    CHECK(dcr_start_component(r, "i2c") == DCR_OK);
    CHECK(dcr_start_component(r, "late") == DCR_OK);
    uint32_t start = pal_millis();
    while (!dcr_i2c_host_test_transactions() && pal_millis() - start < 100)
        pal_sleep(1);
    CHECK(dcr_i2c_host_test_transactions() >= 1);
    CHECK(dcr_stop_component(r, "late", 500) == DCR_OK);
    CHECK(dcr_stop_component(r, "i2c", 500) == DCR_OK);
    CHECK(dcr_i2c_host_test_transactions() >= 1);
    CHECK(dcr_runtime_destroy(r) == DCR_OK);
}

static void stop_all_clients_first(void)
{
    dcr_i2c_host_test_reset();
    dcr_i2c_host_test_set_delay(40);
    dcr_runtime_t *r = runtime();
    create(r, "i2c", "i2c_bus", "{\"port\":0,\"sda_pin\":8,\"scl_pin\":9,\"freq_hz\":100000}", NULL);
    create(r, "client", "test_bus_client", "{\"requests\":1,\"close_after_write\":true}", NULL);
    connect_client(r, "client");
    CHECK(dcr_init_component(r, "i2c") == DCR_OK);
    CHECK(dcr_init_component(r, "client") == DCR_OK);
    CHECK(dcr_start_component(r, "i2c") == DCR_OK);
    CHECK(dcr_start_component(r, "client") == DCR_OK);
    uint32_t start = pal_millis();
    while (!dcr_i2c_host_test_transactions() && pal_millis() - start < 100)
        pal_sleep(1);
    CHECK(dcr_i2c_host_test_transactions() >= 1);
    CHECK(dcr_stop_all(r, 500) == DCR_OK);
    CHECK(dcr_i2c_host_test_transactions() >= 1);
    CHECK(dcr_runtime_destroy(r) == DCR_OK);
}

static void queued_pending_shutdown(void)
{
    stats_reset();
    dcr_i2c_host_test_reset();
    dcr_i2c_host_test_set_delay(40);
    dcr_runtime_t *r = runtime();
    create(r, "i2c", "i2c_bus", "{\"port\":0,\"sda_pin\":8,\"scl_pin\":9,\"freq_hz\":100000}", NULL);
    create(r, "queued", "test_bus_client",
           "{\"requests\":2,\"enqueue_only\":true}", NULL);
    connect_client(r, "queued");
    CHECK(dcr_init_component(r, "i2c") == DCR_OK);
    CHECK(dcr_init_component(r, "queued") == DCR_OK);
    CHECK(dcr_start_component(r, "i2c") == DCR_OK);
    CHECK(dcr_start_component(r, "queued") == DCR_OK);
    uint32_t start = pal_millis();
    while ((stats_get(&enqueued) < 2 || !dcr_i2c_host_test_transactions()) &&
           pal_millis() - start < 100)
        pal_sleep(1);
    CHECK(stats_get(&enqueued) == 2);
    CHECK(dcr_i2c_host_test_transactions() >= 1);
    CHECK(dcr_stop_component(r, "queued", 500) == DCR_OK);
    CHECK(dcr_stop_component(r, "i2c", 500) == DCR_OK);
    CHECK(dcr_i2c_host_test_transactions() == 1);
    CHECK(dcr_runtime_destroy(r) == DCR_OK);
}

static void stop_timeout_keeps_worker(void)
{
    dcr_i2c_host_test_reset();
    stats_reset();
    dcr_runtime_t *r = runtime();
    create(r, "i2c", "i2c_bus", "{\"port\":0,\"sda_pin\":8,\"scl_pin\":9,\"freq_hz\":100000}", NULL);
    create(r, "stuck", "test_bus_client", "{\"requests\":0,\"ignore_stop\":true}", NULL);
    connect_client(r, "stuck");
    CHECK(dcr_init_component(r, "i2c") == DCR_OK);
    CHECK(dcr_init_component(r, "stuck") == DCR_OK);
    CHECK(dcr_start_component(r, "i2c") == DCR_OK);
    CHECK(dcr_start_component(r, "stuck") == DCR_OK);
    CHECK(dcr_stop_all(r, 5) == DCR_ERR_TIMEOUT);
    dcr_instance_info_t info;
    CHECK(dcr_get_component(r, "i2c", &info) == DCR_OK && info.state == DCR_RUNNING);
    stats_add(&release_stuck_client);
    CHECK(dcr_stop_all(r, 500) == DCR_OK);
    CHECK(dcr_runtime_destroy(r) == DCR_OK);
}

static void ownership_and_priority(void)
{
    dcr_i2c_host_test_reset();
    dcr_runtime_t *r = runtime();
    create(r, "first", "i2c_bus", "{\"port\":0,\"sda_pin\":8,\"scl_pin\":9,\"freq_hz\":100000}", NULL);
    create(r, "second", "i2c_bus", "{\"port\":0,\"sda_pin\":8,\"scl_pin\":9,\"freq_hz\":100000}", NULL);
    CHECK(dcr_init_component(r, "first") == DCR_OK);
    CHECK(dcr_init_component(r, "second") == DCR_ERR_INIT);
    CHECK(dcr_runtime_destroy(r) == DCR_OK);

    r = runtime();
    create(r, "first", "i2c_bus", "{\"port\":0,\"sda_pin\":8,\"scl_pin\":9,\"freq_hz\":100000}", NULL);
    create(r, "overlap", "i2c_bus", "{\"port\":1,\"sda_pin\":8,\"scl_pin\":11,\"freq_hz\":100000}", NULL);
    CHECK(dcr_init_component(r, "first") == DCR_OK);
    CHECK(dcr_init_component(r, "overlap") == DCR_ERR_INIT);
    CHECK(dcr_runtime_destroy(r) == DCR_OK);

    r = runtime();
    dcr_options_t high = {true, 8, 4096, -1};
    create(r, "i2c", "i2c_bus", "{\"port\":0,\"sda_pin\":8,\"scl_pin\":9,\"freq_hz\":100000}", NULL);
    create(r, "high", "test_bus_client", "{\"requests\":1}", &high);
    CHECK(dcr_connect(r, "high.bus", "i2c.request", NULL, NULL) == DCR_ERR_ARGUMENT);
    CHECK(dcr_runtime_destroy(r) == DCR_OK);
}

static void topology_rules(void)
{
    dcr_runtime_t *r = runtime();
    create(r, "i2c", "i2c_bus", "{\"port\":0,\"sda_pin\":8,\"scl_pin\":9,\"freq_hz\":100000}", NULL);
    create(r, "a", "test_bus_client", NULL, NULL);
    create(r, "b", "test_bus_client", NULL, NULL);
    connect_client(r, "a");
    CHECK(dcr_connect(r, "a.bus", "i2c.request", NULL, NULL) == DCR_ERR_MULTIPLE_DRIVERS);
    CHECK(dcr_validate(r, NULL) == DCR_ERR_REQUIRED);
    cJSON *topology = dcr_get_topology(r);
    CHECK(topology);
    char *text = cJSON_PrintUnformatted(topology);
    CHECK(text && strstr(text, "bus_request") && strstr(text, "status"));
    free(text);
    cJSON_Delete(topology);
    CHECK(dcr_runtime_destroy(r) == DCR_OK);
}

int main(void)
{
    stats_reset();
    dcr_i2c_host_test_reset();
    size_t baseline = pal_allocations();
    fan_in_and_serial();
    late_reply_lifetime();
    stop_all_clients_first();
    queued_pending_shutdown();
    stop_timeout_keeps_worker();
    ownership_and_priority();
    topology_rules();
    CHECK(pal_allocations() == baseline);
    printf("BUS_TESTS PASS checks=%u\n", checks);
    return 0;
}
