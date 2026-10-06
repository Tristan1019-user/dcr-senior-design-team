#include "internal.h"
dcr_err_t dcr_required(dcr_instance_t *i)
{
    for (unsigned p = 0; p < i->descriptor->port_count; ++p) {
        dcr_port_t *port = &i->ports[p];
        if (port->decl->direction == DCR_INPUT && port->decl->required && !port->source)
            return DCR_ERR_REQUIRED;
        if (port->decl->direction == DCR_OUTPUT && port->decl->type == DCR_BUS_REQUEST) {
            size_t destinations = 0;
            for (size_t n = 0; n < i->runtime->connection_count; ++n)
                if (i->runtime->connections[n].from == port)
                    ++destinations;
            if (destinations != 1)
                return DCR_ERR_REQUIRED;
        }
    }
    return DCR_OK;
}
dcr_err_t dcr_validate(dcr_runtime_t *r, dcr_diagnostic_t *diag)
{
    if (!r)
        return DCR_ERR_ARGUMENT;
    if (diag) {
        diag->code = DCR_OK;
        diag->context[0] = 0;
        diag->warnings = 0;
    }
    for (size_t n = 0; n < r->instance_count; ++n) {
        dcr_instance_t *i = r->instances[n];
        if (dcr_required(i)) {
            if (i->options.autostart)
                return dcr_fail(diag, DCR_ERR_REQUIRED, "%s: missing required input", i->id);
            if (diag)
                ++diag->warnings;
            if (r->settings.log_level <= 2)
                pal_log("warn", i->id, "required input missing; start deferred");
        }
    }
    return DCR_OK;
}
