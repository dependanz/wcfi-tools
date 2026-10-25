#pragma once
#include "transport.h"
#include "public.sdk/source/vst/vstaudioeffect.h"
#include "public.sdk/source/vst/vsteditcontroller.h"
#include <array>

namespace wcfi {
using namespace Steinberg;
using namespace Steinberg::Vst;
inline const FUID ProcessorId(0x8E67F324, 0x2A37471B, 0xA1D8BD6E, 0x271B930C);
inline const FUID ControllerId(0xF2831006, 0xABD04795, 0x96B267F4, 0x009D14A8);
struct Settings {
    std::array<TChar, 64> label {};
    int32 position {};
    Settings();
    bool read(IBStream* stream);
    bool write(IBStream* stream) const;
};

class Processor final : public AudioEffect {
public:
    Processor();
    static FUnknown* createInstance(void*) { return static_cast<IAudioProcessor*>(new Processor); }
    tresult PLUGIN_API initialize(FUnknown*) override;
    tresult PLUGIN_API terminate() override;
    tresult PLUGIN_API setBusArrangements(SpeakerArrangement*, int32, SpeakerArrangement*, int32) override;
    tresult PLUGIN_API canProcessSampleSize(int32) override;
    tresult PLUGIN_API setupProcessing(ProcessSetup&) override;
    tresult PLUGIN_API setProcessing(TBool) override;
    tresult PLUGIN_API process(ProcessData&) override;
    uint32 PLUGIN_API getLatencySamples() override { return 0; }
    uint32 PLUGIN_API getTailSamples() override { return kNoTail; }
    tresult PLUGIN_API getState(IBStream*) override;
    tresult PLUGIN_API setState(IBStream*) override;
    tresult PLUGIN_API notify(IMessage*) override;
private:
    Settings settings;
    Sender sender;
    void configure();
    template<class T> void transfer(ProcessData&, T* const*, T* const*);
};

class Controller final : public EditController {
public:
    static FUnknown* createInstance(void*) { return static_cast<IEditController*>(new Controller); }
    tresult PLUGIN_API setComponentState(IBStream*) override;
    IPlugView* PLUGIN_API createView(FIDString) override;
    bool apply(const wchar_t* label, int position);
    Settings settings;
};
}
