/* WCFI Emulator: user-mode ASIO-compatible driver. No JACK, kernel component,
   disk reads, Python calls, allocation, or blocking locks in the audio cycle.
   cwASIO interface declarations are MIT licensed; see vendor/cwasio/LICENSE. */
#define COBJMACROS
#include <windows.h>
#include <objbase.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include "vendor/cwasio/cwASIO.h"
#include "bridge.h"

#if !defined(_WIN64)
#error WCFI Emulator v1 supports Windows x64 only
#endif

static const GUID WCFI_CLSID = {0xf9eb9932,0x2a49,0x48d7,{0x98,0x93,0x7d,0x4f,0xb4,0x8b,0x97,0x89}};
static const wchar_t *CLSID_TEXT = L"{F9EB9932-2A49-48D7-9893-7D4FB48B9789}";
static const wchar_t *DRIVER_NAME = L"WCFI Emulator";
static HMODULE module_handle;
static volatile LONG object_count, factory_refs, server_locks;

typedef struct {
    struct cwASIODriver base;
    volatile LONG references;
    HANDLE owner, thread, stop_event, timer;
    WcfiBridge bridge;
    struct cwASIOCallbacks callbacks;
    float *buffers[2][WCFI_CHANNELS][2]; /* direction (input=0), channel, half */
    unsigned char active[2][WCFI_CHANNELS];
    long blocksize;
    volatile LONG running;
    volatile LONG64 position, timestamp;
    int initialized;
    char error[124];
} Driver;

static LONG64 atomic_read(volatile LONG64 *p) { return InterlockedCompareExchange64(p, 0, 0); }
static cwASIOError CWASIO_METHOD stop_driver(struct cwASIODriver *base);
static cwASIOError CWASIO_METHOD dispose_buffers(struct cwASIODriver *base);

static int map_bridge(WcfiBridge *b, int create) {
    int fresh = 0;
    if (create) {
        b->mapping = CreateFileMappingW(INVALID_HANDLE_VALUE, NULL, PAGE_READWRITE, 0,
                                        sizeof(WcfiShared), WCFI_MAPPING);
        fresh = b->mapping && GetLastError() != ERROR_ALREADY_EXISTS;
    } else b->mapping = OpenFileMappingW(FILE_MAP_ALL_ACCESS, FALSE, WCFI_MAPPING);
    if (!b->mapping) return 0;
    b->shared = MapViewOfFile(b->mapping, FILE_MAP_ALL_ACCESS, 0, 0, sizeof(WcfiShared));
    if (!b->shared) { CloseHandle(b->mapping); b->mapping = NULL; return 0; }
    if (fresh) {
        memset(b->shared, 0, sizeof(WcfiShared));
        b->shared->version = 2; b->shared->rate = WCFI_RATE;
        b->shared->channels = WCFI_CHANNELS; b->shared->capacity = WCFI_CAPACITY;
        b->shared->blocksize = 512;
        MemoryBarrier(); b->shared->magic = WCFI_MAGIC;
    }
    if (b->shared->magic != WCFI_MAGIC || b->shared->version != 2 ||
        b->shared->rate != WCFI_RATE || b->shared->channels != WCFI_CHANNELS ||
        b->shared->capacity != WCFI_CAPACITY) {
        UnmapViewOfFile(b->shared); CloseHandle(b->mapping); memset(b, 0, sizeof(*b)); return 0;
    }
    return 1;
}
static void unmap_bridge(WcfiBridge *b) {
    if (b->shared) UnmapViewOfFile(b->shared);
    if (b->mapping) CloseHandle(b->mapping);
    if (b->feed_owner) CloseHandle(b->feed_owner);
    if (b->capture_owner) CloseHandle(b->capture_owner);
    if (b->service_owner) CloseHandle(b->service_owner);
    memset(b, 0, sizeof(*b));
}

static HANDLE exclusive_handle(const wchar_t *name) {
    HANDLE handle = CreateMutexW(NULL, FALSE, name);
    if (handle && GetLastError() == ERROR_ALREADY_EXISTS) { CloseHandle(handle); return NULL; }
    return handle;
}
static int service_alive(WcfiShared *s) {
    LONG64 heartbeat = atomic_read(&s->heartbeat_ms);
    ULONGLONG now = GetTickCount64();
    return s->service_pid && s->service_active && heartbeat > 0 &&
        now >= (ULONGLONG)heartbeat && now - heartbeat < 2000;
}
WCFI_API WcfiBridge *WcfiOpenService(void) {
    WcfiBridge *b = calloc(1, sizeof(*b));
    if (!b) return NULL;
    b->service_owner = exclusive_handle(WCFI_NAMESPACE L"Service");
    if (!b->service_owner || !map_bridge(b, 1)) { unmap_bridge(b); free(b); return NULL; }
    InterlockedExchange(&b->shared->service_active, 0);
    InterlockedExchange(&b->shared->service_pid, GetCurrentProcessId());
    InterlockedExchange64(&b->shared->heartbeat_ms, GetTickCount64());
    InterlockedIncrement(&object_count); return b;
}
WCFI_API int WcfiServiceActive(WcfiBridge *b, uint32_t active) {
    if (!b || !b->service_owner || active > 1) return 0;
    InterlockedExchange64(&b->shared->heartbeat_ms, GetTickCount64());
    InterlockedExchange(&b->shared->service_active, active); return 1;
}
WCFI_API void WcfiHeartbeat(WcfiBridge *b) {
    if (b && b->service_owner) InterlockedExchange64(&b->shared->heartbeat_ms, GetTickCount64());
}
WCFI_API WcfiBridge *WcfiOpenBridge(uint32_t roles) {
    if (roles > 3) return NULL;
    WcfiBridge *b = calloc(1, sizeof(*b));
    if (!b) return NULL;
    if (!map_bridge(b, 0)) { free(b); return NULL; }
    if (roles & 1) b->feed_owner = exclusive_handle(WCFI_NAMESPACE L"Feed");
    if (roles & 2) b->capture_owner = exclusive_handle(WCFI_NAMESPACE L"Capture");
    if (((roles & 1) && !b->feed_owner) || ((roles & 2) && !b->capture_owner) ||
        !b->shared->service_pid) { unmap_bridge(b); free(b); return NULL; }
    b->generation = b->shared->generation; b->roles = roles;
    InterlockedIncrement(&object_count);
    return b;
}
WCFI_API void WcfiCloseBridge(WcfiBridge *b) {
    if (b) {
        if (b->service_owner) {
            InterlockedExchange(&b->shared->service_active, 0);
            InterlockedExchange(&b->shared->service_pid, 0);
            InterlockedExchange64(&b->shared->heartbeat_ms, 0);
        }
        unmap_bridge(b); free(b); InterlockedDecrement(&object_count);
    }
}
WCFI_API int WcfiFeed(WcfiBridge *b, const float *samples, uint32_t frames) {
    if (!b || !(b->roles & 1) || !samples || frames > WCFI_CAPACITY ||
        !b->shared->host_pid || b->generation != b->shared->generation) return -1;
    WcfiShared *s = b->shared;
    if (!service_alive(s)) return 0;
    LONG64 write = atomic_read(&s->input_write), read = atomic_read(&s->input_read);
    if (write < read || write - read > WCFI_CAPACITY) return -1;
    if (frames > WCFI_CAPACITY - (uint64_t)(write - read)) return 0;
    for (uint32_t n = 0; n < frames * WCFI_CHANNELS; ++n)
        if (!isfinite(samples[n]) || fabsf(samples[n]) > 1.0f) return -1;
    for (uint32_t n = 0; n < frames; ++n)
        memcpy(s->input[(write + n) & (WCFI_CAPACITY - 1)], samples + n * WCFI_CHANNELS,
               sizeof(float) * WCFI_CHANNELS);
    InterlockedExchange64(&s->input_write, write + frames);
    return (int)frames;
}
WCFI_API int WcfiReadOutput(WcfiBridge *b, float *samples, uint32_t frames) {
    if (!b || !(b->roles & 2) || !samples || frames > WCFI_CAPACITY ||
        !b->shared->host_pid || b->generation != b->shared->generation) return -1;
    WcfiShared *s = b->shared;
    LONG64 write = atomic_read(&s->output_write), read = atomic_read(&s->output_read);
    if (write < read || write - read > WCFI_CAPACITY) return -1;
    if (frames > (uint64_t)(write - read)) frames = (uint32_t)(write - read);
    for (uint32_t n = 0; n < frames; ++n)
        memcpy(samples + n * WCFI_CHANNELS, s->output[(read + n) & (WCFI_CAPACITY - 1)],
               sizeof(float) * WCFI_CHANNELS);
    InterlockedExchange64(&s->output_read, read + frames);
    return (int)frames;
}
WCFI_API void WcfiDiscardOutput(WcfiBridge *b) {
    if (b && (b->roles & 2) && b->generation == b->shared->generation)
        InterlockedExchange64(&b->shared->output_read, atomic_read(&b->shared->output_write));
}
WCFI_API int WcfiGetStatus(WcfiBridge *b, WcfiStatus *v) {
    if (!b || !v) return 0;
    WcfiShared *s = b->shared;
    memset(v, 0, sizeof(*v));
    v->version = s->version; v->rate = s->rate; v->channels = s->channels;
    v->capacity = s->capacity; v->blocksize = s->blocksize;
    v->running = s->running; v->host_pid = s->host_pid;
    v->generation = s->generation;
    v->service_pid = s->service_pid; v->service_active = service_alive(s);
    v->heartbeat_ms = atomic_read(&s->heartbeat_ms);
    LONG64 iw = atomic_read(&s->input_write), ir = atomic_read(&s->input_read);
    LONG64 ow = atomic_read(&s->output_write), or = atomic_read(&s->output_read);
    v->input_queued = iw >= ir ? iw - ir : 0;
    v->output_queued = ow >= or ? ow - or : 0;
    v->underruns = atomic_read(&s->underruns); v->output_drops = atomic_read(&s->output_drops);
    v->late_callbacks = atomic_read(&s->late_callbacks); v->callbacks = atomic_read(&s->callbacks);
    v->sample_position = atomic_read(&s->sample_position);
    return 1;
}

static DWORD WINAPI audio_thread(void *context) {
    Driver *d = context;
    WcfiShared *s = d->bridge.shared;
    LARGE_INTEGER freq, start, now;
    QueryPerformanceFrequency(&freq); QueryPerformanceCounter(&start);
    SetThreadPriority(GetCurrentThread(), THREAD_PRIORITY_HIGHEST);
    HANDLE waits[2] = {d->stop_event, d->timer};
    int half = 0;
    uint64_t ticks = 0;
    while (WaitForSingleObject(d->stop_event, 0) == WAIT_TIMEOUT) {
        LONG64 read = atomic_read(&s->input_read), write = atomic_read(&s->input_write);
        long available = write >= read && write - read <= WCFI_CAPACITY ? (long)(write - read) : 0;
        long take = available < d->blocksize ? available : d->blocksize;
        int enabled = service_alive(s);
        if (!enabled) { read = write; take = 0; }
        for (int ch = 0; ch < WCFI_CHANNELS; ++ch) {
            float *in = d->buffers[0][ch][half], *out = d->buffers[1][ch][half];
            if (in) {
                for (long n = 0; n < take; ++n) in[n] = s->input[(read + n) & (WCFI_CAPACITY - 1)][ch];
                memset(in + take, 0, (d->blocksize - take) * sizeof(float));
            }
            if (out) memset(out, 0, d->blocksize * sizeof(float));
        }
        InterlockedExchange64(&s->input_read, read + take);
        if (enabled && take < d->blocksize) InterlockedIncrement64(&s->underruns);
        QueryPerformanceCounter(&now);
        InterlockedExchange64(&d->timestamp, (now.QuadPart / freq.QuadPart) * 1000000000LL +
            (now.QuadPart % freq.QuadPart) * 1000000000LL / freq.QuadPart);
        d->callbacks.bufferSwitch(half, ASIOTrue);
        LONG64 ow = atomic_read(&s->output_write), or = atomic_read(&s->output_read);
        if (ow >= or && ow - or <= WCFI_CAPACITY - d->blocksize) {
            for (long n = 0; n < d->blocksize; ++n) {
                float *frame = s->output[(ow + n) & (WCFI_CAPACITY - 1)];
                for (int ch = 0; ch < WCFI_CHANNELS; ++ch)
                    frame[ch] = d->buffers[1][ch][half] ? d->buffers[1][ch][half][n] : 0;
            }
            InterlockedExchange64(&s->output_write, ow + d->blocksize);
        } else InterlockedIncrement64(&s->output_drops);
        InterlockedExchangeAdd64(&d->position, d->blocksize);
        InterlockedExchange64(&s->sample_position, atomic_read(&d->position));
        InterlockedIncrement64(&s->callbacks);
        half ^= 1; ++ticks;
        uint64_t elapsed = ticks * d->blocksize;
        LONGLONG deadline = start.QuadPart + (LONGLONG)(elapsed / WCFI_RATE * freq.QuadPart +
            elapsed % WCFI_RATE * freq.QuadPart / WCFI_RATE);
        QueryPerformanceCounter(&now);
        if (now.QuadPart >= deadline) {
            InterlockedIncrement64(&s->late_callbacks);
            /* Do not emit a burst to "catch up" after a scheduling stall. */
            start.QuadPart = now.QuadPart; ticks = 0;
            continue;
        }
        LARGE_INTEGER due;
        due.QuadPart = -((deadline - now.QuadPart) * 10000000LL / freq.QuadPart);
        if (due.QuadPart == 0) due.QuadPart = -1;
        if (!SetWaitableTimer(d->timer, &due, 0, NULL, NULL, FALSE)) break;
        if (WaitForMultipleObjects(2, waits, FALSE, INFINITE) != WAIT_OBJECT_0 + 1) break;
    }
    InterlockedExchange(&s->running, 0);
    return 0;
}

static unsigned long CWASIO_METHOD add_ref(struct cwASIODriver *b) {
    return InterlockedIncrement(&((Driver *)b)->references);
}
static long CWASIO_METHOD query(struct cwASIODriver *b, const cwASIOGUID *id, void **out) {
    if (!out) return E_POINTER;
    *out = NULL;
    if (!id || (!IsEqualGUID(id, &WCFI_CLSID) && !IsEqualGUID(id, &IID_IUnknown))) return E_NOINTERFACE;
    *out = b; add_ref(b); return S_OK;
}
static unsigned long CWASIO_METHOD release_driver(struct cwASIODriver *b) {
    Driver *d = (Driver *)b;
    LONG refs = InterlockedDecrement(&d->references);
    if (!refs) {
        /* A host must release on its control thread, never from its own callback.
           Refuse the invalid release rather than freeing a live callback stack. */
        if (dispose_buffers(b) != ASE_OK) { InterlockedIncrement(&d->references); return 1; }
        if (d->bridge.shared) InterlockedExchange(&d->bridge.shared->host_pid, 0);
        unmap_bridge(&d->bridge);
        if (d->owner) CloseHandle(d->owner);
        if (d->stop_event) CloseHandle(d->stop_event);
        if (d->timer) CloseHandle(d->timer);
        free(d); InterlockedDecrement(&object_count);
    }
    return refs;
}
static cwASIOBool CWASIO_METHOD init_driver(struct cwASIODriver *b, void *sys) {
    (void)sys; Driver *d = (Driver *)b;
    if (d->initialized) return ASIOTrue;
    d->owner = CreateMutexW(NULL, FALSE, WCFI_NAMESPACE L"Host");
    if (!d->owner || GetLastError() == ERROR_ALREADY_EXISTS) {
        if (d->owner) { CloseHandle(d->owner); d->owner = NULL; }
        strcpy(d->error, "WCFI Emulator is already open in another host"); return ASIOFalse;
    }
    if (!map_bridge(&d->bridge, 0)) {
        CloseHandle(d->owner); d->owner = NULL;
        strcpy(d->error, "Start WCFI Audio Emulator before selecting this ASIO device"); return ASIOFalse;
    }
    WcfiShared *s = d->bridge.shared;
    if (!service_alive(s)) {
        unmap_bridge(&d->bridge); CloseHandle(d->owner); d->owner = NULL;
        strcpy(d->error, "WCFI Emulator is stopped; start it in the app or CLI"); return ASIOFalse;
    }
    /* No stream exists yet. A retained feeder must close before reinitializing
       a DAW; reset state only at this explicit device lifecycle boundary. */
    InterlockedIncrement(&s->generation);
    InterlockedExchange64(&s->input_read, atomic_read(&s->input_write));
    InterlockedExchange64(&s->output_read, atomic_read(&s->output_write));
    s->host_pid = GetCurrentProcessId(); d->initialized = 1;
    return ASIOTrue;
}
static void CWASIO_METHOD name_driver(struct cwASIODriver *b, char name[32]) { (void)b; strcpy(name, "WCFI Emulator"); }
static long CWASIO_METHOD version_driver(struct cwASIODriver *b) { (void)b; return 2; }
static void CWASIO_METHOD error_driver(struct cwASIODriver *b, char message[124]) { strcpy(message, ((Driver *)b)->error); }
static cwASIOError CWASIO_METHOD start_driver(struct cwASIODriver *b) {
    Driver *d = (Driver *)b;
    if (!d->initialized || !d->blocksize || !d->callbacks.bufferSwitch) return ASE_InvalidMode;
    if (!service_alive(d->bridge.shared)) return ASE_InvalidMode;
    if (d->running) return ASE_OK;
    ResetEvent(d->stop_event); InterlockedExchange64(&d->position, 0);
    InterlockedExchange(&d->running, 1); d->bridge.shared->blocksize = d->blocksize;
    InterlockedExchange(&d->bridge.shared->running, 1);
    d->thread = CreateThread(NULL, 0, audio_thread, d, 0, NULL);
    if (!d->thread) {
        InterlockedExchange(&d->running, 0); InterlockedExchange(&d->bridge.shared->running, 0);
        return ASE_HWMalfunction;
    }
    return ASE_OK;
}
static cwASIOError CWASIO_METHOD stop_driver(struct cwASIODriver *b) {
    Driver *d = (Driver *)b;
    if (d->thread) {
        /* Never free buffers until a host callback in progress has returned. */
        if (GetThreadId(d->thread) == GetCurrentThreadId()) return ASE_InvalidMode;
        SetEvent(d->stop_event); WaitForSingleObject(d->thread, INFINITE);
        CloseHandle(d->thread); d->thread = NULL;
    }
    InterlockedExchange(&d->running, 0);
    if (d->bridge.shared) InterlockedExchange(&d->bridge.shared->running, 0);
    return ASE_OK;
}
static cwASIOError CWASIO_METHOD channels_driver(struct cwASIODriver *b, long *in, long *out) {
    (void)b; if (!in || !out) return ASE_InvalidParameter;
    *in = *out = WCFI_CHANNELS; return ASE_OK;
}
static cwASIOError CWASIO_METHOD latency_driver(struct cwASIODriver *b, long *in, long *out) {
    if (!in || !out) return ASE_InvalidParameter;
    *in = *out = ((Driver *)b)->blocksize ? ((Driver *)b)->blocksize : 512; return ASE_OK;
}
static cwASIOError CWASIO_METHOD buffer_size(struct cwASIODriver *b, long *min, long *max, long *pref, long *gran) {
    (void)b; if (!min || !max || !pref || !gran) return ASE_InvalidParameter;
    *min = 128; *max = 2048; *pref = 512; *gran = -1; return ASE_OK;
}
static cwASIOError CWASIO_METHOD can_rate(struct cwASIODriver *b, double rate) { (void)b; return rate == WCFI_RATE ? ASE_OK : ASE_NoClock; }
static cwASIOError CWASIO_METHOD get_rate(struct cwASIODriver *b, double *rate) { (void)b; if (!rate) return ASE_InvalidParameter; *rate = WCFI_RATE; return ASE_OK; }
static cwASIOError CWASIO_METHOD set_rate(struct cwASIODriver *b, double rate) { return can_rate(b, rate); }
static cwASIOError CWASIO_METHOD clocks_driver(struct cwASIODriver *b, struct cwASIOClockSource *clocks, long *count) {
    (void)b; if (!count || !clocks || *count < 1) return ASE_InvalidParameter;
    memset(clocks, 0, sizeof(*clocks)); clocks->associatedChannel = clocks->associatedGroup = -1;
    clocks->isCurrentSource = ASIOTrue; strcpy(clocks->name, "WCFI Internal 48 kHz"); *count = 1; return ASE_OK;
}
static cwASIOError CWASIO_METHOD set_clock(struct cwASIODriver *b, long index) { (void)b; return index == 0 ? ASE_OK : ASE_InvalidParameter; }
static cwASIOError CWASIO_METHOD position_driver(struct cwASIODriver *b, cwASIOSamples *pos, cwASIOTimeStamp *stamp) {
    if (!pos || !stamp) return ASE_InvalidParameter;
    uint64_t p = atomic_read(&((Driver *)b)->position), t = atomic_read(&((Driver *)b)->timestamp);
    pos->lo = (uint32_t)p; pos->hi = (uint32_t)(p >> 32);
    stamp->lo = (uint32_t)t; stamp->hi = (uint32_t)(t >> 32); return ASE_OK;
}
static cwASIOError CWASIO_METHOD info_driver(struct cwASIODriver *b, struct cwASIOChannelInfo *info) {
    if (!info || info->channel < 0 || info->channel >= WCFI_CHANNELS ||
        (info->isInput != ASIOTrue && info->isInput != ASIOFalse)) return ASE_InvalidParameter;
    Driver *d = (Driver *)b;
    info->isActive = d->active[info->isInput ? 0 : 1][info->channel];
    info->channelGroup = 0; info->type = ASIOSTFloat32LSB;
    snprintf(info->name, sizeof(info->name), "%s %02ld", info->isInput ? "WCFI Input" : "WCFI Output", info->channel + 1);
    return ASE_OK;
}
static cwASIOError CWASIO_METHOD create_buffers(struct cwASIODriver *b, struct cwASIOBufferInfo *info,
        long count, long size, const struct cwASIOCallbacks *cb) {
    Driver *d = (Driver *)b;
    if (!d->initialized || d->running || d->blocksize) return ASE_InvalidMode;
    if (!cb || !cb->bufferSwitch || !info || count < 1 || count > 64 ||
        size < 128 || size > 2048 || (size & (size - 1))) return ASE_InvalidParameter;
    unsigned char used[2][WCFI_CHANNELS] = {{0}};
    for (long n = 0; n < count; ++n) {
        int direction = info[n].isInput ? 0 : 1; long ch = info[n].channelNum;
        if ((info[n].isInput != ASIOTrue && info[n].isInput != ASIOFalse) ||
            ch < 0 || ch >= WCFI_CHANNELS || used[direction][ch]) return ASE_InvalidParameter;
        used[direction][ch] = 1;
    }
    for (long n = 0; n < count; ++n) {
        int direction = info[n].isInput ? 0 : 1; long ch = info[n].channelNum;
        for (int half = 0; half < 2; ++half) {
            d->buffers[direction][ch][half] = calloc(size, sizeof(float));
            if (!d->buffers[direction][ch][half]) { dispose_buffers(b); return ASE_NoMemory; }
        }
        d->active[direction][ch] = 1;
    }
    for (long n = 0; n < count; ++n)
        for (int half = 0; half < 2; ++half)
            info[n].buffers[half] = d->buffers[info[n].isInput ? 0 : 1][info[n].channelNum][half];
    d->callbacks = *cb; d->blocksize = size; return ASE_OK;
}
static cwASIOError CWASIO_METHOD dispose_buffers(struct cwASIODriver *b) {
    Driver *d = (Driver *)b;
    if (stop_driver(b) != ASE_OK) return ASE_InvalidMode;
    for (int dir = 0; dir < 2; ++dir) for (int ch = 0; ch < WCFI_CHANNELS; ++ch)
        for (int half = 0; half < 2; ++half) { free(d->buffers[dir][ch][half]); d->buffers[dir][ch][half] = NULL; }
    memset(d->active, 0, sizeof(d->active)); memset(&d->callbacks, 0, sizeof(d->callbacks));
    d->blocksize = 0; return ASE_OK;
}
static cwASIOError CWASIO_METHOD panel_driver(struct cwASIODriver *b) {
    (void)b; MessageBoxW(NULL, L"WCFI Emulator prototype\n32 inputs / 32 outputs, 48 kHz\n"
        L"128–2048 frames, 32-bit float\nRun wcfi audio emulate start to supply raw inputs.\n"
        L"No physical speaker output is connected by this driver.", DRIVER_NAME, MB_OK); return ASE_OK;
}
static cwASIOError CWASIO_METHOD future_driver(struct cwASIODriver *b, long sel, void *p) {
    (void)b;
    if ((sel == kAsioGetIoFormat || sel == kAsioCanDoIoFormat || sel == kAsioSetIoFormat) && p) {
        struct cwASIOIoFormat *format = p;
        if (sel == kAsioGetIoFormat) format->FormatType = kASIOPCMFormat;
        return format->FormatType == kASIOPCMFormat ? ASE_SUCCESS : ASE_InvalidParameter;
    }
    /* Use standard bufferSwitch/getSamplePosition; no unsupported timecode promises. */
    return ASE_NotPresent;
}
static cwASIOError CWASIO_METHOD ready_driver(struct cwASIODriver *b) { (void)b; return ASE_NotPresent; }
static const struct cwASIODriverVtbl driver_vtable = {
    query, add_ref, release_driver, init_driver, name_driver, version_driver, error_driver,
    start_driver, stop_driver, channels_driver, latency_driver, buffer_size, can_rate, get_rate,
    set_rate, clocks_driver, set_clock, position_driver, info_driver, create_buffers,
    dispose_buffers, panel_driver, future_driver, ready_driver
};

static HRESULT STDMETHODCALLTYPE factory_query(IClassFactory *f, REFIID id, void **out) {
    if (!out) return E_POINTER; *out = NULL;
    if (!IsEqualGUID(id, &IID_IUnknown) && !IsEqualGUID(id, &IID_IClassFactory)) return E_NOINTERFACE;
    *out = f; InterlockedIncrement(&factory_refs); return S_OK;
}
static ULONG STDMETHODCALLTYPE factory_add(IClassFactory *f) { (void)f; return InterlockedIncrement(&factory_refs); }
static ULONG STDMETHODCALLTYPE factory_release(IClassFactory *f) { (void)f; return InterlockedDecrement(&factory_refs); }
static HRESULT STDMETHODCALLTYPE factory_create(IClassFactory *f, IUnknown *outer, REFIID id, void **out) {
    (void)f; if (!out) return E_POINTER; *out = NULL;
    if (outer) return CLASS_E_NOAGGREGATION;
    Driver *d = calloc(1, sizeof(*d)); if (!d) return E_OUTOFMEMORY;
    d->base.lpVtbl = &driver_vtable; d->references = 1;
    d->stop_event = CreateEventW(NULL, TRUE, FALSE, NULL);
    d->timer = CreateWaitableTimerExW(NULL, NULL, 0x00000002, TIMER_ALL_ACCESS);
    if (!d->timer) d->timer = CreateWaitableTimerW(NULL, FALSE, NULL);
    InterlockedIncrement(&object_count);
    if (!d->stop_event || !d->timer) { release_driver(&d->base); return E_OUTOFMEMORY; }
    HRESULT result = query(&d->base, id, out); release_driver(&d->base); return result;
}
static HRESULT STDMETHODCALLTYPE factory_lock(IClassFactory *f, BOOL lock) {
    (void)f; if (lock) InterlockedIncrement(&server_locks); else InterlockedDecrement(&server_locks); return S_OK;
}
static IClassFactoryVtbl factory_vtable = {factory_query, factory_add, factory_release, factory_create, factory_lock};
static IClassFactory factory = {&factory_vtable};
__declspec(dllexport) HRESULT WINAPI DllGetClassObject(REFCLSID clsid, REFIID iid, void **out) {
    if (!out) return E_POINTER; *out = NULL;
    if (!IsEqualGUID(clsid, &WCFI_CLSID)) return CLASS_E_CLASSNOTAVAILABLE;
    return factory_query(&factory, iid, out);
}
__declspec(dllexport) HRESULT WINAPI DllCanUnloadNow(void) {
    return !object_count && !factory_refs && !server_locks ? S_OK : S_FALSE;
}
static LONG registry_string(const wchar_t *key, const wchar_t *name, const wchar_t *value) {
    HKEY handle; LONG result = RegCreateKeyExW(HKEY_LOCAL_MACHINE, key, 0, NULL, 0,
                                             KEY_WRITE | KEY_WOW64_64KEY, NULL, &handle, NULL);
    if (result != ERROR_SUCCESS) return result;
    result = RegSetValueExW(handle, name, 0, REG_SZ, (const BYTE *)value, (wcslen(value) + 1) * sizeof(wchar_t));
    RegCloseKey(handle); return result;
}
static LONG key_status(const wchar_t *key) {
    HKEY handle;
    LONG result = RegOpenKeyExW(HKEY_LOCAL_MACHINE, key, 0, KEY_READ | KEY_WOW64_64KEY, &handle);
    if (result == ERROR_SUCCESS) RegCloseKey(handle);
    return result;
}
static int registry_matches(const wchar_t *key, const wchar_t *name, const wchar_t *expected) {
    HKEY handle;
    if (RegOpenKeyExW(HKEY_LOCAL_MACHINE, key, 0, KEY_READ | KEY_WOW64_64KEY, &handle)) return 0;
    wchar_t value[32768]; DWORD bytes = sizeof(value);
    LONG result = RegGetValueW(handle, NULL, name, RRF_RT_REG_SZ, NULL, value, &bytes);
    RegCloseKey(handle);
    return result == ERROR_SUCCESS && _wcsicmp(value, expected) == 0;
}
static LONG new_registry_key(const wchar_t *key) {
    HKEY handle; DWORD disposition;
    LONG result = RegCreateKeyExW(HKEY_LOCAL_MACHINE, key, 0, NULL, 0,
        KEY_WRITE | KEY_WOW64_64KEY, NULL, &handle, &disposition);
    if (result != ERROR_SUCCESS) return result;
    RegCloseKey(handle);
    return disposition == REG_CREATED_NEW_KEY ? ERROR_SUCCESS : ERROR_ALREADY_EXISTS;
}
__declspec(dllexport) HRESULT WINAPI DllRegisterServer(void) {
    wchar_t path[32768], key[256], root[256];
    DWORD length = GetModuleFileNameW(module_handle, path, 32768);
    if (!length || length >= 32768) return E_FAIL;
    swprintf(root, 256, L"SOFTWARE\\Classes\\CLSID\\%ls", CLSID_TEXT);
    swprintf(key, 256, L"SOFTWARE\\Classes\\CLSID\\%ls\\InprocServer32", CLSID_TEXT);
    const wchar_t *asio = L"SOFTWARE\\ASIO\\WCFI Emulator";
    LONG a = key_status(root), b = key_status(asio);
    if (a == ERROR_SUCCESS || b == ERROR_SUCCESS) {
        /* Re-running is harmless. Never replace another build/registration. */
        return registry_matches(key, NULL, path) && registry_matches(key, L"ThreadingModel", L"Both") &&
            registry_matches(asio, L"CLSID", CLSID_TEXT) && registry_matches(asio, L"Description", DRIVER_NAME)
            ? S_OK : HRESULT_FROM_WIN32(ERROR_ALREADY_EXISTS);
    }
    if (a != ERROR_FILE_NOT_FOUND) return HRESULT_FROM_WIN32(a);
    if (b != ERROR_FILE_NOT_FOUND) return HRESULT_FROM_WIN32(b);
    LONG result = new_registry_key(root);
    if (result != ERROR_SUCCESS) return HRESULT_FROM_WIN32(result);
    result = new_registry_key(asio);
    if (result != ERROR_SUCCESS) { RegDeleteTreeW(HKEY_LOCAL_MACHINE, root); return HRESULT_FROM_WIN32(result); }
    result = registry_string(key, NULL, path);
    if (result == ERROR_SUCCESS) result = registry_string(key, L"ThreadingModel", L"Both");
    if (result == ERROR_SUCCESS) result = registry_string(asio, L"CLSID", CLSID_TEXT);
    if (result == ERROR_SUCCESS) result = registry_string(asio, L"Description", DRIVER_NAME);
    if (result != ERROR_SUCCESS) {
        RegDeleteTreeW(HKEY_LOCAL_MACHINE, asio); RegDeleteTreeW(HKEY_LOCAL_MACHINE, root);
    }
    return result == ERROR_SUCCESS ? S_OK : HRESULT_FROM_WIN32(result);
}
__declspec(dllexport) HRESULT WINAPI DllUnregisterServer(void) {
    /* Fixed, WCFI-owned keys only. No wildcard or caller-supplied registry paths. */
    wchar_t key[256], inproc[256], path[32768];
    swprintf(key, 256, L"SOFTWARE\\Classes\\CLSID\\%ls", CLSID_TEXT);
    swprintf(inproc, 256, L"%ls\\InprocServer32", key);
    DWORD length = GetModuleFileNameW(module_handle, path, 32768);
    if (!length || length >= 32768) return E_FAIL;
    const wchar_t *asio = L"SOFTWARE\\ASIO\\WCFI Emulator";
    LONG a = key_status(asio), b = key_status(key);
    if (a == ERROR_FILE_NOT_FOUND && b == ERROR_FILE_NOT_FOUND) return S_OK;
    if (a != ERROR_SUCCESS && a != ERROR_FILE_NOT_FOUND) return HRESULT_FROM_WIN32(a);
    if (b != ERROR_SUCCESS && b != ERROR_FILE_NOT_FOUND) return HRESULT_FROM_WIN32(b);
    if ((a == ERROR_SUCCESS && !registry_matches(asio, L"CLSID", CLSID_TEXT)) ||
        (b == ERROR_SUCCESS && !registry_matches(inproc, NULL, path))) return HRESULT_FROM_WIN32(ERROR_ALREADY_EXISTS);
    a = RegDeleteTreeW(HKEY_LOCAL_MACHINE, asio);
    b = RegDeleteTreeW(HKEY_LOCAL_MACHINE, key);
    if (a != ERROR_SUCCESS && a != ERROR_FILE_NOT_FOUND) return HRESULT_FROM_WIN32(a);
    if (b != ERROR_SUCCESS && b != ERROR_FILE_NOT_FOUND) return HRESULT_FROM_WIN32(b);
    return S_OK;
}
BOOL WINAPI DllMain(HINSTANCE module, DWORD reason, LPVOID reserved) {
    (void)reserved;
    if (reason == DLL_PROCESS_ATTACH) { module_handle = module; DisableThreadLibraryCalls(module); }
    return TRUE;
}
