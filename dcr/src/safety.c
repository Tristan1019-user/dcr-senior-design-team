#include "internal.h"
#include <stdio.h>

/* Policy is immutable after configuration loading. The sole control-plane
 * caller also owns topology edits and whole-runtime teardown. */
const char *dcr_connection_lock_id(dcr_runtime_t *r, dcr_port_t *destination)
{
    for (size_t n = 0; n < r->connection_lock_count; ++n)
        if (r->connection_locks[n].to == destination)
            return r->connection_locks[n].id;
    return NULL;
}

cJSON *dcr_get_safety_rules(dcr_runtime_t *r)
{
    if (!r)
        return NULL;
    cJSON *rules = cJSON_CreateArray();
    if (!rules)
        return NULL;
    pal_mutex_lock(r->lock);
    for (size_t n = 0; n < r->connection_lock_count; ++n) {
        const dcr_connection_lock_t *lock = &r->connection_locks[n];
        char from[2 * DCR_NAME_SIZE + 1], to[2 * DCR_NAME_SIZE + 1];
        snprintf(from, sizeof(from), "%s.%s", lock->from->owner->id, lock->from->decl->name);
        snprintf(to, sizeof(to), "%s.%s", lock->to->owner->id, lock->to->decl->name);
        cJSON *item = cJSON_CreateObject();
        if (!item || !cJSON_AddStringToObject(item, "id", lock->id) ||
            !cJSON_AddStringToObject(item, "type", "connection_locked") ||
            !cJSON_AddStringToObject(item, "from", from) ||
            !cJSON_AddStringToObject(item, "to", to) || !cJSON_AddItemToArray(rules, item)) {
            cJSON_Delete(item);
            cJSON_Delete(rules);
            rules = NULL;
            break;
        }
    }
    pal_mutex_unlock(r->lock);
    return rules;
}
