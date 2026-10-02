#pragma once

#include <string>
#include "logos_module_context.h"

/// Probes the storage_module pinned by the release set: one call in,
/// one event out. Deriving LogosModuleContext gives us modules() —
/// typed callers and typed event subscriptions for everything in
/// metadata.json#dependencies.
class ProbeStorageImpl : public LogosModuleContext
{
public:
    /// Subscribes to storage_module's storageUploadDone event.
    /// Returns "ok" once the subscription is registered.
    std::string subscribe();

    /// The payload of the last storageUploadDone we received.
    /// Empty until one arrives.
    std::string lastUploadEvent();

    /// Round-trips a call into storage_module. Returns a
    /// self-describing string so the doc-test can assert the call
    /// actually succeeded rather than that something was printed.
    std::string targetVersion();

private:
    std::string m_lastUpload;
};
