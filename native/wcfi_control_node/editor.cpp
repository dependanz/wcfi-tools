#include "editor.h"
#include "node.h"
#include "public.sdk/source/common/pluginview.h"
#include "pluginterfaces/gui/iplugviewcontentscalesupport.h"
#include <windows.h>
#include <algorithm>
#include <array>
#include <cmath>
#include <cwchar>
#include <cwctype>
#include <memory>
#include <limits>
#include <string>
#include <vector>

namespace wcfi_control {
namespace {
constexpr double Minimum[] {0, -6, -36, 0, 0, -90};
constexpr double Maximum[] {1, 6, -12, 6, 6, -24};
constexpr int Width = 760, Height = 520;
constexpr int AutoButton = 1000, TrimDown = 1001, TrimUp = 1002;
constexpr int EditBase = 1100, ApplyBase = 1200;
constexpr COLORREF Background = RGB(17, 24, 33), Card = RGB(25, 35, 47);
constexpr COLORREF Border = RGB(49, 64, 80), Field = RGB(13, 21, 30);
constexpr COLORREF Text = RGB(235, 242, 248), Muted = RGB(153, 173, 191);
constexpr COLORREF Accent = RGB(98, 218, 201), Amber = RGB(244, 191, 106);
constexpr COLORREF Button = RGB(43, 61, 77);

bool valid_edit(ParamID id, double expected, double value) {
    if (id < Auto || id > Activity || !std::isfinite(expected)
            || expected < 0 || expected > 1 || !std::isfinite(value)) return false;
    const auto index = id - Auto;
    return value >= Minimum[index] && value <= Maximum[index]
        && (id != Auto || value == 0 || value == 1);
}

std::wstring number(double value, int precision = 1, bool sign = false) {
    wchar_t buffer[64] {};
    if (!std::isfinite(value)) return L"—";
    if (std::abs(value) < .0000001) value = 0;
    swprintf_s(buffer, sign ? L"%+.*f" : L"%.*f", precision, value);
    return buffer;
}

void set_text(HWND window, const std::wstring& text) {
    wchar_t current[320] {};
    GetWindowTextW(window, current, static_cast<int>(std::size(current)));
    if (text != current) SetWindowTextW(window, text.c_str());
}
}

EditResult commit_control_edit(Controller& owner, ParamID id,
                               double expected_normalized, double plain_value) {
    if (!valid_edit(id, expected_normalized, plain_value)) return EditResult::InvalidValue;
    const double current = owner.getParamNormalized(id);
    if (!std::isfinite(current) || std::abs(current - expected_normalized) > 1e-6)
        return EditResult::Conflict;
    const double next = normalized(id, plain_value);
    if (owner.beginEdit(id) != kResultOk) return EditResult::HostUnavailable;
    const auto local = owner.setParamNormalized(id, next);
    const auto written = local == kResultOk ? owner.performEdit(id, next) : kResultFalse;
    const auto ended = owner.endEdit(id);
    return local == kResultOk && written == kResultOk && ended == kResultOk
        ? EditResult::Applied : EditResult::HostUnavailable;
}

namespace {
class NativeView final : public CPluginView, public IPlugViewContentScaleSupport {
public:
    explicit NativeView(Controller* controller) : owner(controller) {
        owner->addRef(); rect = ViewRect(0, 0, Width, Height);
        background_brush = CreateSolidBrush(Background);
        card_brush = CreateSolidBrush(Card);
        field_brush = CreateSolidBrush(Field);
    }
    ~NativeView() override {
        removed(); owner->release();
        DeleteObject(background_brush); DeleteObject(card_brush); DeleteObject(field_brush);
    }
    tresult PLUGIN_API isPlatformTypeSupported(FIDString type) override {
        return FIDStringsEqual(type, kPlatformTypeHWND) ? kResultTrue : kResultFalse;
    }
    tresult PLUGIN_API attached(void* parent, FIDString type) override {
        if (!parent || window || isPlatformTypeSupported(type) != kResultTrue) return kResultFalse;
        children.clear(); rows = {}; message_until = 0;
        WNDCLASSEXW wc {sizeof(WNDCLASSEXW)};
        GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
            reinterpret_cast<LPCWSTR>(&procedure), &wc.hInstance);
        module = wc.hInstance;
        wc.lpfnWndProc = procedure; wc.lpszClassName = L"WCFI.ControlNode.Editor.RED.v3";
        wc.hCursor = LoadCursor(nullptr, IDC_ARROW); wc.hbrBackground = nullptr;
        RegisterClassExW(&wc);
        window = CreateWindowExW(WS_EX_CONTROLPARENT, wc.lpszClassName, L"WCFI Control Node RED",
            WS_CHILD | WS_VISIBLE | WS_CLIPCHILDREN, 0, 0, px(Width), px(Height),
            static_cast<HWND>(parent), nullptr, wc.hInstance, this);
        if (!window) return kResultFalse;
        return CPluginView::attached(parent, type);
    }
    tresult PLUGIN_API removed() override {
        if (window) { DestroyWindow(window); window = nullptr; }
        children.clear(); rows = {};
        // The last closed view releases the DLL-owned class; attempts while
        // another instance is still open fail harmlessly.
        if (module) UnregisterClassW(L"WCFI.ControlNode.Editor.RED.v3", module);
        return CPluginView::removed();
    }
    tresult PLUGIN_API onSize(ViewRect* size) override {
        if (!size) return kInvalidArgument;
        const auto result = CPluginView::onSize(size);
        if (window) SetWindowPos(window, nullptr, 0, 0, size->getWidth(), size->getHeight(),
                                 SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE);
        return result;
    }
    tresult PLUGIN_API setContentScaleFactor(ScaleFactor next) override {
        if (!std::isfinite(next) || next < .5f || next > 4.f) return kInvalidArgument;
        scale = next; rect = ViewRect(0, 0, px(Width), px(Height));
        if (window) { create_fonts(); layout(); InvalidateRect(window, nullptr, TRUE); }
        if (plugFrame) plugFrame->resizeView(this, &rect);
        return kResultTrue;
    }
    // Native child controls own keyboard input while focused. In particular,
    // Space/Enter must not fall through to a host transport shortcut.
    tresult PLUGIN_API onKeyDown(char16, int16, int16) override { return owns_focus() ? kResultTrue : kResultFalse; }
    tresult PLUGIN_API onKeyUp(char16, int16, int16) override { return owns_focus() ? kResultTrue : kResultFalse; }
    tresult PLUGIN_API onFocus(TBool focused) override {
        if (focused && window && !owns_focus()) SetFocus(rows[0].edit);
        return kResultTrue;
    }
    OBJ_METHODS(NativeView, CPluginView)
    DEFINE_INTERFACES
        DEF_INTERFACE(IPlugViewContentScaleSupport)
    END_DEFINE_INTERFACES(CPluginView)
    REFCOUNT_METHODS(CPluginView)

private:
    struct Row {
        ParamID id {};
        HWND edit {};
        bool dirty {}, conflict {};
        double expected {};
    };
    struct Child {
        NativeView* view {};
        HWND window {};
        RECT logical {};
        int font {}, row {-1};
        bool edit {}, button {};
        WNDPROC previous {};
    };
    Controller* owner;
    HINSTANCE module {};
    HWND window {}, auto_button {}, mode {}, status {}, input_value {}, input_peak {}, output_value {}, output_peak {};
    HWND gain_value {}, auto_value {}, guard_value {}, activity_value {};
    HBRUSH background_brush {}, card_brush {}, field_brush {};
    std::array<HFONT, 4> fonts {};
    std::array<Row, 5> rows {};
    std::vector<std::unique_ptr<Child>> children;
    UiTelemetry telemetry {};
    double scale {1};
    bool updating {}, auto_on {}, live {}, message_error {};
    ULONGLONG message_until {};

    int px(int logical) const { return static_cast<int>(std::lround(logical * scale)); }
    RECT bounds(int left, int top, int right, int bottom) const { return {px(left), px(top), px(right), px(bottom)}; }
    bool owns_focus() const { const auto focus = GetFocus(); return window && (focus == window || IsChild(window, focus)); }

    void create_fonts() {
        const auto old = fonts;
        constexpr int sizes[] {14, 12, 26, 18};
        for (int i = 0; i < 4; ++i)
            fonts[i] = CreateFontW(-px(sizes[i]), 0, 0, 0, i == 2 || i == 3 ? FW_SEMIBOLD : FW_NORMAL,
                FALSE, FALSE, FALSE, DEFAULT_CHARSET, OUT_DEFAULT_PRECIS, CLIP_DEFAULT_PRECIS,
                CLEARTYPE_QUALITY, DEFAULT_PITCH, L"Segoe UI");
        for (const auto& item : children) SendMessageW(item->window, WM_SETFONT, reinterpret_cast<WPARAM>(fonts[item->font]), TRUE);
        for (auto font : old) if (font) DeleteObject(font);
    }
    HWND child(const wchar_t* type, const wchar_t* text, DWORD style,
               int x, int y, int width, int height, int id = 0, int font = 0, int row = -1) {
        auto item = std::make_unique<Child>();
        item->view = this; item->logical = {x, y, x + width, y + height};
        item->font = font; item->row = row;
        item->edit = wcscmp(type, L"EDIT") == 0; item->button = wcscmp(type, L"BUTTON") == 0;
        item->window = CreateWindowExW(item->edit ? WS_EX_CLIENTEDGE : 0, type, text,
            WS_CHILD | WS_VISIBLE | style, px(x), px(y), px(width), px(height), window,
            reinterpret_cast<HMENU>(static_cast<INT_PTR>(id)), nullptr, nullptr);
        const auto handle = item->window;
        SendMessageW(handle, WM_SETFONT, reinterpret_cast<WPARAM>(fonts[font]), TRUE);
        if (item->edit) { SendMessageW(handle, EM_SETLIMITTEXT, 24, 0); SendMessageW(handle, EM_SETMARGINS, EC_LEFTMARGIN | EC_RIGHTMARGIN, MAKELPARAM(px(8), px(4))); }
        if (item->edit || item->button) {
            SetPropW(handle, L"WCFI.ControlNode.Child", item.get());
            item->previous = reinterpret_cast<WNDPROC>(SetWindowLongPtrW(handle, GWLP_WNDPROC, reinterpret_cast<LONG_PTR>(&child_procedure)));
        }
        children.push_back(std::move(item)); return handle;
    }
    HWND label(const wchar_t* text, int x, int y, int w, int h, int font = 0, DWORD style = 0) {
        return child(L"STATIC", text, SS_NOPREFIX | style, x, y, w, h, 0, font);
    }
    HWND button(const wchar_t* text, int x, int y, int w, int h, int id, int row = -1) {
        return child(L"BUTTON", text, WS_TABSTOP | BS_OWNERDRAW, x, y, w, h, id, 0, row);
    }
    void create() {
        updating = true; create_fonts(); children.reserve(50);
        label(L"WCFI  /  AUDIO TOOLS", 24, 18, 350, 20, 1);
        label(L"Control Node RED", 24, 41, 470, 38, 2);
        label(L"Single mic: calibrate once, then hold. Hard gain limit: +/-6 dB.", 25, 87, 510, 22);
        mode = label(L"MANUAL · TRIM TARGET", 506, 22, 230, 24, 1, SS_RIGHT);
        auto_button = button(L"Start calibration", 568, 58, 168, 42, AutoButton);
        label(L"GAIN & LIMITS", 44, 145, 350, 20, 1);
        constexpr const wchar_t* names[] {L"Trim dB", L"Target RMS", L"Max Boost", L"Max Cut", L"Activity Threshold"};
        constexpr const wchar_t* units[] {L"dB", L"dBFS", L"dB", L"dB", L"dBFS"};
        for (int i = 0; i < 5; ++i) {
            rows[i].id = Trim + i;
            const int y = 175 + i * 52;
            label(names[i], 44, y + 7, i == 0 ? 112 : 147, 24);
            rows[i].edit = child(L"EDIT", L"", WS_TABSTOP | ES_AUTOHSCROLL, 198, y, i == 0 ? 80 : 112, 33, EditBase + i, 0, i);
            if (i == 0) {
                button(L"Trim down 1 dB", 164, y, 28, 33, TrimDown, i);
                button(L"Trim up 1 dB", 282, y, 28, 33, TrimUp, i);
            }
            label(units[i], 315, y + 8, 37, 22, 1);
            const std::wstring apply_name = std::wstring(L"Apply ") + names[i];
            button(apply_name.c_str(), 355, y, 71, 33, ApplyBase + i, i);
        }
        label(L"Trim: manual target, or offset to locked gain.", 44, 429, 382, 18, 1);
        label(L"LIVE SIGNAL", 486, 145, 230, 20, 1);
        label(L"Input RMS", 486, 178, 100, 23);
        input_value = label(L"—", 586, 177, 130, 25, 3, SS_RIGHT);
        input_peak = label(L"Peak —", 486, 222, 230, 19, 1);
        label(L"Output RMS", 486, 253, 110, 23);
        output_value = label(L"—", 586, 252, 130, 25, 3, SS_RIGHT);
        output_peak = label(L"Peak —", 486, 297, 230, 19, 1);
        label(L"Applied gain", 486, 331, 110, 24);
        gain_value = label(L"—", 595, 325, 121, 32, 2, SS_RIGHT);
        auto_value = label(L"Auto correction —", 486, 373, 230, 20, 1);
        guard_value = label(L"Peak protection —", 486, 395, 230, 20, 1);
        activity_value = label(L"NO LIVE DATA", 486, 421, 230, 23, 1);
        status = label(L"Enter applies · Esc restores · Trim buttons step 1 dB", 24, 465, 712, 24, 1);
        label(L"RED single-mic v0.3 · offline review build", 24, 496, 480, 19, 1);
        label(L"WCFI CONTROL NODE", 518, 496, 218, 19, 1, SS_RIGHT);
        synchronize(); SetTimer(window, 1, 100, nullptr);
    }
    void layout() {
        for (const auto& item : children) {
            const auto& r = item->logical;
            SetWindowPos(item->window, nullptr, px(r.left), px(r.top), px(r.right-r.left), px(r.bottom-r.top),
                SWP_NOZORDER | SWP_NOACTIVATE);
        }
    }
    void feedback(const std::wstring& message, bool error = false, bool sticky = false) {
        message_error = error;
        message_until = sticky ? std::numeric_limits<ULONGLONG>::max() : GetTickCount64() + (error ? 6500 : 2500);
        set_text(status, message); InvalidateRect(status, nullptr, TRUE);
    }
    void restore(int index) {
        auto& row = rows[index]; row.dirty = row.conflict = false;
        row.expected = owner->getParamNormalized(row.id);
        updating = true; set_text(row.edit, number(plain(row.id, row.expected), 2)); updating = false;
        feedback(L"Reloaded the current host value. No change was sent.");
        InvalidateRect(row.edit, nullptr, TRUE);
    }
    void result(EditResult result, const wchar_t* success) {
        switch (result) {
        case EditResult::Applied: feedback(success); break;
        case EditResult::InvalidValue: feedback(L"Value is outside this control's range. No change was sent.", true); break;
        case EditResult::Conflict: feedback(L"Changed elsewhere. Press Esc in the field to reload before applying.", true); break;
        case EditResult::HostUnavailable: feedback(L"The host did not confirm this edit. Inspect the host before trying again.", true, true); break;
        }
    }
    void apply(int index) {
        auto& row = rows[index]; wchar_t buffer[64] {};
        GetWindowTextW(row.edit, buffer, static_cast<int>(std::size(buffer)));
        wchar_t* end {}; const auto value = std::wcstod(buffer, &end);
        while (end && *end && std::iswspace(*end)) ++end;
        if (end == buffer || !end || *end || !std::isfinite(value)) {
            feedback(L"Enter a number, without units. Press Esc to restore the host value.", true); return;
        }
        if (row.conflict) { result(EditResult::Conflict, L""); return; }
        const auto applied = commit_control_edit(*owner, row.id, row.expected, value);
        if (applied == EditResult::InvalidValue) {
            const auto slot = row.id - Auto;
            const wchar_t* units = row.id == Target || row.id == Activity ? L" dBFS" : L" dB";
            feedback(L"Allowed range: " + number(Minimum[slot]) + L" to " + number(Maximum[slot])
                     + units + L". No change was sent.", true);
            return;
        }
        if (applied == EditResult::Applied) row.dirty = row.conflict = false;
        if (applied == EditResult::Conflict) row.conflict = true;
        result(applied, row.id == Target || row.id == Activity
            ? L"Applied. If gain is already locked, choose Manual then Start calibration."
            : L"Applied to the host. Agents see the same value."); synchronize();
    }
    void nudge(double delta) {
        auto& row = rows[0];
        if (row.dirty || row.conflict) { feedback(L"Apply or restore the typed Trim value before using the step buttons.", true); return; }
        const auto current = owner->getParamNormalized(Trim);
        const auto next = std::clamp(plain(Trim, current) + delta, -6., 6.);
        result(commit_control_edit(*owner, Trim, current, next), L"Trim changed by one step; calibration mode is unchanged."); synchronize();
    }
    void toggle_auto() {
        const double expected = owner->getParamNormalized(Auto);
        result(commit_control_edit(*owner, Auto, expected, expected >= .5 ? 0. : 1.),
               expected >= .5 ? L"Manual control: moving smoothly to the Trim target." : L"Waiting for 5 seconds of active sound within 30 seconds.");
        synchronize();
    }
    void synchronize() {
        if (!window) return;
        updating = true;
        for (auto& row : rows) {
            const double current = owner->getParamNormalized(row.id);
            if (row.dirty) {
                if (std::abs(current - row.expected) > 1e-6 && !row.conflict) {
                    row.conflict = true;
                    feedback(L"A host or agent edit arrived. Press Esc to reload your pending field.", true);
                    InvalidateRect(row.edit, nullptr, TRUE);
                }
            } else {
                row.expected = current; row.conflict = false;
                set_text(row.edit, number(plain(row.id, current), 2));
            }
        }
        updating = false;
        auto_on = owner->getParamNormalized(Auto) >= .5;
        set_text(mode, auto_on ? L"CALIBRATE & HOLD" : L"MANUAL · TRIM TARGET");
        set_text(auto_button, auto_on ? L"Manual: use Trim" : L"Start calibration");
        live = owner->readTelemetry(telemetry) && telemetry.fresh;
        set_text(input_value, live ? number(telemetry.input_rms_dbfs) + L" dBFS" : L"—");
        set_text(output_value, live ? number(telemetry.output_rms_dbfs) + L" dBFS" : L"—");
        set_text(input_peak, live ? L"Peak " + number(telemetry.input_peak_dbfs) + L" dBFS" : L"Peak —");
        set_text(output_peak, live ? L"Peak " + number(telemetry.output_peak_dbfs) + L" dBFS" : L"Peak —");
        set_text(gain_value, live ? number(telemetry.applied_gain_db, 1, true) + L" dB" : L"—");
        set_text(auto_value, live ? L"Locked correction  " + number(telemetry.auto_gain_db, 1, true) + L" dB" : L"Auto correction —");
        set_text(guard_value, live ? L"Peak protection  " + number(telemetry.guard_reduction_db) + L" dB" : L"Peak protection —");
        const wchar_t* state = L"NO LIVE DATA";
        if (live) switch (telemetry.state) {
            case 0: state = L"MANUAL · TRIM TARGET"; break;
            case 1: state = L"WAITING FOR MIC ACTIVITY"; break;
            case 2: state = L"COLLECTING ACTIVE SOUND"; break;
            case 3: state = L"RAMPING TO LOCKED GAIN"; break;
            case 4: state = L"HOLDING CALIBRATED GAIN"; break;
        }
        set_text(activity_value, state);
        if (GetTickCount64() >= message_until) {
            message_error = false;
            set_text(status, live ? L"Enter applies · Esc restores · Trim buttons step 1 dB"
                                  : L"Waiting for live telemetry. Controls still show the host's current settings.");
        }
        const auto meter_area = bounds(478, 204, 724, 371);
        InvalidateRect(window, &meter_area, FALSE);
        InvalidateRect(mode, nullptr, TRUE); InvalidateRect(activity_value, nullptr, TRUE);
        InvalidateRect(auto_button, nullptr, TRUE);
    }
    void fill(HDC dc, RECT r, COLORREF color) const {
        const auto brush = CreateSolidBrush(color); FillRect(dc, &r, brush); DeleteObject(brush);
    }
    void card(HDC dc, RECT r) const {
        const auto brush = CreateSolidBrush(Card); const auto pen = CreatePen(PS_SOLID, px(1), Border);
        const auto old_brush = SelectObject(dc, brush), old_pen = SelectObject(dc, pen);
        RoundRect(dc, r.left, r.top, r.right, r.bottom, px(12), px(12));
        SelectObject(dc, old_pen); SelectObject(dc, old_brush); DeleteObject(pen); DeleteObject(brush);
    }
    void meter(HDC dc, int y, double level, double peak, bool gain = false) const {
        auto rail = bounds(486, y, 716, y + 8); fill(dc, rail, Field);
        if (!live) return;
        const int width = rail.right - rail.left;
        if (gain) {
            const int center = rail.left + width / 2;
            const int extent = static_cast<int>(std::clamp(level / 6., -1., 1.) * width / 2.);
            RECT amount {std::min(center, center + extent), rail.top, std::max(center, center + extent), rail.bottom};
            fill(dc, amount, level >= 0 ? Accent : Amber);
            fill(dc, {center, rail.top-px(2), center+px(1), rail.bottom+px(2)}, Muted);
        } else {
            RECT amount = rail; amount.right = amount.left + static_cast<int>(std::clamp((level + 72.) / 72., 0., 1.) * width);
            fill(dc, amount, level > -3 ? Amber : Accent);
            const int x = rail.left + static_cast<int>(std::clamp((peak + 72.) / 72., 0., 1.) * (width-px(2)));
            if (peak > -72) fill(dc, {x, rail.top-px(2), x+px(2), rail.bottom+px(2)}, Text);
        }
    }
    void paint() {
        PAINTSTRUCT ps; const auto dc = BeginPaint(window, &ps);
        RECT client; GetClientRect(window, &client); FillRect(dc, &client, background_brush);
        card(dc, bounds(24, 130, 446, 450)); card(dc, bounds(466, 130, 736, 450));
        fill(dc, bounds(24, 117, 736, 118), Border);
        fill(dc, bounds(24, 488, 736, 489), Border);
        meter(dc, 208, telemetry.input_rms_dbfs, telemetry.input_peak_dbfs);
        meter(dc, 283, telemetry.output_rms_dbfs, telemetry.output_peak_dbfs);
        meter(dc, 360, telemetry.applied_gain_db, 0, true);
        EndPaint(window, &ps);
    }
    void draw_button(DRAWITEMSTRUCT& item) const {
        const bool accent = item.CtlID == AutoButton;
        FillRect(item.hDC, &item.rcItem, accent ? background_brush : card_brush);
        auto color = accent ? (auto_on ? RGB(37, 103, 93) : RGB(40, 79, 88)) : Button;
        if (item.itemState & ODS_SELECTED) color = RGB(57, 87, 102);
        const auto brush = CreateSolidBrush(color);
        const auto pen = CreatePen(PS_SOLID, px(1), accent ? Accent : Border);
        const auto old_brush = SelectObject(item.hDC, brush), old_pen = SelectObject(item.hDC, pen);
        RoundRect(item.hDC, item.rcItem.left, item.rcItem.top, item.rcItem.right, item.rcItem.bottom, px(7), px(7));
        SelectObject(item.hDC, old_pen); SelectObject(item.hDC, old_brush); DeleteObject(pen); DeleteObject(brush);
        wchar_t text[96] {}; GetWindowTextW(item.hwndItem, text, static_cast<int>(std::size(text)));
        const wchar_t* display = text;
        if (item.CtlID == TrimDown) display = L"−";
        else if (item.CtlID == TrimUp) display = L"+";
        else if (item.CtlID >= ApplyBase && item.CtlID < ApplyBase + 5) display = L"Apply";
        SetBkMode(item.hDC, TRANSPARENT); SetTextColor(item.hDC, Text);
        const auto old_font = SelectObject(item.hDC, fonts[0]);
        DrawTextW(item.hDC, display, -1, &item.rcItem, DT_CENTER | DT_VCENTER | DT_SINGLELINE | DT_NOPREFIX);
        SelectObject(item.hDC, old_font);
        if (item.itemState & ODS_FOCUS) { RECT r = item.rcItem; InflateRect(&r, -px(4), -px(4)); DrawFocusRect(item.hDC, &r); }
    }
    HBRUSH control_color(HDC dc, HWND handle, bool edit) const {
        SetBkMode(dc, OPAQUE); SetTextColor(dc, Text);
        if (edit) {
            for (const auto& row : rows) if (row.edit == handle && row.conflict) SetTextColor(dc, Amber);
            SetBkColor(dc, Field); return field_brush;
        }
        RECT r; GetWindowRect(handle, &r); MapWindowPoints(nullptr, window, reinterpret_cast<POINT*>(&r), 2);
        const bool in_card = r.top >= px(130) && r.bottom <= px(450);
        if (handle == mode) SetTextColor(dc, auto_on ? Amber : Accent);
        else if (handle == activity_value) SetTextColor(dc, live ? Accent : Muted);
        else if (handle == status) SetTextColor(dc, message_error ? Amber : Muted);
        else for (const auto& item : children) if (item->window == handle && item->font == 1) { SetTextColor(dc, Muted); break; }
        SetBkColor(dc, in_card ? Card : Background); return in_card ? card_brush : background_brush;
    }
    static LRESULT CALLBACK child_procedure(HWND hwnd, UINT message, WPARAM w, LPARAM l) {
        auto item = static_cast<Child*>(GetPropW(hwnd, L"WCFI.ControlNode.Child"));
        if (!item) return DefWindowProcW(hwnd, message, w, l);
        auto self = item->view;
        if (message == WM_GETDLGCODE) return CallWindowProcW(item->previous, hwnd, message, w, l) | DLGC_WANTTAB | DLGC_WANTALLKEYS;
        if (message == WM_KEYDOWN) {
            if (w == VK_TAB) { SetFocus(GetNextDlgTabItem(self->window, hwnd, (GetKeyState(VK_SHIFT) & 0x8000) != 0)); return 0; }
            if (item->row >= 0 && w == VK_ESCAPE) { self->restore(item->row); return 0; }
            if (item->edit && w == VK_RETURN) { self->apply(item->row); return 0; }
            if (item->button && w == VK_RETURN) {
                SendMessageW(self->window, WM_COMMAND, MAKEWPARAM(GetDlgCtrlID(hwnd), BN_CLICKED), reinterpret_cast<LPARAM>(hwnd));
                return 0;
            }
        }
        if (message == WM_CHAR && (w == VK_RETURN || w == VK_ESCAPE || w == VK_TAB)) return 0;
        if (message == WM_NCDESTROY) {
            const auto previous = item->previous;
            RemovePropW(hwnd, L"WCFI.ControlNode.Child"); return CallWindowProcW(previous, hwnd, message, w, l);
        }
        return CallWindowProcW(item->previous, hwnd, message, w, l);
    }
    static LRESULT CALLBACK procedure(HWND hwnd, UINT message, WPARAM w, LPARAM l) {
        auto self = reinterpret_cast<NativeView*>(GetWindowLongPtrW(hwnd, GWLP_USERDATA));
        if (message == WM_NCCREATE) {
            self = static_cast<NativeView*>(reinterpret_cast<CREATESTRUCTW*>(l)->lpCreateParams);
            SetWindowLongPtrW(hwnd, GWLP_USERDATA, reinterpret_cast<LONG_PTR>(self)); self->window = hwnd;
        }
        if (!self) return DefWindowProcW(hwnd, message, w, l);
        switch (message) {
        case WM_CREATE: self->create(); return 0;
        case WM_ERASEBKGND: return 1;
        case WM_PAINT: self->paint(); return 0;
        case WM_TIMER: if (w == 1) self->synchronize(); return 0;
        case WM_CTLCOLOREDIT: return reinterpret_cast<LRESULT>(self->control_color(reinterpret_cast<HDC>(w), reinterpret_cast<HWND>(l), true));
        case WM_CTLCOLORSTATIC: return reinterpret_cast<LRESULT>(self->control_color(reinterpret_cast<HDC>(w), reinterpret_cast<HWND>(l), false));
        case WM_DRAWITEM: self->draw_button(*reinterpret_cast<DRAWITEMSTRUCT*>(l)); return TRUE;
        case WM_COMMAND: {
            const int id = LOWORD(w), event = HIWORD(w);
            if (id >= EditBase && id < EditBase + 5) {
                auto& row = self->rows[id-EditBase];
                if (event == EN_SETFOCUS && !row.dirty) {
                    row.expected = self->owner->getParamNormalized(row.id);
                    self->updating = true; set_text(row.edit, number(plain(row.id, row.expected), 2)); self->updating = false;
                }
                if (event == EN_CHANGE && !self->updating) row.dirty = true;
                return 0;
            }
            if (event != BN_CLICKED) break;
            if (id == AutoButton) self->toggle_auto();
            else if (id == TrimDown) self->nudge(-1);
            else if (id == TrimUp) self->nudge(1);
            else if (id >= ApplyBase && id < ApplyBase + 5) self->apply(id-ApplyBase);
            return 0;
        }
        case WM_DESTROY:
            KillTimer(hwnd, 1); for (auto font : self->fonts) if (font) DeleteObject(font);
            self->fonts.fill(nullptr); return 0;
        case WM_NCDESTROY:
            self->window = nullptr; SetWindowLongPtrW(hwnd, GWLP_USERDATA, 0); return DefWindowProcW(hwnd, message, w, l);
        }
        return DefWindowProcW(hwnd, message, w, l);
    }
};
}

IPlugView* create_control_editor(Controller* owner) {
    return owner ? static_cast<IPlugView*>(new NativeView(owner)) : nullptr;
}
}
