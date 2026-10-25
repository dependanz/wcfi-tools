// WCFI Analysis Node - local observation transport, independent of ASIO.
#pragma once
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <cstdint>

namespace wcfi {
constexpr uint32_t NodeCapacity = 32768;
constexpr uint32_t NodeSlots = 128;
constexpr uint32_t NodeMagic = 0x57414e31;

struct NodeInfo {
    char id[40];
    uint32_t pid;
};
struct NodeStatus {
    uint32_t version, pid, channels, block_size, position, active, epoch, flags;
    double sample_rate;
    uint64_t written_frame, first_frame, callback_ns;
    int64_t host_sample, host_system_ns;
    char label[128];
};
struct SharedNode {
    uint32_t magic, version;
    volatile LONG64 sequence;
    volatile LONG64 meta_sequence;
    NodeStatus status;
    float audio[NodeCapacity][2];
};

class Sender {
public:
    Sender() = default;
    ~Sender();
    Sender(const Sender&) = delete;
    Sender& operator=(const Sender&) = delete;
    bool open(); // UI/setup thread only; failures never disable audio passthrough.
    void close();
    void configure(const char* label, uint32_t position);
    void setup(double rate, uint32_t maximum_block);
    void processing(bool active); // bounded; safe on the audio thread.
    template<class T> void publish(T* const* input, int channels, int frames,
                                   int64_t host_sample, int64_t system_ns, uint32_t flags);
    const char* id() const { return info.id; }
    bool available() const { return shared != nullptr; }
private:
    NodeInfo info {};
    HANDLE mapping {}, directory {}, mutex {};
    void* directory_view {};
    SharedNode* shared {};
    int slot {-1};
};
uint64_t clock_ns();
}

extern "C" {
__declspec(dllexport) uint64_t WcfiNodeClockNs();
__declspec(dllexport) int WcfiNodesEnumerate(wcfi::NodeInfo* infos, uint32_t capacity);
__declspec(dllexport) void* WcfiNodeOpen(const char* id);
__declspec(dllexport) void WcfiNodeClose(void* reader);
// Passive readers own their cursor. Overwrite is reported via first_frame;
// seqlock contention returns zero, never delays the audio writer.
__declspec(dllexport) int WcfiNodeRead(void* reader, uint64_t from_frame, uint32_t frames,
    float* audio, wcfi::NodeStatus* status);
}
