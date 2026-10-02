#include "probe_storage_impl.h"

// modules() needs the generated aggregate to be a complete type, so
// the cross-module calls live here rather than in the header.
#include "logos_sdk.h"

std::string ProbeStorageImpl::subscribe()
{
    const bool ok = modules().storage_module.onStorageUploadDone(
        [this](const std::string& payload) { m_lastUpload = payload; });
    return ok ? "ok" : "failed";
}

std::string ProbeStorageImpl::lastUploadEvent()
{
    return m_lastUpload;
}

std::string ProbeStorageImpl::targetVersion()
{
    const std::string version = modules().storage_module.moduleVersion();
    return version.empty() ? "probe-call-failed: empty version"
                           : "probe-call-ok version=" + version;
}
