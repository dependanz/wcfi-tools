// Offline UI harness. No audio device, playback API, filesystem audio or Ableton access.
#include "node.h"
#include "public.sdk/source/vst/hosting/hostclasses.h"
#include "public.sdk/source/vst/hosting/parameterchanges.h"
#include <windows.h>
#include <array>
#include <cmath>
#include <utility>
#include <vector>

using namespace wcfi_control;
namespace {
constexpr int FrameCount = 2400;
constexpr int PauseButton = 1, AgentButton = 2;
class PreviewHandler final : public FObject, public IComponentHandler {
public:
    std::vector<std::pair<ParamID, ParamValue>> pending;
    tresult PLUGIN_API beginEdit(ParamID) override { return kResultOk; }
    tresult PLUGIN_API performEdit(ParamID id, ParamValue value) override {
        pending.emplace_back(id, value); return kResultOk;
    }
    tresult PLUGIN_API endEdit(ParamID) override { return kResultOk; }
    tresult PLUGIN_API restartComponent(int32) override { return kResultOk; }
    OBJ_METHODS(PreviewHandler, FObject)
    DEFINE_INTERFACES
        DEF_INTERFACE(IComponentHandler)
    END_DEFINE_INTERFACES(FObject)
    REFCOUNT_METHODS(FObject)
};
struct Preview {
    HostApplication host;
    IPtr<Controller> controller;
    IPtr<Processor> processor;
    IPtr<PreviewHandler> handler;
    IPtr<IPlugView> view;
    HWND panel {}, pause_button {};
    bool paused {false};
    uint64_t frame_cursor {};
    std::array<float, FrameCount> left {}, right {}, out_left {}, out_right {};

    bool initialize(HWND window) {
        controller = owned(new Controller);
        processor = owned(new Processor);
        handler = owned(new PreviewHandler);
        if (controller->initialize(&host) != kResultOk || processor->initialize(&host) != kResultOk)
            return false;
        controller->setComponentHandler(handler);
        SpeakerArrangement stereo = SpeakerArr::kStereo;
        if (processor->setBusArrangements(&stereo, 1, &stereo, 1) != kResultOk) return false;
        ProcessSetup setup {kRealtime, kSample32, FrameCount, 48000};
        if (processor->setupProcessing(setup) != kResultOk) return false;
        processor->setActive(true); processor->setProcessing(true);
        view = owned(controller->createView(ViewType::kEditor));
        if (!view) return false;
        ViewRect rect; view->getSize(&rect);
        RECT desired {0, 0, rect.getWidth(), rect.getHeight() + 44};
        AdjustWindowRectEx(&desired, WS_OVERLAPPED | WS_CAPTION | WS_SYSMENU | WS_MINIMIZEBOX, FALSE, 0);
        SetWindowPos(window, nullptr, 0, 0, desired.right - desired.left, desired.bottom - desired.top,
                     SWP_NOMOVE | SWP_NOZORDER);
        pause_button = CreateWindowW(L"BUTTON", L"Pause readings", WS_CHILD | WS_VISIBLE | WS_TABSTOP,
            12, 8, 132, 28, window, reinterpret_cast<HMENU>(PauseButton), nullptr, nullptr);
        CreateWindowW(L"BUTTON", L"Simulate agent: Trim +3 dB", WS_CHILD | WS_VISIBLE | WS_TABSTOP,
            156, 8, 228, 28, window, reinterpret_cast<HMENU>(AgentButton), nullptr, nullptr);
        CreateWindowW(L"STATIC", L"Synthetic input. No audio device.", WS_CHILD | WS_VISIBLE,
            404, 14, 340, 22, window, nullptr, nullptr, nullptr);
        panel = CreateWindowW(L"STATIC", L"", WS_CHILD | WS_VISIBLE, 0, 44,
            rect.getWidth(), rect.getHeight(), window, nullptr, nullptr, nullptr);
        if (!panel || view->attached(panel, kPlatformTypeHWND) != kResultOk) return false;
        SetTimer(window, 1, 50, nullptr);
        return true;
    }
    void tick() {
        if (paused) return;
        for (int i = 0; i < FrameCount; ++i) {
            const double t = static_cast<double>(frame_cursor++) / 48000.;
            // Deterministic quiet phrase/noise pattern, rendered only into memory.
            const double amplitude = std::fmod(t, 7.) < 5. ? .012 * (1. + .3 * std::sin(t)) : .00001;
            left[i] = static_cast<float>(amplitude * std::sin(t * 220. * 6.283185307179586));
            right[i] = left[i];
        }
        ParameterChanges incoming(6), outgoing(16);
        for (const auto& [id, value] : handler->pending) {
            int32 index;
            incoming.addParameterData(id, index)->addPoint(0, value, index);
        }
        handler->pending.clear();
        float* inputs[] {left.data(), right.data()}, *outputs[] {out_left.data(), out_right.data()};
        AudioBusBuffers input {}, output {};
        input.numChannels = output.numChannels = 2;
        input.channelBuffers32 = inputs; output.channelBuffers32 = outputs;
        ProcessData data {};
        data.processMode = kRealtime; data.symbolicSampleSize = kSample32; data.numSamples = FrameCount;
        data.numInputs = data.numOutputs = 1; data.inputs = &input; data.outputs = &output;
        data.inputParameterChanges = &incoming; data.outputParameterChanges = &outgoing;
        processor->process(data);
        for (int32 i = 0; i < outgoing.getParameterCount(); ++i) {
            auto queue = outgoing.getParameterData(i);
            int32 offset; ParamValue value;
            if (queue && queue->getPointCount() &&
                queue->getPoint(queue->getPointCount() - 1, offset, value) == kResultOk)
                controller->setParamNormalized(queue->getParameterId(), value);
        }
    }
    void cleanup() {
        if (view) { view->removed(); view = nullptr; }
        if (processor) { processor->setProcessing(false); processor->setActive(false); processor->terminate(); processor = nullptr; }
        if (controller) { controller->setComponentHandler(nullptr); controller->terminate(); controller = nullptr; }
        handler = nullptr;
    }
};
LRESULT CALLBACK window_proc(HWND window, UINT message, WPARAM w, LPARAM l) {
    auto preview = reinterpret_cast<Preview*>(GetWindowLongPtrW(window, GWLP_USERDATA));
    if (message == WM_NCCREATE) {
        preview = static_cast<Preview*>(reinterpret_cast<CREATESTRUCTW*>(l)->lpCreateParams);
        SetWindowLongPtrW(window, GWLP_USERDATA, reinterpret_cast<LONG_PTR>(preview));
    }
    if (preview) {
        if (message == WM_CREATE) return preview->initialize(window) ? 0 : -1;
        if (message == WM_TIMER) { preview->tick(); return 0; }
        if (message == WM_COMMAND && LOWORD(w) == PauseButton && HIWORD(w) == BN_CLICKED) {
            preview->paused = !preview->paused;
            SetWindowTextW(preview->pause_button, preview->paused ? L"Resume readings" : L"Pause readings");
            return 0;
        }
        if (message == WM_COMMAND && LOWORD(w) == AgentButton && HIWORD(w) == BN_CLICKED) {
            const double value = normalized(Trim, 3.);
            preview->controller->setParamNormalized(Trim, value);
            preview->handler->pending.emplace_back(Trim, value);
            return 0;
        }
        if (message == WM_DESTROY) { KillTimer(window, 1); preview->cleanup(); PostQuitMessage(0); return 0; }
    }
    return DefWindowProcW(window, message, w, l);
}
}
int WINAPI wWinMain(HINSTANCE instance, HINSTANCE, PWSTR, int show) {
    SetProcessDPIAware();
    SetPriorityClass(GetCurrentProcess(), IDLE_PRIORITY_CLASS);
    Preview preview;
    WNDCLASSEXW wc {sizeof(WNDCLASSEXW)};
    wc.hInstance = instance; wc.lpfnWndProc = window_proc;
    wc.lpszClassName = L"WCFI.ControlNode.RED.OfflinePreview";
    wc.hCursor = LoadCursor(nullptr, IDC_ARROW);
    wc.hbrBackground = reinterpret_cast<HBRUSH>(COLOR_BTNFACE + 1);
    if (!RegisterClassExW(&wc)) return 1;
    HWND window = CreateWindowExW(0, wc.lpszClassName,
        L"WCFI Control Node RED — OFFLINE UI PREVIEW · no audio device",
        WS_OVERLAPPED | WS_CAPTION | WS_SYSMENU | WS_MINIMIZEBOX,
        CW_USEDEFAULT, CW_USEDEFAULT, 800, 620, nullptr, nullptr, instance, &preview);
    if (!window) { preview.cleanup(); return 1; }
    ShowWindow(window, show); UpdateWindow(window);
    MSG message;
    while (GetMessageW(&message, nullptr, 0, 0) > 0) { TranslateMessage(&message); DispatchMessageW(&message); }
    return 0;
}
