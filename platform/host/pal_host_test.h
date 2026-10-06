#ifndef DCR_PAL_HOST_TEST_H
#define DCR_PAL_HOST_TEST_H

#include "pal.h"

/* Host-only fault injection. N permitted pal_malloc calls are allowed after
 * each reset; the following call and all later calls fail until reset. The
 * counter is atomic, but tests should still keep failpoint changes quiescent
 * so unrelated worker allocations do not consume the intended slots. */
void pal_host_test_fail_after(size_t permitted_allocations);
void pal_host_test_reset_alloc_fail(void);

#endif
