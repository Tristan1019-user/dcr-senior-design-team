#include "control.h"
#include <ctype.h>
#include <math.h>
#include <stdio.h>
#include <string.h>

struct dcr_control {
    dcr_runtime_t *runtime;
    dcr_control_write_fn writer;
    void *writer_context;
    char boot_id[DCR_NAME_SIZE];
    uint32_t started_ms;
    uint32_t last_snapshot_ms;
    bool snapshot_emitted;
    char output[DCR_CONTROL_MAX_FRAME + 8];
};

_Static_assert(sizeof(dcr_component_snapshot_t) <= 2048,
               "component snapshots must remain bounded for the firmware task stack");

static const char *const capabilities[] = {
    "get_runtime_info", "get_components",    "get_component",  "get_topology",
    "get_configuration", "init_component",  "start_component", "stop_component",
    "set_parameter"};

static bool valid_integer(const cJSON *value, double low, double high)
{
    return cJSON_IsNumber(value) && isfinite(value->valuedouble) &&
           value->valuedouble >= low && value->valuedouble <= high &&
           floor(value->valuedouble) == value->valuedouble;
}

static bool bounded_json(const char *text, size_t length)
{
    if (!text || !length || length > DCR_CONTROL_MAX_FRAME)
        return false;
    unsigned depth = 0;
    bool quoted = false, escaped = false;
    for (size_t n = 0; n < length; ++n) {
        unsigned char ch = (unsigned char)text[n];
        if (quoted) {
            if (ch == 0)
                return false;
            if (escaped)
                escaped = false;
            else if (ch == '\\') {
                if (n + 5 < length && text[n + 1] == 'u' && text[n + 2] == '0' &&
                    text[n + 3] == '0' && text[n + 4] == '0' && text[n + 5] == '0')
                    return false;
                escaped = true;
            } else if (ch == '"')
                quoted = false;
            continue;
        }
        if (ch == '"') {
            quoted = true;
        } else if (ch == '{' || ch == '[') {
            if (++depth > 32)
                return false;
        } else if (ch == '}' || ch == ']') {
            if (!depth)
                return false;
            --depth;
        } else if (ch < 0x20 && ch != '\t' && ch != '\r' && ch != '\n') {
            return false;
        }
    }
    return !quoted && !escaped && depth == 0;
}

static bool has_duplicate_keys(const cJSON *value)
{
    if (!value)
        return false;
    if (cJSON_IsObject(value)) {
        for (const cJSON *p = value->child; p; p = p->next) {
            for (const cJSON *q = p->next; q; q = q->next)
                if (p->string && q->string && !strcmp(p->string, q->string))
                    return true;
            if (has_duplicate_keys(p))
                return true;
        }
        return false;
    }
    if (cJSON_IsArray(value))
        for (const cJSON *p = value->child; p; p = p->next)
            if (has_duplicate_keys(p))
                return true;
    return false;
}

static bool exact_keys(const cJSON *object, const char *const *allowed, size_t count)
{
    if (!cJSON_IsObject(object))
        return false;
    size_t actual = 0;
    for (const cJSON *p = object->child; p; p = p->next) {
        bool known = false;
        for (size_t n = 0; n < count; ++n)
            if (p->string && !strcmp(p->string, allowed[n]))
                known = true;
        if (!known || ++actual > count)
            return false;
    }
    return actual == count;
}

static bool valid_name_string(const cJSON *value)
{
    if (!cJSON_IsString(value) || !value->valuestring || !*value->valuestring ||
        strlen(value->valuestring) >= DCR_NAME_SIZE)
        return false;
    for (const char *p = value->valuestring; *p; ++p)
        if (!isalnum((unsigned char)*p) && *p != '_' && *p != '-')
            return false;
    return true;
}

static const char *protocol_error(dcr_err_t error)
{
    switch (error) {
    case DCR_ERR_ENDPOINT:
        return "NOT_FOUND";
    case DCR_ERR_ARGUMENT:
    case DCR_ERR_SCHEMA:
    case DCR_ERR_DUPLICATE:
    case DCR_ERR_TYPE:
        return "INVALID_ARGUMENT";
    case DCR_ERR_STATE:
    case DCR_ERR_STOPPED:
    case DCR_ERR_REQUIRED:
    case DCR_ERR_BUSY:
        return "INVALID_STATE";
    case DCR_ERR_TIMEOUT:
        return "TIMEOUT";
    default:
        return "INTERNAL";
    }
}

static bool send_payload(dcr_control_t *control, const char *payload)
{
    size_t length = strlen(payload);
    if (length > DCR_CONTROL_MAX_FRAME - 5)
        return false;
    int written = snprintf(control->output, sizeof(control->output), "@DCR %s\n", payload);
    if (written < 0 || (size_t)written >= sizeof(control->output))
        return false;
    return control->writer(control->writer_context, control->output, (size_t)written);
}

static bool send_response(dcr_control_t *control, bool id_valid, int32_t id, cJSON *result,
                          const char *error_code, const char *message)
{
    cJSON *root = cJSON_CreateObject();
    if (!root)
        goto fail;
    if (!cJSON_AddNumberToObject(root, "v", 1) ||
        (id_valid ? !cJSON_AddNumberToObject(root, "id", id) : !cJSON_AddNullToObject(root, "id")) ||
        !cJSON_AddBoolToObject(root, "ok", error_code == NULL))
        goto fail;
    if (error_code) {
        cJSON *error = cJSON_AddObjectToObject(root, "error");
        if (!error || !cJSON_AddStringToObject(error, "code", error_code) ||
            !cJSON_AddStringToObject(error, "message", message ? message : "request rejected"))
            goto fail;
        cJSON_Delete(result);
    } else if (!cJSON_AddItemToObject(root, "result", result)) {
        goto fail;
    } else {
        result = NULL;
    }
    char *text = cJSON_PrintUnformatted(root);
    cJSON_Delete(root);
    if (!text)
        return false;
    bool sent = send_payload(control, text);
    cJSON_free(text);
    return sent;
fail:
    cJSON_Delete(root);
    cJSON_Delete(result);
    return false;
}

static bool send_error(dcr_control_t *control, bool id_valid, int32_t id, const char *code,
                       const char *message)
{
    return send_response(control, id_valid, id, NULL, code, message);
}

static bool send_runtime_error(dcr_control_t *control, bool id_valid, int32_t id, dcr_err_t error,
                               const char *message)
{
    return send_error(control, id_valid, id, protocol_error(error), message);
}

static bool add_value(cJSON *object, const dcr_output_snapshot_t *output)
{
    if (!output->published)
        return cJSON_AddNullToObject(object, "value") != NULL;
    switch (output->type) {
    case DCR_BOOL:
        return cJSON_AddBoolToObject(object, "value", output->value.boolean) != NULL;
    case DCR_INT32:
        return cJSON_AddNumberToObject(object, "value", output->value.i32) != NULL;
    case DCR_UINT32:
        return cJSON_AddNumberToObject(object, "value", output->value.u32) != NULL;
    case DCR_FLOAT:
        if (!isfinite(output->value.real))
            return cJSON_AddNullToObject(object, "value") != NULL;
        return cJSON_AddNumberToObject(object, "value", output->value.real) != NULL;
    default:
        return false;
    }
}

static cJSON *component_json(const dcr_component_snapshot_t *snapshot, bool outputs)
{
    cJSON *object = cJSON_CreateObject();
    if (!object || !cJSON_AddStringToObject(object, "id", snapshot->id) ||
        !cJSON_AddStringToObject(object, "type", snapshot->type) ||
        !cJSON_AddStringToObject(object, "state", dcr_state_name(snapshot->state)) ||
        !cJSON_AddStringToObject(object, "fault", dcr_error_name(snapshot->fault)) ||
        !cJSON_AddItemToObject(object, "config", cJSON_Duplicate(snapshot->config, true)))
        goto fail;
    if (outputs) {
        cJSON *items = cJSON_AddObjectToObject(object, "outputs");
        if (!items)
            goto fail;
        for (size_t n = 0; n < snapshot->output_count; ++n) {
            const dcr_output_snapshot_t *output = &snapshot->outputs[n];
            cJSON *item = cJSON_AddObjectToObject(items, output->name);
            if (!item || !add_value(item, output) ||
                !cJSON_AddNumberToObject(item, "sequence", (double)output->sequence) ||
                (output->published ? !cJSON_AddNumberToObject(item, "timestamp_ms",
                                                               output->timestamp_ms)
                                   : !cJSON_AddNullToObject(item, "timestamp_ms")) ||
                (output->published ? !cJSON_AddNumberToObject(item, "age_ms", output->age_ms)
                                   : !cJSON_AddNullToObject(item, "age_ms")))
                goto fail;
        }
    }
    return object;
fail:
    cJSON_Delete(object);
    return NULL;
}

static cJSON *component_by_id(dcr_control_t *control, const char *id, bool outputs,
                              dcr_err_t *error)
{
    dcr_component_snapshot_t snapshot = {0};
    *error = dcr_get_component_snapshot(control->runtime, id, &snapshot);
    if (*error)
        return NULL;
    cJSON *object = component_json(&snapshot, outputs);
    dcr_component_snapshot_clear(&snapshot);
    if (!object)
        *error = DCR_ERR_OOM;
    return object;
}

static cJSON *all_components(dcr_control_t *control, bool outputs, dcr_err_t *error)
{
    cJSON *items = cJSON_CreateArray();
    if (!items) {
        *error = DCR_ERR_OOM;
        return NULL;
    }
    size_t count = dcr_component_count(control->runtime);
    for (size_t n = 0; n < count; ++n) {
        dcr_component_snapshot_t snapshot = {0};
        *error = dcr_get_component_snapshot_at(control->runtime, n, &snapshot);
        if (*error) {
            cJSON_Delete(items);
            return NULL;
        }
        cJSON *object = component_json(&snapshot, outputs);
        dcr_component_snapshot_clear(&snapshot);
        if (!object || !cJSON_AddItemToArray(items, object)) {
            cJSON_Delete(object);
            cJSON_Delete(items);
            *error = DCR_ERR_OOM;
            return NULL;
        }
    }
    return items;
}

static cJSON *configuration(dcr_control_t *control, dcr_err_t *error)
{
    cJSON *result = cJSON_CreateObject();
    cJSON *items = cJSON_CreateArray();
    if (!result || !items) {
        cJSON_Delete(result);
        cJSON_Delete(items);
        *error = DCR_ERR_OOM;
        return NULL;
    }
    size_t count = dcr_component_count(control->runtime);
    for (size_t n = 0; n < count; ++n) {
        dcr_component_snapshot_t snapshot = {0};
        cJSON *entry = NULL;
        *error = dcr_get_component_snapshot_at(control->runtime, n, &snapshot);
        if (*error)
            goto fail;
        entry = cJSON_CreateObject();
        if (!entry || !cJSON_AddStringToObject(entry, "id", snapshot.id) ||
            !cJSON_AddStringToObject(entry, "type", snapshot.type)) {
            cJSON_Delete(entry);
            dcr_component_snapshot_clear(&snapshot);
            *error = DCR_ERR_OOM;
            goto fail;
        }
        if (!cJSON_AddItemToObject(entry, "config", snapshot.config)) {
            cJSON_Delete(entry);
            dcr_component_snapshot_clear(&snapshot);
            *error = DCR_ERR_OOM;
            goto fail;
        }
        snapshot.config = NULL;
        if (!cJSON_AddItemToArray(items, entry)) {
            cJSON_Delete(entry);
            dcr_component_snapshot_clear(&snapshot);
            *error = DCR_ERR_OOM;
            goto fail;
        }
        dcr_component_snapshot_clear(&snapshot);
    }
    if (!cJSON_AddItemToObject(result, "components", items)) {
        *error = DCR_ERR_OOM;
        cJSON_Delete(items);
        cJSON_Delete(result);
        return NULL;
    }
    if (!cJSON_AddBoolToObject(result, "persistent", false)) {
        *error = DCR_ERR_OOM;
        cJSON_Delete(result);
        return NULL;
    }
    dcr_settings_t settings;
    *error = dcr_get_settings(control->runtime, &settings);
    if (*error) {
        cJSON_Delete(result);
        return NULL;
    }
    const char *const levels[] = {"debug", "info", "warn", "error"};
    const char *level = settings.log_level < 4 ? levels[settings.log_level] : "unknown";
    cJSON *global = cJSON_CreateObject();
    if (!global || !cJSON_AddNumberToObject(global, "watchdog_timeout_ms",
                                             settings.watchdog_timeout_ms) ||
        !cJSON_AddNumberToObject(global, "default_stack_size", settings.default_stack_size) ||
        !cJSON_AddNumberToObject(global, "signal_wait_timeout_ms",
                                 settings.signal_wait_timeout_ms) ||
        !cJSON_AddStringToObject(global, "log_level", level) ||
        !cJSON_AddItemToObject(result, "settings", global)) {
        cJSON_Delete(global);
        *error = DCR_ERR_OOM;
        cJSON_Delete(result);
        return NULL;
    }
    cJSON *rules = dcr_get_safety_rules(control->runtime);
    if (!rules || !cJSON_AddItemToObject(result, "safety_rules", rules)) {
        cJSON_Delete(rules);
        cJSON_Delete(result);
        *error = DCR_ERR_OOM;
        return NULL;
    }
    return result;
fail:
    cJSON_Delete(items);
    cJSON_Delete(result);
    return NULL;
}

static cJSON *runtime_info(dcr_control_t *control)
{
    cJSON *result = cJSON_CreateObject();
    cJSON *items = cJSON_CreateArray();
    bool attached = false;
    if (!result || !items || !cJSON_AddNumberToObject(result, "protocol", 1) ||
        !cJSON_AddStringToObject(result, "boot_id", control->boot_id) ||
        !cJSON_AddNumberToObject(result, "uptime_ms", pal_millis() - control->started_ms) ||
        !cJSON_AddItemToObject(result, "capabilities", items)) {
        cJSON_Delete(result);
        if (!attached)
            cJSON_Delete(items);
        return NULL;
    }
    attached = true;
    for (size_t n = 0; n < sizeof(capabilities) / sizeof(*capabilities); ++n)
        if (!cJSON_AddItemToArray(items, cJSON_CreateString(capabilities[n]))) {
            cJSON_Delete(result);
            return NULL;
        }
    return result;
}

static bool allowed_sensor(const char *id, const char **type)
{
    if (!strcmp(id, "sht45")) {
        *type = "sht45_sensor";
        return true;
    }
    if (!strcmp(id, "bmp280")) {
        *type = "bmp280_sensor";
        return true;
    }
    return false;
}

static bool send_component(dcr_control_t *control, bool id_valid, int32_t id, const char *name,
                           bool outputs)
{
    dcr_err_t error;
    cJSON *result = component_by_id(control, name, outputs, &error);
    if (!result)
        return send_runtime_error(control, id_valid, id, error, "component inspection failed");
    if (!send_response(control, id_valid, id, result, NULL, NULL))
        return send_error(control, id_valid, id, "INTERNAL", "response exceeds frame limit");
    return true;
}

static bool mutate(dcr_control_t *control, bool id_valid, int32_t request_id, const char *op,
                   const cJSON *args)
{
    const cJSON *component = cJSON_GetObjectItemCaseSensitive(args, "component");
    if (!valid_name_string(component))
        return send_error(control, id_valid, request_id, "INVALID_ARGUMENT",
                          "args.component must be a bounded name");
    const char *name = component->valuestring;
    const char *expected_type = NULL;
    if (!allowed_sensor(name, &expected_type))
        return send_error(control, id_valid, request_id, "FORBIDDEN",
                          "only sht45 and bmp280 are mutable");

    dcr_component_snapshot_t snapshot = {0};
    dcr_err_t error = dcr_get_component_snapshot(control->runtime, name, &snapshot);
    if (error)
        return send_runtime_error(control, id_valid, request_id, error, "component not found");
    if (strcmp(snapshot.type, expected_type)) {
        dcr_component_snapshot_clear(&snapshot);
        return send_error(control, id_valid, request_id, "FORBIDDEN", "component type is not a sensor");
    }

    const cJSON *value = cJSON_GetObjectItemCaseSensitive(args, "value");
    if (!strcmp(op, "init_component") && snapshot.state != DCR_STOPPED) {
        dcr_component_snapshot_clear(&snapshot);
        return send_error(control, id_valid, request_id, "INVALID_STATE", "init requires STOPPED");
    }
    if (!strcmp(op, "start_component") && snapshot.state != DCR_INITIALIZED) {
        dcr_component_snapshot_clear(&snapshot);
        return send_error(control, id_valid, request_id, "INVALID_STATE", "start requires INITIALIZED");
    }
    if (!strcmp(op, "stop_component") && snapshot.state != DCR_RUNNING &&
        snapshot.state != DCR_INITIALIZED && snapshot.state != DCR_FAULTED) {
        dcr_component_snapshot_clear(&snapshot);
        return send_error(control, id_valid, request_id, "INVALID_STATE", "stop requires an active state");
    }
    if (!strcmp(op, "set_parameter")) {
        if (snapshot.state != DCR_STOPPED) {
            dcr_component_snapshot_clear(&snapshot);
            return send_error(control, id_valid, request_id, "INVALID_STATE", "set_parameter requires STOPPED");
        }
        if (!valid_integer(value, 250, 2000)) {
            dcr_component_snapshot_clear(&snapshot);
            return send_error(control, id_valid, request_id, "INVALID_ARGUMENT",
                              "period_ms must be an integer from 250 to 2000");
        }
        const cJSON *parameter_name = cJSON_GetObjectItemCaseSensitive(args, "name");
        if (!cJSON_IsString(parameter_name) || strcmp(parameter_name->valuestring, "period_ms")) {
            dcr_component_snapshot_clear(&snapshot);
            return send_error(control, id_valid, request_id, "INVALID_ARGUMENT",
                              "only period_ms is supported");
        }
        cJSON *period = cJSON_GetObjectItemCaseSensitive(snapshot.config, "period_ms");
        if (!cJSON_IsNumber(period)) {
            dcr_component_snapshot_clear(&snapshot);
            return send_error(control, id_valid, request_id, "INTERNAL", "sensor period is absent");
        }
        cJSON_SetNumberValue(period, value->valuedouble);
        error = dcr_update_config(control->runtime, name, snapshot.config);
        dcr_component_snapshot_clear(&snapshot);
    } else if (!strcmp(op, "init_component")) {
        dcr_component_snapshot_clear(&snapshot);
        error = dcr_init_component(control->runtime, name);
    } else if (!strcmp(op, "start_component")) {
        dcr_component_snapshot_clear(&snapshot);
        error = dcr_start_component(control->runtime, name);
    } else {
        dcr_component_snapshot_clear(&snapshot);
        error = dcr_stop_component(control->runtime, name, DCR_CONTROL_STOP_TIMEOUT_MS);
    }
    if (error)
        return send_runtime_error(control, id_valid, request_id, error, "mutation failed");
    return send_component(control, id_valid, request_id, name, true);
}

dcr_control_t *dcr_control_create(dcr_runtime_t *runtime, const char *boot_id,
                                  dcr_control_write_fn writer, void *context)
{
    if (!runtime || !writer)
        return NULL;
    const char *id = boot_id;
    char generated[DCR_NAME_SIZE];
    if (!id) {
        snprintf(generated, sizeof(generated), "boot-%08lx", (unsigned long)pal_millis());
        id = generated;
    }
    if (!*id || strlen(id) >= DCR_NAME_SIZE)
        return NULL;
    dcr_control_t *control = pal_malloc(sizeof(*control));
    if (!control)
        return NULL;
    control->runtime = runtime;
    control->writer = writer;
    control->writer_context = context;
    strcpy(control->boot_id, id);
    control->started_ms = pal_millis();
    return control;
}

void dcr_control_destroy(dcr_control_t *control)
{
    pal_free(control);
}

bool dcr_control_handle_line(dcr_control_t *control, const char *line, size_t length)
{
    if (!control)
        return false;
    if (length > DCR_CONTROL_MAX_FRAME)
        return send_error(control, false, 0, "FRAME_TOO_LARGE", "request frame exceeds 8192 bytes");
    if (!bounded_json(line, length))
        return send_error(control, false, 0, "INVALID_ARGUMENT", "malformed JSON frame");

    const char *end = NULL;
    cJSON *request = cJSON_ParseWithLengthOpts(line, length, &end, false);
    bool id_valid = false;
    int32_t request_id = 0;
    if (request && cJSON_IsObject(request)) {
        const cJSON *id = cJSON_GetObjectItemCaseSensitive(request, "id");
        id_valid = valid_integer(id, 1, 2147483647);
        if (id_valid)
            request_id = (int32_t)id->valuedouble;
    }
    if (end)
        while (end < line + length && isspace((unsigned char)*end))
            ++end;
    if (!request || !end || end != line + length || !cJSON_IsObject(request) ||
        has_duplicate_keys(request)) {
        cJSON_Delete(request);
        return send_error(control, id_valid, request_id, "INVALID_ARGUMENT", "malformed or duplicate JSON keys");
    }
    const char *const top[] = {"v", "id", "op", "args"};
    if (!exact_keys(request, top, 4)) {
        cJSON_Delete(request);
        return send_error(control, id_valid, request_id, "INVALID_ARGUMENT", "request keys must be exactly v,id,op,args");
    }
    const cJSON *version = cJSON_GetObjectItemCaseSensitive(request, "v");
    const cJSON *id = cJSON_GetObjectItemCaseSensitive(request, "id");
    const cJSON *op = cJSON_GetObjectItemCaseSensitive(request, "op");
    const cJSON *args = cJSON_GetObjectItemCaseSensitive(request, "args");
    id_valid = valid_integer(id, 1, 2147483647);
    if (id_valid)
        request_id = (int32_t)id->valuedouble;
    if (!valid_integer(version, 1, 2147483647) || !cJSON_IsString(op) || !cJSON_IsObject(args) ||
        !id_valid) {
        cJSON_Delete(request);
        return send_error(control, id_valid, request_id, "INVALID_ARGUMENT", "invalid v,id,op or args");
    }
    if ((int)version->valuedouble != 1) {
        cJSON_Delete(request);
        return send_error(control, id_valid, request_id, "UNSUPPORTED_VERSION", "protocol version is not 1");
    }

    const char *operation = op->valuestring;
    const char *const empty[] = {NULL};
    const char *const component_args[] = {"component"};
    const char *const parameter_args[] = {"component", "name", "value"};
    bool is_mutation = !strcmp(operation, "init_component") || !strcmp(operation, "start_component") ||
                       !strcmp(operation, "stop_component") || !strcmp(operation, "set_parameter");
    size_t arg_count = is_mutation && !strcmp(operation, "set_parameter")
                           ? 3
                           : (is_mutation ? 1 : 0);
    const char *const *allowed_args = is_mutation && !strcmp(operation, "set_parameter")
                                          ? parameter_args
                                          : component_args;
    if (!strcmp(operation, "get_runtime_info") || !strcmp(operation, "get_components") ||
        !strcmp(operation, "get_topology") || !strcmp(operation, "get_configuration")) {
        allowed_args = empty;
        arg_count = 0;
    } else if (!strcmp(operation, "get_component")) {
        allowed_args = component_args;
        arg_count = 1;
    } else if (!is_mutation) {
        cJSON_Delete(request);
        return send_error(control, id_valid, request_id, "UNKNOWN_OPERATION", "operation is not supported");
    }
    if (!exact_keys(args, allowed_args, arg_count)) {
        cJSON_Delete(request);
        return send_error(control, id_valid, request_id, "INVALID_ARGUMENT", "args keys do not match operation");
    }

    bool sent = false;
    if (!strcmp(operation, "get_runtime_info")) {
        cJSON *result = runtime_info(control);
        sent = result ? send_response(control, id_valid, request_id, result, NULL, NULL)
                      : send_error(control, id_valid, request_id, "INTERNAL", "runtime info unavailable");
    } else if (!strcmp(operation, "get_components")) {
        dcr_err_t error = DCR_OK;
        cJSON *items = all_components(control, true, &error);
        cJSON *result = cJSON_CreateObject();
        if (items && result && cJSON_AddItemToObject(result, "components", items))
            sent = send_response(control, id_valid, request_id, result, NULL, NULL);
        else {
            cJSON_Delete(items);
            cJSON_Delete(result);
            sent = send_runtime_error(control, id_valid, request_id, error ? error : DCR_ERR_OOM,
                                      "component enumeration failed");
        }
    } else if (!strcmp(operation, "get_component")) {
        const cJSON *component = cJSON_GetObjectItemCaseSensitive(args, "component");
        sent = valid_name_string(component)
                   ? send_component(control, id_valid, request_id, component->valuestring, true)
                   : send_error(control, id_valid, request_id, "INVALID_ARGUMENT",
                                "args.component must be a bounded name");
    } else if (!strcmp(operation, "get_topology")) {
        cJSON *result = dcr_get_topology(control->runtime);
        sent = result ? send_response(control, id_valid, request_id, result, NULL, NULL)
                      : send_error(control, id_valid, request_id, "INTERNAL", "topology unavailable");
    } else if (!strcmp(operation, "get_configuration")) {
        dcr_err_t error = DCR_OK;
        cJSON *result = configuration(control, &error);
        sent = result ? send_response(control, id_valid, request_id, result, NULL, NULL)
                      : send_runtime_error(control, id_valid, request_id, error ? error : DCR_ERR_OOM,
                                           "configuration unavailable");
    } else {
        sent = mutate(control, id_valid, request_id, operation, args);
    }
    cJSON_Delete(request);
    return sent;
}

bool dcr_control_emit_snapshot(dcr_control_t *control)
{
    if (!control)
        return false;
    uint32_t now = pal_millis();
    if (control->snapshot_emitted && now - control->last_snapshot_ms < DCR_CONTROL_SNAPSHOT_PERIOD_MS)
        return true;
    dcr_err_t error = DCR_OK;
    cJSON *components = all_components(control, true, &error);
    cJSON *event = cJSON_CreateObject();
    if (!components || !event || !cJSON_AddNumberToObject(event, "v", 1) ||
        !cJSON_AddStringToObject(event, "event", "snapshot") ||
        !cJSON_AddStringToObject(event, "boot_id", control->boot_id) ||
        !cJSON_AddItemToObject(event, "components", components)) {
        cJSON_Delete(components);
        cJSON_Delete(event);
        return false;
    }
    char *text = cJSON_PrintUnformatted(event);
    cJSON_Delete(event);
    if (!text)
        return false;
    bool sent = send_payload(control, text);
    cJSON_free(text);
    if (sent) {
        control->last_snapshot_ms = now;
        control->snapshot_emitted = true;
    }
    return sent;
}
