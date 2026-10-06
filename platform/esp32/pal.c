#include "pal.h"
#include <stdlib.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdatomic.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"
#include "freertos/queue.h"
#include "esp_timer.h"
#include "esp_log.h"
struct pal_mutex {
    SemaphoreHandle_t native;
};
struct pal_event {
    SemaphoreHandle_t native;
};
struct pal_queue {
    QueueHandle_t native;
};
struct pal_task {
    void (*entry)(void *);
    void *arg;
    SemaphoreHandle_t done;
};
static atomic_size_t allocations;
static TickType_t ticks(uint32_t ms)
{
    TickType_t n = pdMS_TO_TICKS(ms);
    return ms && !n ? 1 : n;
}
void *pal_malloc(size_t n)
{
    void *p = calloc(1, n);
    if (p)
        atomic_fetch_add(&allocations, 1);
    return p;
}
void pal_free(void *p)
{
    if (p) {
        atomic_fetch_sub(&allocations, 1);
        free(p);
    }
}
size_t pal_allocations(void)
{
    return atomic_load(&allocations);
}
void pal_log(const char *level, const char *tag, const char *fmt, ...)
{
    char buf[256];
    va_list args;
    va_start(args, fmt);
    vsnprintf(buf, sizeof(buf), fmt, args);
    va_end(args);
    ESP_LOGI(tag, "[%s] %s", level, buf);
}
uint32_t pal_millis(void)
{
    return (uint32_t)(esp_timer_get_time() / 1000);
}
void pal_sleep(uint32_t ms)
{
    vTaskDelay(ticks(ms ? ms : 1));
}
unsigned pal_core_count(void)
{
    return portNUM_PROCESSORS;
}
unsigned pal_max_priority(void)
{
    return configMAX_PRIORITIES - 1;
}
pal_mutex_t *pal_mutex_create(void)
{
    pal_mutex_t *m = pal_malloc(sizeof(*m));
    if (!m)
        return NULL;
    m->native = xSemaphoreCreateMutex();
    if (!m->native) {
        pal_free(m);
        return NULL;
    }
    return m;
}
void pal_mutex_lock(pal_mutex_t *m)
{
    xSemaphoreTake(m->native, portMAX_DELAY);
}
void pal_mutex_unlock(pal_mutex_t *m)
{
    xSemaphoreGive(m->native);
}
void pal_mutex_destroy(pal_mutex_t *m)
{
    if (m) {
        vSemaphoreDelete(m->native);
        pal_free(m);
    }
}
pal_event_t *pal_event_create(void)
{
    pal_event_t *e = pal_malloc(sizeof(*e));
    if (!e)
        return NULL;
    e->native = xSemaphoreCreateBinary();
    if (!e->native) {
        pal_free(e);
        return NULL;
    }
    return e;
}
void pal_event_set(pal_event_t *e)
{
    xSemaphoreGive(e->native);
}
bool pal_event_wait(pal_event_t *e, uint32_t ms)
{
    return xSemaphoreTake(e->native, ticks(ms)) == pdTRUE;
}
void pal_event_destroy(pal_event_t *e)
{
    if (e) {
        vSemaphoreDelete(e->native);
        pal_free(e);
    }
}
pal_queue_t *pal_queue_create(size_t depth, size_t size)
{
    if (!depth || !size || depth > UINT32_MAX || size > UINT32_MAX)
        return NULL;
    pal_queue_t *q = pal_malloc(sizeof(*q));
    if (!q)
        return NULL;
    q->native = xQueueCreate(depth, size);
    if (!q->native) {
        pal_free(q);
        return NULL;
    }
    return q;
}
bool pal_queue_send(pal_queue_t *q, const void *v, uint32_t ms)
{
    return xQueueSend(q->native, v, ticks(ms)) == pdTRUE;
}
bool pal_queue_receive(pal_queue_t *q, void *v, uint32_t ms)
{
    return xQueueReceive(q->native, v, ticks(ms)) == pdTRUE;
}
size_t pal_queue_count(pal_queue_t *q)
{
    return uxQueueMessagesWaiting(q->native);
}
void pal_queue_destroy(pal_queue_t *q)
{
    if (q) {
        vQueueDelete(q->native);
        pal_free(q);
    }
}
static void trampoline(void *arg)
{
    pal_task_t *t = arg;
    t->entry(t->arg);
    /* No access to t after giving completion: join may immediately free it. */
    xSemaphoreGive(t->done);
    vTaskDelete(NULL);
}
pal_task_t *pal_task_create(const char *name, void (*entry)(void *), void *arg, uint32_t stack,
                            unsigned priority, int core)
{
    pal_task_t *t = pal_malloc(sizeof(*t));
    if (!t)
        return NULL;
    t->entry = entry;
    t->arg = arg;
    t->done = xSemaphoreCreateBinary();
    if (!t->done) {
        pal_free(t);
        return NULL;
    }
    BaseType_t ok = xTaskCreatePinnedToCore(trampoline, name, stack, t, priority, NULL,
                                            core < 0 ? tskNO_AFFINITY : core);
    if (ok != pdPASS) {
        vSemaphoreDelete(t->done);
        pal_free(t);
        return NULL;
    }
    return t;
}
bool pal_task_join(pal_task_t *t, uint32_t ms)
{
    if (xSemaphoreTake(t->done, ticks(ms)) != pdTRUE)
        return false;
    vSemaphoreDelete(t->done);
    pal_free(t);
    return true;
}
