#include "internal.h"
#include <string.h>
#include <math.h>
#include <stdio.h>
static const cJSON *field(const cJSON *o, const char *k)
{
    return cJSON_GetObjectItemCaseSensitive(o, k);
}
static bool keys(const cJSON *o, const char *const *allowed, size_t count)
{
    const cJSON *p;
    cJSON_ArrayForEach(p, o)
    {
        bool found = false;
        for (size_t n = 0; n < count; ++n)
            if (p->string && !strcmp(p->string, allowed[n]))
                found = true;
        if (!found)
            return false;
    }
    return true;
}
static bool duplicates(const cJSON *o)
{
    const cJSON *p, *q;
    cJSON_ArrayForEach(p, o)
    {
        if (cJSON_IsObject(o))
            for (q = p->next; q; q = q->next)
                if (!strcmp(p->string, q->string))
                    return true;
        if ((cJSON_IsObject(p) || cJSON_IsArray(p)) && duplicates(p))
            return true;
    }
    return false;
}
static bool bounded_json(const char *s)
{
    if (!s || strlen(s) > 65536)
        return false;
    unsigned depth = 0;
    bool quoted = false, escape = false;
    for (; *s; ++s) {
        if (quoted) {
            if (escape)
                escape = false;
            else if (*s == '\\')
                escape = true;
            else if (*s == '"')
                quoted = false;
        } else if (*s == '"')
            quoted = true;
        else if (*s == '{' || *s == '[') {
            if (++depth > 32)
                return false;
        } else if (*s == '}' || *s == ']') {
            if (!depth)
                return false;
            --depth;
        }
    }
    return !quoted && !depth;
}
static bool integer(const cJSON *v, double low, double high)
{
    return cJSON_IsNumber(v) && isfinite(v->valuedouble) && v->valuedouble >= low &&
           v->valuedouble <= high && floor(v->valuedouble) == v->valuedouble;
}
static dcr_err_t settings(dcr_runtime_t *r, const cJSON *s, dcr_diagnostic_t *diag)
{
    if (!s)
        return DCR_OK;
    const char *const allowed[] = {"watchdog_timeout_ms", "default_stack_size",
                                   "signal_wait_timeout_ms", "log_level"};
    if (!cJSON_IsObject(s) || !keys(s, allowed, 4))
        return dcr_fail(diag, DCR_ERR_SCHEMA, "invalid settings fields");
    const cJSON *v = field(s, "watchdog_timeout_ms");
    if (v) {
        if (!integer(v, 20, 86400000))
            return dcr_fail(diag, DCR_ERR_SCHEMA,
                            "settings.watchdog_timeout_ms must be 20..86400000");
        r->settings.watchdog_timeout_ms = (uint32_t)v->valuedouble;
    }
    v = field(s, "signal_wait_timeout_ms");
    if (v) {
        if (!integer(v, 0, 86400000))
            return dcr_fail(diag, DCR_ERR_SCHEMA, "settings.signal_wait_timeout_ms outside range");
        r->settings.signal_wait_timeout_ms = (uint32_t)v->valuedouble;
    }
    v = field(s, "default_stack_size");
    if (v) {
        if (!integer(v, 2048, 65536))
            return dcr_fail(diag, DCR_ERR_SCHEMA, "settings.default_stack_size outside range");
        r->settings.default_stack_size = (uint32_t)v->valuedouble;
    }
    v = field(s, "log_level");
    if (v) {
        const char *names[] = {"debug", "info", "warn", "error"};
        bool found = false;
        for (unsigned n = 0; n < 4; ++n)
            if (cJSON_IsString(v) && !strcmp(v->valuestring, names[n])) {
                r->settings.log_level = n;
                found = true;
            }
        if (!found)
            return dcr_fail(diag, DCR_ERR_SCHEMA, "settings.log_level invalid");
    }
    return DCR_OK;
}
static dcr_err_t safety_rules(dcr_runtime_t *r, const cJSON *rules, dcr_diagnostic_t *diag)
{
    if (!rules)
        return DCR_OK;
    if (!cJSON_IsArray(rules))
        return dcr_fail(diag, DCR_ERR_SCHEMA, "safety_rules must be an array");
    if (cJSON_GetArraySize(rules) > DCR_MAX_LOCK_RULES)
        return dcr_fail(diag, DCR_ERR_LIMIT, "safety rule limit exceeded");
    const cJSON *item;
    cJSON_ArrayForEach(item, rules) {
        const char *const allowed[] = {"id", "type", "from", "to"};
        const cJSON *id = field(item, "id"), *type = field(item, "type"),
                    *from = field(item, "from"), *to = field(item, "to");
        if (!cJSON_IsObject(item) || !keys(item, allowed, 4) ||
            !cJSON_IsString(id) || !dcr_valid_name(id->valuestring) ||
            !cJSON_IsString(type) || strcmp(type->valuestring, "connection_locked") ||
            !cJSON_IsString(from) || !cJSON_IsString(to))
            return dcr_fail(diag, DCR_ERR_SCHEMA,
                            "only connection_locked safety rules with id/from/to are supported");
        dcr_port_t *a = dcr_resolve_port(r, from->valuestring),
                   *b = dcr_resolve_port(r, to->valuestring);
        bool connected = false;
        for (size_t n = 0; n < r->connection_count; ++n)
            if (r->connections[n].from == a && r->connections[n].to == b)
                connected = true;
        if (!a || !b || !connected)
            return dcr_fail(diag, DCR_ERR_ENDPOINT, "rule %s must name an existing connection",
                            id->valuestring);
        for (size_t n = 0; n < r->connection_lock_count; ++n)
            if (!strcmp(r->connection_locks[n].id, id->valuestring) ||
                r->connection_locks[n].to == b)
                return dcr_fail(diag, DCR_ERR_DUPLICATE, "duplicate rule id or locked destination");
        dcr_connection_lock_t *lock = &r->connection_locks[r->connection_lock_count++];
        strcpy(lock->id, id->valuestring);
        lock->from = a;
        lock->to = b;
    }
    return DCR_OK;
}

dcr_err_t dcr_build_json(const char *json, const dcr_descriptor_t *const *registry, size_t count,
                         dcr_runtime_t **out, dcr_diagnostic_t *diag)
{
    if (!out || !registry || !count)
        return DCR_ERR_ARGUMENT;
    *out = NULL;
    if (diag)
        *diag = (dcr_diagnostic_t){0};
    if (!bounded_json(json))
        return dcr_fail(diag, DCR_ERR_SCHEMA, "JSON exceeds 64 KiB/depth 32 or is unbalanced");
    cJSON *root = cJSON_ParseWithOpts(json, NULL, true);
    const char *const top[] = {"version", "components", "connections", "settings", "safety_rules"};
    if (!cJSON_IsObject(root) || duplicates(root) || !keys(root, top, 5)) {
        cJSON_Delete(root);
        return dcr_fail(
            diag, DCR_ERR_SCHEMA,
            "JSON object required; malformed, duplicate or unknown key");
    }
    const cJSON *version = field(root, "version"), *components = field(root, "components"),
                *connections = field(root, "connections");
    dcr_err_t e = DCR_OK;
    dcr_runtime_t *r = NULL;
    const cJSON *c, *other;
    if (!cJSON_IsString(version) || strcmp(version->valuestring, "1.0") ||
        !cJSON_IsArray(components) || (connections && !cJSON_IsArray(connections))) {
        e = dcr_fail(diag, DCR_ERR_SCHEMA,
                     "version 1.0 and components array required; connections must be array");
        goto done;
    }
    if (cJSON_GetArraySize(components) > DCR_MAX_INSTANCES ||
        cJSON_GetArraySize(connections) > DCR_MAX_CONNECTIONS) {
        e = dcr_fail(diag, DCR_ERR_LIMIT, "instance/connection limit exceeded");
        goto done;
    }
    /* Resolve every type/ID before allocating any instance, queue, or hardware. */
    cJSON_ArrayForEach(c, components)
    {
        const cJSON *id = field(c, "id"), *type = field(c, "type");
        if (!cJSON_IsObject(c) || !cJSON_IsString(id) || !dcr_valid_name(id->valuestring) ||
            !cJSON_IsString(type)) {
            e = dcr_fail(diag, DCR_ERR_SCHEMA, "component needs valid id/type");
            goto done;
        }
        bool found = false;
        for (size_t n = 0; n < count; ++n)
            if (registry[n] && !strcmp(registry[n]->type_name, type->valuestring))
                found = true;
        if (!found) {
            e = dcr_fail(diag, DCR_ERR_TYPE, "%s: unknown type %s", id->valuestring,
                         type->valuestring);
            goto done;
        }
        for (other = c->next; other; other = other->next) {
            const cJSON *id2 = field(other, "id");
            if (cJSON_IsString(id2) && !strcmp(id->valuestring, id2->valuestring)) {
                e = dcr_fail(diag, DCR_ERR_DUPLICATE, "duplicate id %s", id->valuestring);
                goto done;
            }
        }
    }
    r = dcr_runtime_create(registry, count);
    if (!r) {
        e = DCR_ERR_OOM;
        goto done;
    }
    e = settings(r, field(root, "settings"), diag);
    if (e)
        goto done;
    cJSON_ArrayForEach(c, components)
    {
        const char *const allowed[] = {"id",         "type", "autostart", "priority",
                                       "stack_size", "core", "config"};
        const char *id = field(c, "id")->valuestring;
        const char *type = field(c, "type")->valuestring;
        const dcr_descriptor_t *d = dcr_lookup(r, type);
        dcr_options_t o = {true, d->default_priority,
                           r->settings.default_stack_size ? r->settings.default_stack_size
                                                          : d->default_stack_size,
                           -1};
        if (!keys(c, allowed, 7)) {
            e = dcr_fail(diag, DCR_ERR_SCHEMA, "%s: unknown component field", id);
            goto done;
        }
        const cJSON *v = field(c, "autostart");
        if (v) {
            if (!cJSON_IsBool(v))
                goto bad_options;
            o.autostart = cJSON_IsTrue(v);
        }
        v = field(c, "priority");
        if (v) {
            if (!integer(v, 1, pal_max_priority()))
                goto bad_options;
            o.priority = (unsigned)v->valuedouble;
        }
        v = field(c, "stack_size");
        if (v) {
            if (!integer(v, 2048, 65536))
                goto bad_options;
            o.stack_size = (uint32_t)v->valuedouble;
        }
        v = field(c, "core");
        if (v) {
            if (!integer(v, 0, pal_core_count() - 1))
                goto bad_options;
            o.core = (int)v->valuedouble;
        }
        v = field(c, "config");
        if (v && !cJSON_IsObject(v))
            goto bad_options;
        e = dcr_create_component(r, id, type, v, &o, diag);
        if (e)
            goto done;
        continue;
    bad_options:
        e = dcr_fail(diag, DCR_ERR_SCHEMA, "%s: invalid autostart/priority/stack/core/config", id);
        goto done;
    }
    cJSON_ArrayForEach(c, connections)
    {
        const char *const allowed[] = {"from", "to", "signal"};
        const cJSON *a = field(c, "from"), *b = field(c, "to"), *name = field(c, "signal");
        if (!cJSON_IsObject(c) || !keys(c, allowed, 3) || !cJSON_IsString(a) ||
            !cJSON_IsString(b) || (name && !cJSON_IsString(name))) {
            e = dcr_fail(diag, DCR_ERR_SCHEMA,
                         "connection requires string from/to/optional signal");
            goto done;
        }
        e = dcr_connect(r, a->valuestring, b->valuestring, name ? name->valuestring : NULL, diag);
        if (e)
            goto done;
    }
    e = safety_rules(r, field(root, "safety_rules"), diag);
    if (e)
        goto done;
    e = dcr_validate(r, diag);
    if (e)
        goto done;
    for (size_t n = 0; n < r->instance_count; ++n) {
        dcr_err_t init = dcr_init_component(r, r->instances[n]->id);
        if (init && !e)
            e = dcr_fail(diag, init, "%s: init failed (%s)", r->instances[n]->id,
                         dcr_error_name(init));
    }
    *out = r;
    r = NULL;
done:
    if (r)
        dcr_runtime_destroy(r);
    cJSON_Delete(root);
    if (e && diag && !diag->code)
        dcr_fail(diag, e, "%s", dcr_error_name(e));
    return e;
}
