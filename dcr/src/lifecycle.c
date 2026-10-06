#include "internal.h"
static void request_stop(dcr_instance_t *i)
{
    pal_mutex_lock(i->runtime->lock);
    i->stop_requested = true;
    pal_mutex_unlock(i->runtime->lock);
    for (unsigned n = 0; n < i->descriptor->port_count; ++n)
        pal_event_set(i->ports[n].update);
    pal_event_set(i->runtime->space);
}
static bool is_bus_worker(const dcr_instance_t *i)
{
    for (unsigned n = 0; n < i->descriptor->port_count; ++n) {
        const dcr_port_decl_t *port = &i->descriptor->ports[n];
        if (port->direction == DCR_INPUT && port->delivery == DCR_QUEUE &&
            port->type == DCR_BUS_REQUEST)
            return true;
    }
    return false;
}
bool dcr_should_stop(dcr_instance_t *i)
{
    pal_mutex_lock(i->runtime->lock);
    bool stop = i->stop_requested;
    pal_mutex_unlock(i->runtime->lock);
    return stop;
}
void dcr_heartbeat(dcr_instance_t *i)
{
    pal_mutex_lock(i->runtime->lock);
    i->heartbeat = pal_millis();
    pal_mutex_unlock(i->runtime->lock);
}
void dcr_sleep(dcr_instance_t *i, uint32_t ms)
{
    uint32_t start = pal_millis();
    while (!dcr_should_stop(i)) {
        dcr_heartbeat(i);
        uint32_t elapsed = pal_millis() - start;
        if (elapsed >= ms)
            return;
        uint32_t wait = ms - elapsed;
        pal_sleep(wait > 10 ? 10 : wait);
    }
}
void dcr_task_entry(void *arg)
{
    dcr_instance_t *i = arg;
    i->descriptor->run(i->user_data, i);
    pal_mutex_lock(i->runtime->lock);
    if (!i->stop_requested) {
        i->state = DCR_FAULTED;
        i->fault = DCR_ERR_STATE;
    }
    pal_mutex_unlock(i->runtime->lock);
}
static void watchdog(void *arg)
{
    dcr_runtime_t *r = arg;
    for (;;) {
        pal_mutex_lock(r->lock);
        if (r->watchdog_stop) {
            pal_mutex_unlock(r->lock);
            return;
        }
        for (size_t n = 0; n < r->instance_count; ++n) {
            dcr_instance_t *i = r->instances[n];
            if (i->state == DCR_RUNNING &&
                pal_millis() - i->heartbeat > r->settings.watchdog_timeout_ms) {
                i->state = DCR_FAULTED;
                i->fault = DCR_ERR_WATCHDOG;
                i->stop_requested = true;
                pal_log("error", i->id, "task heartbeat timeout; resources retained until join");
            }
        }
        pal_mutex_unlock(r->lock);
        pal_sleep(10);
    }
}
dcr_err_t dcr_watchdog_start(dcr_runtime_t *r)
{
    if (r->watchdog)
        return DCR_OK;
    r->watchdog = pal_task_create("dcr-watchdog", watchdog, r, 3072, pal_max_priority(), -1);
    return r->watchdog ? DCR_OK : DCR_ERR_OOM;
}
dcr_err_t dcr_init_component(dcr_runtime_t *r, const char *id)
{
    dcr_instance_t *i = dcr_find(r, id);
    if (!i)
        return DCR_ERR_ENDPOINT;
    if (dcr_has_active_bus_clients(r, i))
        return DCR_ERR_BUSY;
    if (i->task || i->state == DCR_INITIALIZED || i->state == DCR_RUNNING)
        return DCR_ERR_STATE;
    if (i->needs_cleanup) {
        i->descriptor->stop(i->user_data);
        i->user_data = NULL;
        i->needs_cleanup = false;
    }
    /* A re-init is a new publication epoch; never expose samples from the old epoch. */
    pal_mutex_lock(r->lock);
    for (unsigned p = 0; p < i->descriptor->port_count; ++p) {
        dcr_port_t *port = &i->ports[p];
        if (port->decl->direction == DCR_OUTPUT) {
            port->latest = (dcr_value_t){0};
            port->sequence = 0;
            port->published_at_ms = 0;
        }
    }
    pal_mutex_unlock(r->lock);
    i->needs_cleanup = true;
    dcr_err_t e = i->descriptor->init(i->config, &i->user_data);
    if (e) {
        i->descriptor->stop(i->user_data);
        i->user_data = NULL;
        i->needs_cleanup = false;
    }
    pal_mutex_lock(r->lock);
    if (!e)
        for (unsigned p = 0; p < i->descriptor->port_count; ++p) {
            dcr_port_t *port = &i->ports[p];
            dcr_value_t discarded;
            if (port->queue)
                while (pal_queue_receive(port->queue, &discarded, 0))
                    dcr_release_queued_value(port->decl->type, &discarded);
            port->seen = port->source ? port->source->sequence : 0;
        }
    i->state = e ? DCR_FAULTED : DCR_INITIALIZED;
    i->fault = e;
    i->stop_requested = false;
    pal_mutex_unlock(r->lock);
    if (e)
        pal_log("error", id, "init failed: %s (%d)", dcr_error_name(e), (int)e);
    return e;
}
dcr_err_t dcr_start_component(dcr_runtime_t *r, const char *id)
{
    dcr_instance_t *i = dcr_find(r, id);
    if (!i)
        return DCR_ERR_ENDPOINT;
    if (i->task || i->state != DCR_INITIALIZED)
        return DCR_ERR_STATE;
    dcr_err_t e = dcr_required(i);
    if (e)
        return e;
    e = dcr_watchdog_start(r);
    if (e)
        return e;
    pal_mutex_lock(r->lock);
    i->state = DCR_RUNNING;
    i->stop_requested = false;
    i->heartbeat = pal_millis();
    pal_mutex_unlock(r->lock);
    i->task = pal_task_create(i->id, dcr_task_entry, i, i->options.stack_size, i->options.priority,
                              i->options.core);
    if (!i->task) {
        pal_mutex_lock(r->lock);
        i->state = DCR_FAULTED;
        i->fault = DCR_ERR_OOM;
        pal_mutex_unlock(r->lock);
        return DCR_ERR_OOM;
    }
    return DCR_OK;
}
static void release(dcr_instance_t *i)
{
    if (i->needs_cleanup) {
        i->descriptor->stop(i->user_data);
        i->user_data = NULL;
        i->needs_cleanup = false;
    }
    pal_mutex_lock(i->runtime->lock);
    i->state = DCR_STOPPED;
    pal_mutex_unlock(i->runtime->lock);
}
dcr_err_t dcr_stop_component(dcr_runtime_t *r, const char *id, uint32_t ms)
{
    dcr_instance_t *i = dcr_find(r, id);
    if (!i)
        return DCR_ERR_ENDPOINT;
    if (dcr_has_active_bus_clients(r, i))
        return DCR_ERR_BUSY;
    if (i->state == DCR_UNLOADED || i->state == DCR_STOPPED)
        return DCR_ERR_STATE;
    request_stop(i);
    if (i->task && !pal_task_join(i->task, ms))
        return DCR_ERR_TIMEOUT;
    i->task = NULL;
    release(i);
    return DCR_OK;
}
dcr_err_t dcr_stop_all(dcr_runtime_t *r, uint32_t ms)
{
    if (!r)
        return DCR_ERR_ARGUMENT;
    uint32_t start = pal_millis();
    /* Clients must leave before a shared-resource worker is asked to stop.
     * Otherwise its final queue drain can race a producer that has already
     * passed the unlocked stop check. A timeout in this phase deliberately
     * leaves bus workers running for the still-unjoined clients. */
    for (size_t n = 0; n < r->instance_count; ++n)
        if (!is_bus_worker(r->instances[n]))
            request_stop(r->instances[n]);
    for (size_t n = 0; n < r->instance_count; ++n) {
        dcr_instance_t *i = r->instances[n];
        if (is_bus_worker(i))
            continue;
        uint32_t elapsed = pal_millis() - start;
        if (i->task && !pal_task_join(i->task, elapsed < ms ? ms - elapsed : 0))
            return DCR_ERR_TIMEOUT;
        i->task = NULL;
    }
    for (size_t n = 0; n < r->instance_count; ++n)
        if (is_bus_worker(r->instances[n]))
            request_stop(r->instances[n]);
    for (size_t n = 0; n < r->instance_count; ++n) {
        dcr_instance_t *i = r->instances[n];
        if (!is_bus_worker(i))
            continue;
        uint32_t elapsed = pal_millis() - start;
        if (i->task && !pal_task_join(i->task, elapsed < ms ? ms - elapsed : 0))
            return DCR_ERR_TIMEOUT;
        i->task = NULL;
    }
    for (size_t n = 0; n < r->instance_count; ++n)
        if (r->instances[n]->state != DCR_UNLOADED)
            release(r->instances[n]);
    return DCR_OK;
}
dcr_err_t dcr_send_event(dcr_runtime_t *r, const char *id, const cJSON *event)
{
    dcr_instance_t *i = dcr_find(r, id);
    if (!i || !event)
        return DCR_ERR_ARGUMENT;
    if (i->task || i->state != DCR_INITIALIZED)
        return DCR_ERR_STATE;
    return i->descriptor->on_event ? i->descriptor->on_event(i->user_data, event)
                                   : DCR_ERR_ARGUMENT;
}
dcr_err_t dcr_start_autostart(dcr_runtime_t *r)
{
    dcr_err_t e = dcr_validate(r, NULL);
    if (e)
        return e;
    for (size_t n = 0; n < r->instance_count; ++n)
        if (r->instances[n]->options.autostart && r->instances[n]->state != DCR_INITIALIZED)
            return DCR_ERR_STATE;
    for (size_t n = 0; n < r->instance_count; ++n)
        if (r->instances[n]->options.autostart) {
            e = dcr_start_component(r, r->instances[n]->id);
            if (e) {
                dcr_err_t rollback = dcr_stop_all(r, 2000);
                if (rollback) {
                    pal_log("error", "dcr", "autostart rollback failed: %s",
                            dcr_error_name(rollback));
                    return rollback;
                }
                return e;
            }
        }
    return DCR_OK;
}
