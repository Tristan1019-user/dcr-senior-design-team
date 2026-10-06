#include "pal.h"
#include <stdio.h>
#include <stdlib.h>
#include <stdarg.h>
#include <string.h>
#include <limits.h>
#ifdef _WIN32
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
struct pal_mutex {
    CRITICAL_SECTION native;
};
struct pal_event {
    HANDLE native;
};
struct pal_task {
    HANDLE native;
    void (*entry)(void *);
    void *arg;
};
static volatile LONG allocations;
static volatile LONG allocation_fail_after = -1;
static bool allocation_slot_available(void)
{
    LONG remaining;
    for (;;) {
        remaining = InterlockedCompareExchange(&allocation_fail_after, 0, 0);
        if (remaining < 0)
            return true;
        if (!remaining)
            return false;
        if (InterlockedCompareExchange(&allocation_fail_after, remaining - 1, remaining) ==
            remaining)
            return true;
    }
}
void *pal_malloc(size_t n)
{
    if (!allocation_slot_available())
        return NULL;
    void *p = calloc(1, n);
    if (p)
        InterlockedIncrement(&allocations);
    return p;
}
void pal_free(void *p)
{
    if (p) {
        InterlockedDecrement(&allocations);
        free(p);
    }
}
size_t pal_allocations(void)
{
    return (size_t)InterlockedCompareExchange(&allocations, 0, 0);
}
void pal_host_test_fail_after(size_t successful_allocations)
{
    LONG value = successful_allocations > (size_t)LONG_MAX ? LONG_MAX
                                                            : (LONG)successful_allocations;
    InterlockedExchange(&allocation_fail_after, value);
}
void pal_host_test_reset_alloc_fail(void)
{
    InterlockedExchange(&allocation_fail_after, -1);
}
uint32_t pal_millis(void)
{
    return (uint32_t)GetTickCount64();
}
void pal_sleep(uint32_t ms)
{
    Sleep(ms ? ms : 1);
}
pal_mutex_t *pal_mutex_create(void)
{
    pal_mutex_t *m = pal_malloc(sizeof(*m));
    if (m)
        InitializeCriticalSection(&m->native);
    return m;
}
void pal_mutex_lock(pal_mutex_t *m)
{
    EnterCriticalSection(&m->native);
}
void pal_mutex_unlock(pal_mutex_t *m)
{
    LeaveCriticalSection(&m->native);
}
void pal_mutex_destroy(pal_mutex_t *m)
{
    if (m) {
        DeleteCriticalSection(&m->native);
        pal_free(m);
    }
}
pal_event_t *pal_event_create(void)
{
    pal_event_t *e = pal_malloc(sizeof(*e));
    if (e && !(e->native = CreateEvent(NULL, FALSE, FALSE, NULL))) {
        pal_free(e);
        return NULL;
    }
    return e;
}
void pal_event_set(pal_event_t *e)
{
    SetEvent(e->native);
}
bool pal_event_wait(pal_event_t *e, uint32_t ms)
{
    return WaitForSingleObject(e->native, ms) == WAIT_OBJECT_0;
}
void pal_event_destroy(pal_event_t *e)
{
    if (e) {
        CloseHandle(e->native);
        pal_free(e);
    }
}
static DWORD WINAPI trampoline(LPVOID p)
{
    pal_task_t *t = p;
    t->entry(t->arg);
    return 0;
}
pal_task_t *pal_task_create(const char *name, void (*entry)(void *), void *arg, uint32_t stack,
                            unsigned priority, int core)
{
    (void)name;
    (void)stack;
    (void)priority;
    (void)core;
    pal_task_t *t = pal_malloc(sizeof(*t));
    if (!t)
        return NULL;
    t->entry = entry;
    t->arg = arg;
    t->native = CreateThread(NULL, 0, trampoline, t, 0, NULL);
    if (!t->native) {
        pal_free(t);
        return NULL;
    }
    return t;
}
bool pal_task_join(pal_task_t *t, uint32_t ms)
{
    if (WaitForSingleObject(t->native, ms) != WAIT_OBJECT_0)
        return false;
    CloseHandle(t->native);
    pal_free(t);
    return true;
}
#else
#include <pthread.h>
#include <stdatomic.h>
#include <time.h>
#include <errno.h>
struct pal_mutex {
    pthread_mutex_t native;
};
struct pal_event {
    pthread_mutex_t lock;
    pthread_cond_t cond;
    bool set;
};
struct pal_task {
    pthread_t native;
    void (*entry)(void *);
    void *arg;
    pal_event_t *done;
};
static atomic_size_t allocations;
static atomic_long allocation_fail_after = ATOMIC_VAR_INIT(-1);
static bool allocation_slot_available(void)
{
    long remaining = atomic_load_explicit(&allocation_fail_after, memory_order_relaxed);
    for (;;) {
        if (remaining < 0)
            return true;
        if (!remaining)
            return false;
        if (atomic_compare_exchange_weak_explicit(&allocation_fail_after, &remaining,
                                                  remaining - 1, memory_order_relaxed,
                                                  memory_order_relaxed))
            return true;
    }
}
void *pal_malloc(size_t n)
{
    if (!allocation_slot_available())
        return NULL;
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
void pal_host_test_fail_after(size_t successful_allocations)
{
    long value = successful_allocations > (size_t)LONG_MAX ? LONG_MAX
                                                            : (long)successful_allocations;
    atomic_store_explicit(&allocation_fail_after, value, memory_order_relaxed);
}
void pal_host_test_reset_alloc_fail(void)
{
    atomic_store_explicit(&allocation_fail_after, -1, memory_order_relaxed);
}
uint32_t pal_millis(void)
{
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return (uint32_t)((uint64_t)t.tv_sec * 1000 + t.tv_nsec / 1000000);
}
void pal_sleep(uint32_t ms)
{
    struct timespec t = {ms / 1000, (long)(ms % 1000) * 1000000};
    while (nanosleep(&t, &t) && errno == EINTR) {
    }
}
pal_mutex_t *pal_mutex_create(void)
{
    pal_mutex_t *m = pal_malloc(sizeof(*m));
    if (m)
        pthread_mutex_init(&m->native, NULL);
    return m;
}
void pal_mutex_lock(pal_mutex_t *m)
{
    pthread_mutex_lock(&m->native);
}
void pal_mutex_unlock(pal_mutex_t *m)
{
    pthread_mutex_unlock(&m->native);
}
void pal_mutex_destroy(pal_mutex_t *m)
{
    if (m) {
        pthread_mutex_destroy(&m->native);
        pal_free(m);
    }
}
pal_event_t *pal_event_create(void)
{
    pal_event_t *e = pal_malloc(sizeof(*e));
    if (!e)
        return NULL;
    pthread_mutex_init(&e->lock, NULL);
    pthread_condattr_t a;
    pthread_condattr_init(&a);
    pthread_condattr_setclock(&a, CLOCK_MONOTONIC);
    pthread_cond_init(&e->cond, &a);
    pthread_condattr_destroy(&a);
    return e;
}
void pal_event_set(pal_event_t *e)
{
    pthread_mutex_lock(&e->lock);
    e->set = true;
    pthread_cond_signal(&e->cond);
    pthread_mutex_unlock(&e->lock);
}
bool pal_event_wait(pal_event_t *e, uint32_t ms)
{
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    t.tv_sec += ms / 1000;
    t.tv_nsec += (long)(ms % 1000) * 1000000;
    if (t.tv_nsec >= 1000000000) {
        ++t.tv_sec;
        t.tv_nsec -= 1000000000;
    }
    pthread_mutex_lock(&e->lock);
    int rc = 0;
    while (!e->set && !rc)
        rc = pthread_cond_timedwait(&e->cond, &e->lock, &t);
    bool set = e->set;
    e->set = false;
    pthread_mutex_unlock(&e->lock);
    return set;
}
void pal_event_destroy(pal_event_t *e)
{
    if (e) {
        pthread_cond_destroy(&e->cond);
        pthread_mutex_destroy(&e->lock);
        pal_free(e);
    }
}
static void *trampoline(void *p)
{
    pal_task_t *t = p;
    t->entry(t->arg);
    pal_event_set(t->done);
    return NULL;
}
pal_task_t *pal_task_create(const char *name, void (*entry)(void *), void *arg, uint32_t stack,
                            unsigned priority, int core)
{
    (void)name;
    (void)stack;
    (void)priority;
    (void)core;
    pal_task_t *t = pal_malloc(sizeof(*t));
    if (!t)
        return NULL;
    t->entry = entry;
    t->arg = arg;
    t->done = pal_event_create();
    if (!t->done || pthread_create(&t->native, NULL, trampoline, t)) {
        pal_event_destroy(t->done);
        pal_free(t);
        return NULL;
    }
    return t;
}
bool pal_task_join(pal_task_t *t, uint32_t ms)
{
    if (!pal_event_wait(t->done, ms))
        return false;
    pthread_join(t->native, NULL);
    pal_event_destroy(t->done);
    pal_free(t);
    return true;
}
#endif

unsigned pal_core_count(void)
{
    return 2;
} /* Model the S3's accepted config; no host affinity guarantee. */
unsigned pal_max_priority(void)
{
    return 24;
}
void pal_log(const char *level, const char *tag, const char *fmt, ...)
{
    char line[512];
    va_list args;
    va_start(args, fmt);
    vsnprintf(line, sizeof(line), fmt, args);
    va_end(args);
    fprintf(stderr, "[%s] %s: %s\n", level, tag, line);
}
struct pal_queue {
    pal_mutex_t *lock;
    pal_event_t *readable, *writable;
    unsigned char *data;
    size_t depth, size, head, count;
};
pal_queue_t *pal_queue_create(size_t depth, size_t size)
{
    if (!depth || !size || depth > SIZE_MAX / size)
        return NULL;
    pal_queue_t *q = pal_malloc(sizeof(*q));
    if (!q)
        return NULL;
    q->lock = pal_mutex_create();
    q->readable = pal_event_create();
    q->writable = pal_event_create();
    q->data = pal_malloc(depth * size);
    q->depth = depth;
    q->size = size;
    if (!q->lock || !q->readable || !q->writable || !q->data) {
        pal_queue_destroy(q);
        return NULL;
    }
    return q;
}
static bool transfer(pal_queue_t *q, void *v, uint32_t ms, bool send)
{
    uint32_t start = pal_millis();
    for (;;) {
        pal_mutex_lock(q->lock);
        bool ready = send ? q->count < q->depth : q->count > 0;
        if (ready) {
            size_t index = send ? (q->head + q->count) % q->depth : q->head;
            if (send) {
                memcpy(q->data + index * q->size, v, q->size);
                ++q->count;
            } else {
                memcpy(v, q->data + index * q->size, q->size);
                q->head = (q->head + 1) % q->depth;
                --q->count;
            }
            pal_mutex_unlock(q->lock);
            pal_event_set(send ? q->readable : q->writable);
            return true;
        }
        pal_mutex_unlock(q->lock);
        uint32_t elapsed = pal_millis() - start;
        if (elapsed >= ms)
            return false;
        uint32_t wait = ms - elapsed;
        if (wait > 10)
            wait = 10;
        pal_event_wait(send ? q->writable : q->readable, wait);
    }
}
bool pal_queue_send(pal_queue_t *q, const void *v, uint32_t ms)
{
    return transfer(q, (void *)v, ms, true);
}
bool pal_queue_receive(pal_queue_t *q, void *v, uint32_t ms)
{
    return transfer(q, v, ms, false);
}
size_t pal_queue_count(pal_queue_t *q)
{
    pal_mutex_lock(q->lock);
    size_t n = q->count;
    pal_mutex_unlock(q->lock);
    return n;
}
void pal_queue_destroy(pal_queue_t *q)
{
    if (q) {
        pal_mutex_destroy(q->lock);
        pal_event_destroy(q->readable);
        pal_event_destroy(q->writable);
        pal_free(q->data);
        pal_free(q);
    }
}
