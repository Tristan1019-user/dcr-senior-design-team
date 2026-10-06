#include "control.h"
#include "software.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static unsigned checks;
#define CHECK(condition)                                                                            \
    do {                                                                                            \
        ++checks;                                                                                  \
        if (!(condition)) {                                                                         \
            fprintf(stderr, "FAIL line %d: %s\n", __LINE__, #condition);                         \
            exit(1);                                                                               \
        }                                                                                           \
    } while (0)

typedef struct {
    uint32_t period_ms;
    bool bmp;
    uint32_t sample;
} fake_sensor_t;

static dcr_err_t fake_init(const cJSON *config, void **data, bool bmp)
{
    const cJSON *period = cJSON_GetObjectItemCaseSensitive(config, "period_ms");
    if (!cJSON_IsNumber(period) || period->valuedouble < 250 || period->valuedouble > 2000 ||
        period->valuedouble != (double)(int)period->valuedouble)
        return DCR_ERR_INIT;
    fake_sensor_t *sensor = pal_malloc(sizeof(*sensor));
    if (!sensor)
        return DCR_ERR_OOM;
    sensor->period_ms = (uint32_t)period->valuedouble;
    sensor->bmp = bmp;
    *data = sensor;
    return DCR_OK;
}

static dcr_err_t init_sht(const cJSON *config, void **data)
{
    return fake_init(config, data, false);
}

static dcr_err_t init_bmp(const cJSON *config, void **data)
{
    return fake_init(config, data, true);
}

static void fake_stop(void *data)
{
    pal_free(data);
}

static void fake_run(void *data, dcr_instance_t *instance)
{
    fake_sensor_t *sensor = data;
    dcr_port_t *temperature = dcr_port(instance, "temperature_c");
    dcr_port_t *second = dcr_port(instance, sensor->bmp ? "pressure_pa" : "humidity_pct");
    dcr_port_t *status = dcr_port(instance, "status");
    while (!dcr_should_stop(instance)) {
        dcr_value_t t = {.real = 20.0f + (float)sensor->sample};
        dcr_value_t value = {.real = sensor->bmp ? 100000.0f : 40.0f};
        dcr_value_t state = {.u32 = 0};
        dcr_port_write(temperature, &t, 0);
        dcr_port_write(second, &value, 0);
        dcr_port_write(status, &state, 0);
        ++sensor->sample;
        dcr_sleep(instance, sensor->period_ms);
    }
}

static const dcr_port_decl_t sht_ports[] = {
    {"temperature_c", DCR_OUTPUT, DCR_FLOAT, DCR_SIGNAL, false, false},
    {"humidity_pct", DCR_OUTPUT, DCR_FLOAT, DCR_SIGNAL, false, false},
    {"status", DCR_OUTPUT, DCR_UINT32, DCR_SIGNAL, false, false}};
static const dcr_port_decl_t bmp_ports[] = {
    {"temperature_c", DCR_OUTPUT, DCR_FLOAT, DCR_SIGNAL, false, false},
    {"pressure_pa", DCR_OUTPUT, DCR_FLOAT, DCR_SIGNAL, false, false},
    {"status", DCR_OUTPUT, DCR_UINT32, DCR_SIGNAL, false, false}};
static const dcr_descriptor_t sht_desc = {
    "sht45_sensor", sht_ports, 3, 4096, 5, init_sht, fake_run, fake_stop, NULL};
static const dcr_descriptor_t bmp_desc = {
    "bmp280_sensor", bmp_ports, 3, 4096, 5, init_bmp, fake_run, fake_stop, NULL};

typedef struct {
    char line[DCR_CONTROL_MAX_FRAME + 32];
    size_t writes;
} capture_t;

static bool capture_write(void *context, const char *line, size_t length)
{
    capture_t *capture = context;
    CHECK(length < sizeof(capture->line));
    CHECK(length >= 6 && !memcmp(line, "@DCR ", 5) && line[length - 1] == '\n');
    memcpy(capture->line, line, length);
    capture->line[length] = 0;
    ++capture->writes;
    return true;
}

static cJSON *last_message(const capture_t *capture)
{
    CHECK(capture->writes > 0);
    CHECK(strlen(capture->line) > 5);
    return cJSON_Parse(capture->line + 5);
}

static void expect_error(dcr_control_t *control, capture_t *capture, const char *request,
                         const char *code)
{
    size_t before = capture->writes;
    CHECK(dcr_control_handle_line(control, request, strlen(request)));
    CHECK(capture->writes == before + 1);
    cJSON *message = last_message(capture);
    CHECK(message && cJSON_IsFalse(cJSON_GetObjectItemCaseSensitive(message, "ok")));
    const cJSON *error = cJSON_GetObjectItemCaseSensitive(message, "error");
    CHECK(error && !strcmp(cJSON_GetObjectItemCaseSensitive(error, "code")->valuestring, code));
    cJSON_Delete(message);
}

static bool send_request(dcr_control_t *control, const char *request)
{
    return dcr_control_handle_line(control, request, strlen(request));
}

static dcr_runtime_t *make_runtime(void)
{
    const dcr_descriptor_t *registry[] = {&sht_desc, &bmp_desc, dcr_software_registry[3]};
    dcr_runtime_t *runtime = dcr_runtime_create(registry, 3);
    CHECK(runtime);
    cJSON *sht = cJSON_Parse("{\"pins_confirmed\":true,\"sda\":8,\"scl\":9,\"period_ms\":300}");
    cJSON *bmp = cJSON_Parse("{\"pins_confirmed\":true,\"sda\":10,\"scl\":11,\"period_ms\":400}");
    CHECK(dcr_create_component(runtime, "sht45", "sht45_sensor", sht, NULL, NULL) == DCR_OK);
    CHECK(dcr_create_component(runtime, "bmp280", "bmp280_sensor", bmp, NULL, NULL) == DCR_OK);
    CHECK(dcr_create_component(runtime, "logger", "logger", NULL, NULL, NULL) == DCR_OK);
    cJSON_Delete(sht);
    cJSON_Delete(bmp);
    CHECK(dcr_connect(runtime, "sht45.temperature_c", "logger.input", NULL, NULL) == DCR_OK);
    CHECK(dcr_init_component(runtime, "sht45") == DCR_OK);
    CHECK(dcr_init_component(runtime, "bmp280") == DCR_OK);
    CHECK(dcr_init_component(runtime, "logger") == DCR_OK);
    return runtime;
}

static void strict_requests(dcr_control_t *control, capture_t *capture)
{
    expect_error(control, capture,
                 "{\"v\":1,\"id\":1,\"id\":1,\"op\":\"get_components\",\"args\":{}}",
                 "INVALID_ARGUMENT");
    expect_error(control, capture,
                 "{\"v\":1,\"id\":2,\"op\":\"get_components\",\"args\":{},\"extra\":0}",
                 "INVALID_ARGUMENT");
    expect_error(control, capture,
                 "{\"v\":1,\"id\":3,\"op\":\"get_components\",\"args\":{\"x\":1}}",
                 "INVALID_ARGUMENT");
    expect_error(control, capture,
                 "{\"v\":1,\"id\":true,\"op\":\"get_components\",\"args\":{}}",
                 "INVALID_ARGUMENT");
    expect_error(control, capture,
                 "{\"v\":1,\"id\":4.5,\"op\":\"get_components\",\"args\":{}}",
                 "INVALID_ARGUMENT");
    expect_error(control, capture,
                 "{\"v\":2,\"id\":5,\"op\":\"get_components\",\"args\":{}}",
                 "UNSUPPORTED_VERSION");
    expect_error(control, capture,
                 "{\"v\":1,\"id\":6,\"op\":\"unknown\",\"args\":{}}",
                 "UNKNOWN_OPERATION");
    expect_error(control, capture,
                 "{\"v\":1,\"id\":7,\"op\":\"get_components\",\"args\":{}} trailing",
                 "INVALID_ARGUMENT");
    expect_error(control, capture,
                 "{\"v\":1,\"id\":8,\"op\":\"get_component\",\"args\":{\"component\":{\"x\":1,\"x\":2}}}",
                 "INVALID_ARGUMENT");
    expect_error(control, capture,
                 "{\"v\":1,\"id\":9,\"op\":\"get_component\",\"args\":{\"component\":\"sht45\\u0000x\"}}",
                 "INVALID_ARGUMENT");
    expect_error(control, capture,
                 "{\"v\":1,\"id\":1e309,\"op\":\"get_components\",\"args\":{}}",
                 "INVALID_ARGUMENT");
    char oversized[DCR_CONTROL_MAX_FRAME + 2];
    memset(oversized, 'x', sizeof(oversized));
    size_t before = capture->writes;
    CHECK(dcr_control_handle_line(control, oversized, sizeof(oversized)));
    CHECK(capture->writes == before + 1);
    cJSON *message = last_message(capture);
    CHECK(!strcmp(cJSON_GetObjectItemCaseSensitive(cJSON_GetObjectItemCaseSensitive(message, "error"), "code")
                      ->valuestring,
                  "FRAME_TOO_LARGE"));
    cJSON_Delete(message);
}

static void protocol_operations(dcr_control_t *control, capture_t *capture, dcr_runtime_t *runtime)
{
    size_t before = capture->writes;
    CHECK(dcr_control_emit_snapshot(control));
    CHECK(capture->writes == before + 1);
    CHECK(dcr_control_emit_snapshot(control));
    CHECK(capture->writes == before + 1);

    expect_error(control, capture,
                 "{\"v\":1,\"id\":10,\"op\":\"init_component\",\"args\":{\"component\":\"sht45\"}}",
                 "INVALID_STATE");
    CHECK(send_request(control,
                       "{\"v\":1,\"id\":11,\"op\":\"start_component\",\"args\":{\"component\":\"sht45\"}}"));
    cJSON *started = last_message(capture);
    CHECK(cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(started, "ok")));
    cJSON_Delete(started);
    expect_error(control, capture,
                 "{\"v\":1,\"id\":12,\"op\":\"stop_component\",\"args\":{\"component\":\"logger\"}}",
                 "FORBIDDEN");

    CHECK(send_request(control, "{\"v\":1,\"id\":13,\"op\":\"get_configuration\",\"args\":{}}"));
    cJSON *message = last_message(capture);
    CHECK(cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(message, "ok")));
    cJSON *components = cJSON_GetObjectItemCaseSensitive(cJSON_GetObjectItemCaseSensitive(message, "result"),
                                                         "components");
    CHECK(cJSON_GetArraySize(components) == 3);
    cJSON *logger = cJSON_GetArrayItem(components, 2);
    CHECK(cJSON_GetObjectItemCaseSensitive(logger, "config") &&
          cJSON_GetArraySize(cJSON_GetObjectItemCaseSensitive(logger, "config")) == 0);
    cJSON *settings = cJSON_GetObjectItemCaseSensitive(cJSON_GetObjectItemCaseSensitive(message, "result"),
                                                       "settings");
    CHECK(settings);
    CHECK(cJSON_GetNumberValue(cJSON_GetObjectItemCaseSensitive(settings, "watchdog_timeout_ms")) == 5000);
    CHECK(cJSON_GetNumberValue(cJSON_GetObjectItemCaseSensitive(settings, "default_stack_size")) == 0);
    CHECK(cJSON_GetNumberValue(cJSON_GetObjectItemCaseSensitive(settings, "signal_wait_timeout_ms")) == 1000);
    CHECK(!strcmp(cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(settings, "log_level")), "info"));
    cJSON_Delete(message);

    pal_sleep(30);

    CHECK(send_request(control, "{\"v\":1,\"id\":15,\"op\":\"get_component\",\"args\":{\"component\":\"sht45\"}}"));
    message = last_message(capture);
    cJSON *component = cJSON_GetObjectItemCaseSensitive(cJSON_GetObjectItemCaseSensitive(message, "result"),
                                                        "outputs");
    cJSON *temperature = cJSON_GetObjectItemCaseSensitive(component, "temperature_c");
    CHECK(cJSON_GetNumberValue(cJSON_GetObjectItemCaseSensitive(temperature, "sequence")) > 0);
    CHECK(!cJSON_IsNull(cJSON_GetObjectItemCaseSensitive(temperature, "timestamp_ms")));
    cJSON_Delete(message);

    expect_error(control, capture,
                 "{\"v\":1,\"id\":16,\"op\":\"set_parameter\",\"args\":{\"component\":\"sht45\",\"name\":\"period_ms\",\"value\":750}}",
                 "INVALID_STATE");
    CHECK(send_request(control, "{\"v\":1,\"id\":17,\"op\":\"stop_component\",\"args\":{\"component\":\"sht45\"}}"));
    CHECK(cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(last_message(capture), "ok")));
    expect_error(control, capture,
                 "{\"v\":1,\"id\":18,\"op\":\"set_parameter\",\"args\":{\"component\":\"sht45\",\"name\":\"period_ms\",\"value\":NaN}}",
                 "INVALID_ARGUMENT");
    expect_error(control, capture,
                 "{\"v\":1,\"id\":19,\"op\":\"set_parameter\",\"args\":{\"component\":\"sht45\",\"name\":\"period_ms\",\"value\":1e309}}",
                 "INVALID_ARGUMENT");
    expect_error(control, capture,
                 "{\"v\":1,\"id\":20,\"op\":\"set_parameter\",\"args\":{\"component\":\"sht45\",\"name\":\"sda\",\"value\":750}}",
                 "INVALID_ARGUMENT");
    CHECK(send_request(control, "{\"v\":1,\"id\":21,\"op\":\"set_parameter\",\"args\":{\"component\":\"sht45\",\"name\":\"period_ms\",\"value\":750}}"));
    CHECK(cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(last_message(capture), "ok")));

    CHECK(send_request(control, "{\"v\":1,\"id\":22,\"op\":\"init_component\",\"args\":{\"component\":\"sht45\"}}"));
    message = last_message(capture);
    CHECK(cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(message, "ok")));
    component = cJSON_GetObjectItemCaseSensitive(cJSON_GetObjectItemCaseSensitive(message, "result"), "outputs");
    temperature = cJSON_GetObjectItemCaseSensitive(component, "temperature_c");
    CHECK(cJSON_GetNumberValue(cJSON_GetObjectItemCaseSensitive(temperature, "sequence")) == 0);
    CHECK(cJSON_IsNull(cJSON_GetObjectItemCaseSensitive(temperature, "timestamp_ms")));
    cJSON_Delete(message);

    CHECK(send_request(control, "{\"v\":1,\"id\":23,\"op\":\"start_component\",\"args\":{\"component\":\"sht45\"}}"));
    CHECK(cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(last_message(capture), "ok")));
    pal_sleep(20);
    CHECK(dcr_component_count(runtime) == 3);
}

int main(void)
{
    dcr_runtime_t *runtime = make_runtime();
    capture_t capture = {0};
    dcr_control_t *control = dcr_control_create(runtime, "test-boot", capture_write, &capture);
    CHECK(control);
    strict_requests(control, &capture);
    protocol_operations(control, &capture, runtime);
    dcr_control_destroy(control);
    CHECK(dcr_runtime_destroy(runtime) == DCR_OK);
    CHECK(pal_allocations() == 0);
    printf("DCR_CONTROL_TESTS PASS checks=%u\n", checks);
    return 0;
}
