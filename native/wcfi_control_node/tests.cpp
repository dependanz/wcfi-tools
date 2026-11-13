#include "node.h"
#include "public.sdk/source/vst/hosting/hostclasses.h"
#include "public.sdk/source/vst/hosting/parameterchanges.h"
#include "public.sdk/source/common/memorystream.h"
#include "base/source/fstreamer.h"
#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <limits>
#include <string>
#include <thread>
#include <vector>

using namespace wcfi_control;
void check(bool condition, const char* message) {
    if (!condition) { std::fprintf(stderr, "FAIL: %s\n", message); std::exit(1); }
}
double lin(double db) { return std::pow(10., db / 20.); }
void rewind(MemoryStream& s) { s.seek(0, IBStream::kIBSeekSet, nullptr); }
void run_constant(Engine& e, int frames, double amplitude) {
    double in[2] {amplitude, -amplitude}, out[2];
    for (int i = 0; i < frames; ++i) e.frame(in, out);
}
template<class T> void unity(Processor& p, int channels, int frames, bool inplace, bool silence, bool offline) {
    SpeakerArrangement layout = channels == 1 ? SpeakerArr::kMono : SpeakerArr::kStereo;
    check(p.setBusArrangements(&layout, 1, &layout, 1) == kResultOk, "mono/stereo layout");
    ProcessSetup setup {offline ? kOffline : kRealtime, sizeof(T) == 4 ? kSample32 : kSample64, 4096, 48000};
    check(p.setupProcessing(setup) == kResultOk, "valid sample setup");
    check(p.setActive(true) == kResultOk, "activate"); p.setProcessing(true);
    std::vector<T> a(frames), b(frames), x(frames, 99), y(frames, 99);
    for (int i = 0; i < frames; ++i) { a[i] = silence ? T(0) : T(std::sin(i * .123) * .4); b[i] = silence ? T(0) : -a[i] * T(.5); }
    if (frames && !silence) { a[0] = T(.4); b[0] = T(-.2); }
    const auto ea = a, eb = b;
    T* input[] {a.data(), b.data()}, *output[] {inplace ? a.data() : x.data(), inplace ? b.data() : y.data()};
    AudioBusBuffers in {}, out {}; in.numChannels = out.numChannels = channels;
    in.silenceFlags = silence ? (1 << channels) - 1 : 0;
    if constexpr(sizeof(T) == 4) { in.channelBuffers32 = input; out.channelBuffers32 = output; }
    else { in.channelBuffers64 = input; out.channelBuffers64 = output; }
    ProcessData data {}; data.processMode = setup.processMode; data.symbolicSampleSize = setup.symbolicSampleSize;
    data.numSamples = frames; data.numInputs = data.numOutputs = 1; data.inputs = &in; data.outputs = &out;
    check(p.process(data) == kResultOk, "unity process");
    for (int ch = 0; ch < channels; ++ch)
        check(!frames || !std::memcmp(ch ? eb.data() : ea.data(), output[ch], frames * sizeof(T)), "exact finite below-ceiling input unity");
    if (frames) check(out.silenceFlags == in.silenceFlags, "silence flags");
    p.setProcessing(false); p.setActive(false);
}
void dsp_cases() {
    static_assert(sizeof(WcfiControlConfig) == 48 && sizeof(WcfiControlStatus) == 88);
    static_assert(std::atomic<double>::is_always_lock_free && std::atomic<uint64_t>::is_always_lock_free);
    for (double rate : {8000., 44100., 48000., 96000., 384000.}) {
        Engine e(rate, 2); WcfiControlConfig c; c.auto_enabled = 1; c.max_boost_db = 6; c.max_cut_db = 6;
        check(e.configure(c), "auto config");
        run_constant(e, int(rate), 0);
        check(e.status().auto_gain_db == 0 && e.status().applied_gain_db == 0, "initial silence no gain creep");
        run_constant(e, int(rate * 2), lin(-40));
        check(e.status().auto_gain_db > 1.7 && e.status().auto_gain_db < 1.9, "sample-rate independent qualification and 1dB/s rise");
        run_constant(e, int(rate * 8), lin(-40));
        check(e.status().auto_gain_db > 5.4 && e.status().auto_gain_db <= 6, "bounded boost convergence");
        run_constant(e, int(rate * .2), 0);
        const double held = e.status().auto_gain_db;
        run_constant(e, int(rate), 0);
        check(e.status().auto_gain_db == held && e.status().activity == 0, "pause freezes auto offset");
        run_constant(e, int(rate * .1), lin(-40));
        check(e.status().auto_gain_db == held && e.status().state == 2, "return requires qualification");
        run_constant(e, int(rate * 3), lin(-12));
        check(e.status().auto_gain_db < -5.4 && e.status().auto_gain_db >= -6, "faster bounded cut");
        e.reset(); check(e.status().auto_gain_db == 0 && e.config().max_boost_db == 6, "reset clears volatile gain, retains config");
    }
    Engine e; WcfiControlConfig c; c.auto_enabled = 1; c.trim_db = 24; c.max_boost_db = 24;
    check(e.configure(c), "headroom config"); run_constant(e, 48000, .001);
    check(e.status().target_gain_db == 24, "total trim plus auto hard bound");
    double in[2] {.99, -.33}, out[2]; e.frame(in, out);
    check(std::abs(out[0]) <= lin(-1) + 1e-12 && std::abs(out[1]) <= lin(-1) + 1e-12, "new transient is immediately sample restrained");
    check(std::abs(out[0] / out[1] + 3) < 1e-12, "headroom restraint is stereo linked");
    const double guard_after_impulse = e.status().guard_attenuation_db;
    run_constant(e, 48000, 1e-5);
    const auto after_quiet = e.status();
    check(after_quiet.activity == 0 && after_quiet.guard_attenuation_db > guard_after_impulse + 5.99
          && after_quiet.guard_attenuation_db <= guard_after_impulse + 6.001, "guard releases 6dB/s during subthreshold audio");
    run_constant(e, 48000 * 4, 1e-5);
    check(std::abs(e.status().guard_attenuation_db) < 1e-8, "guard does not latch after loud bump");
    in[0] = std::numeric_limits<double>::max(); in[1] = -in[0]; e.frame(in, out);
    check(std::isfinite(out[0]) && std::abs(out[0]) <= lin(-1) + 1e-12, "hot finite double protected");
    in[0] = std::numeric_limits<double>::quiet_NaN(); in[1] = std::numeric_limits<double>::infinity(); e.frame(in, out);
    check(out[0] == 0 && out[1] == 0 && e.status().nonfinite_samples == 2, "nonfinite audio becomes zero and is counted");
    const auto good = e.config(); c.trim_db = 25; check(!e.configure(c) && e.config().trim_db == good.trim_db, "range config rejected atomically");
    c = good; c.target_dbfs = std::numeric_limits<double>::quiet_NaN(); check(!e.configure(c), "NaN config rejected");
    check(!e.setup(0, 2) && !e.setup(48000, 3), "invalid engine setup rejected");
    c = {}; c.trim_db = 18; check(e.configure(c), "manual config");
    run_constant(e, 48000, .01); const double manual_before = e.status().applied_gain_db;
    c.trim_db = 0; e.configure(c); in[0] = .01; in[1] = -.005; e.frame(in, out);
    check(e.status().applied_gain_db < manual_before && e.status().applied_gain_db > manual_before - .02, "manual trim to zero is smoothed");
    run_constant(e, 48000, .01); in[0] = 1.2; in[1] = -0.; e.frame(in, out);
    check(std::memcmp(in, out, sizeof(in)) == 0, "manual trim reaches exact unity after convergence");
    e.reset(); c = {}; c.auto_enabled = 1; c.target_dbfs = -24; e.configure(c); run_constant(e, 48000 * 4, .001);
    const double boosted = e.status().applied_gain_db; c.auto_enabled = 0; e.configure(c); in[0] = .001; in[1] = -.001; e.frame(in,out);
    check(boosted > 3 && e.status().applied_gain_db < boosted && e.status().applied_gain_db > boosted - .02, "auto boost off is smoothed");
    c.auto_enabled = 1; c.trim_db = 18; e.configure(c); run_constant(e,48000,.01); in[0]=.99; in[1]=-.99; e.frame(in,out);
    const double restrained=e.status().applied_gain_db; c.auto_enabled=0; e.configure(c); in[0]=.01;in[1]=-.01;e.frame(in,out);
    check(e.status().applied_gain_db > restrained && e.status().applied_gain_db < restrained+.1, "guard is folded into gain before Auto off recovery");
    std::puts("PASS: DSP rate, gain bounds, silence, pause, reset, transient, linked headroom, guard recovery, invalid input.");
}
void bridge_cases() {
    constexpr int frames = 128000; std::vector<float> source(frames * 2), baseline(frames * 2), split(frames * 2);
    for (int i = 0; i < frames; ++i) { source[i*2] = float(std::sin(i * .05) * (i % 30000 < 22000 ? .01 : .00001)); source[i*2+1] = -source[i*2] * .5f; }
    WcfiControlConfig c; c.auto_enabled = 1; c.trim_db = 18; c.max_boost_db = c.max_cut_db = 6;
    Engine reference; reference.configure(c);
    for (int i = 0; i < frames; ++i) reference.frame(source.data()+i*2, baseline.data()+i*2);
    for (uint32_t block : {1u, 64u, 512u, 1379u}) {
        auto h = WcfiControlCreate(48000, 2); check(h && !WcfiControlConfigure(h, &c), "create and configure ABI");
        split = source;
        for (uint32_t start = 0; start < frames; start += block)
            check(!WcfiControlProcess(h, split.data()+start*2, split.data()+start*2, std::min(block, frames-start)), "in-place C process");
        check(std::memcmp(split.data(), baseline.data(), split.size()*sizeof(float)) == 0, "C ABI same DSP and block partition invariant");
        WcfiControlStatus status; WcfiControlGetStatus(h, &status);
        check(status.samples_processed == frames && status.auto_gain_db == reference.status().auto_gain_db, "ABI exact status parity");
        check(WcfiControlReset(h) == 0, "ABI reset"); WcfiControlGetStatus(h, &status);
        check(status.samples_processed == 0 && status.auto_gain_db == 0, "ABI reset status"); WcfiControlDestroy(h);
    }
    check(!WcfiControlCreate(0,2) && !WcfiControlCreate(48000,9) && !WcfiControlCreate(std::nan(""),1), "ABI invalid setup rejected");
    check(WcfiControlProcess(nullptr,nullptr,nullptr,0) == -1, "ABI null rejected");
    std::puts("PASS: native C ABI parity, 4 block partitions, in-place processing, reset, invalid arguments.");
}
template<class T> void host_hot_input(Processor& p) {
    SpeakerArrangement stereo=SpeakerArr::kStereo;
    p.setBusArrangements(&stereo,1,&stereo,1);
    ProcessSetup setup{kOffline,sizeof(T)==4?kSample32:kSample64,8,48000};
    check(p.setupProcessing(setup)==kResultOk,"hot input host setup");p.setActive(true);p.setProcessing(true);
    T left[]{std::numeric_limits<T>::max(),T(.99),T(-2),T(.2),T(0),T(0),T(0),T(0)};
    T right[]{-std::numeric_limits<T>::max(),T(-.495),T(1),T(-.1),T(0),T(0),T(0),T(0)};
    T* channels[]{left,right};AudioBusBuffers in{},out{};in.numChannels=out.numChannels=2;
    if constexpr(sizeof(T)==4){in.channelBuffers32=out.channelBuffers32=channels;}
    else{in.channelBuffers64=out.channelBuffers64=channels;}
    ProcessData data{};data.processMode=kOffline;data.symbolicSampleSize=setup.symbolicSampleSize;data.numSamples=8;
    data.numInputs=data.numOutputs=1;data.inputs=&in;data.outputs=&out;
    check(p.process(data)==kResultOk,"in-place hot input host process");
    for(auto* channel:channels)for(int i=0;i<8;++i)
        check(std::isfinite(channel[i])&&std::abs(channel[i])<=lin(-1)+1e-7,"manual mode protects hot finite samples");
    check(p.status().headroom_restraints>0,"manual guard restraint counted");
    p.setProcessing(false);p.setActive(false);
}
void host_cases(Processor& p, HostApplication& host) {
    auto controller=owned(new Controller);check(controller->initialize(&host)==kResultOk,"controller initialize");
    check(controller->getParameterCount()==ParameterCount+MeterCount,"six writable controls plus read-only telemetry");
    ParameterInfo info;for(int i=0;i<6;++i){controller->getParameterInfo(i,info);check(info.id==Auto+i,"stable host parameter ids");}
    controller->getParameterInfo(0,info);
    check(std::memcmp(info.title, STR16("Calibrate & Hold"), sizeof(STR16("Calibrate & Hold")))==0,
        "RED calibration parameter fingerprint differs from the legacy device");
    check(plain(Trim,0)==-6&&plain(Trim,1)==6&&plain(Boost,1)==6&&plain(Cut,1)==6,"RED hard parameter ranges");
    auto c=mic_defaults();c.auto_enabled=1;c.trim_db=3;
    MemoryStream state;check(write_state(&state,c),"write v2 state");rewind(state);
    check(p.setState(&state)==kResultOk,"queue safe state restore");
    MemoryStream saved;check(p.getState(&saved)==kResultOk,"state save before processing");rewind(saved);
    WcfiControlConfig recovered;
    check(read_state(&saved,recovered)&&recovered.trim_db==3&&recovered.auto_enabled==0,"saved state never rearms calibration");
    rewind(state);check(controller->setComponentState(&state)==kResultOk&&controller->getParamNormalized(Trim)==normalized(Trim,3)
        &&controller->getParamNormalized(Auto)==0,"controller restores manual bounded settings");
    MemoryStream legacy;IBStreamer old_writer(&legacy,kLittleEndian);old_writer.writeInt32(1);
    for(double value:{1.,18.,-26.,12.,24.,-55.})old_writer.writeDouble(value);
    rewind(legacy);check(read_state(&legacy,recovered)&&recovered.auto_enabled==0&&recovered.trim_db==6
        &&recovered.max_boost_db==6&&recovered.max_cut_db==6,"legacy state migrates bounded and manual");
    MemoryStream broken(state.getData(),state.getSize()-1);check(p.setState(&broken)!=kResultOk,"truncated state rejected");
    std::vector<char> corrupt(state.getData(),state.getData()+state.getSize());
    int32 bad_version=99;std::memcpy(corrupt.data(),&bad_version,sizeof(bad_version));MemoryStream wrong_version(corrupt.data(),corrupt.size());
    check(p.setState(&wrong_version)!=kResultOk,"unknown state version rejected");
    corrupt.assign(state.getData(),state.getData()+state.getSize());double bad_trim=7;std::memcpy(corrupt.data()+4+8,&bad_trim,sizeof(bad_trim));
    MemoryStream wrong_range(corrupt.data(),corrupt.size());check(p.setState(&wrong_range)!=kResultOk,"out-of-range v2 state rejected");
    SpeakerArrangement stereo=SpeakerArr::kStereo;p.setBusArrangements(&stereo,1,&stereo,1);
    ProcessSetup setup{kRealtime,kSample32,512,48000};check(p.setupProcessing(setup)==kResultOk,"host setup");
    p.setActive(true);p.setProcessing(true);
    std::vector<float>a(512,.01f),b(512,-.005f),x(512),y(512);float*ins[]{a.data(),b.data()},*outs[]{x.data(),y.data()};
    AudioBusBuffers in{},out{};in.numChannels=out.numChannels=2;in.channelBuffers32=ins;out.channelBuffers32=outs;
    ProcessData data{};data.symbolicSampleSize=kSample32;data.numSamples=512;data.numInputs=data.numOutputs=1;data.inputs=&in;data.outputs=&out;
    c.auto_enabled=0;MicEngine reference;reference.configure(c);
    ParameterChanges changes(6);int32 index;changes.addParameterData(Auto,index)->addPoint(127,0,index);
    auto trimq=changes.addParameterData(Trim,index);trimq->addPoint(127,normalized(Trim,0),index);trimq->addPoint(300,normalized(Trim,-6),index);
    data.inputParameterChanges=&changes;check(p.process(data)==kResultOk,"host parameter process");
    for(int i=0;i<512;++i){if(i==127){c.auto_enabled=0;c.trim_db=0;reference.configure(c);}if(i==300){c.trim_db=-6;reference.configure(c);}
        float input[]{a[i],b[i]},expected[2];reference.frame(input,expected);check(x[i]==expected[0]&&y[i]==expected[1],"sample accurate automation matches MicEngine");}
    MemoryStream automated;p.getState(&automated);rewind(automated);
    check(read_state(&automated,recovered)&&recovered.trim_db==-6&&recovered.auto_enabled==0,"manual host edits own saved state");
    ParameterChanges invalid(6);invalid.addParameterData(Trim,index)->addPoint(0,std::nan(""),index);data.inputParameterChanges=&invalid;p.process(data);
    MemoryStream still_good;p.getState(&still_good);rewind(still_good);
    check(read_state(&still_good,recovered)&&recovered.trim_db==-6,"invalid automation retains last valid state");

    // A real host parameter command arms calibration; saved state never does.
    auto fresh=mic_defaults();MemoryStream reset;write_state(&reset,fresh);rewind(reset);p.setState(&reset);
    ParameterChanges enable(1);enable.addParameterData(Auto,index)->addPoint(0,1,index);data.inputParameterChanges=&enable;
    p.process(data);data.inputParameterChanges=nullptr;
    for(int i=1;i<469;++i)p.process(data);
    check(p.status().state==3&&p.status().auto_gain_db==6,"VST locks after completed five-second quorum and begins ramp");
    for(int i=0;i<600;++i)p.process(data);
    check(p.status().state==4&&std::abs(p.status().target_gain_db-6)<1e-9,"VST reaches and holds calibrated gain");
    const auto held=p.status().auto_gain_db;std::fill(a.begin(),a.end(),0);std::fill(b.begin(),b.end(),0);
    for(int i=0;i<120;++i)p.process(data);
    check(p.status().state==4&&p.status().auto_gain_db==held,"VST pauses retain locked calibration");
    std::fill(a.begin(),a.end(),.01f);std::fill(b.begin(),b.end(),-.005f);
    ParameterChanges manual(2);manual.addParameterData(Auto,index)->addPoint(129,0,index);
    manual.addParameterData(Trim,index)->addPoint(129,normalized(Trim,-2),index);data.inputParameterChanges=&manual;p.process(data);
    check(p.status().state==0&&p.status().target_gain_db==-2&&p.status().auto_gain_db==0,"sample-offset manual command takes ownership");
    check(p.status().applied_gain_db>-2&&p.status().applied_gain_db<6,"manual takeover smooths instead of jumping");
    rewind(reset);p.setState(&reset);data.inputParameterChanges=nullptr;p.process(data);
    check(p.status().samples_processed==512&&p.status().auto_gain_db==0&&p.status().state==0,"restore clears volatile gain and stays manual");
    p.setProcessing(false);p.setActive(false);
    setup.sampleRate=0;check(p.setupProcessing(setup)!=kResultOk,"invalid host sample rate");controller->terminate();
    std::puts("PASS: RED IDs/ranges, safe state migration, exact automation, causal calibration/hold and manual ownership.");
}
void mailbox_case() {
    ConfigMailbox box; WcfiControlConfig a,b;a=mic_defaults();b=mic_defaults();a.trim_db=4;a.target_dbfs=-18;b.trim_db=-4;b.target_dbfs=-30;box.publish(a);
    std::atomic<bool> done=false;std::thread writer([&]{for(int i=0;i<100000;++i)box.publish(i&1?a:b);done=true;});
    int successes=0;while(!done || successes==0){WcfiControlConfig c;uint64_t gen;if(box.read(c,gen)){check((c.trim_db==4&&c.target_dbfs==-18)||(c.trim_db==-4&&c.target_dbfs==-30),"coherent concurrent mailbox");++successes;}}
    writer.join();check(successes>0,"concurrent mailbox made progress");
    std::puts("PASS: concurrent state mailbox coherence with bounded readers and lock-free x64 atomics.");
}
int main(){
    HostApplication host;auto p=owned(new Processor);check(p->initialize(&host)==kResultOk,"processor initialize");
    int cases=0;for(int ch:{1,2})for(int frames:{0,1,17,512,4096})for(bool inplace:{false,true})for(bool silence:{false,true})for(bool offline:{false,true}){
        unity<float>(*p,ch,frames,inplace,silence,offline);unity<double>(*p,ch,frames,inplace,silence,offline);cases+=2;}
    std::printf("PASS: %d exact-unity host cases (mono/stereo, float32/64, in-place, silence, realtime/offline).\n",cases);
    host_hot_input<float>(*p);host_hot_input<double>(*p);dsp_cases();bridge_cases();host_cases(*p,host);mailbox_case();p->terminate();
    Engine benchmark;WcfiControlConfig c;c.auto_enabled=1;benchmark.configure(c);
    auto start=std::chrono::steady_clock::now();run_constant(benchmark,48000*10,.01);
    double elapsed=std::chrono::duration<double>(std::chrono::steady_clock::now()-start).count();
    std::printf("PASS: all native tests. 10s stereo audio DSP elapsed %.6fs (%.3f%% realtime wall time, idle-priority test process).\n",elapsed,elapsed*10);
}
