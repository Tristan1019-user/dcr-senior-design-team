#ifndef DCR_PAL_H
#define DCR_PAL_H
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
typedef struct pal_mutex pal_mutex_t;
typedef struct pal_event pal_event_t;
typedef struct pal_queue pal_queue_t;
typedef struct pal_task pal_task_t;
void *pal_malloc(size_t size);
void pal_free(void *ptr);
size_t pal_allocations(void);
void pal_log(const char *level, const char *tag, const char *fmt, ...);
uint32_t pal_millis(void);
void pal_sleep(uint32_t ms);
int pal_read_file(const char *path, char *buf, size_t max_len);
pal_mutex_t *pal_mutex_create(void);
void pal_mutex_lock(pal_mutex_t *m);
void pal_mutex_unlock(pal_mutex_t *m);
void pal_mutex_destroy(pal_mutex_t *m);
pal_event_t *pal_event_create(void);
void pal_event_set(pal_event_t *e);
bool pal_event_wait(pal_event_t *e, uint32_t ms);
void pal_event_destroy(pal_event_t *e);
pal_queue_t *pal_queue_create(size_t depth, size_t item_size);
bool pal_queue_send(pal_queue_t *q, const void *value, uint32_t ms);
bool pal_queue_receive(pal_queue_t *q, void *value, uint32_t ms);
size_t pal_queue_count(pal_queue_t *q);
void pal_queue_destroy(pal_queue_t *q);
pal_task_t *pal_task_create(const char *name, void (*entry)(void *), void *arg,
                            uint32_t stack_bytes, unsigned priority, int core);
/* A successful join frees the PAL task handle; timeout preserves it. */
bool pal_task_join(pal_task_t *task, uint32_t ms);
unsigned pal_core_count(void);
unsigned pal_max_priority(void);
#endif
