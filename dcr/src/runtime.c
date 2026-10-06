#include "internal.h"
#include <string.h>
#include <stdio.h>
#include <stdarg.h>
#include <ctype.h>
const char *dcr_error_name(dcr_err_t e)
{
    static const char *const names[] = {
        "ok",    "argument", "out_of_memory", "schema",           "duplicate",
        "type",  "endpoint", "direction",     "multiple_drivers", "required",
        "state", "init",     "timeout",       "stopped",          "limit",
        "busy",  "empty",    "watchdog", "safety_rule"};
    return (unsigned)e < sizeof(names) / sizeof(*names) ? names[e] : "unknown";
}
const char *dcr_state_name(dcr_state_t s)
{
    static const char *const names[] = {"UNLOADED", "INITIALIZED", "RUNNING", "STOPPED", "FAULTED"};
    return (unsigned)s < 5 ? names[s] : "UNKNOWN";
}
dcr_err_t dcr_fail(dcr_diagnostic_t *d, dcr_err_t e, const char *fmt, ...)
{
    if (d) {
        d->code = e;
        va_list a;
        va_start(a, fmt);
        vsnprintf(d->context, sizeof(d->context), fmt, a);
        va_end(a);
    }
    return e;
}
bool dcr_valid_name(const char *n)
{
    if (!n || !*n || strlen(n) >= DCR_NAME_SIZE)
        return false;
    for (; *n; ++n)
        if (!isalnum((unsigned char)*n) && *n != '_' && *n != '-')
            return false;
    return true;
}
dcr_instance_t *dcr_find(dcr_runtime_t *r, const char *id)
{
    if (!r || !id)
        return NULL;
    for (size_t n = 0; n < r->instance_count; ++n)
        if (!strcmp(r->instances[n]->id, id))
            return r->instances[n];
    return NULL;
}
const dcr_descriptor_t *dcr_lookup(dcr_runtime_t *r, const char *type)
{
    if (!r || !type)
        return NULL;
    for (size_t n = 0; n < r->registry_count; ++n)
        if (!strcmp(r->registry[n]->type_name, type))
            return r->registry[n];
    return NULL;
}
bool dcr_has_tasks(dcr_runtime_t *r)
{
    for (size_t n = 0; n < r->instance_count; ++n)
        if (r->instances[n]->task)
            return true;
    return false;
}
dcr_runtime_t *dcr_runtime_create(const dcr_descriptor_t *const *registry, size_t count)
{
    if (!registry || !count)
        return NULL;
    for (size_t n = 0; n < count; ++n) {
        const dcr_descriptor_t *d = registry[n];
        if (!d || !dcr_valid_name(d->type_name) || !d->init || !d->run || !d->stop ||
            d->port_count > DCR_MAX_PORTS || (d->port_count && !d->ports))
            return NULL;
        for (size_t k = 0; k < n; ++k)
            if (!strcmp(d->type_name, registry[k]->type_name))
                return NULL;
        for (unsigned p = 0; p < d->port_count; ++p) {
            const dcr_port_decl_t *v = &d->ports[p];
            if (!dcr_valid_name(v->name) || v->type > DCR_BUS_REQUEST ||
                v->direction > DCR_OUTPUT || v->delivery > DCR_QUEUE ||
                (v->type == DCR_BUS_REQUEST && v->delivery != DCR_QUEUE) ||
                (v->multi_producer && (v->direction != DCR_INPUT || v->delivery != DCR_QUEUE)))
                return NULL;
            for (unsigned q = 0; q < p; ++q)
                if (!strcmp(v->name, d->ports[q].name))
                    return NULL;
        }
    }
    dcr_runtime_t *r = pal_malloc(sizeof(*r));
    if (!r)
        return NULL;
    r->registry = registry;
    r->registry_count = count;
    r->settings = (dcr_settings_t){5000, 0, 1000, 1};
    r->lock = pal_mutex_create();
    r->space = pal_event_create();
    if (!r->lock || !r->space) {
        pal_mutex_destroy(r->lock);
        pal_event_destroy(r->space);
        pal_free(r);
        return NULL;
    }
    return r;
}
dcr_err_t dcr_create_component(dcr_runtime_t *r, const char *id, const char *type, const cJSON *cfg,
                               const dcr_options_t *options, dcr_diagnostic_t *diag)
{
    if (!r || !dcr_valid_name(id) || (cfg && !cJSON_IsObject(cfg)))
        return dcr_fail(diag, DCR_ERR_ARGUMENT, "invalid instance/config: %s", id ? id : "(null)");
    if (dcr_has_tasks(r))
        return DCR_ERR_BUSY;
    if (dcr_find(r, id))
        return dcr_fail(diag, DCR_ERR_DUPLICATE, "duplicate instance: %s", id);
    const dcr_descriptor_t *d = dcr_lookup(r, type);
    if (!d)
        return dcr_fail(diag, DCR_ERR_TYPE, "%s: unknown type %s", id, type ? type : "(null)");
    if (r->instance_count == DCR_MAX_INSTANCES)
        return DCR_ERR_LIMIT;
    dcr_options_t o =
        options ? *options
                : (dcr_options_t){true, d->default_priority,
                                  r->settings.default_stack_size ? r->settings.default_stack_size
                                                                 : d->default_stack_size,
                                  -1};
    if (!o.stack_size)
        o.stack_size = 3072;
    if (!o.priority || o.priority > pal_max_priority() || o.stack_size < 2048 ||
        o.stack_size > 65536 || o.core < -1 || o.core >= (int)pal_core_count())
        return dcr_fail(diag, DCR_ERR_ARGUMENT,
                        "%s: task priority/stack/core outside platform limits", id);
    dcr_instance_t *i = pal_malloc(sizeof(*i));
    if (!i)
        return DCR_ERR_OOM;
    i->runtime = r;
    i->descriptor = d;
    i->options = o;
    strcpy(i->id, id);
    i->config = cfg ? cJSON_Duplicate(cfg, true) : cJSON_CreateObject();
    if (!i->config) {
        pal_free(i);
        return DCR_ERR_OOM;
    }
    for (unsigned p = 0; p < d->port_count; ++p) {
        i->ports[p].owner = i;
        i->ports[p].decl = &d->ports[p];
        i->ports[p].update = pal_event_create();
        if (!i->ports[p].update) {
            for (unsigned k = 0; k < p; ++k)
                pal_event_destroy(i->ports[k].update);
            cJSON_Delete(i->config);
            pal_free(i);
            return DCR_ERR_OOM;
        }
    }
    pal_mutex_lock(r->lock);
    r->instances[r->instance_count++] = i;
    pal_mutex_unlock(r->lock);
    return DCR_OK;
}
dcr_err_t dcr_destroy_component(dcr_runtime_t *r, const char *id)
{
    dcr_instance_t *i = dcr_find(r, id);
    if (!i)
        return DCR_ERR_ENDPOINT;
    if (dcr_has_tasks(r))
        return DCR_ERR_BUSY;
    for (size_t n = 0; n < r->connection_count; ++n)
        if (r->connections[n].from->owner == i || r->connections[n].to->owner == i)
            return DCR_ERR_BUSY;
    if (i->needs_cleanup)
        i->descriptor->stop(i->user_data);
    for (unsigned p = 0; p < i->descriptor->port_count; ++p)
        pal_event_destroy(i->ports[p].update);
    pal_mutex_lock(r->lock);
    for (size_t n = 0; n < r->instance_count; ++n)
        if (r->instances[n] == i) {
            r->instances[n] = r->instances[--r->instance_count];
            break;
        }
    pal_mutex_unlock(r->lock);
    cJSON_Delete(i->config);
    pal_free(i);
    return DCR_OK;
}
dcr_err_t dcr_update_config(dcr_runtime_t *r, const char *id, const cJSON *config)
{
    dcr_instance_t *i = dcr_find(r, id);
    if (!i || !cJSON_IsObject(config))
        return DCR_ERR_ARGUMENT;
    if (dcr_has_active_bus_clients(r, i))
        return DCR_ERR_BUSY;
    if (i->task || i->state == DCR_INITIALIZED)
        return DCR_ERR_STATE;
    cJSON *copy = cJSON_Duplicate(config, true);
    if (!copy)
        return DCR_ERR_OOM;
    cJSON_Delete(i->config);
    i->config = copy;
    return DCR_OK;
}
dcr_err_t dcr_get_component(dcr_runtime_t *r, const char *id, dcr_instance_info_t *info)
{
    dcr_instance_t *i = dcr_find(r, id);
    if (!i || !info)
        return DCR_ERR_ARGUMENT;
    pal_mutex_lock(r->lock);
    strcpy(info->id, i->id);
    strcpy(info->type, i->descriptor->type_name);
    info->options = i->options;
    info->state = i->state;
    info->fault = i->fault;
    pal_mutex_unlock(r->lock);
    return DCR_OK;
}
size_t dcr_component_count(dcr_runtime_t *r)
{
    if (!r)
        return 0;
    pal_mutex_lock(r->lock);
    size_t count = r->instance_count;
    pal_mutex_unlock(r->lock);
    return count;
}
dcr_err_t dcr_get_settings(dcr_runtime_t *r, dcr_settings_t *out)
{
    if (!r || !out)
        return DCR_ERR_ARGUMENT;
    pal_mutex_lock(r->lock);
    *out = r->settings;
    pal_mutex_unlock(r->lock);
    return DCR_OK;
}
void dcr_component_snapshot_clear(dcr_component_snapshot_t *snapshot)
{
    if (!snapshot)
        return;
    cJSON_Delete(snapshot->config);
    *snapshot = (dcr_component_snapshot_t){0};
}
static dcr_err_t snapshot_instance(dcr_runtime_t *r, dcr_instance_t *i,
                                   dcr_component_snapshot_t *snapshot)
{
    *snapshot = (dcr_component_snapshot_t){0};
    pal_mutex_lock(r->lock);
    strcpy(snapshot->id, i->id);
    strcpy(snapshot->type, i->descriptor->type_name);
    snapshot->state = i->state;
    snapshot->fault = i->fault;
    snapshot->config = cJSON_Duplicate(i->config, true);
    if (!snapshot->config) {
        pal_mutex_unlock(r->lock);
        return DCR_ERR_OOM;
    }
    uint32_t now = pal_millis();
    for (unsigned p = 0; p < i->descriptor->port_count; ++p) {
        dcr_port_t *port = &i->ports[p];
        if (port->decl->direction != DCR_OUTPUT || port->decl->type == DCR_BUS_REQUEST)
            continue;
        dcr_output_snapshot_t *out = &snapshot->outputs[snapshot->output_count++];
        strcpy(out->name, port->decl->name);
        out->type = port->decl->type;
        out->published = port->sequence != 0;
        out->value = port->latest;
        out->sequence = port->sequence;
        out->timestamp_ms = port->published_at_ms;
        out->age_ms = out->published ? now - port->published_at_ms : 0;
    }
    pal_mutex_unlock(r->lock);
    return DCR_OK;
}
dcr_err_t dcr_get_component_snapshot(dcr_runtime_t *r, const char *id,
                                     dcr_component_snapshot_t *snapshot)
{
    if (!r || !id || !snapshot)
        return DCR_ERR_ARGUMENT;
    dcr_instance_t *i = dcr_find(r, id);
    if (!i)
        return DCR_ERR_ENDPOINT;
    return snapshot_instance(r, i, snapshot);
}
dcr_err_t dcr_get_component_snapshot_at(dcr_runtime_t *r, size_t index,
                                         dcr_component_snapshot_t *snapshot)
{
    if (!r || !snapshot)
        return DCR_ERR_ARGUMENT;
    pal_mutex_lock(r->lock);
    if (index >= r->instance_count) {
        pal_mutex_unlock(r->lock);
        return DCR_ERR_ENDPOINT;
    }
    dcr_instance_t *i = r->instances[index];
    pal_mutex_unlock(r->lock);
    return snapshot_instance(r, i, snapshot);
}
cJSON *dcr_get_topology(dcr_runtime_t *r)
{
    if (!r)
        return NULL;
    cJSON *root = cJSON_CreateObject();
    if (!root)
        return NULL;
    cJSON *items = cJSON_AddArrayToObject(root, "components"),
          *edges = cJSON_AddArrayToObject(root, "connections");
    if (!items || !edges) {
        cJSON_Delete(root);
        return NULL;
    }
    pal_mutex_lock(r->lock);
    for (size_t n = 0; n < r->instance_count; ++n) {
        dcr_instance_t *i = r->instances[n];
        cJSON *o = cJSON_CreateObject();
        if (!o)
            goto fail;
        cJSON_AddItemToArray(items, o);
        if (!cJSON_AddStringToObject(o, "id", i->id) ||
            !cJSON_AddStringToObject(o, "type", i->descriptor->type_name) ||
            !cJSON_AddStringToObject(o, "state", dcr_state_name(i->state)) ||
            !cJSON_AddStringToObject(o, "fault", dcr_error_name(i->fault)))
            goto fail;
        cJSON *ports = cJSON_AddArrayToObject(o, "ports");
        if (!ports)
            goto fail;
        for (unsigned p = 0; p < i->descriptor->port_count; ++p) {
            const dcr_port_decl_t *decl = i->ports[p].decl;
            static const char *const types[] = {"bool", "int32", "uint32", "float",
                                                "bus_request"};
            static const char *const directions[] = {"input", "output"};
            static const char *const deliveries[] = {"signal", "queue"};
            cJSON *port = cJSON_CreateObject();
            if (!port || !cJSON_AddStringToObject(port, "name", decl->name) ||
                !cJSON_AddStringToObject(port, "direction", directions[decl->direction]) ||
                !cJSON_AddStringToObject(port, "type", types[decl->type]) ||
                !cJSON_AddStringToObject(port, "delivery", deliveries[decl->delivery]) ||
                !cJSON_AddBoolToObject(port, "required", decl->required) ||
                !cJSON_AddBoolToObject(port, "multi_producer", decl->multi_producer)) {
                cJSON_Delete(port);
                goto fail;
            }
            cJSON_AddItemToArray(ports, port);
        }
    }
    for (size_t n = 0; n < r->connection_count; ++n) {
        dcr_connection_t *c = &r->connections[n];
        char a[100], b[100];
        snprintf(a, sizeof(a), "%s.%s", c->from->owner->id, c->from->decl->name);
        snprintf(b, sizeof(b), "%s.%s", c->to->owner->id, c->to->decl->name);
        cJSON *o = cJSON_CreateObject();
        if (!o)
            goto fail;
        cJSON_AddItemToArray(edges, o);
        if (!cJSON_AddStringToObject(o, "from", a) || !cJSON_AddStringToObject(o, "to", b) ||
            !cJSON_AddStringToObject(o, "signal", c->from->signal_name))
            goto fail;
    }
    pal_mutex_unlock(r->lock);
    return root;
fail:
    pal_mutex_unlock(r->lock);
    cJSON_Delete(root);
    return NULL;
}
dcr_err_t dcr_runtime_destroy(dcr_runtime_t *r)
{
    if (!r)
        return DCR_OK;
    dcr_err_t e = dcr_stop_all(r, 2000);
    if (e)
        return e;
    pal_mutex_lock(r->lock);
    r->watchdog_stop = true;
    pal_mutex_unlock(r->lock);
    if (r->watchdog && !pal_task_join(r->watchdog, 2000))
        return DCR_ERR_TIMEOUT;
    r->watchdog = NULL;
    while (r->connection_count) {
        dcr_port_t *p = r->connections[0].to;
        char endpoint[100];
        snprintf(endpoint, sizeof(endpoint), "%s.%s", p->owner->id, p->decl->name);
        dcr_disconnect_for_teardown(r, endpoint);
    }
    while (r->instance_count)
        dcr_destroy_component(r, r->instances[0]->id);
    pal_event_destroy(r->space);
    pal_mutex_destroy(r->lock);
    pal_free(r);
    return DCR_OK;
}
const char *dcr_instance_id(const dcr_instance_t *i)
{
    return i->id;
}
void dcr_log(dcr_instance_t *i, const char *level, const char *fmt, ...)
{
    const char *const levels[] = {"debug", "info", "warn", "error"};
    unsigned severity = 3;
    for (unsigned n = 0; n < 4; ++n)
        if (!strcmp(level, levels[n]))
            severity = n;
    if (severity < i->runtime->settings.log_level)
        return;
    char message[256];
    va_list args;
    va_start(args, fmt);
    vsnprintf(message, sizeof(message), fmt, args);
    va_end(args);
    pal_log(level, i->id, "%s", message);
}
