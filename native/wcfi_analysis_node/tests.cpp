#include "node.h"
#include "public.sdk/source/vst/hosting/hostclasses.h"
#include "public.sdk/source/common/memorystream.h"
#include "pluginterfaces/vst/ivstprocesscontext.h"
#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <vector>
#include <thread>
#include <atomic>

using namespace wcfi;
void check(bool yes, const char* what) {
    if (!yes) { fprintf(stderr, "FAIL: %s\n", what); std::exit(1); }
}
template<class T> void passthrough(Processor& p, int channels, int frames, bool inplace, bool silence, bool offline) {
    SpeakerArrangement layout = channels == 1 ? SpeakerArr::kMono : SpeakerArr::kStereo;
    check(p.setBusArrangements(&layout, 1, &layout, 1) == kResultOk, "mono/stereo arrangement");
    ProcessSetup setup {offline ? kOffline : kRealtime, sizeof(T) == 4 ? kSample32 : kSample64, 4096, 48000};
    check(p.setupProcessing(setup) == kResultOk, "setup");
    check(p.setActive(true) == kResultOk, "activate");
    p.setProcessing(true);
    std::vector<T> left(frames), right(frames), a(frames, 99), b(frames, 99);
    for (int n = 0; n < frames; ++n) {
        left[n] = silence ? T(0) : T(std::sin(n * .123) * 2.7); // Headroom is preserved.
        right[n] = -left[n] * T(.5);
    }
    const auto expectedLeft = left, expectedRight = right;
    T* input[] {left.data(), right.data()}, *output[] {inplace ? left.data() : a.data(), inplace ? right.data() : b.data()};
    AudioBusBuffers in {}, out {};
    in.numChannels = out.numChannels = channels;
    in.silenceFlags = silence ? (1 << channels) - 1 : 0;
    if constexpr(sizeof(T) == 4) { in.channelBuffers32 = input; out.channelBuffers32 = output; }
    else { in.channelBuffers64 = input; out.channelBuffers64 = output; }
    ProcessData data {};
    data.processMode = setup.processMode; data.symbolicSampleSize = setup.symbolicSampleSize;
    data.numInputs = data.numOutputs = 1; data.inputs = &in; data.outputs = &out; data.numSamples = frames;
    check(p.process(data) == kResultOk, "process");
    for (int ch = 0; ch < channels; ++ch)
        check(memcmp(ch ? expectedRight.data() : expectedLeft.data(), output[ch], frames * sizeof(T)) == 0, "bit-identical passthrough");
    if (frames) check(out.silenceFlags == in.silenceFlags, "silence flags");
    check(p.getLatencySamples() == 0 && p.getTailSamples() == kNoTail, "zero latency / tail");
    p.setProcessing(false); p.setActive(false);
}
int main(int argc, char** argv) {
    HostApplication host;
    auto p = owned(new Processor), second = owned(new Processor);
    check(p->initialize(&host) == kResultOk && second->initialize(&host) == kResultOk, "initialize two instances");
    NodeInfo infos[NodeSlots] {};
    int total = WcfiNodesEnumerate(infos, NodeSlots);
    check(total >= 2, "discovery");
    for (int channels : {1, 2}) for (int frames : {0, 1, 17, 512, 4096})
        for (bool inplace : {false, true}) for (bool silence : {false, true}) for (bool offline : {false, true}) {
            passthrough<float>(*p, channels, frames, inplace, silence, offline);
            passthrough<double>(*p, channels, frames, inplace, silence, offline);
        }
    auto controller = owned(new Controller);
    check(controller->initialize(&host) == kResultOk, "controller initialize");
    p->connect(controller); controller->connect(p);
    check(controller->apply(L"RED", 1), "native editor settings message");
    MemoryStream state;
    check(p->getState(&state) == kResultOk, "save state");
    state.seek(0, IBStream::kIBSeekSet, nullptr);
    check(second->setState(&state) == kResultOk, "restore state");
    state.seek(0, IBStream::kIBSeekSet, nullptr);
    check(controller->setComponentState(&state) == kResultOk && controller->settings.label[3] == 0 && controller->settings.position == 1,
        "restored short name and position");
    void* reader = nullptr;
    float audio[NodeCapacity * 2]; NodeStatus status {};
    for (int n = 0; n < total; ++n) {
        auto attempt = WcfiNodeOpen(infos[n].id);
        if (attempt && WcfiNodeRead(attempt, 0, NodeCapacity, audio, &status) >= 0 && strcmp(status.label, "RED") == 0) reader = attempt;
        else WcfiNodeClose(attempt);
        if (reader) break;
    }
    check(reader, "read-only bridge sees configured instance");
    // Direct transport test exercises wrap, independent cursors and a slow app.
    Sender sender;
    check(sender.open(), "transport open"); sender.setup(48000, 512); sender.configure("Transport test", 0); sender.processing(true);
    auto r1 = WcfiNodeOpen(sender.id()), r2 = WcfiNodeOpen(sender.id());
    check(r1 && r2, "multiple passive readers");
    std::vector<float> source(512); float* ptr[] {source.data()};
    std::vector<double> timings;
    for (int block = 0; block < 20000; ++block) {
        for (int n = 0; n < 512; ++n) source[n] = static_cast<float>(block * 512 + n);
        auto began = clock_ns(); sender.publish(ptr, 1, 512, block * 512, 0, 1);
        timings.push_back((clock_ns() - began) / 1000.0);
    }
    int count = WcfiNodeRead(r1, 0, NodeCapacity, audio, &status);
    check(count == NodeCapacity && status.first_frame == 20000ULL * 512 - NodeCapacity, "slow reader reports overwritten frames");
    for (int n = 0; n < count; ++n)
        check(audio[n * 2] == float(status.first_frame + n) && audio[n * 2 + 1] == audio[n * 2], "ring order and mono duplication");
    check(WcfiNodeRead(r2, 20000ULL * 512 - 17, 17, audio, &status) == 17, "independent second cursor");
    sender.processing(false); sender.processing(true);
    sender.publish(ptr, 1, 512, 0, 0, 1);
    check(WcfiNodeRead(r1, 0, 512, audio, &status) == 512 && status.epoch == 2, "restart epoch");
    std::sort(timings.begin(), timings.end());
    printf("PASS: 160 passthrough cases; settings persistence; discovery; overwrite; independent readers; epochs.\n");
    printf("512-frame mono capture: p50 %.2f us, p95 %.2f us, max %.2f us (20,000 callbacks).\n",
        timings[timings.size()/2], timings[timings.size()*95/100], timings.back());
    WcfiNodeClose(r1); WcfiNodeClose(r2); WcfiNodeClose(reader); sender.close();
    controller->disconnect(p); p->disconnect(controller);
    controller->terminate(); second->terminate(); p->terminate();
    if (argc > 1 && strcmp(argv[1], "--stream") == 0) {
        printf("Streaming test nodes for 60 seconds (not Ableton).\n"); fflush(stdout);
        auto pre = owned(new Processor), post = owned(new Processor);
        auto c1 = owned(new Controller), c2 = owned(new Controller);
        pre->initialize(&host); post->initialize(&host); c1->initialize(&host); c2->initialize(&host);
        pre->connect(c1); c1->connect(pre); post->connect(c2); c2->connect(post);
        check(c1->apply(L"Native test", 0) && c2->apply(L"Native test", 1), "stream labels");
        SpeakerArrangement monoLayout = SpeakerArr::kMono;
        pre->setBusArrangements(&monoLayout, 1, &monoLayout, 1);
        post->setBusArrangements(&monoLayout, 1, &monoLayout, 1);
        ProcessSetup setup {kRealtime, kSample32, 480, 48000};
        pre->setupProcessing(setup); post->setupProcessing(setup);
        pre->setActive(true); post->setActive(true); pre->setProcessing(true); post->setProcessing(true);
        std::vector<float> signal(480); float* mono[] {signal.data()};
        AudioBusBuffers bus {}; bus.numChannels = 1; bus.channelBuffers32 = mono;
        ProcessContext context {}; context.state = ProcessContext::kContTimeValid;
        ProcessData data {}; data.processMode = kRealtime; data.symbolicSampleSize = kSample32;
        data.numSamples = 480; data.numInputs = data.numOutputs = 1; data.inputs = data.outputs = &bus;
        data.processContext = &context;
        uint32_t random = 1234567;
        auto start = std::chrono::steady_clock::now();
        for (int block = 0; block < 6000; ++block) {
            for (int n = 0; n < 480; ++n) {
                random ^= random << 13; random ^= random >> 17; random ^= random << 5;
                signal[n] = (float(random) / 4294967295.f - .5f) * .2f;
            }
            context.continousTimeSamples = block * 480;
            pre->process(data); post->process(data);
            std::this_thread::sleep_until(start + std::chrono::milliseconds((block + 1) * 10));
        }
        pre->setProcessing(false); post->setProcessing(false); pre->setActive(false); post->setActive(false);
        c1->disconnect(pre); pre->disconnect(c1); c2->disconnect(post); post->disconnect(c2);
        c1->terminate(); c2->terminate(); pre->terminate(); post->terminate();
    }
    return 0;
}
