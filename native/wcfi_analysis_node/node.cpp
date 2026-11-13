#include "node.h"
#include "base/source/fstreamer.h"
#include "pluginterfaces/base/ibstream.h"
#include "pluginterfaces/vst/ivstmessage.h"
#include "pluginterfaces/vst/ivstprocesscontext.h"
#include <algorithm>
#include <cstring>

namespace wcfi {
Settings::Settings() {
    const char16_t text[] = u"Unassigned";
    std::copy(std::begin(text), std::end(text), label.begin());
}
bool Settings::read(IBStream* stream) {
    if (!stream) return false;
    IBStreamer reader(stream, kLittleEndian);
    int32 version, stage;
    std::array<TChar, 64> name {};
    if (!reader.readInt32(version) || version != 1 || !reader.readInt32(stage) ||
        stage < 0 || stage > 1) return false;
    for (auto& character : name) {
        uint16 value;
        if (!reader.readInt16u(value)) return false;
        character = static_cast<TChar>(value);
    }
    name.back() = 0;
    label = name; position = stage;
    return true;
}
bool Settings::write(IBStream* stream) const {
    if (!stream) return false;
    IBStreamer writer(stream, kLittleEndian);
    if (!writer.writeInt32(1) || !writer.writeInt32(position)) return false;
    for (auto character : label)
        if (!writer.writeInt16u(static_cast<uint16>(character))) return false;
    return true;
}

Processor::Processor() {
    setControllerClass(ControllerId);
    processContextRequirements.needContinousTimeSamples().needSystemTime().needTransportState();
}
tresult PLUGIN_API Processor::initialize(FUnknown* context) {
    auto result = AudioEffect::initialize(context);
    if (result != kResultOk) return result;
    addAudioInput(STR16("Input"), SpeakerArr::kStereo);
    addAudioOutput(STR16("Output"), SpeakerArr::kStereo);
    sender.open(); // Observation is optional. Audio works if capture cannot open.
    configure();
    return kResultOk;
}
tresult PLUGIN_API Processor::terminate() {
    sender.close(); return AudioEffect::terminate();
}
void Processor::configure() {
    char utf8[256] {};
    WideCharToMultiByte(CP_UTF8, 0, reinterpret_cast<const wchar_t*>(settings.label.data()),
        -1, utf8, sizeof(utf8), nullptr, nullptr);
    sender.configure(utf8, settings.position);
}
tresult PLUGIN_API Processor::setBusArrangements(SpeakerArrangement* in, int32 nIn,
                                                 SpeakerArrangement* out, int32 nOut) {
    if (!in || !out || nIn != 1 || nOut != 1 || in[0] != out[0] ||
        (in[0] != SpeakerArr::kMono && in[0] != SpeakerArr::kStereo)) return kResultFalse;
    return AudioEffect::setBusArrangements(in, nIn, out, nOut);
}
tresult PLUGIN_API Processor::canProcessSampleSize(int32 size) {
    return size == kSample32 || size == kSample64 ? kResultTrue : kResultFalse;
}
tresult PLUGIN_API Processor::setupProcessing(ProcessSetup& setup) {
    auto result = AudioEffect::setupProcessing(setup);
    if (result == kResultOk) sender.setup(setup.sampleRate, setup.maxSamplesPerBlock);
    return result;
}
tresult PLUGIN_API Processor::setProcessing(TBool state) {
    sender.processing(state != 0); return AudioEffect::setProcessing(state);
}
template<class T> void Processor::transfer(ProcessData& data, T* const* in, T* const* out) {
    const auto channels = data.outputs[0].numChannels;
    for (int ch = 0; ch < channels; ++ch) {
        if (!out || !out[ch]) continue;
        if (!in || !in[ch])
            memset(out[ch], 0, data.numSamples * sizeof(T));
        else if (out[ch] != in[ch]) memmove(out[ch], in[ch], data.numSamples * sizeof(T));
    }
    // No allocation, locks, sockets, files, SDK messages or waits on this path.
    // Offline rendering stays transparent and does not flood live observation.
    if (data.processMode == kOffline) return;
    int64 sample = 0, system = 0; uint32 flags = 0;
    if (auto context = data.processContext) {
        if (context->state & ProcessContext::kContTimeValid) {
            sample = context->continousTimeSamples; flags |= 1;
        }
        if (context->state & ProcessContext::kSystemTimeValid) {
            system = context->systemTime; flags |= 2;
        }
        if (context->state & ProcessContext::kPlaying) flags |= 4;
    }
    sender.publish(out, channels, data.numSamples, sample, system, flags);
}
tresult PLUGIN_API Processor::process(ProcessData& data) {
    if (!data.numInputs || !data.numOutputs || !data.inputs || !data.outputs || data.numSamples <= 0)
        return kResultOk;
    if (data.inputs[0].numChannels != data.outputs[0].numChannels ||
        data.outputs[0].numChannels < 1 || data.outputs[0].numChannels > 2) return kInvalidArgument;
    data.outputs[0].silenceFlags = data.inputs[0].silenceFlags;
    if (data.symbolicSampleSize == kSample32)
        transfer(data, data.inputs[0].channelBuffers32, data.outputs[0].channelBuffers32);
    else if (data.symbolicSampleSize == kSample64)
        transfer(data, data.inputs[0].channelBuffers64, data.outputs[0].channelBuffers64);
    else return kInvalidArgument;
    return kResultOk;
}
tresult PLUGIN_API Processor::getState(IBStream* stream) {
    return settings.write(stream) ? kResultOk : kResultFalse;
}
tresult PLUGIN_API Processor::setState(IBStream* stream) {
    Settings next;
    if (!next.read(stream)) return kResultFalse;
    settings = next; configure(); return kResultOk;
}
tresult PLUGIN_API Processor::notify(IMessage* message) {
    if (message && FIDStringsEqual(message->getMessageID(), "wcfi.settings.v1")) {
        Settings next; int64 stage;
        if (message->getAttributes()->getString("label", next.label.data(), sizeof(next.label)) != kResultOk ||
            message->getAttributes()->getInt("position", stage) != kResultOk || stage < 0 || stage > 1)
            return kInvalidArgument;
        next.label.back() = 0; next.position = static_cast<int32>(stage);
        settings = next; configure(); return kResultOk;
    }
    return AudioEffect::notify(message);
}
}
