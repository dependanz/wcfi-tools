#pragma once
#include "dsp.h"
#include "single_mic.h"
#include "public.sdk/source/vst/vstaudioeffect.h"
#include "public.sdk/source/vst/vsteditcontroller.h"
#include <array>
#include <atomic>
#include <chrono>

namespace wcfi_control {
using namespace Steinberg;
using namespace Steinberg::Vst;
inline const FUID ProcessorId(0x11FF3697, 0x2243456C, 0x9B4C6355, 0xDE67FC26);
inline const FUID ControllerId(0x6A336E01, 0xBE634FB4, 0xAC76545E, 0x69B0AF65);
enum ParameterIds : ParamID { Auto = 100, Trim = 101, Target = 102, Boost = 103, Cut = 104, Activity = 105 };
constexpr int ParameterCount = 6;
// These read-only host parameters are deliberately excluded from saved settings.
// Sentinel defaults mean "waiting", below the real meter floors; the first
// publication changes every field even when the audio is digital silence.
enum MeterIds : ParamID {
    InputRms = 200, InputPeak, OutputRms, OutputPeak, AutoGain, AppliedGain,
    GuardReduction, ActivityState, ActivityGate, TelemetrySequence
};
constexpr int MeterCount = 10;
constexpr int MeterPayloadCount = MeterCount - 1;
double meter_plain(ParamID id, double normalized_value);
double meter_normalized(ParamID id, double plain_value);
struct UiTelemetry {
    double input_rms_dbfs {-160}, input_peak_dbfs {-160};
    double output_rms_dbfs {-160}, output_peak_dbfs {-160};
    double auto_gain_db {0}, applied_gain_db {0}, guard_reduction_db {0};
    uint32_t state {0};
    bool activity {false}, fresh {false};
    uint64_t sequence {0};
    double age_ms {0};
};
double plain(ParamID id, double normalized);
double normalized(ParamID id, double plain_value);
bool set_parameter(WcfiControlConfig&, ParamID, double normalized_value);
bool read_state(IBStream*, WcfiControlConfig&);
bool write_state(IBStream*, const WcfiControlConfig&);
// Single writer per mailbox; bounded readers never wait on the audio thread.
struct ConfigMailbox {
    std::atomic<uint64_t> sequence {0};
    std::array<std::atomic<double>, ParameterCount> values {};
    void publish(const WcfiControlConfig&);
    bool read(WcfiControlConfig&, uint64_t&) const;
};

class Processor final : public AudioEffect {
public:
    Processor();
    static FUnknown* createInstance(void*) { return static_cast<IAudioProcessor*>(new Processor); }
    tresult PLUGIN_API initialize(FUnknown*) override;
    tresult PLUGIN_API setBusArrangements(SpeakerArrangement*, int32, SpeakerArrangement*, int32) override;
    tresult PLUGIN_API canProcessSampleSize(int32) override;
    tresult PLUGIN_API setupProcessing(ProcessSetup&) override;
    tresult PLUGIN_API setProcessing(TBool) override;
    tresult PLUGIN_API process(ProcessData&) override;
    uint32 PLUGIN_API getLatencySamples() override { return 0; }
    uint32 PLUGIN_API getTailSamples() override { return kNoTail; }
    tresult PLUGIN_API getState(IBStream*) override;
    tresult PLUGIN_API setState(IBStream*) override;
    WcfiControlStatus status() const { return engine.status(); }
private:
    MicEngine engine;
    WcfiControlConfig config {mic_defaults()};
    double sample_rate {48000};
    uint32_t channels {2};
    ConfigMailbox pending_state, saved_state;
    std::atomic<uint64_t> consumed_state {0};
    std::array<double, 10> output_powers {}, output_peaks {};
    double output_squares {}, output_frame_peak {}, output_rms_dbfs {-160}, output_peak_dbfs {-160};
    uint32_t output_frames {}, output_history_count {}, output_history_index {}, output_quantum {480};
    uint64_t meter_elapsed_frames {};
    uint32_t telemetry_sequence {};
    void synchronize_state();
    void reset_meters();
    template<class T> void meter_frame(const T* output);
    void publish_meters(ProcessData&);
    template<class T> void transfer(ProcessData&, T* const*, T* const*);
};
class Controller final : public EditController {
public:
    static FUnknown* createInstance(void*) { return static_cast<IEditController*>(new Controller); }
    tresult PLUGIN_API initialize(FUnknown*) override;
    tresult PLUGIN_API setComponentState(IBStream*) override;
    tresult PLUGIN_API setParamNormalized(ParamID, ParamValue) override;
    IPlugView* PLUGIN_API createView(FIDString) override;
    // UI-thread accessor. Host meter callbacks and editor timer use the same
    // VST3 UI-thread contract; there is no audio-thread controller access.
    bool readTelemetry(UiTelemetry&) const;
private:
    UiTelemetry telemetry {};
    uint16_t meter_seen_mask {};
    std::chrono::steady_clock::time_point meter_received {};
};
}
