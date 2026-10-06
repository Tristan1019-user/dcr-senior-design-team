#include "dcr.h"
#include "software.h"
#include "../platform/host/pal_host_test.h"
#include <stdio.h>
#include <stdlib.h>

static unsigned checks;
#define CHECK(x)                                                                                \
    do {                                                                                         \
        ++checks;                                                                                \
        if (!(x)) {                                                                              \
            fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #x);                      \
            exit(1);                                                                             \
        }                                                                                        \
    } while (0)

static const char fanout_json[] =
    "{\"version\":\"1.0\",\"components\":["
    "{\"id\":\"counter\",\"type\":\"counter_source\",\"config\":{\"value\":1,\"step\":1,\"period_ms\":50}},"
    "{\"id\":\"scale\",\"type\":\"float_scale\",\"config\":{\"gain\":2}},"
    "{\"id\":\"scaled\",\"type\":\"logger\"},"
    "{\"id\":\"raw_queue\",\"type\":\"logger\"},"
    "{\"id\":\"raw_signal\",\"type\":\"signal_logger\"}],"
    "\"connections\":["
    "{\"from\":\"counter.output\",\"to\":\"scale.input\",\"signal\":\"count\"},"
    "{\"from\":\"counter.output\",\"to\":\"raw_queue.input\",\"signal\":\"count\"},"
    "{\"from\":\"counter.output\",\"to\":\"raw_signal.input\"},"
    "{\"from\":\"scale.output\",\"to\":\"scaled.input\"}],"
    "\"settings\":{\"watchdog_timeout_ms\":5000,\"signal_wait_timeout_ms\":100,\"log_level\":\"info\"}}";

static void normal_behavior(void)
{
    size_t baseline = pal_allocations();
    pal_host_test_reset_alloc_fail();
    void *value = pal_malloc(8);
    CHECK(value != NULL);
    pal_free(value);
    pal_host_test_fail_after(0);
    CHECK(pal_malloc(8) == NULL);
    pal_host_test_reset_alloc_fail();
    CHECK(pal_allocations() == baseline);

    /* cJSON uses its own allocator and is deliberately outside this injector. */
    pal_host_test_fail_after(0);
    cJSON *json = cJSON_Parse("{\"ok\":true}");
    CHECK(json != NULL);
    cJSON_Delete(json);
    pal_host_test_reset_alloc_fail();
    CHECK(pal_allocations() == baseline);
}

static void runtime_creation_failpoints(void)
{
    size_t baseline = pal_allocations();
    unsigned failures = 0, successes = 0;
    for (size_t n = 0; n < 12; ++n) {
        pal_host_test_fail_after(n);
        dcr_runtime_t *runtime = dcr_runtime_create(dcr_software_registry,
                                                     dcr_software_registry_count);
        pal_host_test_reset_alloc_fail();
        if (runtime) {
            ++successes;
            CHECK(dcr_runtime_destroy(runtime) == DCR_OK);
        } else {
            ++failures;
        }
        CHECK(pal_allocations() == baseline);
    }
    CHECK(failures > 0 && successes > 0);
}

static void fanout_build_failpoints(void)
{
    size_t baseline = pal_allocations();
    unsigned preinit_rollbacks = 0, retained_init_failures = 0, successes = 0;
    for (size_t n = 0; n < 192; ++n) {
        dcr_runtime_t *runtime = NULL;
        dcr_diagnostic_t diagnostic;
        pal_host_test_fail_after(n);
        dcr_err_t error = dcr_build_json(fanout_json, dcr_software_registry,
                                         dcr_software_registry_count, &runtime, &diagnostic);
        pal_host_test_reset_alloc_fail();
        if (!runtime) {
            ++preinit_rollbacks;
            CHECK(error == DCR_ERR_OOM);
        } else if (error == DCR_ERR_OOM) {
            bool faulted = false;
            static const char *const ids[] = {"counter", "scale", "scaled", "raw_queue",
                                              "raw_signal"};
            ++retained_init_failures;
            CHECK(diagnostic.code == DCR_ERR_OOM);
            CHECK(dcr_component_count(runtime) == sizeof(ids) / sizeof(*ids));
            for (size_t index = 0; index < sizeof(ids) / sizeof(*ids); ++index) {
                dcr_instance_info_t info;
                CHECK(dcr_get_component(runtime, ids[index], &info) == DCR_OK);
                faulted = faulted || info.state == DCR_FAULTED;
                CHECK(info.state != DCR_RUNNING);
            }
            CHECK(faulted);
            CHECK(dcr_runtime_destroy(runtime) == DCR_OK);
        } else {
            CHECK(error == DCR_OK);
            ++successes;
            CHECK(dcr_runtime_destroy(runtime) == DCR_OK);
        }
        CHECK(pal_allocations() == baseline);
    }
    CHECK(preinit_rollbacks > 0);
    CHECK(retained_init_failures > 0);
    CHECK(successes > 0);
}

static size_t connection_count(dcr_runtime_t *runtime)
{
    cJSON *topology = dcr_get_topology(runtime);
    CHECK(topology != NULL);
    const cJSON *connections = cJSON_GetObjectItemCaseSensitive(topology, "connections");
    CHECK(cJSON_IsArray(connections));
    size_t count = (size_t)cJSON_GetArraySize(connections);
    cJSON_Delete(topology);
    return count;
}

static void connect_queue_failpoints(void)
{
    size_t baseline = pal_allocations();
    unsigned failures = 0, successes = 0;
    const dcr_descriptor_t *const registry[] = {dcr_software_registry[0],
                                                dcr_software_registry[3]};
    for (size_t n = 0; n < 8; ++n) {
        pal_host_test_reset_alloc_fail();
        dcr_runtime_t *runtime = dcr_runtime_create(registry, 2);
        CHECK(runtime != NULL);
        CHECK(dcr_create_component(runtime, "source", "counter_source", NULL, NULL, NULL) ==
              DCR_OK);
        CHECK(dcr_create_component(runtime, "sink", "logger", NULL, NULL, NULL) == DCR_OK);
        size_t graph_baseline = pal_allocations();

        pal_host_test_fail_after(n);
        dcr_err_t error = dcr_connect(runtime, "source.output", "sink.input", NULL, NULL);
        pal_host_test_reset_alloc_fail();
        if (error == DCR_ERR_OOM) {
            ++failures;
            CHECK(connection_count(runtime) == 0);
            /* A failed queue allocation must leave the graph retryable. */
            CHECK(dcr_connect(runtime, "source.output", "sink.input", NULL, NULL) == DCR_OK);
        } else {
            ++successes;
            CHECK(error == DCR_OK);
            CHECK(connection_count(runtime) == 1);
        }
        CHECK(dcr_disconnect(runtime, "sink.input") == DCR_OK);
        CHECK(pal_allocations() == graph_baseline);
        CHECK(dcr_runtime_destroy(runtime) == DCR_OK);
        CHECK(pal_allocations() == baseline);
    }
    CHECK(failures > 0 && successes > 0);
}

static void response_queue_failpoints(void)
{
    size_t baseline = pal_allocations();
    unsigned failures = 0, successes = 0;
    for (size_t n = 0; n < 24; ++n) {
        pal_host_test_fail_after(n);
        dcr_response_queue_t *queue = dcr_response_queue_create(4);
        pal_host_test_reset_alloc_fail();
        if (queue) {
            ++successes;
            dcr_response_queue_destroy(queue);
        } else {
            ++failures;
        }
        CHECK(pal_allocations() == baseline);
    }
    CHECK(failures > 0 && successes > 0);
}

int main(void)
{
    pal_host_test_reset_alloc_fail();
    normal_behavior();
    runtime_creation_failpoints();
    fanout_build_failpoints();
    connect_queue_failpoints();
    response_queue_failpoints();
    pal_host_test_reset_alloc_fail();
    CHECK(pal_allocations() == 0);
    printf("ALLOCATION_TESTS PASS checks=%u\n", checks);
    return 0;
}
