#include "probe_delivery_impl.h"

// modules() needs the generated aggregate to be a complete type, so
// the cross-module calls live here rather than in the header.
#include "logos_sdk.h"

std::string ProbeDeliveryImpl::subscribe()
{
    const bool ok = modules().delivery_module.onNodeStarted(
        [this](bool success, const std::string& message, int64_t timestamp) {
            m_lastNodeStarted = std::string("success=") +
                (success ? "true" : "false") +
                " message=" + message +
                " timestamp=" + std::to_string(timestamp);
        });
    return ok ? "ok" : "failed";
}

std::string ProbeDeliveryImpl::lastNodeStarted()
{
    return m_lastNodeStarted;
}

std::string ProbeDeliveryImpl::nodeInfo(const std::string& what)
{
    auto result = modules().delivery_module.getNodeInfo(what);
    // Self-describing so the doc-test can assert the call actually
    // succeeded rather than that something was printed.
    return result.success ? ("probe-call-ok " + result.value.dump())
                          : ("probe-call-failed " + result.error);
}
