#include "dsp.h"
#include <algorithm>
#include <cmath>
#include <new>
#include <limits>

namespace wcfi_control {
namespace {
double db(double x) { return x > 0 ? 20 * std::log10(x) : -160.; }
double linear(double x) { return std::pow(10., x / 20.); }
bool between(double x, double a, double b) { return std::isfinite(x) && x >= a && x <= b; }
}
bool valid_config(const WcfiControlConfig& c) {
    return c.version == 1 && c.auto_enabled <= 1 && between(c.trim_db, -24, 24)
        && between(c.target_dbfs, -36, -12) && between(c.max_boost_db, 0, 24)
        && between(c.max_cut_db, 0, 24) && between(c.activity_dbfs, -90, -24);
}
Engine::Engine(double sample_rate, uint32_t count) { setup(sample_rate, count); }
bool Engine::setup(double sample_rate, uint32_t count) {
    if (!between(sample_rate, 8000, 384000) || (count != 1 && count != 2)) return false;
    rate = sample_rate; channels = count;
    quantum = static_cast<uint32_t>(std::max(1., std::round(rate * .01)));
    smooth_coefficient = 1. - std::exp(-1. / (rate * .020));
    guard_recovery = linear(6. / rate);
    reset(); return true;
}
void Engine::reset() {
    metrics = {}; gain = 1.; guard = 1.; frame_squares = frame_peak = active_seconds = 0.;
    powers.fill(0.); peaks.fill(0.); frame_count = history_count = history_index = 0;
    qualified = false;
    metrics.target_gain_db = settings.trim_db;
}
bool Engine::configure(const WcfiControlConfig& next) {
    if (!valid_config(next)) return false;
    const bool was_auto = settings.auto_enabled != 0;
    settings = next;
    metrics.auto_gain_db = std::clamp(metrics.auto_gain_db, -settings.max_cut_db, settings.max_boost_db);
    if (!settings.auto_enabled) {
        metrics.auto_gain_db = 0.; active_seconds = 0.; qualified = false;
        // Fold protection into the current gain before removing it, so disabling
        // Auto cannot jump past a restraint. The manual target then ramps normally.
        if (was_auto) gain *= guard;
        guard = 1.;
    } else if (!was_auto) {
        active_seconds = 0.; qualified = false;
    }
    metrics.target_gain_db = std::clamp(settings.trim_db + metrics.auto_gain_db, -24., 24.);
    return true;
}
void Engine::tick() {
    const double duration = static_cast<double>(frame_count) / rate;
    const double power = frame_squares / (frame_count * channels);
    powers[history_index] = power; peaks[history_index] = frame_peak;
    history_index = (history_index + 1) % 10; history_count = std::min(10u, history_count + 1);
    double sum = 0., peak = 0.;
    for (uint32_t i = 0; i < history_count; ++i) { sum += powers[i]; peak = std::max(peak, peaks[i]); }
    metrics.input_rms_dbfs = db(std::sqrt(sum / history_count));
    metrics.input_peak_dbfs = db(peak);
    const bool above = db(std::sqrt(power)) >= settings.activity_dbfs;
    active_seconds = above ? active_seconds + duration : 0.;
    qualified = above && active_seconds >= .150 - 1e-9;
    metrics.activity = qualified ? 1 : 0;
    metrics.state = !settings.auto_enabled ? 0 : qualified ? 3 : above ? 2 : 1;
    if (settings.auto_enabled && qualified) {
        double desired = std::clamp(settings.target_dbfs - metrics.input_rms_dbfs - settings.trim_db,
                                    -settings.max_cut_db, settings.max_boost_db);
        desired = std::clamp(desired, -24. - settings.trim_db, 24. - settings.trim_db);
        const double error = desired - metrics.auto_gain_db;
        if (std::abs(error) > .5) {
            const double speed = error > 0 ? 1. : 6.;
            metrics.auto_gain_db += std::clamp(error, -speed * duration, speed * duration);
        }
    }
    metrics.target_gain_db = std::clamp(settings.trim_db + metrics.auto_gain_db, -24., 24.);
    frame_count = 0; frame_squares = frame_peak = 0.;
}
template<class T> void Engine::frame(const T* input, T* output) {
    double values[2] {}, input_peak = 0.;
    for (uint32_t ch = 0; ch < channels; ++ch) {
        const double x = input ? static_cast<double>(input[ch]) : 0.;
        if (std::isfinite(x)) values[ch] = x;
        else ++metrics.nonfinite_samples;
        input_peak = std::max(input_peak, std::abs(values[ch]));
        // Saturating measurement math avoids overflow on otherwise finite hot float64.
        const double measured = std::clamp(values[ch], -1e100, 1e100);
        frame_squares += measured * measured;
    }
    frame_peak = std::max(frame_peak, input_peak);
    ++frame_count;
    if (frame_count >= quantum) tick();
    const double requested = linear(metrics.target_gain_db);
    gain += smooth_coefficient * (requested - gain);
    // Pristine unity remains bit-exact. After a parameter transition, snap only
    // once the smoothing residual is far below float32 resolution.
    if (std::abs(gain - requested) < 1e-12 * std::max(1., requested)) gain = requested;
    if (settings.auto_enabled) {
        // Linked, current-input sample restraint: immediate downward protection;
        // recovery is elapsed-time based, at most 6 dB/s, including silence.
        // Only the automatic level offset freezes during inactivity.
        guard = std::min(1., guard * guard_recovery);
        const double ceiling = linear(HeadroomDb);
        const double allowed = input_peak > 0 ? ceiling / input_peak / gain : 1.;
        if (allowed < guard) { guard = std::max(0., allowed); ++metrics.headroom_restraints; }
    } else guard = 1.;
    const double applied = gain * guard;
    for (uint32_t ch = 0; ch < channels; ++ch) {
        const double out = values[ch] * applied;
        output[ch] = static_cast<T>(std::clamp(out, -static_cast<double>(std::numeric_limits<T>::max()),
                                              static_cast<double>(std::numeric_limits<T>::max())));
    }
    metrics.applied_gain_db = db(applied);
    metrics.guard_attenuation_db = db(guard);
    ++metrics.samples_processed;
}
template void Engine::frame<float>(const float*, float*);
template void Engine::frame<double>(const double*, double*);
WcfiControlStatus Engine::status() const { return metrics; }
}

namespace {
struct Handle {
    wcfi_control::Engine engine;
    uint32_t channels;
    Handle(double rate, uint32_t count) : engine(rate, count), channels(count) {}
};
}
extern "C" {
void* WcfiControlCreate(double rate, uint32_t channels) {
    if (!std::isfinite(rate) || rate < 8000 || rate > 384000 || (channels != 1 && channels != 2)) return nullptr;
    return new (std::nothrow) Handle(rate, channels);
}
int WcfiControlConfigure(void* h, const WcfiControlConfig* c) {
    return h && c && static_cast<Handle*>(h)->engine.configure(*c) ? 0 : -1;
}
int WcfiControlProcess(void* h, const float* input, float* output, uint32_t frames) {
    if (!h || (frames && (!input || !output)) || frames > 1048576) return -1;
    auto handle = static_cast<Handle*>(h);
    for (uint32_t i = 0; i < frames; ++i)
        handle->engine.frame(input + i * handle->channels, output + i * handle->channels);
    return 0;
}
int WcfiControlGetStatus(void* h, WcfiControlStatus* out) {
    if (!h || !out) return -1;
    *out = static_cast<Handle*>(h)->engine.status(); return 0;
}
int WcfiControlReset(void* h) {
    if (!h) return -1;
    static_cast<Handle*>(h)->engine.reset(); return 0;
}
void WcfiControlDestroy(void* h) { delete static_cast<Handle*>(h); }
}
