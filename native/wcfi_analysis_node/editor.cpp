#include "node.h"
#include "pluginterfaces/vst/ivstmessage.h"
#include <cstring>

namespace wcfi {
namespace {
constexpr int LabelEdit = 100, StageCombo = 101, ApplyButton = 102;
class NativeView final : public CPluginView {
public:
    explicit NativeView(Controller* owner) : owner(owner) {
        owner->addRef(); rect = ViewRect(0, 0, 460, 256);
    }
    ~NativeView() override { removed(); owner->release(); }
    tresult PLUGIN_API isPlatformTypeSupported(FIDString type) override {
        return FIDStringsEqual(type, kPlatformTypeHWND) ? kResultTrue : kResultFalse;
    }
    tresult PLUGIN_API attached(void* parent, FIDString type) override {
        if (!parent || isPlatformTypeSupported(type) != kResultTrue || window) return kResultFalse;
        WNDCLASSEXW wc {sizeof(WNDCLASSEXW)};
        // Class is scoped to this loaded DLL, so a host test and the plugin can coexist.
        GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
            reinterpret_cast<LPCWSTR>(&procedure), &wc.hInstance);
        wc.lpfnWndProc = procedure; wc.lpszClassName = L"WCFI.AnalysisNode.Editor.v1";
        wc.hCursor = LoadCursor(nullptr, IDC_ARROW); wc.hbrBackground = reinterpret_cast<HBRUSH>(COLOR_WINDOW + 1);
        RegisterClassExW(&wc);
        window = CreateWindowExW(0, wc.lpszClassName, L"WCFI Analysis Node", WS_CHILD | WS_VISIBLE,
            0, 0, 460, 256, static_cast<HWND>(parent), nullptr, wc.hInstance, this);
        if (!window) return kResultFalse;
        return CPluginView::attached(parent, type);
    }
    tresult PLUGIN_API removed() override {
        if (window) { DestroyWindow(window); window = nullptr; }
        return CPluginView::removed();
    }
private:
    Controller* owner;
    HWND window {}, name {}, stage {}, status {};
    HFONT font {};
    HWND control(const wchar_t* type, const wchar_t* text, DWORD style, int x, int y, int w, int h, int id = 0) {
        HWND child = CreateWindowExW(0, type, text, WS_CHILD | WS_VISIBLE | style, x, y, w, h,
            window, reinterpret_cast<HMENU>(static_cast<INT_PTR>(id)), nullptr, nullptr);
        SendMessageW(child, WM_SETFONT, reinterpret_cast<WPARAM>(font), TRUE);
        return child;
    }
    void create() {
        font = CreateFontW(-16, 0, 0, 0, FW_NORMAL, FALSE, FALSE, FALSE, DEFAULT_CHARSET,
            OUT_DEFAULT_PRECIS, CLIP_DEFAULT_PRECIS, CLEARTYPE_QUALITY, DEFAULT_PITCH, L"Segoe UI");
        control(L"STATIC", L"WCFI  /  ANALYSIS NODE", 0, 20, 16, 420, 24);
        control(L"STATIC", L"Channel / pair name", 0, 20, 52, 190, 24);
        name = control(L"EDIT", reinterpret_cast<const wchar_t*>(owner->settings.label.data()),
            WS_TABSTOP | WS_BORDER | ES_AUTOHSCROLL, 20, 79, 244, 30, LabelEdit);
        SendMessageW(name, EM_SETLIMITTEXT, 63, 0);
        control(L"STATIC", L"Position in chain", 0, 280, 52, 160, 24);
        stage = control(L"COMBOBOX", L"", WS_TABSTOP | CBS_DROPDOWNLIST | WS_VSCROLL, 280, 79, 160, 120, StageCombo);
        SendMessageW(stage, CB_ADDSTRING, 0, reinterpret_cast<LPARAM>(L"Before effects"));
        SendMessageW(stage, CB_ADDSTRING, 0, reinterpret_cast<LPARAM>(L"After effects"));
        SendMessageW(stage, CB_SETCURSEL, owner->settings.position, 0);
        control(L"BUTTON", L"Apply label", WS_TABSTOP | BS_PUSHBUTTON, 20, 125, 130, 32, ApplyButton);
        status = control(L"STATIC", L"Set the same name on the two nodes in a pair.", 0, 20, 168, 420, 28);
        control(L"STATIC", L"Audio passes unchanged · 0 samples added latency\nLast node is before the track fader / pan / mute.", 0, 20, 202, 420, 48);
    }
    static LRESULT CALLBACK procedure(HWND hwnd, UINT message, WPARAM w, LPARAM l) {
        auto self = reinterpret_cast<NativeView*>(GetWindowLongPtrW(hwnd, GWLP_USERDATA));
        if (message == WM_NCCREATE) {
            self = static_cast<NativeView*>(reinterpret_cast<CREATESTRUCTW*>(l)->lpCreateParams);
            SetWindowLongPtrW(hwnd, GWLP_USERDATA, reinterpret_cast<LONG_PTR>(self));
            self->window = hwnd;
        }
        if (self) {
            if (message == WM_CREATE) { self->create(); return 0; }
            if (message == WM_COMMAND && LOWORD(w) == ApplyButton && HIWORD(w) == BN_CLICKED) {
                wchar_t label[64] {}; GetWindowTextW(self->name, label, 64);
                auto position = static_cast<int>(SendMessageW(self->stage, CB_GETCURSEL, 0, 0));
                bool ok = self->owner->apply(label, position);
                SetWindowTextW(self->status, ok ? L"Saved. The app discovers this node automatically." : L"Could not update the processor. Try reopening this editor.");
                return 0;
            }
            if (message == WM_DESTROY) { DeleteObject(self->font); self->font = nullptr; return 0; }
        }
        return DefWindowProcW(hwnd, message, w, l);
    }
};
}
tresult PLUGIN_API Controller::setComponentState(IBStream* stream) {
    return settings.read(stream) ? kResultOk : kResultFalse;
}
IPlugView* PLUGIN_API Controller::createView(FIDString name) {
    return FIDStringsEqual(name, ViewType::kEditor) ? new NativeView(this) : nullptr;
}
bool Controller::apply(const wchar_t* label, int position) {
    if (position < 0 || position > 1 || !label || !label[0]) return false;
    auto msg = owned(allocateMessage());
    if (!msg) return false;
    Settings next; next.label.fill(0);
    for (int n = 0; n < 63 && label[n]; ++n) next.label[n] = static_cast<TChar>(label[n]);
    next.position = position;
    msg->setMessageID("wcfi.settings.v1");
    msg->getAttributes()->setString("label", next.label.data());
    msg->getAttributes()->setInt("position", position);
    if (sendMessage(msg) != kResultOk) return false;
    settings = next; setDirty(true); return true;
}
}
