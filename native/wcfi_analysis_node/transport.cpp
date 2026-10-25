#include "transport.h"
#include <objbase.h>
#include <algorithm>
#include <cstdio>
#include <cstring>
#include <string>

namespace {
struct Directory { uint32_t magic, version; wcfi::NodeInfo slots[wcfi::NodeSlots]; };
struct Reader { HANDLE mapping; const wcfi::SharedNode* node; };

std::wstring prefix() {
    // Per-user plus per-logon-session namespace; default Windows object ACLs.
    HANDLE token = nullptr;
    unsigned char storage[1024]; DWORD count = 0;
    uint64_t hash = 1469598103934665603ULL;
    if (!OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &token)) return {};
    bool ok = GetTokenInformation(token, TokenUser, storage, sizeof(storage), &count) != FALSE;
    CloseHandle(token);
    if (!ok) return {};
    auto sid = reinterpret_cast<TOKEN_USER*>(storage)->User.Sid;
    auto bytes = static_cast<unsigned char*>(sid);
    for (DWORD n = 0; n < GetLengthSid(sid); ++n) { hash ^= bytes[n]; hash *= 1099511628211ULL; }
    wchar_t name[96]; swprintf_s(name, L"Local\\WCFI.Analysis.v1.%016llx.", hash);
    return name;
}
bool valid_id(const char* id) {
    if (!id || strnlen_s(id, 40) != 32) return false;
    for (int n = 0; n < 32; ++n)
        if (!((id[n] >= '0' && id[n] <= '9') || (id[n] >= 'a' && id[n] <= 'f'))) return false;
    return true;
}
std::wstring node_name(const std::wstring& root, const char* id) {
    std::wstring name = root + L"Node.";
    for (int n = 0; n < 32; ++n) name.push_back(static_cast<wchar_t>(id[n]));
    return name;
}
bool alive(DWORD pid) {
    HANDLE process = OpenProcess(SYNCHRONIZE, FALSE, pid);
    if (!process) return GetLastError() == ERROR_ACCESS_DENIED;
    bool running = WaitForSingleObject(process, 0) == WAIT_TIMEOUT;
    CloseHandle(process); return running;
}
bool held(DWORD result) { return result == WAIT_OBJECT_0 || result == WAIT_ABANDONED; }
}

namespace wcfi {
uint64_t clock_ns() {
    LARGE_INTEGER ticks, frequency;
    QueryPerformanceCounter(&ticks); QueryPerformanceFrequency(&frequency);
    return (ticks.QuadPart / frequency.QuadPart) * 1000000000ULL +
        (ticks.QuadPart % frequency.QuadPart) * 1000000000ULL / frequency.QuadPart;
}
Sender::~Sender() { close(); }
bool Sender::open() {
    if (shared) return true;
    auto root = prefix(); if (root.empty()) return false;
    GUID guid; if (FAILED(CoCreateGuid(&guid))) return false;
    sprintf_s(info.id, "%08lx%04x%04x%02x%02x%02x%02x%02x%02x%02x%02x", guid.Data1,
        guid.Data2, guid.Data3, guid.Data4[0], guid.Data4[1], guid.Data4[2], guid.Data4[3],
        guid.Data4[4], guid.Data4[5], guid.Data4[6], guid.Data4[7]);
    info.pid = GetCurrentProcessId();
    mutex = CreateMutexW(nullptr, FALSE, (root + L"RegistryLock").c_str());
    if (!mutex || !held(WaitForSingleObject(mutex, 1000))) { close(); return false; }
    directory = CreateFileMappingW(INVALID_HANDLE_VALUE, nullptr, PAGE_READWRITE, 0,
        sizeof(Directory), (root + L"Directory").c_str());
    bool fresh = directory && GetLastError() != ERROR_ALREADY_EXISTS;
    directory_view = directory ? MapViewOfFile(directory, FILE_MAP_ALL_ACCESS, 0, 0, sizeof(Directory)) : nullptr;
    auto index = static_cast<Directory*>(directory_view);
    if (index && fresh) { memset(index, 0, sizeof(*index)); index->version = 1; index->magic = NodeMagic; }
    if (index && index->magic == NodeMagic && index->version == 1) {
        for (uint32_t n = 0; n < NodeSlots; ++n) {
            if (!index->slots[n].pid || !alive(index->slots[n].pid)) { slot = static_cast<int>(n); break; }
        }
        if (slot >= 0) {
            mapping = CreateFileMappingW(INVALID_HANDLE_VALUE, nullptr, PAGE_READWRITE, 0,
                sizeof(SharedNode), node_name(root, info.id).c_str());
            shared = mapping ? static_cast<SharedNode*>(MapViewOfFile(mapping, FILE_MAP_ALL_ACCESS, 0, 0, sizeof(SharedNode))) : nullptr;
            if (shared) {
                memset(shared, 0, sizeof(*shared));
                shared->version = shared->status.version = 1;
                shared->status.pid = info.pid;
                shared->status.channels = 2;
                shared->status.sample_rate = 48000;
                strcpy_s(shared->status.label, "Unassigned");
                MemoryBarrier(); shared->magic = NodeMagic;
                index->slots[slot] = info;
            }
        }
    }
    ReleaseMutex(mutex);
    if (!shared) { close(); return false; }
    return true;
}
void Sender::close() {
    if (shared) InterlockedExchange(reinterpret_cast<volatile LONG*>(&shared->status.active), 0);
    if (directory_view && mutex && slot >= 0 && held(WaitForSingleObject(mutex, 1000))) {
        auto index = static_cast<Directory*>(directory_view);
        if (strcmp(index->slots[slot].id, info.id) == 0) memset(&index->slots[slot], 0, sizeof(NodeInfo));
        ReleaseMutex(mutex);
    }
    if (shared) UnmapViewOfFile(shared);
    if (mapping) CloseHandle(mapping);
    if (directory_view) UnmapViewOfFile(directory_view);
    if (directory) CloseHandle(directory);
    if (mutex) CloseHandle(mutex);
    shared = nullptr; mapping = directory = mutex = nullptr; directory_view = nullptr; slot = -1;
}
void Sender::configure(const char* label, uint32_t position) {
    if (!shared) return;
    InterlockedIncrement64(&shared->meta_sequence);
    strncpy_s(shared->status.label, label ? label : "Unassigned", _TRUNCATE);
    InterlockedExchange(reinterpret_cast<volatile LONG*>(&shared->status.position), position ? 1 : 0);
    InterlockedIncrement64(&shared->meta_sequence);
}
void Sender::setup(double rate, uint32_t maximum_block) {
    if (!shared) return;
    shared->status.sample_rate = rate;
    shared->status.block_size = maximum_block;
}
void Sender::processing(bool active) {
    if (!shared) return;
    InterlockedIncrement64(&shared->sequence);
    if (active) { ++shared->status.epoch; shared->status.written_frame = 0; }
    shared->status.active = active ? 1 : 0;
    InterlockedIncrement64(&shared->sequence);
}
template<class T> void Sender::publish(T* const* input, int channels, int frames,
                                     int64_t host_sample, int64_t system_ns, uint32_t flags) {
    if (!shared || !input || channels < 1 || channels > 2 || frames < 1) return;
    const uint64_t end = shared->status.written_frame + static_cast<uint64_t>(frames);
    const int skip = std::max(0, frames - static_cast<int>(NodeCapacity));
    InterlockedIncrement64(&shared->sequence);
    for (int n = skip; n < frames; ++n) {
        auto& destination = shared->audio[(end - frames + n) & (NodeCapacity - 1)];
        destination[0] = input[0] ? static_cast<float>(input[0][n]) : 0;
        destination[1] = channels == 2 && input[1] ? static_cast<float>(input[1][n]) : destination[0];
    }
    shared->status.channels = channels;
    shared->status.block_size = frames;
    shared->status.written_frame = end;
    shared->status.callback_ns = clock_ns();
    shared->status.host_sample = host_sample;
    shared->status.host_system_ns = system_ns;
    shared->status.flags = flags;
    InterlockedIncrement64(&shared->sequence);
}
template void Sender::publish<float>(float* const*, int, int, int64_t, int64_t, uint32_t);
template void Sender::publish<double>(double* const*, int, int, int64_t, int64_t, uint32_t);
}

uint64_t WcfiNodeClockNs() { return wcfi::clock_ns(); }
int WcfiNodesEnumerate(wcfi::NodeInfo* infos, uint32_t capacity) {
    if (!infos || capacity > wcfi::NodeSlots) return -1;
    auto root = prefix(); if (root.empty()) return 0;
    HANDLE mapping = OpenFileMappingW(FILE_MAP_READ, FALSE, (root + L"Directory").c_str());
    if (!mapping) return 0;
    auto index = static_cast<const Directory*>(MapViewOfFile(mapping, FILE_MAP_READ, 0, 0, sizeof(Directory)));
    HANDLE mutex = OpenMutexW(SYNCHRONIZE | MUTEX_MODIFY_STATE, FALSE, (root + L"RegistryLock").c_str());
    int count = 0;
    if (index && mutex && held(WaitForSingleObject(mutex, 0))) {
        if (index->magic == wcfi::NodeMagic && index->version == 1)
            for (uint32_t n = 0; n < wcfi::NodeSlots && count < static_cast<int>(capacity); ++n)
                if (index->slots[n].pid && valid_id(index->slots[n].id) && alive(index->slots[n].pid)) infos[count++] = index->slots[n];
        ReleaseMutex(mutex);
    }
    if (mutex) CloseHandle(mutex);
    if (index) UnmapViewOfFile(index);
    CloseHandle(mapping); return count;
}
void* WcfiNodeOpen(const char* id) {
    if (!valid_id(id)) return nullptr;
    auto root = prefix(); if (root.empty()) return nullptr;
    HANDLE mapping = OpenFileMappingW(FILE_MAP_READ, FALSE, node_name(root, id).c_str());
    if (!mapping) return nullptr;
    auto node = static_cast<const wcfi::SharedNode*>(MapViewOfFile(mapping, FILE_MAP_READ, 0, 0, sizeof(wcfi::SharedNode)));
    if (!node || node->magic != wcfi::NodeMagic || node->version != 1) {
        if (node) UnmapViewOfFile(node); CloseHandle(mapping); return nullptr;
    }
    return new Reader {mapping, node};
}
void WcfiNodeClose(void* reader) {
    auto r = static_cast<Reader*>(reader);
    if (!r) return;
    UnmapViewOfFile(r->node); CloseHandle(r->mapping); delete r;
}
int WcfiNodeRead(void* reader, uint64_t from_frame, uint32_t frames, float* audio, wcfi::NodeStatus* status) {
    auto r = static_cast<Reader*>(reader);
    if (!r || !audio || !status || frames > wcfi::NodeCapacity) return -1;
    auto node = r->node;
    for (int attempt = 0; attempt < 3; ++attempt) {
        LONG64 sequence = node->sequence, meta = node->meta_sequence;
        MemoryBarrier();
        if ((sequence & 1) || (meta & 1)) continue;
        wcfi::NodeStatus snapshot = node->status;
        uint64_t end = snapshot.written_frame;
        uint64_t start = std::min(end, std::max(from_frame, end > wcfi::NodeCapacity ? end - wcfi::NodeCapacity : 0));
        uint32_t take = static_cast<uint32_t>(std::min<uint64_t>(end - start, frames));
        for (uint32_t n = 0; n < take; ++n)
            memcpy(audio + n * 2, node->audio[(start + n) & (wcfi::NodeCapacity - 1)], 2 * sizeof(float));
        MemoryBarrier();
        if (sequence != node->sequence || meta != node->meta_sequence) continue;
        snapshot.first_frame = start;
        *status = snapshot; return static_cast<int>(take);
    }
    return -2; // Writer won the race. Readers retry next polling cycle.
}
