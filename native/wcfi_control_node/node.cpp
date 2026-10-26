#include "node.h"
#include "editor.h"
#include "base/source/fstreamer.h"
#include "pluginterfaces/vst/ivstparameterchanges.h"
#include "public.sdk/source/vst/vstparameters.h"
#include <algorithm>
#include <cmath>

namespace wcfi_control {
namespace {
constexpr double minimum[] {0, -6, -36, 0, 0, -90};
constexpr double maximum[] {1, 6, -12, 6, 6, -24};
constexpr double defaults[] {0, 0, -26, 6, 6, -55};
const TChar* names[] {STR16("Calibrate & Hold"), STR16("Trim dB"), STR16("Target RMS"),
    STR16("Max Boost"), STR16("Max Cut"), STR16("Activity Threshold")};
const TChar* units[] {nullptr, STR16("dB"), STR16("dBFS"), STR16("dB"), STR16("dB"), STR16("dBFS")};
constexpr double meter_minimum[] {-161, -161, -161, -161, -25, -193, -1, -1, -1, 0};
constexpr double meter_maximum[] {48, 48, 48, 48, 24, 24, 192, 4, 1, 65535};
const TChar* meter_names[] {STR16("Input RMS"), STR16("Input Peak"), STR16("Output RMS"),
    STR16("Output Peak"), STR16("Auto Gain"), STR16("Applied Gain"), STR16("Guard Reduction"),
    STR16("Activity State"), STR16("Activity Gate"), STR16("Telemetry Sequence")};
double level_db(double linear) { return linear > 0 ? std::max(-160., 20. * std::log10(linear)) : -160.; }
double value(const WcfiControlConfig& c, int i) {
    switch (i) { case 0: return c.auto_enabled; case 1: return c.trim_db; case 2: return c.target_dbfs;
        case 3: return c.max_boost_db; case 4: return c.max_cut_db; default: return c.activity_dbfs; }
}
}
double meter_plain(ParamID id, double n) {
    if (id < InputRms || id > TelemetrySequence || !std::isfinite(n)) return 0.;
    const auto i = id - InputRms;
    return meter_minimum[i] + std::clamp(n, 0., 1.) * (meter_maximum[i] - meter_minimum[i]);
}
double meter_normalized(ParamID id, double p) {
    if (id < InputRms || id > TelemetrySequence || !std::isfinite(p)) return 0.;
    const auto i = id - InputRms;
    return std::clamp((p - meter_minimum[i]) / (meter_maximum[i] - meter_minimum[i]), 0., 1.);
}
double plain(ParamID id, double n) { const int i = id - Auto; return minimum[i] + n * (maximum[i] - minimum[i]); }
double normalized(ParamID id, double p) { const int i = id - Auto; return (p - minimum[i]) / (maximum[i] - minimum[i]); }
bool set_parameter(WcfiControlConfig& c, ParamID id, double n) {
    if (id < Auto || id > Activity || !std::isfinite(n) || n < 0 || n > 1) return false;
    const double p = plain(id, n);
    switch (id) { case Auto: c.auto_enabled = n >= .5; break; case Trim: c.trim_db = p; break;
        case Target: c.target_dbfs = p; break; case Boost: c.max_boost_db = p; break;
        case Cut: c.max_cut_db = p; break; case Activity: c.activity_dbfs = p; break; }
    return true;
}
bool write_state(IBStream* stream, const WcfiControlConfig& c) {
    if (!stream || !valid_mic_config(c)) return false;
    IBStreamer writer(stream, kLittleEndian);
    if (!writer.writeInt32(2)) return false;
    // Learning and automatic activation belong to this running instance only.
    // A saved preset never silently rearms calibration on a different source.
    for (int i = 0; i < ParameterCount; ++i)
        if (!writer.writeDouble(i == 0 ? 0. : value(c, i))) return false;
    return true;
}
bool read_state(IBStream* stream, WcfiControlConfig& destination) {
    if (!stream) return false;
    IBStreamer reader(stream, kLittleEndian);
    int32 version;
    if (!reader.readInt32(version) || (version != 1 && version != 2)) return false;
    constexpr double legacy_minimum[] {0, -24, -36, 0, 0, -90};
    constexpr double legacy_maximum[] {1, 24, -12, 24, 24, -24};
    WcfiControlConfig next = mic_defaults();
    for (int i = 0; i < ParameterCount; ++i) {
        double p;
        const double low = version == 1 ? legacy_minimum[i] : minimum[i];
        const double high = version == 1 ? legacy_maximum[i] : maximum[i];
        if (!reader.readDouble(p) || !std::isfinite(p) || p < low || p > high
                || (i == 0 && p != 0 && p != 1)) return false;
        p = i == 0 ? 0. : std::clamp(p, minimum[i], maximum[i]);
        if (!set_parameter(next, Auto + i, normalized(Auto + i, p))) return false;
    }
    if (!valid_mic_config(next)) return false;
    destination = next; return true;
}
void ConfigMailbox::publish(const WcfiControlConfig& c) {
    sequence.fetch_add(1, std::memory_order_acq_rel);
    for (int i = 0; i < ParameterCount; ++i) values[i].store(value(c, i), std::memory_order_relaxed);
    sequence.fetch_add(1, std::memory_order_release);
}
bool ConfigMailbox::read(WcfiControlConfig& c, uint64_t& generation) const {
    for (int attempt = 0; attempt < 3; ++attempt) {
        const auto before = sequence.load(std::memory_order_acquire);
        if (before & 1) continue;
        WcfiControlConfig next = mic_defaults();
        for (int i = 0; i < ParameterCount; ++i)
            if (!set_parameter(next, Auto + i, normalized(Auto + i, values[i].load(std::memory_order_relaxed)))) return false;
        std::atomic_thread_fence(std::memory_order_acquire);
        if (before == sequence.load(std::memory_order_acquire)) { c = next; generation = before; return true; }
    }
    return false;
}
Processor::Processor() {
    setControllerClass(ControllerId);
    pending_state.publish(config); saved_state.publish(config);
    consumed_state.store(pending_state.sequence.load());
}
void Processor::synchronize_state() {
    WcfiControlConfig next; uint64_t generation;
    if (pending_state.read(next, generation) && generation != consumed_state.load(std::memory_order_relaxed)) {
        config = next; engine.reset(); engine.configure(config);
        reset_meters();
        saved_state.publish(config); consumed_state.store(generation, std::memory_order_release);
    }
}
void Processor::reset_meters() {
    output_powers.fill(0.); output_peaks.fill(0.);
    output_squares = output_frame_peak = 0.;
    output_rms_dbfs = output_peak_dbfs = -160.;
    output_frames = output_history_count = output_history_index = 0;
    output_quantum = static_cast<uint32_t>(std::max(1., std::round(sample_rate * .01)));
    // Publish the first available block; retain sequence across process restarts.
    meter_elapsed_frames = static_cast<uint64_t>(std::max(1., std::round(sample_rate * .05)));
}

template<class T> void Processor::meter_frame(const T* output) {
    for (uint32_t ch = 0; ch < channels; ++ch) {
        const double value = static_cast<double>(output[ch]);
        const double measured = std::clamp(value, -1e100, 1e100);
        output_squares += measured * measured;
        output_frame_peak = std::max(output_frame_peak, std::abs(value));
    }
    ++output_frames;
    if (output_frames < output_quantum) return;
    output_powers[output_history_index] = output_squares / (output_frames * channels);
    output_peaks[output_history_index] = output_frame_peak;
    output_history_index = (output_history_index + 1) % 10;
    output_history_count = std::min(10u, output_history_count + 1);
    double power = 0., peak = 0.;
    for (uint32_t i = 0; i < output_history_count; ++i) {
        power += output_powers[i]; peak = std::max(peak, output_peaks[i]);
    }
    output_rms_dbfs = level_db(std::sqrt(power / output_history_count));
    output_peak_dbfs = level_db(peak);
    output_frames = 0; output_squares = output_frame_peak = 0.;
}

void Processor::publish_meters(ProcessData& data) {
    if (data.numSamples <= 0) return;
    const auto interval = static_cast<uint64_t>(std::max(1., std::round(sample_rate * .05)));
    meter_elapsed_frames = std::min(interval, meter_elapsed_frames + static_cast<uint64_t>(data.numSamples));
    // RED input measurements are completed 100 ms observations. Do not publish
    // default input floors as live measurements during the first partial bin.
    const auto first_input_bin = static_cast<uint64_t>(std::max(1., std::round(sample_rate * .1)));
    if (!data.outputParameterChanges || meter_elapsed_frames < interval || !output_history_count
            || engine.status().samples_processed < first_input_bin) return;
    // Bound attempts as well as successful publications. A failing host queue
    // must not turn a 20 Hz display into callback-rate repeated work.
    meter_elapsed_frames = 0;
    const auto s = engine.status();
    const double values[] {std::max(-160., s.input_rms_dbfs), std::max(-160., s.input_peak_dbfs),
        output_rms_dbfs, output_peak_dbfs, s.auto_gain_db, std::max(-192., s.applied_gain_db),
        std::max(0., -s.guard_attenuation_db), static_cast<double>(s.state), static_cast<double>(s.activity)};
    bool complete = true;
    for (int i = 0; i < MeterPayloadCount; ++i) {
        int32 queue_index = 0, point_index = 0;
        const ParamID id = InputRms + i;
        auto queue = data.outputParameterChanges->addParameterData(id, queue_index);
        if (!queue || queue->addPoint(data.numSamples - 1, meter_normalized(id, values[i]), point_index) != kResultOk)
            complete = false;
    }
    // The host owns these queues. No allocation is performed by this processor;
    // hosts must provide realtime-safe IParameterChanges implementations.
    // A pulse proves that all payload points were accepted, not merely that
    // the process function ran. Null/partial host queues remain stale in the UI.
    if (complete) {
        int32 queue_index = 0, point_index = 0;
        auto queue = data.outputParameterChanges->addParameterData(TelemetrySequence, queue_index);
        const uint32_t next = telemetry_sequence >= 65535 ? 1 : telemetry_sequence + 1;
        if (queue && queue->addPoint(data.numSamples - 1,
                meter_normalized(TelemetrySequence, next), point_index) == kResultOk) {
            telemetry_sequence = next;
        }
    }
}

tresult PLUGIN_API Processor::initialize(FUnknown* context) {
    auto result = AudioEffect::initialize(context);
    if (result != kResultOk) return result;
    addAudioInput(STR16("Input"), SpeakerArr::kStereo); addAudioOutput(STR16("Output"), SpeakerArr::kStereo);
    return kResultOk;
}
tresult PLUGIN_API Processor::setBusArrangements(SpeakerArrangement* in, int32 ni, SpeakerArrangement* out, int32 no) {
    if (!in || !out || ni != 1 || no != 1 || in[0] != out[0]
        || (in[0] != SpeakerArr::kMono && in[0] != SpeakerArr::kStereo)) return kResultFalse;
    auto result = AudioEffect::setBusArrangements(in, ni, out, no);
    if (result == kResultOk) { channels = in[0] == SpeakerArr::kMono ? 1 : 2; engine.setup(sample_rate, channels); engine.configure(config); reset_meters(); }
    return result;
}
tresult PLUGIN_API Processor::canProcessSampleSize(int32 size) { return size == kSample32 || size == kSample64 ? kResultTrue : kResultFalse; }
tresult PLUGIN_API Processor::setupProcessing(ProcessSetup& setup) {
    if (!engine.setup(setup.sampleRate, channels)) return kInvalidArgument;
    auto result = AudioEffect::setupProcessing(setup);
    if (result == kResultOk) { sample_rate = setup.sampleRate; engine.configure(config); reset_meters(); }
    return result;
}
tresult PLUGIN_API Processor::setProcessing(TBool state) {
    if (state) { engine.reset(); engine.configure(config); reset_meters(); }
    return AudioEffect::setProcessing(state);
}
template<class T> void Processor::transfer(ProcessData& data, T* const* in, T* const* out) {
    std::array<IParamValueQueue*, ParameterCount> queues {};
    std::array<int32, ParameterCount> indices {};
    if (data.inputParameterChanges) for (int32 i = 0; i < data.inputParameterChanges->getParameterCount(); ++i) {
        auto queue = data.inputParameterChanges->getParameterData(i);
        if (queue && queue->getParameterId() >= Auto && queue->getParameterId() <= Activity)
            queues[queue->getParameterId() - Auto] = queue;
    }
    for (int32 frame = 0; frame < data.numSamples; ++frame) {
        bool changed = false;
        for (int i = 0; i < ParameterCount; ++i) if (auto queue = queues[i]) {
            while (indices[i] < queue->getPointCount()) {
                int32 offset; ParamValue n;
                if (queue->getPoint(indices[i], offset, n) != kResultTrue) { ++indices[i]; continue; }
                if (offset > frame) break;
                if (offset >= 0) changed |= set_parameter(config, Auto + i, n);
                ++indices[i];
            }
        }
        if (changed) engine.configure(config);
        T input[2] {}, output[2] {};
        for (uint32_t ch = 0; ch < channels; ++ch)
            if (in && in[ch] && !(data.inputs[0].silenceFlags & (uint64_t(1) << ch))) input[ch] = in[ch][frame];
        engine.frame(input, output);
        meter_frame(output);
        for (uint32_t ch = 0; ch < channels; ++ch) if (out && out[ch]) out[ch][frame] = output[ch];
    }
}
tresult PLUGIN_API Processor::process(ProcessData& data) {
    synchronize_state();
    if (data.numSamples <= 0) {
        if (data.inputParameterChanges) for (int32 i = 0; i < data.inputParameterChanges->getParameterCount(); ++i) {
            auto q = data.inputParameterChanges->getParameterData(i);
            if (q && q->getPointCount()) { int32 offset; ParamValue n;
                if (q->getPoint(q->getPointCount() - 1, offset, n) == kResultTrue) set_parameter(config, q->getParameterId(), n); }
        }
        engine.configure(config); saved_state.publish(config); return kResultOk;
    }
    if (!data.numInputs || !data.numOutputs || !data.inputs || !data.outputs) return kResultOk;
    if (data.inputs[0].numChannels != channels || data.outputs[0].numChannels != channels) return kInvalidArgument;
    data.outputs[0].silenceFlags = data.inputs[0].silenceFlags;
    if (data.symbolicSampleSize == kSample32) transfer(data, data.inputs[0].channelBuffers32, data.outputs[0].channelBuffers32);
    else if (data.symbolicSampleSize == kSample64) transfer(data, data.inputs[0].channelBuffers64, data.outputs[0].channelBuffers64);
    else return kInvalidArgument;
    publish_meters(data);
    saved_state.publish(config); return kResultOk;
}
tresult PLUGIN_API Processor::getState(IBStream* stream) {
    WcfiControlConfig current; uint64_t generation;
    if (!pending_state.read(current, generation)) return kResultFalse;
    if (generation == consumed_state.load(std::memory_order_acquire) && !saved_state.read(current, generation)) return kResultFalse;
    return write_state(stream, current) ? kResultOk : kResultFalse;
}
tresult PLUGIN_API Processor::setState(IBStream* stream) {
    WcfiControlConfig next;
    if (!read_state(stream, next)) return kResultFalse;
    pending_state.publish(next); return kResultOk;
}
tresult PLUGIN_API Controller::initialize(FUnknown* context) {
    auto result = EditController::initialize(context);
    if (result != kResultOk) return result;
    for (int i = 0; i < ParameterCount; ++i)
        parameters.addParameter(new RangeParameter(names[i], Auto + i, units[i], minimum[i], maximum[i], defaults[i], i == 0 ? 1 : 0));
    for (int i = 0; i < MeterCount; ++i) {
        const ParamID id = InputRms + i;
        if (id == ActivityState || id == ActivityGate) {
            auto parameter = new StringListParameter(meter_names[i], id, nullptr,
                ParameterInfo::kIsReadOnly | ParameterInfo::kIsList);
            parameter->appendString(STR16("Unavailable"));
            if (id == ActivityState) {
                parameter->appendString(STR16("Manual")); parameter->appendString(STR16("Waiting"));
                parameter->appendString(STR16("Collecting")); parameter->appendString(STR16("Ramping"));
                parameter->appendString(STR16("Holding"));
            } else {
                parameter->appendString(STR16("Inactive")); parameter->appendString(STR16("Active"));
            }
            parameters.addParameter(parameter);
        } else {
            auto parameter = new RangeParameter(meter_names[i], id,
                i < 4 ? STR16("dBFS") : i < 7 ? STR16("dB") : nullptr,
                meter_minimum[i], meter_maximum[i], meter_minimum[i], 0, ParameterInfo::kIsReadOnly);
            parameter->setPrecision(id == TelemetrySequence ? 0 : 1);
            parameters.addParameter(parameter);
        }
    }
    return kResultOk;
}
tresult PLUGIN_API Controller::setComponentState(IBStream* stream) {
    WcfiControlConfig c;
    if (!read_state(stream, c)) return kResultFalse;
    telemetry = {}; meter_seen_mask = 0; meter_received = {};
    for (int i = 0; i < MeterCount; ++i) EditController::setParamNormalized(InputRms + i, 0.);
    for (int i = 0; i < ParameterCount; ++i) setParamNormalized(Auto + i, normalized(Auto + i, value(c, i)));
    return kResultOk;
}

tresult PLUGIN_API Controller::setParamNormalized(ParamID id, ParamValue n) {
    if (!std::isfinite(n) || n < 0. || n > 1.) return kInvalidArgument;
    auto result = EditController::setParamNormalized(id, n);
    if (result != kResultOk || id < InputRms || id > TelemetrySequence) return result;
    const auto i = id - InputRms;
    const double p = meter_plain(id, n);
    if (id != TelemetrySequence) {
        const uint16_t bit = static_cast<uint16_t>(1u << i);
        if (n <= 0.) { meter_seen_mask &= ~bit; return result; }
        meter_seen_mask |= bit;
    }
    switch (id) {
        case InputRms: telemetry.input_rms_dbfs = p; break;
        case InputPeak: telemetry.input_peak_dbfs = p; break;
        case OutputRms: telemetry.output_rms_dbfs = p; break;
        case OutputPeak: telemetry.output_peak_dbfs = p; break;
        case AutoGain: telemetry.auto_gain_db = p; break;
        case AppliedGain: telemetry.applied_gain_db = p; break;
        case GuardReduction: telemetry.guard_reduction_db = p; break;
        case ActivityState: telemetry.state = static_cast<uint32_t>(std::lround(p)); break;
        case ActivityGate: telemetry.activity = std::lround(p) != 0; break;
        case TelemetrySequence: {
            const auto next = static_cast<uint64_t>(std::llround(p));
            if (next && next != telemetry.sequence) meter_received = std::chrono::steady_clock::now();
            if (!next) meter_received = {};
            telemetry.sequence = next;
            break;
        }
        default: break;
    }
    // Host callbacks never echo performEdit, beginEdit or endEdit.
    return result;
}

bool Controller::readTelemetry(UiTelemetry& out) const {
    out = telemetry;
    const bool received = meter_received != std::chrono::steady_clock::time_point {};
    out.age_ms = received ? std::chrono::duration<double, std::milli>(
        std::chrono::steady_clock::now() - meter_received).count() : 0.;
    out.fresh = received && telemetry.sequence != 0
        && meter_seen_mask == (1u << MeterPayloadCount) - 1 && out.age_ms <= 750.;
    return out.fresh;
}

IPlugView* PLUGIN_API Controller::createView(FIDString name) {
    return FIDStringsEqual(name, ViewType::kEditor) ? create_control_editor(this) : nullptr;
}
}
