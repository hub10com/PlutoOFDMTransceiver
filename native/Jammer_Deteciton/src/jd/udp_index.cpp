#include "jd/udp_index.hpp"
#include <cstring>

#ifdef _WIN32
  #include <winsock2.h>
  #include <ws2tcpip.h>
  #pragma comment(lib, "Ws2_32.lib")
  static bool g_wsastarted=false;
  static void wsainit(){ if(!g_wsastarted){ WSADATA w; WSAStartup(MAKEWORD(2,2), &w); g_wsastarted=true; } }
  static void set_nonblock(SOCKET s){ u_long m=1; ioctlsocket(s, FIONBIO, &m); }
#else
  #include <unistd.h>
  #include <fcntl.h>
  #include <arpa/inet.h>
  #include <sys/socket.h>
  static void set_nonblock(int s){ int fl=fcntl(s,F_GETFL,0); fcntl(s,F_SETFL, fl|O_NONBLOCK); }
#endif

namespace jd {

UdpIndex::UdpIndex(const std::string& ip, uint16_t port) {
#ifdef _WIN32
    wsainit();
    _fd = ::socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    if (_fd == (socket_t)INVALID_SOCKET) { _ok=false; return; }
#else
    _fd = ::socket(AF_INET, SOCK_DGRAM, 0);
    if (_fd < 0) { _ok=false; return; }
#endif
    set_nonblock(_fd);

    sockaddr_in sa{};
    sa.sin_family = AF_INET;
    sa.sin_port   = htons(port);
    ::inet_pton(AF_INET, ip.c_str(), &sa.sin_addr);

    if (::connect(_fd, (sockaddr*)&sa, sizeof(sa)) != 0) {
#ifdef _WIN32
        ::closesocket(_fd);
#else
        ::close(_fd);
#endif
        _fd=(socket_t)-1;
        _ok=false;
        return;
    }
    _ok=true;
}

UdpIndex::~UdpIndex() {
#ifdef _WIN32
    if (_fd != (socket_t)INVALID_SOCKET) ::closesocket(_fd);
#else
    if (_fd >= 0) ::close(_fd);
#endif
}

void UdpIndex::start(uint64_t seq) {
    // START paketinde value = 0 gönderiyoruz
    send(JdxState::START, seq, 0);
}

void UdpIndex::tick(const Counter& ctr) {
    int val=0;
    if (!ctr.current_value(val)) return;
    send(JdxState::TICK, ctr.seq(), static_cast<uint64_t>(val));
}

void UdpIndex::stop(const Counter& ctr) {
    int val=0;
    if (!ctr.current_value(val)) return;
    send(JdxState::STOP, ctr.seq(), static_cast<uint64_t>(val));
}

void UdpIndex::send(JdxState st, uint64_t seq, uint64_t val) {
    if (!_ok) return;
    JdxPacketV1 p{};
    p.seq        = seq;
    p.counter_us = val;   // artık "pattern value"
    p.state      = static_cast<uint8_t>(st);

#ifdef _WIN32
    ::send((SOCKET)_fd, reinterpret_cast<const char*>(&p), sizeof(p), 0);
#else
    ::send(_fd, &p, sizeof(p), 0);
#endif
}

} // namespace jd
