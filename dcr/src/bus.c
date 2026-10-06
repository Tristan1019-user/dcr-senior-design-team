#include "internal.h"

struct dcr_response_queue {
    pal_queue_t *queue;
    pal_mutex_t *lock;
    unsigned references;
    bool closed;
};

static bool acquire_use(dcr_response_queue_t *response)
{
    if (!response)
        return false;
    pal_mutex_lock(response->lock);
    bool open = !response->closed;
    if (open)
        ++response->references;
    pal_mutex_unlock(response->lock);
    return open;
}

static void release_reference(dcr_response_queue_t *response)
{
    if (!response)
        return;
    pal_queue_t *queue = NULL;
    pal_mutex_t *lock = NULL;
    pal_mutex_lock(response->lock);
    if (response->references)
        --response->references;
    if (!response->references) {
        queue = response->queue;
        lock = response->lock;
    }
    pal_mutex_unlock(response->lock);
    if (lock) {
        pal_queue_destroy(queue);
        pal_mutex_destroy(lock);
        pal_free(response);
    }
}

dcr_response_queue_t *dcr_response_queue_create(size_t depth)
{
    if (!depth)
        depth = DCR_BUS_RESPONSE_DEPTH;
    dcr_response_queue_t *response = pal_malloc(sizeof(*response));
    if (!response)
        return NULL;
    response->lock = pal_mutex_create();
    response->queue = pal_queue_create(depth, sizeof(dcr_bus_response_t));
    response->references = 1;
    if (!response->lock || !response->queue) {
        pal_queue_destroy(response->queue);
        pal_mutex_destroy(response->lock);
        pal_free(response);
        return NULL;
    }
    return response;
}

void dcr_response_queue_destroy(dcr_response_queue_t *response)
{
    if (!response)
        return;
    pal_mutex_lock(response->lock);
    response->closed = true;
    pal_mutex_unlock(response->lock);
    release_reference(response);
}

bool dcr_response_queue_send(dcr_response_queue_t *response,
                             const dcr_bus_response_t *value, uint32_t timeout_ms)
{
    if (!value || !acquire_use(response))
        return false;
    bool sent = pal_queue_send(response->queue, value, timeout_ms);
    release_reference(response);
    return sent;
}

bool dcr_response_queue_receive(dcr_response_queue_t *response,
                                dcr_bus_response_t *value, uint32_t timeout_ms)
{
    if (!value || !acquire_use(response))
        return false;
    bool received = pal_queue_receive(response->queue, value, timeout_ms);
    release_reference(response);
    return received;
}

static bool retain_response_queue(dcr_response_queue_t *response)
{
    return acquire_use(response);
}

static void release_response_queue(dcr_response_queue_t *response)
{
    release_reference(response);
}

void dcr_bus_request_release(const dcr_bus_request_t *request)
{
    if (request)
        release_response_queue(request->response_queue);
}

bool dcr_retain_queued_value(dcr_type_t type, const dcr_value_t *value)
{
    return type != DCR_BUS_REQUEST ||
           retain_response_queue(value ? value->bus_request.response_queue : NULL);
}

void dcr_release_queued_value(dcr_type_t type, const dcr_value_t *value)
{
    if (type == DCR_BUS_REQUEST && value)
        dcr_bus_request_release(&value->bus_request);
}
