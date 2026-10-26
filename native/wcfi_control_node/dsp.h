#pragma once
#include <array>
#include <cstdint>

// ABI v1: caller owns the handle and serializes configure/process/status/reset.
// Activity means energy qualification on a dedicated speech source, not a VAD.
struct WcfiControlConfig {
    uint32_t version {1}, auto_enabled {0};
    double trim_db {0}, target_dbfs {-24}, max_boost_db {12}, max_cut_db {12}, activity_dbfs {-60};
};
struct WcfiControlStatus {
    uint32_t version {1}, activity {0}, state {0}, reserved {0};
    double input_rms_dbfs {-160}, input_peak_dbfs {-160}, auto_gain_db {0};
    double applied_gain_db {0}, target_gain_db {0}, guard_attenuation_db {0};
    uint64_t samples_processed {0}, nonfinite_samples {0}, headroom_restraints {0};
};

namespace wcfi_control {
constexpr double HeadroomDb = -1.0;
constexpr double GainMinimumDb = -24.0, GainMaximumDb = 24.0;
bool valid_config(const WcfiControlConfig&);
class Engine {
public:
    Engine(double sample_rate = 48000, uint32_t channels = 2);
    bool setup(double sample_rate, uint32_t channels);
    bool configure(const WcfiControlConfig&);
    void reset();
    const WcfiControlConfig& config() const { return settings; }
    WcfiControlStatus status() const;
    // One sample frame; no allocation, locks, I/O or host messages.
    template<class T> void frame(const T* input, T* output);
private:
    WcfiControlConfig settings {};
    WcfiControlStatus metrics {};
    double rate {48000}, smooth_coefficient {}, guard_recovery {}, gain {1}, guard {1};
    double frame_squares {}, frame_peak {}, active_seconds {};
    std::array<double, 10> powers {}, peaks {};
    uint32_t channels {2}, quantum {480}, frame_count {}, history_count {}, history_index {};
    bool qualified {false};
    void tick();
};
}

#ifdef WCFI_CONTROL_EXPORTS
#define WCFI_CONTROL_API __declspec(dllexport)
#else
#define WCFI_CONTROL_API
#endif
extern "C" {
WCFI_CONTROL_API void* WcfiControlCreate(double sample_rate, uint32_t channels);
WCFI_CONTROL_API int WcfiControlConfigure(void*, const WcfiControlConfig*);
WCFI_CONTROL_API int WcfiControlProcess(void*, const float* interleaved_input, float* interleaved_output, uint32_t frames);
WCFI_CONTROL_API int WcfiControlGetStatus(void*, WcfiControlStatus*);
WCFI_CONTROL_API int WcfiControlReset(void*);
WCFI_CONTROL_API void WcfiControlDestroy(void*);
}
