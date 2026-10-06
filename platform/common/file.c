#include "pal.h"
#include <stdio.h>
#include <limits.h>
int pal_read_file(const char *path, char *buf, size_t max_len)
{
    if (!path || !buf || max_len < 2 || max_len > INT_MAX)
        return -1;
    FILE *f = fopen(path, "rb");
    if (!f)
        return -1;
    size_t n = fread(buf, 1, max_len - 1, f);
    int extra = fgetc(f);
    int bad = ferror(f) || extra != EOF;
    fclose(f);
    buf[n] = '\0';
    return bad ? -1 : (int)n;
}
