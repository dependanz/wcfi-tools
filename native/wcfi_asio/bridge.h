/* WCFI native audio bridge, protocol v1. Windows x64 only. */
#pragma once
#include <windows.h>
#include <stdint.h>

#define WCFI_CHANNELS 32
#define WCFI_RATE 48000
#define WCFI_CAPACITY 32768
#define WCFI_MAGIC 0x57434649u
#ifdef WCFI_TEST_NAMESPACE
#define WCFI_NAMESPACE L"Local\\WCFI.Audio.Test.v2."
#else
#define WCFI_NAMESPACE L"Local\\WCFI.Audio.v2."
#endif
#define WCFI_MAPPING WCFI_NAMESPACE L"Emulator"

/* Only the native functions below publish ring positions. Python never writes
   shared counters: aligned Interlocked operations supply the memory barriers. */
typedef struct {
    uint32_t magic, version, rate, channels, capacity, blocksize;
    volatile LONG running, host_pid;
    volatile LONG64 input_write, input_read, output_write, output_read;
    volatile LONG64 underruns, output_drops, late_callbacks, callbacks;
    volatile LONG64 sample_position;
    volatile LONG generation;
    volatile LONG service_pid, service_active;
    uint32_t reserved;
    volatile LONG64 heartbeat_ms;
    float input[WCFI_CAPACITY][WCFI_CHANNELS];
    float output[WCFI_CAPACITY][WCFI_CHANNELS];
} WcfiShared;

typedef struct {
    HANDLE mapping, feed_owner, capture_owner, service_owner;
    WcfiShared *shared;
    LONG generation;
    uint32_t roles;
} WcfiBridge;
typedef struct {
    uint32_t version, rate, channels, capacity, blocksize, running, host_pid, generation;
    uint64_t input_queued, output_queued, underruns, output_drops, late_callbacks, callbacks;
    uint64_t sample_position;
    uint32_t service_pid, service_active;
    uint64_t heartbeat_ms;
} WcfiStatus;

#ifndef WCFI_API
#define WCFI_API __declspec(dllexport)
#endif
/* roles: 0=status, 1=feed, 2=capture, 3=both. One producer and one consumer. */
WCFI_API WcfiBridge *WcfiOpenBridge(uint32_t roles);
WCFI_API void WcfiCloseBridge(WcfiBridge *bridge);
WCFI_API int WcfiFeed(WcfiBridge *bridge, const float *samples, uint32_t frames);
WCFI_API int WcfiReadOutput(WcfiBridge *bridge, float *samples, uint32_t frames);
WCFI_API int WcfiGetStatus(WcfiBridge *bridge, WcfiStatus *status);
WCFI_API void WcfiDiscardOutput(WcfiBridge *bridge);
/* Service owns availability; an ASIO host cannot create/start it implicitly. */
WCFI_API WcfiBridge *WcfiOpenService(void);
WCFI_API int WcfiServiceActive(WcfiBridge *bridge, uint32_t active);
WCFI_API void WcfiHeartbeat(WcfiBridge *bridge);
