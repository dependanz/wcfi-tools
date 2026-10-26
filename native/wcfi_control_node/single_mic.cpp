#include "single_mic.h"
#include <algorithm>
#include <cmath>
#include <limits>
#include <new>

namespace wcfi_control {
namespace {
double db(double value) { return value > 0 ? 20. * std::log10(value) : -160.; }
double linear(double value) { return std::pow(10., value / 20.); }
bool between(double value, double low, double high) {
    return std::isfinite(value) && value >= low && value <= high;
}
double total_bound(double value, const WcfiControlConfig& config) {
    return std::clamp(value, -config.max_cut_db, config.max_boost_db);
}
}

WcfiControlConfig mic_defaults() {
    WcfiControlConfig config;
    config.target_dbfs = -26.; config.max_boost_db = config.max_cut_db = 6.;
    config.activity_dbfs = -55.;
    return config;
}
bool valid_mic_config(const WcfiControlConfig& config) {
    return config.version == 1 && config.auto_enabled <= 1
        && between(config.trim_db, -6., 6.) && between(config.target_dbfs, -36., -12.)
        && between(config.max_boost_db, 0., 6.) && between(config.max_cut_db, 0., 6.)
        && between(config.activity_dbfs, -90., -24.);
}
MicEngine::MicEngine(double sample_rate, uint32_t count) { setup(sample_rate, count); }
bool MicEngine::setup(double sample_rate, uint32_t count) {
    if (!between(sample_rate, 8000., 384000.) || (count != 1 && count != 2)) return false;
    rate = sample_rate; channels = count;
    quantum = static_cast<uint32_t>(std::max(1., std::round(rate * .1)));
    smooth_coefficient = 1. - std::exp(-1. / (rate * .020));
    guard_recovery = linear(6. / rate);
    reset(); return true;
}
void MicEngine::clear_evidence() {
    levels.fill(0.); active.fill(false);
    history_count = history_index = support = 0;
    metrics.reserved = 0;
}
void MicEngine::reset() {
    metrics = {}; gain = guard = 1.; gain_db = locked_offset = 0.;
    frame_squares = frame_peak = 0.; frame_count = 0; bin_valid = true;
    locked = false; waiting_target = 0.; clear_evidence();
    update_target(); update_state();
}
bool MicEngine::configure(const WcfiControlConfig& next) {
    if (!valid_mic_config(next)) return false;
    const auto previous = settings;
    const bool changed_mode = previous.auto_enabled != next.auto_enabled;
    settings = next;
    if (changed_mode) {
        locked = false; locked_offset = 0.; clear_evidence();
        frame_count = 0; frame_squares = frame_peak = 0.; bin_valid = true;
        metrics.activity = 0;
        // Enabling calibration preserves the current base gain. It never
        // jumps to a stale manual or previously learned setting while waiting.
        waiting_target = total_bound(gain_db, settings);
    } else if (settings.auto_enabled && !locked) {
        waiting_target = total_bound(waiting_target + settings.trim_db - previous.trim_db, settings);
        if (settings.target_dbfs != previous.target_dbfs || settings.activity_dbfs != previous.activity_dbfs) {
            clear_evidence(); frame_count = 0; frame_squares = frame_peak = 0.; bin_valid = true;
            metrics.activity = 0;
        }
    }
    update_target(); update_state(); return true;
}
void MicEngine::update_target() {
    // A tighter user limit bounds the command immediately. Existing gain
    // approaches it smoothly; the hard global +/-6 dB base-gain bound always
    // holds. Protective attenuation can exceed that downward bound.
    metrics.auto_gain_db = locked && settings.auto_enabled
        ? std::clamp(locked_offset, -settings.max_cut_db, settings.max_boost_db) : 0.;
    const double requested = !settings.auto_enabled ? settings.trim_db
        : locked ? settings.trim_db + metrics.auto_gain_db : waiting_target;
    metrics.target_gain_db = total_bound(requested, settings);
}
void MicEngine::update_state() {
    metrics.state = !settings.auto_enabled ? 0 : !locked ? (support ? 2 : 1)
        : std::abs(gain_db - metrics.target_gain_db) > 1e-9 ? 3 : 4;
    metrics.reserved = support;
}
void MicEngine::tick() {
    const double power = frame_squares / (frame_count * channels);
    metrics.input_rms_dbfs = db(std::sqrt(power));
    metrics.input_peak_dbfs = db(frame_peak);
    const bool above = bin_valid && power > 0 && metrics.input_rms_dbfs >= settings.activity_dbfs;
    metrics.activity = above ? 1 : 0;
    if (settings.auto_enabled && !locked) {
        if (!bin_valid) clear_evidence();
        else {
            if (history_count == levels.size() && active[history_index]) --support;
            levels[history_index] = metrics.input_rms_dbfs;
            active[history_index] = above;
            support += above ? 1 : 0;
            history_index = (history_index + 1) % static_cast<uint32_t>(levels.size());
            history_count = std::min(static_cast<uint32_t>(levels.size()), history_count + 1);
            if (support >= 50) {
                std::array<double, 300> evidence {};
                uint32_t count = 0;
                for (uint32_t i = 0; i < history_count; ++i)
                    if (active[i]) evidence[count++] = levels[i];
                std::sort(evidence.begin(), evidence.begin() + count);
                const auto middle = count / 2;
                const double median = count % 2 ? evidence[middle]
                    : evidence[middle - 1] / 2. + evidence[middle] / 2.;
                locked_offset = std::clamp(settings.target_dbfs - median - settings.trim_db,
                                            -settings.max_cut_db, settings.max_boost_db);
                locked = true;
            }
        }
    }
    frame_count = 0; frame_squares = frame_peak = 0.; bin_valid = true;
    update_target(); update_state();
}
template<class T> void MicEngine::frame(const T* input, T* output) {
    double values[2] {}, input_peak = 0.;
    for (uint32_t channel = 0; channel < channels; ++channel) {
        const double value = input ? static_cast<double>(input[channel]) : 0.;
        if (std::isfinite(value)) values[channel] = value;
        else { ++metrics.nonfinite_samples; bin_valid = false; }
        input_peak = std::max(input_peak, std::abs(values[channel]));
        const double measured = std::clamp(values[channel], -1e100, 1e100);
        frame_squares += measured * measured;
    }
    frame_peak = std::max(frame_peak, input_peak);
    // The target here comes only from already-completed observations.
    if (settings.auto_enabled) {
        const double error = metrics.target_gain_db - gain_db;
        gain_db += std::clamp(error, -1. / rate, 1. / rate);
        gain_db = std::clamp(gain_db, -6., 6.);
        gain = linear(gain_db);
    } else {
        const double requested = linear(metrics.target_gain_db);
        gain += smooth_coefficient * (requested - gain);
        if (std::abs(gain - requested) < 1e-12 * std::max(1., requested)) gain = requested;
        gain = std::clamp(gain, linear(-6.), linear(6.));
        gain_db = db(gain);
    }
    // Protection remains active during calibration, holding and manual control.
    guard = std::min(1., guard * guard_recovery);
    const double allowed = input_peak > 0 ? linear(HeadroomDb) / input_peak / gain : 1.;
    if (allowed < guard) { guard = std::max(0., allowed); ++metrics.headroom_restraints; }
    const double applied = gain * guard;
    for (uint32_t channel = 0; channel < channels; ++channel) {
        const double result = values[channel] * applied;
        output[channel] = static_cast<T>(std::clamp(result,
            -static_cast<double>(std::numeric_limits<T>::max()), static_cast<double>(std::numeric_limits<T>::max())));
    }
    metrics.applied_gain_db = db(applied); metrics.guard_attenuation_db = db(guard);
    ++metrics.samples_processed;
    if (++frame_count >= quantum) tick();
    else update_state();
}
template void MicEngine::frame<float>(const float*, float*);
template void MicEngine::frame<double>(const double*, double*);
}

namespace {
struct MicHandle {
    wcfi_control::MicEngine engine;
    uint32_t channels;
    MicHandle(double rate, uint32_t count) : engine(rate, count), channels(count) {}
};
}
extern "C" {
void* WcfiMicCreate(double rate, uint32_t channels) {
    if (!std::isfinite(rate) || rate < 8000 || rate > 384000 || (channels != 1 && channels != 2)) return nullptr;
    return new (std::nothrow) MicHandle(rate, channels);
}
int WcfiMicConfigure(void* handle, const WcfiControlConfig* config) {
    return handle && config && static_cast<MicHandle*>(handle)->engine.configure(*config) ? 0 : -1;
}
int WcfiMicProcess(void* handle, const float* input, float* output, uint32_t frames) {
    if (!handle || (frames && (!input || !output)) || frames > 1048576) return -1;
    auto state = static_cast<MicHandle*>(handle);
    for (uint32_t i = 0; i < frames; ++i)
        state->engine.frame(input + i * state->channels, output + i * state->channels);
    return 0;
}
int WcfiMicGetStatus(void* handle, WcfiControlStatus* output) {
    if (!handle || !output) return -1;
    *output = static_cast<MicHandle*>(handle)->engine.status(); return 0;
}
int WcfiMicReset(void* handle) {
    if (!handle) return -1;
    static_cast<MicHandle*>(handle)->engine.reset(); return 0;
}
void WcfiMicDestroy(void* handle) { delete static_cast<MicHandle*>(handle); }
}
