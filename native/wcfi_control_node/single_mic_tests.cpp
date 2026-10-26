#include "single_mic.h"
#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <limits>
#include <vector>

using namespace wcfi_control;
namespace {
void check(bool condition, const char* message) {
    if (!condition) { std::fprintf(stderr, "FAIL: %s\n", message); std::exit(1); }
}
bool near(double a, double b, double tolerance = 1e-8) { return std::abs(a - b) <= tolerance; }
double lin(double value) { return std::pow(10., value / 20.); }
void run(MicEngine& engine, uint32_t count, double amplitude) {
    double input[2] {amplitude, -amplitude}, output[2];
    for (uint32_t i = 0; i < count; ++i) engine.frame(input, output);
}
void bins(MicEngine& engine, uint32_t count, double level, uint32_t quantum = 4800) {
    run(engine, count * quantum, lin(level));
}
void arm(MicEngine& engine) {
    auto config = mic_defaults(); config.auto_enabled = 1;
    check(engine.configure(config), "arm calibration");
}

template<class T> void ordinary_unity(uint32_t channels) {
    MicEngine engine(48000, channels);
    for (uint32_t i = 0; i < 10000; ++i) {
        T input[2] {static_cast<T>(std::sin(i * .01) * .2), static_cast<T>(-std::sin(i * .03) * .1)}, output[2] {};
        if (!i) input[0] = static_cast<T>(-0.);
        engine.frame(input, output);
        check(std::memcmp(input, output, channels * sizeof(T)) == 0, "manual ordinary unity is bit exact");
        check(engine.status().state == 0 && engine.status().auto_gain_db == 0, "default manual state");
    }
}
void configuration_cases() {
    static_assert(sizeof(WcfiControlConfig) == 48 && sizeof(WcfiControlStatus) == 88);
    MicEngine engine;
    auto config = mic_defaults();
    check(config.auto_enabled == 0 && config.trim_db == 0 && config.target_dbfs == -26
          && config.max_boost_db == 6 && config.max_cut_db == 6 && config.activity_dbfs == -55, "RED defaults");
    check(valid_mic_config(config), "defaults valid");
    for (int which = 0; which < 8; ++which) {
        auto bad = config;
        if (which == 0) bad.trim_db = 6.1;
        if (which == 1) bad.trim_db = -6.1;
        if (which == 2) bad.max_boost_db = 6.1;
        if (which == 3) bad.max_cut_db = -1;
        if (which == 4) bad.target_dbfs = std::numeric_limits<double>::quiet_NaN();
        if (which == 5) bad.activity_dbfs = -91;
        if (which == 6) bad.auto_enabled = 2;
        if (which == 7) bad.version = 2;
        check(!engine.configure(bad) && engine.config().trim_db == 0, "invalid config rejected atomically");
    }
    check(!engine.setup(0, 2) && !engine.setup(48000, 3), "invalid setup rejected");
    ordinary_unity<float>(1); ordinary_unity<float>(2);
    ordinary_unity<double>(1); ordinary_unity<double>(2);
    std::puts("PASS: defaults, configuration validation, mono/stereo float32/64 ordinary unity.");
}
void calibration_cases() {
    MicEngine engine; arm(engine);
    run(engine, 48000 * 31, 0);
    check(engine.status().state == 1 && engine.status().reserved == 0 && engine.status().target_gain_db == 0,
          "long initial quiet cannot calibrate");
    bins(engine, 25, -32); bins(engine, 100, -80); bins(engine, 24, -28);
    check(engine.status().state == 2 && engine.status().reserved == 49 && engine.status().auto_gain_db == 0,
          "quorum waits across pauses");
    run(engine, 4799, lin(-28));
    check(engine.status().reserved == 49 && engine.status().target_gain_db == 0, "partial bin cannot lock");
    double input[2] {lin(-28), -lin(-28)}, output[2];
    engine.frame(input, output);
    check(output[0] == input[0] && output[1] == input[1], "quorum frame uses preceding gain");
    check(engine.status().reserved == 50 && near(engine.status().auto_gain_db, 4.)
          && near(engine.status().target_gain_db, 4.) && engine.status().applied_gain_db == 0,
          "first fifty-bin median locks causally");
    engine.frame(input, output);
    check(output[0] > input[0] && near(engine.status().applied_gain_db, 1. / 48000., 1e-10),
          "first following sample begins one dB per second ramp");
    run(engine, 48000 * 5, 0);
    check(engine.status().state == 4 && near(engine.status().applied_gain_db, 4.), "ramp completes during pause");
    bins(engine, 60, -15);
    check(near(engine.status().auto_gain_db, 4.) && near(engine.status().target_gain_db, 4.),
          "later loud audio cannot relearn fixed calibration");
    auto config = engine.config(); config.target_dbfs = -30; config.activity_dbfs = -40;
    engine.configure(config); bins(engine, 10, -20);
    check(near(engine.status().auto_gain_db, 4.), "settings after lock do not silently recalibrate");

    MicEngine expired; arm(expired);
    bins(expired, 49, -30); bins(expired, 300, -80);
    check(expired.status().reserved == 0 && expired.status().state == 1, "old evidence fully expires");
    bins(expired, 49, -30);
    check(expired.status().auto_gain_db == 0 && expired.status().reserved == 49, "expired evidence cannot count again");
    bins(expired, 1, -30);
    check(near(expired.status().auto_gain_db, 4.), "fresh quorum calibrates after expiry");

    for (double level : {-50., -10.}) {
        MicEngine bounded; arm(bounded); bins(bounded, 50, level);
        const double expected = level < -26 ? 6. : -6.;
        check(near(bounded.status().auto_gain_db, expected), "median gain clamps to hard bounds");
    }
    // A comfortably above-threshold input tests inclusive eligibility without
    // making this test depend on libm rounding exactly on a dB boundary.
    MicEngine threshold; arm(threshold); bins(threshold, 50, -55.0001);
    check(threshold.status().reserved == 0, "below-threshold bins do not qualify");
    bins(threshold, 50, -54.9999);
    check(threshold.status().reserved == 50, "above-threshold bins qualify");
    std::puts("PASS: delayed activity, pauses, median/quorum, future causality, frozen gain, expiry and bounds.");
}
void transitions_and_rates() {
    for (double rate : {8000., 44100., 48000., 96000.}) {
        MicEngine engine(rate, 2); arm(engine);
        const auto quantum = static_cast<uint32_t>(std::round(rate * .1));
        bins(engine, 50, -40, quantum);
        check(engine.status().applied_gain_db == 0 && engine.status().state == 3, "rate-independent completed quorum");
        run(engine, static_cast<uint32_t>(rate * 2), .001);
        check(near(engine.status().applied_gain_db, 2., 1e-7), "sample-rate independent automatic ramp");
        run(engine, static_cast<uint32_t>(rate * 6), 0);
        check(near(engine.status().applied_gain_db, 6., 1e-8) && engine.status().state == 4, "automatic hold after bounded ramp");
        auto config = engine.config(); config.max_boost_db = 2.; engine.configure(config);
        check(engine.status().target_gain_db == 2. && engine.status().applied_gain_db > 5.9,
              "tightened bound applies to command immediately, applied gain transitions smoothly");
        run(engine, static_cast<uint32_t>(rate), .001);
        check(near(engine.status().applied_gain_db, 5., 1e-7), "limit tightening follows automatic slew");
        config.auto_enabled = 0; config.trim_db = -2.; engine.configure(config);
        check(engine.status().state == 0 && engine.status().auto_gain_db == 0 && engine.status().target_gain_db == -2.,
              "manual mode immediately owns target and clears calibration");
        const double before = engine.status().applied_gain_db;
        double input[2] {.001, -.001}, output[2]; engine.frame(input, output);
        check(engine.status().applied_gain_db < before && engine.status().applied_gain_db > before - .1,
              "manual takeover is smoothed, without discontinuous gain reset");
        run(engine, static_cast<uint32_t>(rate), .001);
        check(near(engine.status().applied_gain_db, -2., 1e-7), "manual target reached");
        config.auto_enabled = 1; engine.configure(config);
        run(engine, static_cast<uint32_t>(rate), 0);
        check(near(engine.status().applied_gain_db, -2., 1e-7) && engine.status().reserved == 0,
              "reenabling waits at current gain rather than stale correction");
    }
    MicEngine engine; auto config = mic_defaults(); config.trim_db = 3.; engine.configure(config);
    run(engine, 48000, .001); config.auto_enabled = 1; engine.configure(config);
    bins(engine, 25, -30);
    config.target_dbfs = -25.; engine.configure(config);
    check(engine.status().reserved == 0, "pre-lock target change clears evidence");
    bins(engine, 50, -30);
    check(near(engine.status().auto_gain_db, 2.) && near(engine.status().target_gain_db, 5.),
          "calibration accounts for manual trim, bounds total gain");
    config.trim_db = -1.; engine.configure(config);
    check(near(engine.status().auto_gain_db, 2.) && near(engine.status().target_gain_db, 1.),
          "manual trim offsets frozen result without being learned away");
    engine.reset();
    check(engine.status().samples_processed == 0 && engine.status().auto_gain_db == 0 && engine.status().reserved == 0,
          "reset clears measurement and learned state");
    std::puts("PASS: rate/one-dB slew, bounded transitions, immediate manual ownership, trim and reset.");
}
void protection_and_invalid_bins() {
    for (bool automatic : {false, true}) {
        MicEngine engine; auto config = mic_defaults(); config.auto_enabled = automatic; engine.configure(config);
        double input[2] {2., -1.}, output[2]; engine.frame(input, output);
        check(near(std::abs(output[0]), lin(-1), 1e-12) && near(output[1] / output[0], -.5),
              "always-on sample guard links stereo channels");
        const double restrained = engine.status().guard_attenuation_db;
        run(engine, 4800, 0);
        check(engine.status().guard_attenuation_db <= restrained + .6000001,
              "guard recovery remains bounded during silence");
        input[0] = std::numeric_limits<double>::max(); input[1] = -input[0]; engine.frame(input, output);
        check(std::isfinite(output[0]) && std::abs(output[0]) <= lin(-1) + 1e-12,
              "extreme finite double remains protected");
        input[0] = std::numeric_limits<double>::quiet_NaN(); input[1] = std::numeric_limits<double>::infinity();
        engine.frame(input, output);
        check(output[0] == 0 && output[1] == 0 && engine.status().nonfinite_samples == 2,
              "nonfinite samples are zeroed and counted");
    }
    MicEngine engine; arm(engine); bins(engine, 49, -30);
    double input[2] {std::numeric_limits<double>::quiet_NaN(), .01}, output[2]; engine.frame(input, output);
    run(engine, 4799, lin(-30));
    check(engine.status().reserved == 0 && engine.status().auto_gain_db == 0,
          "invalid whole bin clears unfinished support rather than appending quiet");
    bins(engine, 50, -30);
    const double correction = engine.status().auto_gain_db;
    engine.frame(input, output); run(engine, 4799, lin(-30));
    check(near(engine.status().auto_gain_db, correction), "invalid audio cannot change frozen calibration");
    std::puts("PASS: manual/auto guard, stereo linkage, release, extreme input and invalid-bin evidence reset.");
}
void bridge_cases() {
    constexpr uint32_t count = 48000 * 12;
    std::vector<float> source(count * 2), reference(count * 2), partitioned(count * 2);
    for (uint32_t i = 0; i < count; ++i) {
        const float amplitude = i < 48000 ? 0.f : static_cast<float>(lin(-31));
        source[2 * i] = amplitude; source[2 * i + 1] = -amplitude;
    }
    auto config = mic_defaults(); config.auto_enabled = 1;
    MicEngine engine; engine.configure(config);
    for (uint32_t i = 0; i < count; ++i) engine.frame(source.data() + i * 2, reference.data() + i * 2);
    for (uint32_t block : {1u, 64u, 512u, 1379u, 1000000u}) {
        auto handle = WcfiMicCreate(48000, 2);
        check(handle && WcfiMicConfigure(handle, &config) == 0, "create/configure new ABI");
        partitioned = source;
        for (uint32_t offset = 0; offset < count; offset += block)
            check(WcfiMicProcess(handle, partitioned.data() + offset * 2, partitioned.data() + offset * 2,
                std::min(block, count - offset)) == 0, "in-place ABI process");
        check(std::memcmp(reference.data(), partitioned.data(), count * 2 * sizeof(float)) == 0,
              "arbitrary block partitions match frame-reference bit exactly");
        WcfiControlStatus status; check(WcfiMicGetStatus(handle, &status) == 0, "ABI status");
        check(status.samples_processed == count && near(status.auto_gain_db, engine.status().auto_gain_db)
              && status.state == engine.status().state, "partition-independent telemetry");
        check(WcfiMicReset(handle) == 0 && WcfiMicGetStatus(handle, &status) == 0 && !status.samples_processed,
              "ABI reset");
        WcfiMicDestroy(handle);
    }
    check(!WcfiMicCreate(0, 2) && !WcfiMicCreate(48000, 3), "ABI invalid create");
    check(WcfiMicProcess(nullptr, nullptr, nullptr, 0) == -1 && WcfiMicConfigure(nullptr, &config) == -1
          && WcfiMicGetStatus(nullptr, nullptr) == -1 && WcfiMicReset(nullptr) == -1, "ABI invalid handles");
    auto handle = WcfiMicCreate(48000, 1);
    check(WcfiMicProcess(handle, nullptr, nullptr, 0) == 0
          && WcfiMicProcess(handle, nullptr, nullptr, 1) == -1, "ABI zero block and invalid audio pointers");
    WcfiMicDestroy(handle);
    std::puts("PASS: independent WcfiMic ABI, frame parity, five partitions, in-place and invalid arguments.");
}
}
int main() {
    configuration_cases(); calibration_cases(); transitions_and_rates(); protection_and_invalid_bins(); bridge_cases();
    std::puts("PASS: RED single-mic native suite.");
    return 0;
}
