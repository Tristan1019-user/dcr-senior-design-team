#include "dcr.h"
#include "control.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static unsigned checks, init_calls;
#define CHECK(x) do { ++checks; if (!(x)) { \
    fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #x); exit(1); \
} } while (0)

static dcr_err_t init(const cJSON *config, void **data)
{
    (void)config;
    ++init_calls;
    *data = NULL;
    return DCR_OK;
}
static void run(void *data, dcr_instance_t *instance)
{
    (void)data;
    while (!dcr_should_stop(instance))
        dcr_sleep(instance, 5);
}
static void stop(void *data) { (void)data; }
static const dcr_port_decl_t output[] = {
    {"value", DCR_OUTPUT, DCR_FLOAT, DCR_SIGNAL, false, false},
    {"request", DCR_OUTPUT, DCR_BUS_REQUEST, DCR_QUEUE, false, false}};
static const dcr_port_decl_t input[] = {
    {"value", DCR_INPUT, DCR_FLOAT, DCR_QUEUE, false, false}};
static const dcr_port_decl_t bus_input[] = {
    {"request", DCR_INPUT, DCR_BUS_REQUEST, DCR_QUEUE, false, true}};
static const dcr_descriptor_t source = {"source", output, 2, 4096, 5, init, run, stop, NULL};
static const dcr_descriptor_t sink = {"sink", input, 1, 4096, 5, init, run, stop, NULL};
static const dcr_descriptor_t bus = {"bus", bus_input, 1, 4096, 7, init, run, stop, NULL};
static const dcr_descriptor_t *const registry[] = {&source, &sink, &bus};
static const char *base =
    "{\"version\":\"1.0\",\"components\":["
    "{\"id\":\"a\",\"type\":\"source\"},{\"id\":\"b\",\"type\":\"source\"},"
    "{\"id\":\"c\",\"type\":\"source\",\"autostart\":false},{\"id\":\"x\",\"type\":\"sink\"},"
    "{\"id\":\"y\",\"type\":\"sink\"},{\"id\":\"z\",\"type\":\"sink\"},"
    "{\"id\":\"bus\",\"type\":\"bus\"}],\"connections\":["
    "{\"from\":\"a.value\",\"to\":\"x.value\"},"
    "{\"from\":\"a.value\",\"to\":\"y.value\"},"
    "{\"from\":\"a.request\",\"to\":\"bus.request\"},"
    "{\"from\":\"b.request\",\"to\":\"bus.request\"}]}";
static const char *locked =
    "[{\"id\":\"protect_x\",\"type\":\"connection_locked\",\"from\":\"a.value\",\"to\":\"x.value\"},"
    "{\"id\":\"protect_bus\",\"type\":\"connection_locked\",\"from\":\"a.request\",\"to\":\"bus.request\"}]";

static dcr_err_t build(const char *rules, dcr_runtime_t **runtime, dcr_diagnostic_t *diag)
{
    cJSON *root = cJSON_Parse(base), *policy = cJSON_Parse(rules);
    CHECK(root && policy);
    CHECK(cJSON_AddItemToObject(root, "safety_rules", policy));
    char *json = cJSON_PrintUnformatted(root);
    CHECK(json);
    dcr_err_t result = dcr_build_json(json, registry, 3, runtime, diag);
    if (result && diag)
        fprintf(stderr, "build: %s: %s\n", dcr_error_name(result), diag->context);
    cJSON_free(json);
    cJSON_Delete(root);
    return result;
}
static void bad(const char *rules, dcr_err_t expected)
{
    size_t before = pal_allocations();
    unsigned inits = init_calls;
    dcr_runtime_t *runtime = NULL;
    dcr_diagnostic_t diag;
    CHECK(build(rules, &runtime, &diag) == expected);
    CHECK(runtime == NULL);
    CHECK(diag.code == expected && diag.context[0]);
    CHECK(init_calls == inits);
    CHECK(pal_allocations() == before);
}
static char response[32768];
static bool writer(void *context, const char *line, size_t length)
{
    (void)context;
    if (length >= sizeof(response)) return false;
    memcpy(response, line, length);
    response[length] = 0;
    return true;
}
static void behavior(void)
{
    size_t baseline = pal_allocations();
    dcr_runtime_t *runtime = NULL;
    dcr_diagnostic_t diag;
    CHECK(build(locked, &runtime, &diag) == DCR_OK);
    CHECK(!strcmp(dcr_error_name(DCR_ERR_SAFETY), "safety_rule"));
    cJSON *rules = dcr_get_safety_rules(runtime);
    CHECK(rules && cJSON_GetArraySize(rules) == 2);
    CHECK(!strcmp(cJSON_GetObjectItemCaseSensitive(cJSON_GetArrayItem(rules, 0), "id")->valuestring,
                  "protect_x"));
    cJSON_Delete(rules);
    size_t before = pal_allocations();
    CHECK(dcr_disconnect(runtime, "x.value") == DCR_ERR_SAFETY);
    CHECK(dcr_connect(runtime, "b.value", "x.value", NULL, &diag) == DCR_ERR_SAFETY);
    CHECK(strstr(diag.context, "protect_x") != NULL);
    CHECK(dcr_disconnect(runtime, "bus.request") == DCR_ERR_SAFETY);
    CHECK(dcr_connect(runtime, "c.request", "bus.request", NULL, &diag) == DCR_ERR_SAFETY);
    CHECK(strstr(diag.context, "protect_bus") != NULL);
    CHECK(pal_allocations() == before);
    CHECK(dcr_destroy_component(runtime, "a") == DCR_ERR_BUSY);
    CHECK(dcr_destroy_component(runtime, "x") == DCR_ERR_BUSY);
    CHECK(dcr_disconnect(runtime, "y.value") == DCR_OK);
    CHECK(dcr_connect(runtime, "b.value", "y.value", NULL, &diag) == DCR_OK);
    CHECK(dcr_connect(runtime, "a.value", "z.value", NULL, &diag) == DCR_OK);
    CHECK(dcr_validate(runtime, &diag) == DCR_OK);
    dcr_control_t *control = dcr_control_create(runtime, "lock-test", writer, NULL);
    CHECK(control);
    const char *query = "{\"v\":1,\"id\":1,\"op\":\"get_configuration\",\"args\":{}}";
    CHECK(dcr_control_handle_line(control, query, strlen(query)));
    CHECK(strstr(response, "safety_rules") && strstr(response, "protect_x"));
    dcr_control_destroy(control);
    CHECK(dcr_start_component(runtime, "a") == DCR_OK);
    CHECK(dcr_stop_component(runtime, "a", 1000) == DCR_OK);
    CHECK(dcr_init_component(runtime, "a") == DCR_OK);
    CHECK(dcr_start_component(runtime, "a") == DCR_OK);
    CHECK(dcr_runtime_destroy(runtime) == DCR_OK);
    CHECK(pal_allocations() == baseline);
}
int main(void)
{
    behavior();
    bad("null", DCR_ERR_SCHEMA);
    bad("{}", DCR_ERR_SCHEMA);
    bad("[null]", DCR_ERR_SCHEMA);
    bad("[{\"id\":\"x\",\"type\":\"port_bounds\"}]", DCR_ERR_SCHEMA);
    bad("[{\"id\":\"x\",\"type\":\"dependency_required\"}]", DCR_ERR_SCHEMA);
    bad("[{\"id\":\"x\",\"type\":\"mutex\"}]", DCR_ERR_SCHEMA);
    bad("[{\"id\":\"x\",\"type\":\"rate_limit\"}]", DCR_ERR_SCHEMA);
    bad("[{\"type\":\"connection_locked\",\"from\":\"a.value\",\"to\":\"x.value\"}]", DCR_ERR_SCHEMA);
    bad("[{\"id\":\"bad.id\",\"type\":\"connection_locked\",\"from\":\"a.value\",\"to\":\"x.value\"}]", DCR_ERR_SCHEMA);
    bad("[{\"id\":\"x\",\"type\":\"connection_locked\",\"from\":\"a.value\",\"to\":\"x.value\",\"extra\":0}]", DCR_ERR_SCHEMA);
    bad("[{\"id\":\"x\",\"type\":\"connection_locked\",\"from\":\"missing.value\",\"to\":\"x.value\"}]", DCR_ERR_ENDPOINT);
    bad("[{\"id\":\"x\",\"type\":\"connection_locked\",\"from\":\"b.value\",\"to\":\"x.value\"}]", DCR_ERR_ENDPOINT);
    /* The first installed lock must not prevent rollback of a later rejected rule. */
    bad("[{\"id\":\"x\",\"type\":\"connection_locked\",\"from\":\"a.value\",\"to\":\"x.value\"},"
        "{\"id\":\"x\",\"type\":\"connection_locked\",\"from\":\"a.value\",\"to\":\"y.value\"}]", DCR_ERR_DUPLICATE);
    bad("[{\"id\":\"x\",\"type\":\"connection_locked\",\"from\":\"a.value\",\"to\":\"x.value\"},"
        "{\"id\":\"y\",\"type\":\"connection_locked\",\"from\":\"a.value\",\"to\":\"x.value\"}]", DCR_ERR_DUPLICATE);
    bad("[{\"id\":\"x\",\"type\":\"connection_locked\",\"from\":\"a.value\",\"to\":\"x.value\"},"
        "{\"id\":\"y\",\"type\":\"port_bounds\"}]", DCR_ERR_SCHEMA);
    bad("[0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0]", DCR_ERR_LIMIT);
    dcr_runtime_t *runtime = NULL;
    CHECK(build("[]", &runtime, NULL) == DCR_OK);
    CHECK(dcr_disconnect(runtime, "x.value") == DCR_OK);
    CHECK(dcr_runtime_destroy(runtime) == DCR_OK);
    CHECK(pal_allocations() == 0);
    printf("%u connection-lock checks passed\n", checks);
    return 0;
}
