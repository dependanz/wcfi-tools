#include "node.h"
#include "editor.h"
#include "public.sdk/source/vst/hosting/hostclasses.h"
#include "public.sdk/source/vst/hosting/parameterchanges.h"
#include "pluginterfaces/gui/iplugviewcontentscalesupport.h"
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <array>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cwchar>
#include <limits>
#include <thread>
#include <vector>

using namespace wcfi_control;
void require(bool condition, const char* message) {
    if (!condition) { std::fprintf(stderr, "FAIL: %s\n", message); std::exit(1); }
}
class Handler final : public FObject, public IComponentHandler {
public:
    std::vector<char> events;
    double written {}; ParamID parameter {};
    bool fail_begin {}, fail_write {};
    tresult PLUGIN_API beginEdit(ParamID id) override { events.push_back('b'); parameter = id; return fail_begin ? kResultFalse : kResultOk; }
    tresult PLUGIN_API performEdit(ParamID id, ParamValue value) override {
        events.push_back('p'); parameter = id; written = value; return fail_write ? kResultFalse : kResultOk;
    }
    tresult PLUGIN_API endEdit(ParamID) override { events.push_back('e'); return kResultOk; }
    tresult PLUGIN_API restartComponent(int32) override { return kResultOk; }
    OBJ_METHODS(Handler, FObject)
    DEFINE_INTERFACES
        DEF_INTERFACE(IComponentHandler)
    END_DEFINE_INTERFACES(FObject)
    REFCOUNT_METHODS(FObject)
};
void edits(HostApplication& host) {
    auto controller = owned(new Controller); controller->initialize(&host);
    auto handler = owned(new Handler); controller->setComponentHandler(handler);
    require(commit_control_edit(*controller, Trim, .5, 3.) == EditResult::Applied, "human trim commit accepted");
    require(handler->events == std::vector<char>({'b','p','e'}) && handler->parameter == Trim
        && handler->written == normalized(Trim, 3.), "one balanced host gesture with normalized value");
    handler->events.clear();
    controller->setParamNormalized(Trim, normalized(Trim, 6.));
    require(handler->events.empty(), "host or agent update must not echo host edit");
    require(commit_control_edit(*controller, Trim, normalized(Trim, 3.), 4.) == EditResult::Conflict,
        "stale typed value cannot overwrite a newer host or agent edit");
    require(handler->events.empty(), "conflict causes no host edit");
    for (auto id : {InputRms, TelemetrySequence})
        require(commit_control_edit(*controller, id, 0., 0.) == EditResult::InvalidValue, "readonly telemetry cannot be edited");
    require(commit_control_edit(*controller, Auto, 0., .5) == EditResult::InvalidValue, "auto is explicitly binary");
    require(commit_control_edit(*controller, Trim, normalized(Trim,6.), 25.) == EditResult::InvalidValue, "gain limit enforced");
    require(commit_control_edit(*controller, Trim, normalized(Trim,6.), std::nan("")) == EditResult::InvalidValue, "nonfinite edit rejected");
    require(handler->events.empty(), "invalid edit causes no host gesture");
    handler->fail_begin = true;
    require(commit_control_edit(*controller, Trim, normalized(Trim,6.), 5.) == EditResult::HostUnavailable,
        "unavailable host prevents commit");
    require(handler->events == std::vector<char>({'b'}) && controller->getParamNormalized(Trim) == normalized(Trim,6.),
        "failed begin leaves parameter unchanged");
    handler->events.clear(); handler->fail_begin = false; handler->fail_write = true;
    require(commit_control_edit(*controller, Trim, normalized(Trim,6.), 5.) == EditResult::HostUnavailable, "failed perform reported");
    require(handler->events == std::vector<char>({'b','p','e'}), "failed perform ends gesture without retry");
    auto view = owned(controller->createView(ViewType::kEditor));
    require(bool(view), "custom editor exists"); ViewRect size;
    require(view->getSize(&size) == kResultOk && size.getWidth() >= 700 && size.getHeight() >= 450, "usable editor dimensions");
    require(view->isPlatformTypeSupported(kPlatformTypeHWND) == kResultTrue, "native Windows editor supported");
    require(view->isPlatformTypeSupported("unsupported") != kResultTrue, "unsupported platform rejected");
    require(view->attached(nullptr,kPlatformTypeHWND) != kResultTrue, "null parent rejected");
    view = nullptr; controller->setComponentHandler(nullptr); controller->terminate();
    std::puts("PASS: editor host gestures, manual/agent conflict, no echo, limits, failures and view contract.");
}

// Own hidden HWNDs only. These checks do not focus a window, synthesize user
// input, open an audio device, or discover/control another application.
class HiddenFrame final : public FObject, public IPlugFrame {
public:
    int resize_calls {};
    tresult PLUGIN_API resizeView(IPlugView* view, ViewRect* size) override {
        ++resize_calls;
        return view && size ? view->onSize(size) : kInvalidArgument;
    }
    OBJ_METHODS(HiddenFrame, FObject)
    DEFINE_INTERFACES
        DEF_INTERFACE(IPlugFrame)
    END_DEFINE_INTERFACES(FObject)
    REFCOUNT_METHODS(FObject)
};
struct ChildCounts {
    int edits {}, buttons {}, total {};
    bool operator==(const ChildCounts&) const = default;
};
BOOL CALLBACK count_child(HWND child, LPARAM argument) {
    auto& counts = *reinterpret_cast<ChildCounts*>(argument);
    wchar_t type[64] {};
    GetClassNameW(child, type, static_cast<int>(std::size(type)));
    ++counts.total;
    if (_wcsicmp(type, L"EDIT") == 0) ++counts.edits;
    if (_wcsicmp(type, L"BUTTON") == 0) ++counts.buttons;
    return TRUE;
}
ChildCounts child_counts(HWND editor) {
    ChildCounts counts;
    EnumChildWindows(editor, count_child, reinterpret_cast<LPARAM>(&counts));
    return counts;
}
HWND hidden_parent() {
    const auto parent = CreateWindowExW(WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW,
        L"STATIC", L"WCFI hidden editor lifecycle test", WS_OVERLAPPED,
        0, 0, 2000, 1400, nullptr, nullptr, GetModuleHandleW(nullptr), nullptr);
    require(parent != nullptr && !IsWindowVisible(parent), "hidden native parent created without activation");
    return parent;
}
HWND editor_child(HWND parent, int width, int height) {
    const auto editor = GetWindow(parent, GW_CHILD);
    require(editor && GetParent(editor) == parent, "native editor HWND belongs to hidden parent");
    require(GetWindow(editor, GW_HWNDNEXT) == nullptr, "one native editor per attachment");
    RECT client {};
    require(GetClientRect(editor, &client) && client.right == width && client.bottom == height,
        "native editor dimensions match the host view size");
    require(!IsWindowVisible(editor), "editor remains hidden with its parent");
    return editor;
}
void lifecycle(HostApplication& host) {
    auto controller = owned(new Controller);
    require(controller->initialize(&host) == kResultOk, "lifecycle controller initializes");
    auto handler = owned(new Handler); controller->setComponentHandler(handler);
    auto parent = hidden_parent();
    const auto other_parent = hidden_parent();
    {
        auto view = owned(controller->createView(ViewType::kEditor));
        auto other = owned(controller->createView(ViewType::kEditor));
        auto frame = owned(new HiddenFrame);
        FUnknownPtr<IPlugViewContentScaleSupport> scale(static_cast<IPlugView*>(view));
        require(bool(scale), "native editor exposes content scale support");

        // A host may supply DPI scale before either a frame or HWND exists.
        require(scale->setContentScaleFactor(1.25f) == kResultTrue, "DPI scale accepted before attachment");
        ViewRect size;
        require(view->getSize(&size) == kResultOk && size.getWidth() == 950 && size.getHeight() == 650,
            "pre-attachment scale changes advertised dimensions");
        require(view->setFrame(frame) == kResultTrue, "hidden host frame installed");
        require(view->attached(parent, kPlatformTypeHWND) == kResultOk, "native editor attaches to hidden parent");
        auto editor = editor_child(parent, 950, 650);
        const auto initial_counts = child_counts(editor);
        require(initial_counts.edits == 5 && initial_counts.buttons == 8 && initial_counts.total > 30,
            "five native editable fields and eight native buttons created");
        require(view->attached(parent, kPlatformTypeHWND) != kResultOk, "duplicate attachment rejected");
        const auto trim = GetDlgItem(editor, 1100);
        require(trim != nullptr, "native Trim edit is available");
        wchar_t text[64] {};
        GetWindowTextW(trim, text, static_cast<int>(std::size(text)));
        require(wcscmp(text, L"0.00") == 0, "attached editor shows the current host Trim");
        const auto mode_button = GetDlgItem(editor, 1000);
        require(mode_button != nullptr, "native calibration/manual control is available");
        GetWindowTextW(mode_button, text, static_cast<int>(std::size(text)));
        require(wcscmp(text, L"Start calibration") == 0, "manual startup presents an explicit calibration action");
        SendMessageW(editor, WM_COMMAND, MAKEWPARAM(1000, BN_CLICKED), reinterpret_cast<LPARAM>(mode_button));
        require(controller->getParamNormalized(Auto) == 1 && handler->parameter == Auto,
            "native button explicitly arms calibration through the host gesture");
        GetWindowTextW(mode_button, text, static_cast<int>(std::size(text)));
        require(wcscmp(text, L"Manual: use Trim") == 0, "active mode clearly offers manual Trim ownership");
        SendMessageW(editor, WM_COMMAND, MAKEWPARAM(1000, BN_CLICKED), reinterpret_cast<LPARAM>(mode_button));
        require(controller->getParamNormalized(Auto) == 0 && handler->events == std::vector<char>({'b','p','e','b','p','e'}),
            "manual button sends one explicit balanced gesture");
        handler->events.clear();


        // A real HWND resize verifies more than the IPlugView size declaration:
        // child controls and fonts must follow the new scale as well.
        RECT old_edit {};
        require(GetWindowRect(trim, &old_edit), "original field bounds available");
        const auto old_font = reinterpret_cast<HFONT>(SendMessageW(trim, WM_GETFONT, 0, 0));
        LOGFONTW old_font_info {};
        require(old_font && GetObjectW(old_font, sizeof(old_font_info), &old_font_info), "original field font available");
        require(scale->setContentScaleFactor(1.5f) == kResultTrue && frame->resize_calls == 1,
            "attached scale requests one host resize");
        require(editor_child(parent, 1140, 780) == editor, "DPI resize preserves native editor identity");
        RECT new_edit {};
        require(GetWindowRect(trim, &new_edit) && new_edit.right-new_edit.left == 120
            && old_edit.right-old_edit.left == 100, "numeric field width follows DPI scale");
        const auto new_font = reinterpret_cast<HFONT>(SendMessageW(trim, WM_GETFONT, 0, 0));
        LOGFONTW new_font_info {};
        require(new_font && GetObjectW(new_font, sizeof(new_font_info), &new_font_info)
            && std::abs(new_font_info.lfHeight) > std::abs(old_font_info.lfHeight), "numeric field font scales with DPI");
        for (float bad : {0.f, 5.f, std::numeric_limits<float>::quiet_NaN()})
            require(scale->setContentScaleFactor(bad) != kResultTrue, "invalid DPI scale rejected");
        require(frame->resize_calls == 1 && view->getSize(&size) == kResultOk
            && size.getWidth() == 1140 && size.getHeight() == 780, "invalid DPI scale leaves geometry unchanged");

        // Pending input belongs to the view lifetime. Closing must never send it
        // to the host, and reopening must not resurrect stale edit/conflict state.
        SetWindowTextW(trim, L"7.00");
        require(handler->events.empty(), "typing into the hidden native field does not commit");
        require(view->removed() == kResultOk && !IsWindow(editor)
            && GetWindow(parent, GW_CHILD) == nullptr, "detach destroys the editor and its children");
        controller->setParamNormalized(Trim, normalized(Trim, 3.));
        require(view->attached(parent, kPlatformTypeHWND) == kResultOk, "same view reattaches after teardown");
        editor = editor_child(parent, 1140, 780);
        require(child_counts(editor) == initial_counts, "reattachment creates one fresh control inventory");
        GetWindowTextW(GetDlgItem(editor, 1100), text, static_cast<int>(std::size(text)));
        require(wcscmp(text, L"3.00") == 0, "reattachment reloads host state instead of abandoned typed value");

        // Closing one of two editors must not unregister a still-used class;
        // closing the last must still permit a clean later registration.
        require(other->attached(other_parent, kPlatformTypeHWND) == kResultOk, "second editor instance attaches");
        const auto other_editor = editor_child(other_parent, 760, 520);
        require(view->removed() == kResultOk && IsWindow(other_editor), "one editor can close while another remains");
        SendMessageW(other_editor, WM_TIMER, 1, 0);
        require(child_counts(other_editor) == initial_counts, "remaining editor controls survive sibling teardown");
        require(other->removed() == kResultOk && !IsWindow(other_editor), "last editor tears down cleanly");
        require(view->attached(parent, kPlatformTypeHWND) == kResultOk, "editor class can register again after last close");
        editor = editor_child(parent, 1140, 780);

        // Also exercise destruction initiated by a hidden parent, followed by
        // the host's ordinary removed/attach sequence on the surviving view.
        require(DestroyWindow(parent) && !IsWindow(editor), "parent destruction tears down the child editor");
        require(view->removed() == kResultOk, "removed remains safe after parent destruction");
        parent = hidden_parent();
        require(view->attached(parent, kPlatformTypeHWND) == kResultOk, "view reattaches after parent destruction");
        editor = editor_child(parent, 1140, 780);
        require(child_counts(editor) == initial_counts, "parent-destruction recovery has no duplicate controls");
        require(view->removed() == kResultOk && view->removed() == kResultOk,
            "repeated detach is harmless");
        require(handler->events.empty(), "attach, DPI changes and teardown never emit parameter edits");
        view->setFrame(nullptr);
    }
    DestroyWindow(parent); DestroyWindow(other_parent);
    controller->setComponentHandler(nullptr); controller->terminate();
    std::puts("PASS: hidden HWND attach/detach/reattach, concurrent views, parent teardown and DPI geometry/font scaling.");
}
struct TextQuery { const wchar_t* expected; bool found {}; };
BOOL CALLBACK find_child_text(HWND child, LPARAM argument) {
    auto& query=*reinterpret_cast<TextQuery*>(argument);wchar_t text[320]{};
    GetWindowTextW(child,text,static_cast<int>(std::size(text)));
    query.found |= wcscmp(text,query.expected)==0;return TRUE;
}
bool has_child_text(HWND editor,const wchar_t* text) {
    TextQuery query{text};EnumChildWindows(editor,find_child_text,reinterpret_cast<LPARAM>(&query));return query.found;
}
void calibration_state_labels(HostApplication& host) {
    auto controller=owned(new Controller);controller->initialize(&host);
    controller->setParamNormalized(Auto,1.);
    const double payload[]{-40.,-30.,-34.,-24.,6.,6.,0.,3.,1.};
    for(int i=0;i<MeterPayloadCount;++i)controller->setParamNormalized(InputRms+i,meter_normalized(InputRms+i,payload[i]));
    controller->setParamNormalized(TelemetrySequence,meter_normalized(TelemetrySequence,1));
    auto parent=hidden_parent();auto view=owned(controller->createView(ViewType::kEditor));
    require(view->attached(parent,kPlatformTypeHWND)==kResultOk,"state-label editor attaches hidden");
    const auto editor=editor_child(parent,760,520);
    require(has_child_text(editor,L"RAMPING TO LOCKED GAIN"),"fresh state3 displays ramping instead of tracking");
    controller->setParamNormalized(ActivityState,meter_normalized(ActivityState,4));
    controller->setParamNormalized(TelemetrySequence,meter_normalized(TelemetrySequence,2));
    SendMessageW(editor,WM_TIMER,1,0);
    require(has_child_text(editor,L"HOLDING CALIBRATED GAIN")&&!has_child_text(editor,L"NO LIVE DATA"),
        "fresh state4 displays holding instead of no-data");
    controller->setParamNormalized(ActivityState,meter_normalized(ActivityState,1));
    controller->setParamNormalized(TelemetrySequence,meter_normalized(TelemetrySequence,3));
    SendMessageW(editor,WM_TIMER,1,0);
    require(has_child_text(editor,L"WAITING FOR MIC ACTIVITY"),"fresh state1 describes missing activity");
    controller->setParamNormalized(ActivityState,meter_normalized(ActivityState,2));
    controller->setParamNormalized(TelemetrySequence,meter_normalized(TelemetrySequence,4));
    SendMessageW(editor,WM_TIMER,1,0);
    require(has_child_text(editor,L"COLLECTING ACTIVE SOUND"),"fresh state2 describes calibration collection");
    view->removed();view=nullptr;DestroyWindow(parent);controller->terminate();
    std::puts("PASS: fresh calibration waiting/collecting/ramping/holding native labels.");
}
void ingest(Controller& controller, IParameterChanges& changes) {
    for (int32 i = 0; i < changes.getParameterCount(); ++i) {
        auto q = changes.getParameterData(i); int32 offset; ParamValue value;
        require(q && q->getPointCount() > 0, "meter queue has point");
        require(q->getPoint(q->getPointCount()-1,offset,value) == kResultOk && std::isfinite(value)
                && value >= 0 && value <= 1, "normalized meter values finite and bounded");
        controller.setParamNormalized(q->getParameterId(),value);
    }
}
class MissingQueue : public ParameterChanges {
public:
    int attempts {};
    MissingQueue() : ParameterChanges(MeterCount) {}
    IParamValueQueue* PLUGIN_API addParameterData(const ParamID& id, int32& index) override {
        if (id == InputRms) ++attempts;
        return id == OutputRms ? nullptr : ParameterChanges::addParameterData(id,index);
    }
};
void telemetry(HostApplication& host) {
    auto controller = owned(new Controller); controller->initialize(&host);
    UiTelemetry status;
    require(!controller->readTelemetry(status) && !status.fresh, "new editor shows waiting, not fake zero meters");
    ParameterInfo info;
    for (int i = ParameterCount; i < ParameterCount+MeterCount; ++i) {
        controller->getParameterInfo(i,info);
        require(info.id == InputRms+i-ParameterCount && (info.flags & ParameterInfo::kIsReadOnly)
            && !(info.flags & ParameterInfo::kCanAutomate), "meters are readonly and nonautomatable");
    }
    auto processor = owned(new Processor); processor->initialize(&host);
    SpeakerArrangement stereo = SpeakerArr::kStereo;
    processor->setBusArrangements(&stereo,1,&stereo,1);
    ProcessSetup setup {kRealtime,kSample32,480,48000}; processor->setupProcessing(setup);
    processor->setActive(true); processor->setProcessing(true);
    std::array<float,480> left,right,out_left,out_right; left.fill(.01f); right.fill(.01f);
    float* inputs[]{left.data(),right.data()}, *outputs[]{out_left.data(),out_right.data()};
    AudioBusBuffers input{},output{};input.numChannels=output.numChannels=2;
    input.channelBuffers32=inputs;output.channelBuffers32=outputs;
    ProcessData data{};data.processMode=kRealtime;data.symbolicSampleSize=kSample32;data.numSamples=480;
    data.numInputs=data.numOutputs=1;data.inputs=&input;data.outputs=&output;
    ParameterChanges early(MeterCount); data.outputParameterChanges=&early;
    for(int i=0;i<9;++i) {
        processor->process(data);
        require(early.getParameterCount()==0, "partial input bin never advertises fresh default telemetry");
    }
    data.outputParameterChanges=nullptr; processor->process(data);
    require(out_left==left && out_right==right, "absent telemetry queues do not alter audio");
    ParameterChanges meters(MeterCount);data.outputParameterChanges=&meters;
    processor->process(data); ingest(*controller,meters);
    require(controller->readTelemetry(status) && status.fresh && status.sequence > 0, "complete telemetry becomes fresh");
    require(std::abs(status.input_rms_dbfs+40.)<.001 && std::abs(status.output_rms_dbfs+40.)<.001
            && std::abs(status.applied_gain_db)<1e-9, "meters report true unity input and output");
    require(status.state == 0, "manual state is explicit");
    ParameterChanges command(1);int32 index;
    command.addParameterData(Trim,index)->addPoint(0,normalized(Trim,6.),index);
    data.inputParameterChanges=&command;
    for(int i=0;i<110;++i){ meters.clearQueue();processor->process(data);ingest(*controller,meters);data.inputParameterChanges=nullptr; }
    require(controller->readTelemetry(status) && std::abs(status.output_rms_dbfs+34.)<.01
        && std::abs(status.applied_gain_db-6.)<.01, "output meter follows actual manual gain");
    MissingQueue missing;data.outputParameterChanges=&missing;
    for(int i=0;i<10;++i)processor->process(data);
    require(missing.attempts <= 2, "failed meter publications remain rate limited");
    for(int32 i=0;i<missing.getParameterCount();++i)
        require(missing.getParameterData(i)->getParameterId()!=TelemetrySequence, "partial publication has no freshness heartbeat");
    require(std::isfinite(out_left[0]), "partial queues preserve processing");
    std::this_thread::sleep_for(std::chrono::milliseconds(820));
    require(!controller->readTelemetry(status) && !status.fresh, "stopped telemetry expires");
    controller->setParamNormalized(TelemetrySequence,meter_normalized(TelemetrySequence,status.sequence));
    require(!controller->readTelemetry(status), "repeated cached heartbeat does not renew freshness");
    controller->setParamNormalized(TelemetrySequence,meter_normalized(TelemetrySequence,65535));
    controller->setParamNormalized(TelemetrySequence,meter_normalized(TelemetrySequence,1));
    require(controller->readTelemetry(status) && status.fresh && status.sequence==1, "heartbeat wrap remains fresh");
    processor->setProcessing(false);processor->setActive(false);processor->terminate();controller->terminate();
    std::puts("PASS: read-only telemetry, unity/gain meter accuracy, absent/partial queues, expiry and wrap.");
}
int main(){ HostApplication host;edits(host);lifecycle(host);calibration_state_labels(host);telemetry(host);return 0; }
