#pragma once

#include <string>
#include "logos_module_context.h"

/// Probes the delivery_module pinned by the release set. Subscribes
/// to nodeStarted before the node comes up, so the event assertion
/// does not depend on the node finding peers.
class ProbeDeliveryImpl : public LogosModuleContext
{
public:
    /// Subscribes to delivery_module's nodeStarted event.
    std::string subscribe();

    /// Description of the last nodeStarted event, or "" if none yet.
    std::string lastNodeStarted();

    /// Round-trips a call into delivery_module and returns what it said.
    std::string nodeInfo(const std::string& what);

private:
    std::string m_lastNodeStarted;
};
