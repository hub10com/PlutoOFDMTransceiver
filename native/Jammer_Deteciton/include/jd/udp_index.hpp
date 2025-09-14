#pragma once
#include <cstdint>
#include <atomic>
#include <string>
#include "jd/counter.hpp"

namespace jd {

enum class JdxState : uint8_t { START=1, TICK=2, STOP=3 };

#pragma pack(push,1)
struct JdxPacketV1 {
    uint32_t magic = 0x3158444A; // 'JDX1'
    uint64_t seq;
    uint64_t counter_us;
    uint8_t  state;
    uint8_t  _pad[7]{};
};
#pragma pack(pop)

class UdpIndex {
public:
    UdpIndex(const std::string& ip, uint16_t port);
    ~UdpIndex();

    bool ok() const { return _ok; }

    // Jammer tespiti anında çağır
    void start(uint64_t seq);

    // Her frame’de çağır
    void tick(const Counter& ctr);

    // Jammer bittiğinde çağır
    void stop(const Counter& ctr);

private:
    void send(JdxState st, uint64_t seq, uint64_t us);

    bool _ok=false;
#ifdef _WIN32
    using socket_t = uintptr_t;
#else
    using socket_t = int;
#endif
    socket_t _fd=(socket_t)-1;
};

} // namespace jd
