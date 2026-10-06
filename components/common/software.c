#include "software.h"
#include <math.h>
typedef struct {
    float value, step, gain;
    uint32_t period_ms;
    bool quiet;
} state_t;
static bool number(const cJSON *c, const char *key, float def, float *out)
{
    const cJSON *v = cJSON_GetObjectItemCaseSensitive(c, key);
    if (!v) {
        *out = def;
        return true;
    }
    if (!cJSON_IsNumber(v) || !isfinite(v->valuedouble) || fabs(v->valuedouble) > 1e20)
        return false;
    *out = (float)v->valuedouble;
    return true;
}
static dcr_err_t init(const cJSON *c, void **data)
{
    state_t *s = pal_malloc(sizeof(*s));
    if (!s)
        return DCR_ERR_OOM;
    *data = s;
    float period;
    if (!number(c, "value", 0, &s->value) || !number(c, "step", 1, &s->step) ||
        !number(c, "gain", 2, &s->gain) || !number(c, "period_ms", 50, &period) || period < 1 ||
        period > 60000 || floorf(period) != period)
        return DCR_ERR_INIT;
    s->period_ms = (uint32_t)period;
    const cJSON *quiet = cJSON_GetObjectItemCaseSensitive(c, "quiet");
    if (quiet && !cJSON_IsBool(quiet))
        return DCR_ERR_INIT;
    s->quiet = cJSON_IsTrue(quiet);
    return DCR_OK;
}
static void stop(void *data)
{
    pal_free(data);
}
static void counter(void *data, dcr_instance_t *i)
{
    state_t *s = data;
    dcr_port_t *out = dcr_port(i, "output");
    while (!dcr_should_stop(i)) {
        dcr_value_t v = {.real = s->value};
        if (dcr_port_write(out, &v, DCR_DEFAULT_TIMEOUT) == DCR_OK)
            s->value += s->step;
        dcr_sleep(i, s->period_ms);
    }
}
static void constant(void *data, dcr_instance_t *i)
{
    state_t *s = data;
    dcr_port_t *out = dcr_port(i, "output");
    while (!dcr_should_stop(i)) {
        dcr_value_t v = {.real = s->value};
        dcr_port_write(out, &v, DCR_DEFAULT_TIMEOUT);
        dcr_sleep(i, s->period_ms);
    }
}
static void scale(void *data, dcr_instance_t *i)
{
    state_t *s = data;
    dcr_port_t *in = dcr_port(i, "input"), *out = dcr_port(i, "output");
    while (!dcr_should_stop(i)) {
        dcr_value_t v;
        if (dcr_port_wait(in, &v, DCR_DEFAULT_TIMEOUT) == DCR_OK) {
            v.real *= s->gain;
            dcr_port_write(out, &v, DCR_DEFAULT_TIMEOUT);
        }
    }
}
static void logger(void *data, dcr_instance_t *i)
{
    state_t *s = data;
    dcr_port_t *in = dcr_port(i, "input");
    while (!dcr_should_stop(i)) {
        dcr_value_t v;
        if (dcr_port_wait(in, &v, DCR_DEFAULT_TIMEOUT) == DCR_OK && !s->quiet)
            dcr_log(i, "info", "value=%.3f", (double)v.real);
    }
}
static const dcr_port_decl_t source_ports[] = {
    {"output", DCR_OUTPUT, DCR_FLOAT, DCR_SIGNAL, false, false}};
static const dcr_port_decl_t scale_ports[] = {
    {"input", DCR_INPUT, DCR_FLOAT, DCR_SIGNAL, true, false},
    {"output", DCR_OUTPUT, DCR_FLOAT, DCR_SIGNAL, false, false}};
static const dcr_port_decl_t log_ports[] = {
    {"input", DCR_INPUT, DCR_FLOAT, DCR_QUEUE, true, false}};
static const dcr_port_decl_t signal_log_ports[] = {
    {"input", DCR_INPUT, DCR_FLOAT, DCR_SIGNAL, true, false}};
static const dcr_descriptor_t counter_desc = {"counter_source", source_ports, 1,   4096, 5, init,
                                              counter,          stop,         NULL};
static const dcr_descriptor_t constant_desc = {"constant_float", source_ports, 1,   4096, 5, init,
                                               constant,         stop,         NULL};
static const dcr_descriptor_t scale_desc = {"float_scale", scale_ports, 2,    4096, 5,
                                            init,          scale,       stop, NULL};
static const dcr_descriptor_t log_desc = {"logger", log_ports, 1,    4096, 5,
                                          init,     logger,    stop, NULL};
static const dcr_descriptor_t signal_log_desc = {
    "signal_logger", signal_log_ports, 1, 4096, 5, init, logger, stop, NULL};
const dcr_descriptor_t *const dcr_software_registry[] = {&counter_desc, &constant_desc, &scale_desc,
                                                         &log_desc, &signal_log_desc};
const size_t dcr_software_registry_count =
    sizeof(dcr_software_registry) / sizeof(*dcr_software_registry);
