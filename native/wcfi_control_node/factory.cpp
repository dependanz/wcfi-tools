#include "node.h"
#include "public.sdk/source/main/pluginfactory.h"
using namespace Steinberg;
using namespace Steinberg::Vst;
BEGIN_FACTORY_DEF("WCFI", "https://wcfi.org", "")
DEF_CLASS2(INLINE_UID_FROM_FUID(wcfi_control::ProcessorId), PClassInfo::kManyInstances,
    kVstAudioEffectClass, "WCFI Control Node RED", 0, "Fx|Dynamics", "0.3.0", kVstVersionString,
    wcfi_control::Processor::createInstance)
DEF_CLASS2(INLINE_UID_FROM_FUID(wcfi_control::ControllerId), PClassInfo::kManyInstances,
    kVstComponentControllerClass, "WCFI Control Node RED Controller", 0, "", "0.3.0", kVstVersionString,
    wcfi_control::Controller::createInstance)
END_FACTORY
