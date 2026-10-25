#include "node.h"
#include "public.sdk/source/main/pluginfactory.h"
using namespace Steinberg;
using namespace Steinberg::Vst;
BEGIN_FACTORY_DEF("WCFI", "https://wcfi.org", "")
DEF_CLASS2(INLINE_UID_FROM_FUID(wcfi::ProcessorId), PClassInfo::kManyInstances,
    kVstAudioEffectClass, "WCFI Analysis Node", 0, "Fx|Analyzer", "0.1.0", kVstVersionString,
    wcfi::Processor::createInstance)
DEF_CLASS2(INLINE_UID_FROM_FUID(wcfi::ControllerId), PClassInfo::kManyInstances,
    kVstComponentControllerClass, "WCFI Analysis Node Controller", 0, "", "0.1.0", kVstVersionString,
    wcfi::Controller::createInstance)
END_FACTORY
