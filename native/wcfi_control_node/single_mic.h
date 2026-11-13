#pragma once
#include "dsp.h"
#include <array>

namespace wcfi_control {
// The legacy Engine and its ABI remain unchanged. This engine is the bounded
// single-microphone calibration/hold policy, with separate WcfiMic* exports.
WcfiControlConfig mic_defaults();
bool valid_mic_config(const WcfiControlConfig&);

class MicEngine {
public:
    MicEngine(double sample_rate = 48000, uint32_t channels = 2);
    bool setup(double sample_rate, uint32_t channels);
    bool configure(const WcfiControlConfig&);
    void reset();
    const WcfiControlConfig& config() const { return settings; }
    WcfiControlStatus status() const { return metrics; }
    // No allocation, locking, host messages or I/O. Measurements from this
    // sample can change only following samples, including the quorum boundary.
    template<class T> void frame(const T* input, T* output);
private:
    WcfiControlConfig settings {mic_defaults()};
    WcfiControlStatus metrics {};
    double rate {48000}, smooth_coefficient {}, guard_recovery {}, gain {1}, guard {1};
    double gain_db {}, frame_squares {}, frame_peak {}, locked_offset {}, waiting_target {};
    std::array<double, 300> levels {};
    std::array<bool, 300> active {};
    uint32_t channels {2}, quantum {4800}, frame_count {}, history_count {}, history_index {}, support {};
    bool locked {false}, bin_valid {true};
    void clear_evidence();
    void tick();
    void update_target();
    void update_state();
};
}

extern "C" {
WCFI_CONTROL_API void* WcfiMicCreate(double sample_rate, uint32_t channels);
WCFI_CONTROL_API int WcfiMicConfigure(void*, const WcfiControlConfig*);
WCFI_CONTROL_API int WcfiMicProcess(void*, const float* interleaved_input, float* interleaved_output, uint32_t frames);
WCFI_CONTROL_API int WcfiMicGetStatus(void*, WcfiControlStatus*);
WCFI_CONTROL_API int WcfiMicReset(void*);
WCFI_CONTROL_API void WcfiMicDestroy(void*);
}
