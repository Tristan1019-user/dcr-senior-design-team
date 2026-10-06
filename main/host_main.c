#include "software.h"
#include <stdio.h>
#include <stdlib.h>
int main(int argc, char **argv)
{
    if (argc < 2) {
        fprintf(stderr, "usage: dcr_demo config.json [duration_ms]\n");
        return 2;
    }
    char *text = pal_malloc(65537);
    if (!text)
        return 2;
    if (pal_read_file(argv[1], text, 65537) < 0) {
        pal_free(text);
        return 2;
    }
    dcr_runtime_t *r = NULL;
    dcr_diagnostic_t diag;
    dcr_err_t e =
        dcr_build_json(text, dcr_software_registry, dcr_software_registry_count, &r, &diag);
    pal_free(text);
    if (e) {
        fprintf(stderr, "%s: %s\n", dcr_error_name(e), diag.context);
        dcr_runtime_destroy(r);
        return 1;
    }
    e = dcr_start_autostart(r);
    if (!e)
        pal_sleep(argc > 2 ? (uint32_t)strtoul(argv[2], NULL, 10) : 1500);
    cJSON *topology = dcr_get_topology(r);
    char *out = cJSON_Print(topology);
    if (out) {
        puts(out);
        cJSON_free(out);
    }
    cJSON_Delete(topology);
    dcr_err_t stop = dcr_runtime_destroy(r);
    printf("DCR_DEMO %s allocations=%zu\n", !e && !stop && !pal_allocations() ? "PASS" : "FAIL",
           pal_allocations());
    return e || stop || pal_allocations() ? 1 : 0;
}
