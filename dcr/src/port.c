#include "internal.h"
#include <string.h>
#include <stdio.h>
dcr_port_t *dcr_resolve_port(dcr_runtime_t *r, const char *endpoint)
{
    if (!endpoint)
        return NULL;
    const char *dot = strchr(endpoint, '.');
    if (!dot || dot == endpoint || (size_t)(dot - endpoint) >= DCR_NAME_SIZE ||
        !dcr_valid_name(dot + 1))
        return NULL;
    char id[DCR_NAME_SIZE];
    memcpy(id, endpoint, (size_t)(dot - endpoint));
    id[dot - endpoint] = 0;
    dcr_instance_t *i = dcr_find(r, id);
    if (!i)
        return NULL;
    for (unsigned p = 0; p < i->descriptor->port_count; ++p)
        if (!strcmp(i->ports[p].decl->name, dot + 1))
            return &i->ports[p];
    return NULL;
}
dcr_port_t *dcr_port(dcr_instance_t *i, const char *name)
{
    if (!i || !name)
        return NULL;
    for (unsigned p = 0; p < i->descriptor->port_count; ++p)
        if (!strcmp(i->ports[p].decl->name, name))
            return &i->ports[p];
    pal_mutex_lock(i->runtime->lock);
    i->state = DCR_FAULTED;
    i->fault = DCR_ERR_ENDPOINT;
    i->stop_requested = true;
    pal_mutex_unlock(i->runtime->lock);
    pal_log("error", i->id, "undeclared port %s", name);
    return NULL;
}
bool dcr_has_active_bus_clients(dcr_runtime_t *r, dcr_instance_t *bus)
{
    if (!r || !bus)
        return false;
    bool active = false;
    pal_mutex_lock(r->lock);
    for (size_t n = 0; n < r->connection_count; ++n) {
        dcr_connection_t *connection = &r->connections[n];
        if (connection->to->owner == bus && connection->to->decl->type == DCR_BUS_REQUEST &&
            connection->from->owner->task) {
            active = true;
            break;
        }
    }
    pal_mutex_unlock(r->lock);
    return active;
}
dcr_err_t dcr_connect(dcr_runtime_t *r, const char *from, const char *to, const char *name,
                      dcr_diagnostic_t *diag)
{
    if (!r)
        return DCR_ERR_ARGUMENT;
    if (dcr_has_tasks(r))
        return DCR_ERR_BUSY;
    dcr_port_t *a = dcr_resolve_port(r, from), *b = dcr_resolve_port(r, to);
    if (!a || !b)
        return dcr_fail(diag, DCR_ERR_ENDPOINT, "unresolved connection %s -> %s",
                        from ? from : "(null)", to ? to : "(null)");
    if (a->decl->direction != DCR_OUTPUT || b->decl->direction != DCR_INPUT)
        return dcr_fail(diag, DCR_ERR_DIRECTION, "expected OUTPUT -> INPUT: %s -> %s", from, to);
    if (a->decl->type != b->decl->type)
        return dcr_fail(diag, DCR_ERR_TYPE, "port type mismatch: %s -> %s", from, to);
    const char *rule = dcr_connection_lock_id(r, b);
    if (rule)
        return dcr_fail(diag, DCR_ERR_SAFETY, "rule %s locks destination %s", rule, to);
    if (a->decl->type == DCR_BUS_REQUEST)
        for (size_t n = 0; n < r->connection_count; ++n)
            if (r->connections[n].from == a)
                return dcr_fail(diag, DCR_ERR_MULTIPLE_DRIVERS,
                                "%s bus request output already connected", from);
    if (name && !dcr_valid_name(name))
        return dcr_fail(diag, DCR_ERR_ARGUMENT, "invalid signal name");
    for (size_t n = 0; n < r->connection_count; ++n) {
        dcr_connection_t *c = &r->connections[n];
        if (c->to == b && (c->from == a || !b->decl->multi_producer))
            return dcr_fail(diag, DCR_ERR_MULTIPLE_DRIVERS, "%s already driven", to);
        if (name && c->from != a && !strcmp(c->from->signal_name, name))
            return dcr_fail(diag, DCR_ERR_DUPLICATE, "signal name %s belongs to another output",
                            name);
    }
    if (b->decl->multi_producer && b->owner->options.priority < a->owner->options.priority)
        return dcr_fail(diag, DCR_ERR_ARGUMENT,
                        "%s resource owner priority lower than requester %s (R23)", to, from);
    if (r->connection_count == DCR_MAX_CONNECTIONS)
        return DCR_ERR_LIMIT;
    if (b->decl->delivery == DCR_QUEUE && !b->queue) {
        b->queue = pal_queue_create(DCR_QUEUE_DEPTH, sizeof(dcr_value_t));
        if (!b->queue)
            return DCR_ERR_OOM;
    }
    pal_mutex_lock(r->lock);
    if (!*a->signal_name) {
        if (name)
            strcpy(a->signal_name, name);
        else
            snprintf(a->signal_name, sizeof(a->signal_name), "%s.%s", a->owner->id, a->decl->name);
    }
    b->source = a;
    b->seen = 0;
    r->connections[r->connection_count++] = (dcr_connection_t){a, b};
    pal_mutex_unlock(r->lock);
    return DCR_OK;
}
static dcr_err_t disconnect(dcr_runtime_t *r, const char *to, bool teardown)
{
    if (!r)
        return DCR_ERR_ARGUMENT;
    if (dcr_has_tasks(r))
        return DCR_ERR_BUSY;
    dcr_port_t *b = dcr_resolve_port(r, to);
    if (!b)
        return DCR_ERR_ENDPOINT;
    if (!b->source)
        return DCR_ERR_ENDPOINT;
    if (!teardown && dcr_connection_lock_id(r, b))
        return DCR_ERR_SAFETY;
    pal_mutex_lock(r->lock);
    for (size_t n = 0; n < r->connection_count;) {
        if (r->connections[n].to == b)
            r->connections[n] = r->connections[--r->connection_count];
        else
            ++n;
    }
    b->source = NULL;
    b->seen = 0;
    dcr_value_t discarded;
    while (b->queue && pal_queue_receive(b->queue, &discarded, 0))
        dcr_release_queued_value(b->decl->type, &discarded);
    pal_queue_destroy(b->queue);
    b->queue = NULL;
    pal_mutex_unlock(r->lock);
    return DCR_OK;
}
dcr_err_t dcr_disconnect(dcr_runtime_t *r, const char *to)
{
    return disconnect(r, to, false);
}
dcr_err_t dcr_disconnect_for_teardown(dcr_runtime_t *r, const char *to)
{
    return disconnect(r, to, true);
}
static uint32_t timeout(dcr_port_t *p, uint32_t ms)
{
    return ms == DCR_DEFAULT_TIMEOUT ? p->owner->runtime->settings.signal_wait_timeout_ms : ms;
}
dcr_err_t dcr_port_write(dcr_port_t *p, const dcr_value_t *v, uint32_t ms)
{
    if (!p || !v)
        return DCR_ERR_ARGUMENT;
    if (p->decl->direction != DCR_OUTPUT)
        return DCR_ERR_DIRECTION;
    dcr_runtime_t *r = p->owner->runtime;
    uint32_t start = pal_millis();
    ms = timeout(p, ms);
    for (;;) {
        if (dcr_should_stop(p->owner))
            return DCR_ERR_STOPPED;
        dcr_heartbeat(p->owner);
        pal_mutex_lock(r->lock);
        /* The pre-lock stop check is only an optimization. A stop request
         * can arrive while a producer is waiting for queue space, so reject
         * the write again under the same lock used by request_stop(). */
        if (p->owner->stop_requested) {
            pal_mutex_unlock(r->lock);
            return DCR_ERR_STOPPED;
        }
        bool destination_stopping = false;
        for (size_t n = 0; n < r->connection_count; ++n) {
            dcr_connection_t *c = &r->connections[n];
            if (c->from == p && c->to->decl->type == DCR_BUS_REQUEST &&
                c->to->owner->stop_requested) {
                destination_stopping = true;
                break;
            }
        }
        if (destination_stopping) {
            pal_mutex_unlock(r->lock);
            return DCR_ERR_STOPPED;
        }
        bool ready = true;
        for (size_t n = 0; n < r->connection_count; ++n) {
            dcr_connection_t *c = &r->connections[n];
            if (c->from == p && c->to->queue && pal_queue_count(c->to->queue) == DCR_QUEUE_DEPTH)
                ready = false;
        }
        if (ready) {
            size_t retained = 0;
            bool retain_ok = true;
            for (size_t n = 0; n < r->connection_count; ++n) {
                dcr_connection_t *c = &r->connections[n];
                if (c->from != p || !c->to->queue || c->to->decl->type != DCR_BUS_REQUEST)
                    continue;
                if (!dcr_retain_queued_value(c->to->decl->type, v)) {
                    retain_ok = false;
                    break;
                }
                ++retained;
            }
            if (!retain_ok) {
                size_t released = 0;
                for (size_t n = 0; n < r->connection_count && released < retained; ++n) {
                    dcr_connection_t *c = &r->connections[n];
                    if (c->from == p && c->to->queue && c->to->decl->type == DCR_BUS_REQUEST) {
                        dcr_release_queued_value(c->to->decl->type, v);
                        ++released;
                    }
                }
                pal_mutex_unlock(r->lock);
                return DCR_ERR_STOPPED;
            }
            /* Readers and writers share this lock: reserve all queue capacity
             * before publishing anything, so timeout cannot partially fan out. */
            p->latest = *v;
            ++p->sequence;
            p->published_at_ms = pal_millis();
            for (size_t n = 0; n < r->connection_count; ++n) {
                dcr_connection_t *c = &r->connections[n];
                if (c->from != p)
                    continue;
                if (c->to->queue && !pal_queue_send(c->to->queue, v, 0) &&
                    c->to->decl->type == DCR_BUS_REQUEST)
                    dcr_release_queued_value(c->to->decl->type, v);
                pal_event_set(c->to->update);
            }
            pal_mutex_unlock(r->lock);
            return DCR_OK;
        }
        pal_mutex_unlock(r->lock);
        uint32_t elapsed = pal_millis() - start;
        if (elapsed >= ms)
            return DCR_ERR_TIMEOUT;
        uint32_t wait = ms - elapsed;
        pal_event_wait(r->space, wait > 10 ? 10 : wait);
    }
}
static dcr_err_t read_value(dcr_port_t *p, dcr_value_t *v, bool update_only)
{
    dcr_runtime_t *r = p->owner->runtime;
    pal_mutex_lock(r->lock);
    dcr_err_t e = DCR_OK;
    if (!p->source)
        e = DCR_ERR_ENDPOINT;
    else if (p->queue) {
        if (!pal_queue_receive(p->queue, v, 0))
            e = DCR_ERR_EMPTY;
        else
            pal_event_set(r->space);
    } else if (!p->source->sequence || (update_only && p->seen == p->source->sequence))
        e = DCR_ERR_EMPTY;
    else {
        *v = p->source->latest;
        if (update_only)
            p->seen = p->source->sequence;
    }
    pal_mutex_unlock(r->lock);
    return e;
}
dcr_err_t dcr_port_read(dcr_port_t *p, dcr_value_t *v)
{
    if (!p || !v)
        return DCR_ERR_ARGUMENT;
    if (p->decl->direction != DCR_INPUT)
        return DCR_ERR_DIRECTION;
    return read_value(p, v, false);
}
dcr_err_t dcr_port_wait(dcr_port_t *p, dcr_value_t *v, uint32_t ms)
{
    if (!p || !v)
        return DCR_ERR_ARGUMENT;
    if (p->decl->direction != DCR_INPUT)
        return DCR_ERR_DIRECTION;
    uint32_t start = pal_millis();
    ms = timeout(p, ms);
    for (;;) {
        if (dcr_should_stop(p->owner))
            return DCR_ERR_STOPPED;
        dcr_heartbeat(p->owner);
        dcr_err_t e = read_value(p, v, true);
        if (e != DCR_ERR_EMPTY)
            return e;
        uint32_t elapsed = pal_millis() - start;
        if (elapsed >= ms)
            return DCR_ERR_TIMEOUT;
        uint32_t wait = ms - elapsed;
        pal_event_wait(p->update, wait > 10 ? 10 : wait);
    }
}
