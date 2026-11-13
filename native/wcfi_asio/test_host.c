/* Local integration tests. Never opens a physical audio endpoint. */
#define COBJMACROS
#include <windows.h>
#include <objbase.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include "vendor/cwasio/cwASIO.h"
#define WCFI_API
#include "bridge.h"

static const GUID CLSID_WCFI = {0xf9eb9932,0x2a49,0x48d7,{0x98,0x93,0x7d,0x4f,0xb4,0x8b,0x97,0x89}};
static struct cwASIODriver *driver;
static struct cwASIOBufferInfo buffers[64];
static long blocksize;
static volatile LONG calls, bad_input;
static int external;
static wchar_t test_registry_key[160];
static HKEY isolated_registry;
static int registry_overridden;
static void clean_registry(void) {
    if (registry_overridden) RegOverridePredefKey(HKEY_LOCAL_MACHINE, NULL);
    registry_overridden = 0;
    if (isolated_registry) RegCloseKey(isolated_registry);
    isolated_registry = NULL;
    if (test_registry_key[0]) RegDeleteTreeW(HKEY_CURRENT_USER, test_registry_key);
    test_registry_key[0] = 0;
}
static void switched(long half, cwASIOBool direct) {
    (void)direct;
    for (long n = 0; n < blocksize; ++n) {
        if (!external && ((float *)buffers[0].buffers[half])[n] != 0) {
            for (int ch = 0; ch < 32; ++ch)
                if (((float *)buffers[ch].buffers[half])[n] != (ch + 1) / 64.0f)
                    InterlockedIncrement(&bad_input);
        }
        for (int ch = 0; ch < 32; ++ch) {
            int input = external ? (ch == 22 ? 0 : ch == 23 ? 1 : ch) : ch;
            ((float *)buffers[32 + ch].buffers[half])[n] =
                ((float *)buffers[input].buffers[half])[n] * .5f;
        }
    }
    InterlockedIncrement(&calls);
}
static void rate_changed(double rate) { (void)rate; }
static long message(long selector, long value, void *p, double *opt) {
    (void)selector; (void)value; (void)p; (void)opt; return 0;
}
static const struct cwASIOCallbacks callbacks = {switched, rate_changed, message, NULL};
#define CHECK(condition) do { if (!(condition)) { fprintf(stderr, "FAIL line %d: %s\n", __LINE__, #condition); exit(1); } } while (0)
#define LOAD(type, name) type name; { FARPROC address = GetProcAddress(module, #name); \
    _Static_assert(sizeof(name) == sizeof(address), "Windows function pointer size"); \
    memcpy(&name, &address, sizeof(name)); } CHECK(name)
typedef HRESULT (WINAPI *GetClass)(REFCLSID, REFIID, void **);
typedef HRESULT (WINAPI *CanUnload)(void);
typedef HRESULT (WINAPI *RegisterServer)(void);
typedef WcfiBridge *(*OpenBridge)(uint32_t);
typedef WcfiBridge *(*OpenAudioService)(void);
typedef int (*ServiceActive)(WcfiBridge *, uint32_t);
typedef void (*Heartbeat)(WcfiBridge *);
typedef void (*CloseBridge)(WcfiBridge *);
typedef int (*Feed)(WcfiBridge *, const float *, uint32_t);
typedef int (*ReadOutput)(WcfiBridge *, float *, uint32_t);
typedef int (*GetStatus)(WcfiBridge *, WcfiStatus *);
typedef void (*DiscardOutput)(WcfiBridge *);

static void configure(long size) {
    for (int ch = 0; ch < 32; ++ch) {
        buffers[ch].isInput = ASIOTrue; buffers[ch].channelNum = ch;
        buffers[32 + ch].isInput = ASIOFalse; buffers[32 + ch].channelNum = ch;
    }
    blocksize = size;
    CHECK(driver->lpVtbl->createBuffers(driver, buffers, 64, size, &callbacks) == ASE_OK);
}

int wmain(int argc, wchar_t **argv) {
    CHECK(argc == 2 || argc == 4);
    HMODULE module = LoadLibraryW(argv[1]); CHECK(module);
    LOAD(GetClass, DllGetClassObject); LOAD(CanUnload, DllCanUnloadNow);
    LOAD(OpenBridge, WcfiOpenBridge); LOAD(CloseBridge, WcfiCloseBridge);
    LOAD(Feed, WcfiFeed); LOAD(ReadOutput, WcfiReadOutput);
    LOAD(GetStatus, WcfiGetStatus); LOAD(DiscardOutput, WcfiDiscardOutput);
    LOAD(OpenAudioService, WcfiOpenService); LOAD(ServiceActive, WcfiServiceActive);
    LOAD(Heartbeat, WcfiHeartbeat);
    LOAD(RegisterServer, DllRegisterServer); LOAD(RegisterServer, DllUnregisterServer);
    CHECK(DllCanUnloadNow() == S_OK);
    if (argc == 2) {
        /* Test registration against a fresh private HKCU key. The
           HKLM override is process-local; installed drivers are never touched. */
        DWORD disposition;
        atexit(clean_registry);
        swprintf(test_registry_key, 160, L"Software\\WCFI-ASIO-test-%lu-%llu", GetCurrentProcessId(), GetTickCount64());
        CHECK(RegCreateKeyExW(HKEY_CURRENT_USER, test_registry_key, 0, NULL, REG_OPTION_NON_VOLATILE,
            KEY_ALL_ACCESS, NULL, &isolated_registry, &disposition) == ERROR_SUCCESS);
        CHECK(disposition == REG_CREATED_NEW_KEY);
        CHECK(RegOverridePredefKey(HKEY_LOCAL_MACHINE, isolated_registry) == ERROR_SUCCESS);
        registry_overridden = 1;
        CHECK(DllRegisterServer() == S_OK && DllRegisterServer() == S_OK);
        HKEY conflict;
        CHECK(RegOpenKeyExW(isolated_registry, L"SOFTWARE\\ASIO\\WCFI Emulator", 0, KEY_WRITE, &conflict) == ERROR_SUCCESS);
        const wchar_t *wrong = L"{00000000-0000-0000-0000-000000000000}";
        CHECK(RegSetValueExW(conflict, L"CLSID", 0, REG_SZ, (const BYTE *)wrong,
            (wcslen(wrong) + 1) * sizeof(wchar_t)) == ERROR_SUCCESS);
        CHECK(DllRegisterServer() != S_OK && DllUnregisterServer() != S_OK);
        const wchar_t *right = L"{F9EB9932-2A49-48D7-9893-7D4FB48B9789}";
        CHECK(RegSetValueExW(conflict, L"CLSID", 0, REG_SZ, (const BYTE *)right,
            (wcslen(right) + 1) * sizeof(wchar_t)) == ERROR_SUCCESS);
        RegCloseKey(conflict);
        CHECK(DllUnregisterServer() == S_OK && DllUnregisterServer() == S_OK);
        clean_registry();
        puts("{\"registration_isolation\":true,\"conflict_protection\":true}");
    }
    IClassFactory *factory = NULL;
    CHECK(DllGetClassObject(&CLSID_WCFI, &IID_IClassFactory, (void **)&factory) == S_OK);
    CHECK(IClassFactory_CreateInstance(factory, NULL, &CLSID_WCFI, (void **)&driver) == S_OK);
    int outside_service = argc == 4 && wcscmp(argv[2], L"--external-service") == 0;
    WcfiBridge *service = NULL;
    if (!outside_service) {
        /* A registered device must refuse to open while the emulator is offline. */
        CHECK(!driver->lpVtbl->init(driver, NULL));
        service = WcfiOpenService(); CHECK(service);
        CHECK(!WcfiOpenService());
        CHECK(!driver->lpVtbl->init(driver, NULL));
        CHECK(WcfiServiceActive(service, 1));
    }
    CHECK(driver->lpVtbl->init(driver, NULL));
    struct cwASIODriver *second = NULL;
    CHECK(IClassFactory_CreateInstance(factory, NULL, &CLSID_WCFI, (void **)&second) == S_OK);
    CHECK(!second->lpVtbl->init(second, NULL)); second->lpVtbl->release(second);
    long in, out; double rate;
    CHECK(driver->lpVtbl->getChannels(driver, &in, &out) == ASE_OK && in == 32 && out == 32);
    CHECK(driver->lpVtbl->getSampleRate(driver, &rate) == ASE_OK && rate == 48000);
    CHECK(driver->lpVtbl->setSampleRate(driver, 44100) != ASE_OK);
    struct cwASIOBufferInfo duplicate[2] = {{ASIOTrue, 0, {NULL, NULL}}, {ASIOTrue, 0, {NULL, NULL}}};
    CHECK(driver->lpVtbl->createBuffers(driver, duplicate, 2, 512, &callbacks) == ASE_InvalidParameter);
    struct cwASIOChannelInfo info = {0}; info.isInput = ASIOTrue;
    CHECK(driver->lpVtbl->getChannelInfo(driver, &info) == ASE_OK && info.type == ASIOSTFloat32LSB);

    if (argc == 4) {
        CHECK(wcscmp(argv[2], L"--serve") == 0 || outside_service);
        int seconds = _wtoi(argv[3]); CHECK(seconds >= 1 && seconds <= 60);
        external = 1; configure(512);
        CHECK(driver->lpVtbl->start(driver) == ASE_OK);
        puts("READY"); fflush(stdout);
        for (int n = 0; n < seconds * 10; ++n) { WcfiHeartbeat(service); Sleep(100); }
        CHECK(driver->lpVtbl->stop(driver) == ASE_OK);
        CHECK(driver->lpVtbl->disposeBuffers(driver) == ASE_OK);
    } else {
        WcfiBridge *bridge = WcfiOpenBridge(3); CHECK(bridge);
        CHECK(!WcfiOpenBridge(1) && !WcfiOpenBridge(2));
        WcfiBridge *status_only = WcfiOpenBridge(0); CHECK(status_only);
        float *source = calloc(4096 * 32, sizeof(float));
        float *output = calloc(WCFI_CAPACITY * 32, sizeof(float)); CHECK(source && output);
        for (int n = 0; n < 4096; ++n) for (int ch = 0; ch < 32; ++ch) source[n * 32 + ch] = (ch + 1) / 64.0f;
        CHECK(WcfiFeed(status_only, source, 1) == -1);
        CHECK(WcfiReadOutput(status_only, output, 1) == -1);
        WcfiCloseBridge(status_only);
        source[0] = NAN; CHECK(WcfiFeed(bridge, source, 1) == -1);
        source[0] = 1.01f; CHECK(WcfiFeed(bridge, source, 1) == -1); source[0] = 1 / 64.0f;
        long sizes[3] = {128, 512, 2048};
        unsigned long long validated = 0;
        for (int pass = 0; pass < 3; ++pass) {
            configure(sizes[pass]); WcfiDiscardOutput(bridge);
            WcfiStatus status; CHECK(WcfiGetStatus(bridge, &status));
            if (status.input_queued < 4096) CHECK(WcfiFeed(bridge, source, 4096) == 4096);
            CHECK(driver->lpVtbl->start(driver) == ASE_OK);
            ULONGLONG until = GetTickCount64() + 1200;
            while (GetTickCount64() < until) {
                WcfiHeartbeat(service);
                CHECK(WcfiGetStatus(bridge, &status));
                if (status.input_queued < 4096) CHECK(WcfiFeed(bridge, source, 4096) == 4096);
                int got = WcfiReadOutput(bridge, output, WCFI_CAPACITY); CHECK(got >= 0);
                for (int n = 0; n < got; ++n) {
                    if (output[n * 32] == 0) continue; /* Explicit underrun silence. */
                    for (int ch = 0; ch < 32; ++ch) CHECK(output[n * 32 + ch] == (ch + 1) / 128.0f);
                    ++validated;
                }
                Sleep(2);
            }
            CHECK(driver->lpVtbl->stop(driver) == ASE_OK);
            LONG stopped = calls; Sleep(20); CHECK(calls == stopped);
            CHECK(WcfiGetStatus(bridge, &status) && !status.running && status.sample_position > 0);
            printf("{\"blocksize\":%ld,\"callbacks\":%llu,\"underruns\":%llu,\"late_callbacks\":%llu}\n",
                blocksize, status.callbacks, status.underruns, status.late_callbacks);
            /* Repeated start/stop on the same buffers. */
            CHECK(driver->lpVtbl->start(driver) == ASE_OK); Sleep(25);
            CHECK(driver->lpVtbl->stop(driver) == ASE_OK);
            CHECK(driver->lpVtbl->disposeBuffers(driver) == ASE_OK);
        }
        CHECK(!bad_input && validated > 100000);
        printf("{\"passed\":true,\"validated_frames\":%llu,\"channels\":32}\n", validated);
        free(source); free(output); WcfiCloseBridge(bridge);
    }
    if (service) CHECK(WcfiServiceActive(service, 0));
    driver->lpVtbl->release(driver);
    if (service) {
        CHECK(IClassFactory_CreateInstance(factory, NULL, &CLSID_WCFI, (void **)&driver) == S_OK);
        CHECK(!driver->lpVtbl->init(driver, NULL)); driver->lpVtbl->release(driver);
    }
    WcfiCloseBridge(service); IClassFactory_Release(factory);
    CHECK(DllCanUnloadNow() == S_OK); FreeLibrary(module); return 0;
}
