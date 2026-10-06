#include "software.h"
#include "../dcr/src/internal.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#define CHECK(x)                                                                                   \
    do {                                                                                           \
        ++checks;                                                                                  \
        if (!(x)) {                                                                                \
            fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #x);                           \
            exit(1);                                                                               \
        }                                                                                          \
    } while (0)
static unsigned checks, init_calls, stop_calls;
static dcr_err_t init(const cJSON *c, void **p)
{
    ++init_calls;
    *p = pal_malloc(4);
    if (!*p)
        return DCR_ERR_OOM;
    return cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(c, "fail")) ? DCR_ERR_INIT : DCR_OK;
}
static void stop(void *p)
{
    ++stop_calls;
    pal_free(p);
}
static void idle(void *p, dcr_instance_t *i)
{
    (void)p;
    while (!dcr_should_stop(i))
        dcr_sleep(i, 5);
}
static void hung(void *p, dcr_instance_t *i)
{
    (void)p;
    (void)i;
    pal_sleep(180);
}
static void returned(void *p, dcr_instance_t *i)
{
    (void)p;
    (void)i;
}
static const dcr_port_decl_t out[] = {{"f", DCR_OUTPUT, DCR_FLOAT, DCR_SIGNAL, false, false},
                                      {"b", DCR_OUTPUT, DCR_BOOL, DCR_SIGNAL, false, false},
                                      {"i", DCR_OUTPUT, DCR_INT32, DCR_SIGNAL, false, false},
                                      {"u", DCR_OUTPUT, DCR_UINT32, DCR_SIGNAL, false, false}};
static const dcr_port_decl_t in[] = {{"f", DCR_INPUT, DCR_FLOAT, DCR_SIGNAL, false, false},
                                     {"b", DCR_INPUT, DCR_BOOL, DCR_SIGNAL, false, false},
                                     {"i", DCR_INPUT, DCR_INT32, DCR_SIGNAL, false, false},
                                     {"u", DCR_INPUT, DCR_UINT32, DCR_SIGNAL, false, false}};
static const dcr_port_decl_t queue[] = {{"f", DCR_INPUT, DCR_FLOAT, DCR_QUEUE, true, false}};
static const dcr_port_decl_t bus[] = {{"request", DCR_INPUT, DCR_FLOAT, DCR_QUEUE, true, true}};
static const dcr_descriptor_t source = {"source", out, 4, 4096, 5, init, idle, stop, NULL};
static const dcr_descriptor_t sink = {"sink", in, 4, 4096, 5, init, idle, stop, NULL};
static const dcr_descriptor_t queued = {"queued", queue, 1, 4096, 5, init, idle, stop, NULL};
static const dcr_descriptor_t bus_desc = {"bus", bus, 1, 4096, 7, init, idle, stop, NULL};
static const dcr_descriptor_t hung_desc = {"hung", NULL, 0, 4096, 5, init, hung, stop, NULL};
static const dcr_descriptor_t returned_desc = {"returned", NULL,     0,    4096, 5,
                                               init,       returned, stop, NULL};
static const dcr_descriptor_t *const registry[] = {&source,   &sink,      &queued,
                                                   &bus_desc, &hung_desc, &returned_desc};
static const size_t count = sizeof(registry) / sizeof(*registry);
static dcr_runtime_t *runtime(void)
{
    dcr_runtime_t *r = dcr_runtime_create(registry, count);
    CHECK(r);
    return r;
}
static void create(dcr_runtime_t *r, const char *id, const char *type)
{
    CHECK(dcr_create_component(r, id, type, NULL, NULL, NULL) == DCR_OK);
}
static dcr_port_t *port(dcr_runtime_t *r, const char *id, const char *name)
{
    return dcr_port(dcr_find(r, id), name);
}
static void bad(const char *json, dcr_err_t expected)
{
    size_t baseline = pal_allocations();
    dcr_runtime_t *r = NULL;
    dcr_diagnostic_t d;
    unsigned before = init_calls;
    CHECK(dcr_build_json(json, registry, count, &r, &d) == expected);
    CHECK(!r);
    CHECK(d.context[0]);
    CHECK(init_calls == before);
    CHECK(pal_allocations() == baseline);
}
static void parsing(void)
{
    bad("{}", DCR_ERR_SCHEMA);
    bad("{\"version\":\"9\",\"components\":[]}", DCR_ERR_SCHEMA);
    bad("{\"version\":\"1.0\",\"components\":[]} trailing", DCR_ERR_SCHEMA);
    bad("{\"version\":\"1.0\",\"version\":\"1.0\",\"components\":[]}", DCR_ERR_SCHEMA);
    bad("{\"version\":\"1.0\",\"components\":[],\"safety_rules\":[{\"type\":\"port_bounds\"}]}", DCR_ERR_SCHEMA);
    bad("{\"version\":\"1.0\",\"components\":[{\"id\":\"a\",\"type\":\"missing\"}]}", DCR_ERR_TYPE);
    bad("{\"version\":\"1.0\",\"components\":[{\"id\":\"a\",\"type\":\"source\"},{\"id\":\"a\","
        "\"type\":\"source\"}]}",
        DCR_ERR_DUPLICATE);
    bad("{\"version\":\"1.0\",\"components\":[{\"id\":\"a\",\"type\":\"source\",\"priority\":1.5}]"
        "}",
        DCR_ERR_SCHEMA);
    bad("{\"version\":\"1.0\",\"components\":[{\"id\":\"a\",\"type\":\"source\",\"core\":2}]}",
        DCR_ERR_SCHEMA);
    bad("{\"version\":\"1.0\",\"components\":[{\"id\":\"a\",\"type\":\"source\",\"autostart\":1}]}",
        DCR_ERR_SCHEMA);
    bad("{\"version\":\"1.0\",\"components\":[{\"id\":\"a\",\"type\":\"queued\"}]}",
        DCR_ERR_REQUIRED);
    bad("{\"version\":\"1.0\",\"components\":[],\"settings\":{\"log_level\":\"loud\"}}",
        DCR_ERR_SCHEMA);
    bad("{\"version\":\"1.0\",\"components\":[],\"settings\":{\"watchdog_timeout_ms\":19}}",
        DCR_ERR_SCHEMA);
    bad("{\"version\":\"1.0\",\"components\":[],\"settings\":{\"default_stack_size\":2047}}",
        DCR_ERR_SCHEMA);
    bad("{\"version\":\"1.0\",\"components\":[],\"settings\":{\"signal_wait_timeout_ms\":86400001}}",
        DCR_ERR_SCHEMA);
    bad("{\"version\":\"1.0\",\"components\":[],\"settings\":{\"unknown\":1}}",
        DCR_ERR_SCHEMA);
    bad("{\"version\":\"1.0\",\"components\":[],\"connections\":[{\"from\":\"x.f\",\"to\":\"y.f\"}]"
        "}",
        DCR_ERR_ENDPOINT);
    dcr_runtime_t *r = NULL;
    dcr_diagnostic_t d;
    CHECK(dcr_build_json(
              "{\"version\":\"1.0\",\"components\":[],\"settings\":{\"watchdog_timeout_ms\":30,"
              "\"default_stack_size\":4096,\"signal_wait_timeout_ms\":25,\"log_level\":\"debug\"}}",
              registry, count, &r, &d) == DCR_OK);
    CHECK(r->settings.watchdog_timeout_ms == 30);
    CHECK(r->settings.default_stack_size == 4096);
    CHECK(r->settings.signal_wait_timeout_ms == 25);
    CHECK(r->settings.log_level == 0);
    CHECK(dcr_runtime_destroy(r) == DCR_OK);
    CHECK(dcr_build_json("{\"version\":\"1.0\",\"components\":[{\"id\":\"q\",\"type\":\"queued\","
                         "\"autostart\":false}]}",
                         registry, count, &r, &d) == DCR_OK);
    CHECK(d.warnings == 1);
    CHECK(dcr_start_component(r, "q") == DCR_ERR_REQUIRED);
    CHECK(dcr_runtime_destroy(r) == DCR_OK);
    CHECK(dcr_build_json("{\"version\":\"1.0\",\"components\":[{\"id\":\"s\",\"type\":\"source\","
                         "\"config\":{\"fail\":true}}]}",
                         registry, count, &r, &d) == DCR_ERR_INIT);
    CHECK(r);
    dcr_instance_info_t info;
    CHECK(dcr_get_component(r, "s", &info) == DCR_OK);
    CHECK(info.state == DCR_FAULTED);
    CHECK(!dcr_find(r, "s")->task);
    CHECK(dcr_start_autostart(r) == DCR_ERR_STATE);
    CHECK(dcr_runtime_destroy(r) == DCR_OK);
}
static void wiring(void)
{
    dcr_runtime_t *r = runtime();
    create(r, "s", "source");
    create(r, "s2", "source");
    create(r, "in", "sink");
    create(r, "in2", "sink");
    create(r, "q", "queued");
    create(r, "q2", "queued");
    CHECK(dcr_connect(r, "s.missing", "in.f", NULL, NULL) == DCR_ERR_ENDPOINT);
    CHECK(dcr_connect(r, "in.f", "s.f", NULL, NULL) == DCR_ERR_DIRECTION);
    CHECK(dcr_connect(r, "s.f", "s2.f", NULL, NULL) == DCR_ERR_DIRECTION);
    CHECK(dcr_connect(r, "s.b", "in.f", NULL, NULL) == DCR_ERR_TYPE);
    CHECK(dcr_connect(r, "s.f", "in.f", "shared", NULL) == DCR_OK);
    CHECK(dcr_connect(r, "s2.f", "in.f", NULL, NULL) == DCR_ERR_MULTIPLE_DRIVERS);
    CHECK(dcr_connect(r, "s2.f", "in2.f", "shared", NULL) == DCR_ERR_DUPLICATE);
    CHECK(dcr_connect(r, "s.f", "in2.f", "alias", NULL) == DCR_OK);
    CHECK(dcr_connect(r, "s.f", "q.f", NULL, NULL) == DCR_OK);
    CHECK(dcr_connect(r, "s.f", "q2.f", NULL, NULL) == DCR_OK);
    CHECK(port(r, "q", "f")->queue != port(r, "q2", "f")->queue);
    CHECK(port(r, "in", "f")->source == port(r, "in2", "f")->source);
    dcr_value_t v = {.real = 3}, got;
    CHECK(dcr_port_wait(port(r, "in", "f"), &got, 5) == DCR_ERR_TIMEOUT);
    CHECK(dcr_port_write(port(r, "s", "f"), &v, 0) == DCR_OK);
    CHECK(dcr_port_wait(port(r, "in", "f"), &got, 0) == DCR_OK && got.real == 3);
    CHECK(dcr_port_wait(port(r, "in2", "f"), &got, 0) == DCR_OK && got.real == 3);
    CHECK(dcr_port_read(port(r, "in", "f"), &got) == DCR_OK && got.real == 3);
    CHECK(dcr_port_read(port(r, "in", "f"), &got) == DCR_OK);
    CHECK(dcr_port_wait(port(r, "in", "f"), &got, 0) == DCR_ERR_TIMEOUT);
    for (unsigned n = 0; n < 7; ++n) {
        v.real = (float)(n + 4);
        CHECK(dcr_port_write(port(r, "s", "f"), &v, 0) == DCR_OK);
    }
    v.real = 99;
    CHECK(dcr_port_write(port(r, "s", "f"), &v, 5) == DCR_ERR_TIMEOUT);
    CHECK(dcr_port_read(port(r, "in", "f"), &got) == DCR_OK && got.real == 10);
    for (unsigned n = 0; n < 8; ++n) {
        CHECK(dcr_port_wait(port(r, "q", "f"), &got, 0) == DCR_OK && got.real == (float)(n + 3));
    }
    CHECK(dcr_port_write(port(r, "s", "f"), &v, 0) ==
          DCR_ERR_TIMEOUT); /* q2 still full; q receives nothing */
    CHECK(dcr_port_wait(port(r, "q", "f"), &got, 0) == DCR_ERR_TIMEOUT);
    for (unsigned n = 0; n < 8; ++n)
        CHECK(dcr_port_wait(port(r, "q2", "f"), &got, 0) == DCR_OK && got.real == (float)(n + 3));
    CHECK(dcr_port_write(port(r, "s", "f"), &v, 0) == DCR_OK);
    CHECK(dcr_disconnect(r, "in.f") == DCR_OK);
    CHECK(dcr_port_read(port(r, "in", "f"), &got) == DCR_ERR_ENDPOINT);
    CHECK(dcr_destroy_component(r, "s") == DCR_ERR_BUSY);
    cJSON *topology = dcr_get_topology(r);
    CHECK(topology);
    CHECK(cJSON_GetArraySize(cJSON_GetObjectItem(topology, "connections")) == 3);
    cJSON_Delete(topology);
    CHECK(dcr_runtime_destroy(r) == DCR_OK);
}
static void typed_and_bus(void)
{
    dcr_runtime_t *r = runtime();
    create(r, "s", "source");
    create(r, "s2", "source");
    create(r, "sink", "sink");
    create(r, "bus", "bus");
    const char *names[] = {"b", "i", "u"};
    dcr_value_t values[] = {{.boolean = true}, {.i32 = -123}, {.u32 = 4000000000U}};
    for (unsigned n = 0; n < 3; ++n) {
        char a[20], b[20];
        snprintf(a, sizeof(a), "s.%s", names[n]);
        snprintf(b, sizeof(b), "sink.%s", names[n]);
        CHECK(dcr_connect(r, a, b, NULL, NULL) == DCR_OK);
        CHECK(dcr_port_write(port(r, "s", names[n]), &values[n], 0) == DCR_OK);
        dcr_value_t got = {0};
        CHECK(dcr_port_wait(port(r, "sink", names[n]), &got, 0) == DCR_OK);
        if (!n)
            CHECK(got.boolean);
        else if (n == 1)
            CHECK(got.i32 == -123);
        else
            CHECK(got.u32 == 4000000000U);
    }
    CHECK(dcr_connect(r, "s.f", "bus.request", NULL, NULL) == DCR_OK);
    CHECK(dcr_connect(r, "s2.f", "bus.request", NULL, NULL) == DCR_OK);
    dcr_value_t v = {.real = 1}, got;
    CHECK(dcr_port_write(port(r, "s", "f"), &v, 0) == DCR_OK);
    v.real = 2;
    CHECK(dcr_port_write(port(r, "s2", "f"), &v, 0) == DCR_OK);
    CHECK(dcr_port_wait(port(r, "bus", "request"), &got, 0) == DCR_OK && got.real == 1);
    CHECK(dcr_port_wait(port(r, "bus", "request"), &got, 0) == DCR_OK && got.real == 2);
    dcr_options_t high = {false, 8, 4096, -1};
    CHECK(dcr_create_component(r, "high", "source", NULL, &high, NULL) == DCR_OK);
    CHECK(dcr_connect(r, "high.f", "bus.request", NULL, NULL) == DCR_ERR_ARGUMENT);
    CHECK(dcr_runtime_destroy(r) == DCR_OK);
}
static void lifecycle(void)
{
    dcr_runtime_t *r = runtime();
    create(r, "s", "source");
    CHECK(dcr_start_component(r, "s") == DCR_ERR_STATE);
    CHECK(dcr_stop_component(r, "s", 50) == DCR_ERR_STATE);
    for (unsigned n = 0; n < 20; ++n) {
        CHECK(dcr_init_component(r, "s") == DCR_OK);
        CHECK(dcr_init_component(r, "s") == DCR_ERR_STATE);
        CHECK(dcr_start_component(r, "s") == DCR_OK);
        CHECK(dcr_create_component(r, "other", "sink", NULL, NULL, NULL) == DCR_ERR_BUSY);
        CHECK(dcr_stop_component(r, "s", 500) == DCR_OK);
    }
    cJSON *cfg = cJSON_Parse("{\"custom\":42}");
    CHECK(dcr_update_config(r, "s", cfg) == DCR_OK);
    cJSON_Delete(cfg);
    CHECK(dcr_init_component(r, "s") == DCR_OK);
    CHECK(dcr_start_component(r, "s") == DCR_OK);
    CHECK(dcr_runtime_destroy(r) == DCR_OK);
    r = runtime();
    create(r, "h", "hung");
    r->settings.watchdog_timeout_ms = 30;
    CHECK(dcr_init_component(r, "h") == DCR_OK);
    CHECK(dcr_start_component(r, "h") == DCR_OK);
    pal_sleep(80);
    dcr_instance_info_t info;
    CHECK(dcr_get_component(r, "h", &info) == DCR_OK);
    CHECK(info.state == DCR_FAULTED && info.fault == DCR_ERR_WATCHDOG);
    CHECK(dcr_stop_component(r, "h", 1) == DCR_ERR_TIMEOUT);
    CHECK(dcr_find(r, "h")->user_data != NULL);
    CHECK(dcr_stop_component(r, "h", 500) == DCR_OK);
    CHECK(dcr_runtime_destroy(r) == DCR_OK);
    r = runtime();
    create(r, "ret", "returned");
    CHECK(dcr_init_component(r, "ret") == DCR_OK);
    CHECK(dcr_start_component(r, "ret") == DCR_OK);
    pal_sleep(20);
    CHECK(dcr_get_component(r, "ret", &info) == DCR_OK);
    CHECK(info.state == DCR_FAULTED);
    CHECK(dcr_runtime_destroy(r) == DCR_OK);
}
typedef struct {
    dcr_port_t *port;
    dcr_value_t value;
    dcr_err_t result;
} transfer_t;
static void receive_task(void *arg)
{
    transfer_t *t = arg;
    t->result = dcr_port_wait(t->port, &t->value, 1000);
}
static void send_task(void *arg)
{
    transfer_t *t = arg;
    t->result = dcr_port_write(t->port, &t->value, 1000);
}
static void concurrency(void)
{
    dcr_runtime_t *r = runtime();
    create(r, "s", "source");
    create(r, "a", "sink");
    create(r, "b", "sink");
    CHECK(dcr_connect(r, "s.f", "a.f", NULL, NULL) == DCR_OK);
    CHECK(dcr_connect(r, "s.f", "b.f", NULL, NULL) == DCR_OK);
    transfer_t a = {port(r, "a", "f"), {0}, DCR_ERR_EMPTY},
               b = {port(r, "b", "f"), {0}, DCR_ERR_EMPTY};
    pal_task_t *ta = pal_task_create("wait-a", receive_task, &a, 4096, 5, -1),
               *tb = pal_task_create("wait-b", receive_task, &b, 4096, 5, -1);
    CHECK(ta && tb);
    pal_sleep(20);
    dcr_value_t v = {.real = 42};
    CHECK(dcr_port_write(port(r, "s", "f"), &v, 0) == DCR_OK);
    CHECK(pal_task_join(ta, 1500));
    CHECK(pal_task_join(tb, 1500));
    CHECK(a.result == DCR_OK && b.result == DCR_OK && a.value.real == 42 && b.value.real == 42);
    create(r, "q", "queued");
    CHECK(dcr_connect(r, "s.f", "q.f", NULL, NULL) == DCR_OK);
    for (unsigned n = 0; n < 8; ++n)
        CHECK(dcr_port_write(port(r, "s", "f"), &v, 0) == DCR_OK);
    transfer_t writer = {port(r, "s", "f"), {.real = 100}, DCR_ERR_EMPTY};
    ta = pal_task_create("blocked-writer", send_task, &writer, 4096, 5, -1);
    CHECK(ta);
    pal_sleep(20);
    CHECK(dcr_port_wait(port(r, "q", "f"), &v, 0) == DCR_OK);
    CHECK(pal_task_join(ta, 1500));
    CHECK(writer.result == DCR_OK);
    for (unsigned n = 0; n < 8; ++n)
        CHECK(dcr_port_wait(port(r, "q", "f"), &v, 0) == DCR_OK);
    CHECK(v.real == 100);
    CHECK(dcr_runtime_destroy(r) == DCR_OK);
}
static void repeated_load(void)
{
    for (unsigned n = 0; n < 100; ++n) {
        dcr_runtime_t *r = NULL;
        dcr_diagnostic_t d;
        CHECK(dcr_build_json(
                  "{\"version\":\"1.0\",\"components\":[{\"id\":\"s\",\"type\":\"source\"}]}",
                  registry, count, &r, &d) == DCR_OK);
        CHECK(dcr_start_autostart(r) == DCR_OK);
        CHECK(dcr_runtime_destroy(r) == DCR_OK);
        CHECK(pal_allocations() == 0);
    }
}
static void software_pipeline(void)
{
    const char *json = "{\"version\":\"1.0\",\"components\":["
                       "{\"id\":\"src\",\"type\":\"counter_source\",\"config\":{\"value\":7,"
                       "\"step\":1,\"period_ms\":10}},"
                       "{\"id\":\"scale\",\"type\":\"float_scale\",\"config\":{\"gain\":3}},"
                       "{\"id\":\"capture\",\"type\":\"logger\",\"autostart\":false}],"
                       "\"connections\":[{\"from\":\"src.output\",\"to\":\"scale.input\"},{"
                       "\"from\":\"scale.output\",\"to\":\"capture.input\"}]}";
    dcr_runtime_t *r = NULL;
    CHECK(dcr_build_json(json, dcr_software_registry, dcr_software_registry_count, &r, NULL) ==
          DCR_OK);
    CHECK(dcr_start_autostart(r) == DCR_OK);
    float prior = 18;
    for (unsigned n = 0; n < 20; ++n) {
        dcr_value_t v;
        CHECK(dcr_port_wait(port(r, "capture", "input"), &v, 1000) == DCR_OK);
        CHECK(v.real > prior && v.real >= 21 && (int)v.real % 3 == 0);
        prior = v.real;
    }
    CHECK(dcr_runtime_destroy(r) == DCR_OK);
    CHECK(pal_allocations() == 0);
}
int main(void)
{
    parsing();
    CHECK(pal_allocations() == 0);
    wiring();
    CHECK(pal_allocations() == 0);
    typed_and_bus();
    CHECK(pal_allocations() == 0);
    lifecycle();
    CHECK(pal_allocations() == 0);
    concurrency();
    CHECK(pal_allocations() == 0);
    repeated_load();
    software_pipeline();
    CHECK(init_calls == stop_calls);
    printf("DCR_TESTS PASS checks=%u init=%u cleanup=%u allocations=%zu\n", checks, init_calls,
           stop_calls, pal_allocations());
    return 0;
}
