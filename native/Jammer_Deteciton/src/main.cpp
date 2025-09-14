// main.cpp — JammerDetection (RX kapanınca da sayaç/UDP devam + dıştan STOP)
#include "jd/pluto_source.hpp"
#include "jd/jammer_detector.hpp"
#include "jd/config.hpp"
#include "jd/counter.hpp"
#include "jd/udp_index.hpp"

#include <string>
#include <cstring>
#include <cstdlib>
#include <cstdio>
#include <iostream>
#include <thread>
#include <atomic>
#include <chrono>
#include <csignal>

#ifdef _WIN32
  #include <winsock2.h>
  #include <ws2tcpip.h>
  #pragma comment(lib, "Ws2_32.lib")
  static bool g_wsastarted=false;
  static void wsainit(){ if(!g_wsastarted){ WSADATA w; WSAStartup(MAKEWORD(2,2), &w); g_wsastarted=true; } }
  static void closesock(SOCKET s){ if(s!=INVALID_SOCKET) ::closesocket(s); }
#else
  #include <unistd.h>
  #include <fcntl.h>
  #include <arpa/inet.h>
  #include <sys/socket.h>
  static void closesock(int s){ if(s>=0) ::close(s); }
#endif

// ------------------------------------------------------------
// Basit CLI
struct CliRadio {
    std::string uri   = "ip:192.168.2.1";
    double      freq  = 2.402e9;   // Hz
    double      samp  = 4e6;       // Hz
    double      rfbw  = 4e6;       // Hz
    int         gain  = -20;       // dB
    int         fsize = 4096;      // samples per frame
};

static bool looks_number(const char* s) {
    if (!s || !*s) return false;
    char* end=nullptr;
    std::strtod(s, &end);
    return end && *end=='\0';
}

static void print_help() {
    std::puts(
"Usage: jammer_detect [options] | [gain]\n"
"\n"
" Radio / Pluto:\n"
"   -g, --gain <int>          RX gain dB (default -20)\n"
"   -f, --freq <Hz>           center frequency (e.g. 2.402e9)\n"
"   -s, --samp <Hz>           sample rate (e.g. 4e6)\n"
"   -b, --rfbw <Hz>           RF bandwidth (e.g. 4e6)\n"
"       --uri <str>           iio uri (ip:192.168.2.1 | usb:)\n"
"   -n, --framesize <int>     samples per frame (default 4096)\n"
"\n"
" Calibration:\n"
"   -T, --calib-secs <dbl>    target seconds (default 5.0)\n"
"   -D, --calib-dummy <int>   dummy frames (default 10)\n"
"   -P, --calib-probes <int>  time probe frames (default 20)\n"
"   -C, --calib-clean <int>   clean consecutive (default 10)\n"
"\n"
" Power meter:\n"
"       --no-dc               disable DC removal\n"
"       --dc-alpha <dbl>      DC EMA alpha (default 0.01)\n"
"       --floor-watt <dbl>    numeric floor (default 1e-15)\n"
"       --calib-db <dbl>      chain calibration offset in dB\n"
"\n"
" Threshold / GMM:\n"
"       --p-low <dbl>         lower trim percentile (default 1.0)\n"
"       --p-high <dbl>        upper trim percentile (default 99.0)\n"
"       --gmm-eps <dbl>       EM epsilon (default 1e-6)\n"
"       --gmm-iters <int>     EM max iters (default 200)\n"
"\n"
" Detect:\n"
"       --detect-consec <int> consecutive positives (default 5)\n"
"       --detect-max <int>    max detection frames (default 1500)\n"
"\n"
" Control:\n"
"       Program STOP icin UDP 127.0.0.1:25000'a 'STOP' gonderin (veya Ctrl+C).\n"
    );
}

static bool parse_cli(int argc, char** argv, CliRadio& r, jd::Params& p) {
    if (argc == 2 && looks_number(argv[1])) { r.gain = std::atoi(argv[1]); return true; }
    for (int i=1; i<argc; ++i) {
        std::string a = argv[i];
        auto need = [&](const char* what){
            if (i+1 >= argc) { std::fprintf(stderr,"missing value for %s\n", what); return false; }
            return true;
        };
        if (a=="-h" || a=="--help") { print_help(); return false; }
        else if (a=="-g"||a=="--gain")       { if(!need(a.c_str())) return false; r.gain  = std::atoi(argv[++i]); }
        else if (a=="-f"||a=="--freq")       { if(!need(a.c_str())) return false; r.freq  = std::strtod(argv[++i], nullptr); }
        else if (a=="-s"||a=="--samp")       { if(!need(a.c_str())) return false; r.samp  = std::strtod(argv[++i], nullptr); }
        else if (a=="-b"||a=="--rfbw")       { if(!need(a.c_str())) return false; r.rfbw  = std::strtod(argv[++i], nullptr); }
        else if (a=="--uri")                 { if(!need(a.c_str())) return false; r.uri   = argv[++i]; }
        else if (a=="-n"||a=="--framesize")  { if(!need(a.c_str())) return false; r.fsize = std::atoi(argv[++i]); }
        else if (a=="-T"||a=="--calib-secs") { if(!need(a.c_str())) return false; p.calib_target_seconds    = std::strtod(argv[++i], nullptr); }
        else if (a=="-D"||a=="--calib-dummy"){ if(!need(a.c_str())) return false; p.calib_dummy_frames      = std::atoi(argv[++i]); }
        else if (a=="-P"||a=="--calib-probes"){if(!need(a.c_str())) return false; p.calib_time_probe_frames = std::atoi(argv[++i]); }
        else if (a=="-C"||a=="--calib-clean"){ if(!need(a.c_str())) return false; p.calib_clean_consecutive = std::atoi(argv[++i]); }
        else if (a=="--no-dc")               { p.remove_dc = false; }
        else if (a=="--dc-alpha")            { if(!need(a.c_str())) return false; p.dc_alpha   = std::strtod(argv[++i], nullptr); }
        else if (a=="--floor-watt")          { if(!need(a.c_str())) return false; p.floor_watt = std::strtod(argv[++i], nullptr); }
        else if (a=="--calib-db")            { if(!need(a.c_str())) return false; p.calib_db_offset = std::strtod(argv[++i], nullptr); }
        else if (a=="--p-low")               { if(!need(a.c_str())) return false; p.gmm_p_low  = std::strtod(argv[++i], nullptr); }
        else if (a=="--p-high")              { if(!need(a.c_str())) return false; p.gmm_p_high = std::strtod(argv[++i], nullptr); }
        else if (a=="--gmm-eps")             { if(!need(a.c_str())) return false; p.gmm_eps    = std::strtod(argv[++i], nullptr); }
        else if (a=="--gmm-iters")           { if(!need(a.c_str())) return false; p.gmm_max_iter = std::atoi(argv[++i]); }
        else if (a=="--detect-consec")       { if(!need(a.c_str())) return false; p.detect_jammer_consecutive = std::atoi(argv[++i]); }
        else if (a=="--detect-max")          { if(!need(a.c_str())) return false; p.detect_max_frames         = std::atoi(argv[++i]); }
        else { std::fprintf(stderr, "unknown option: %s\n", a.c_str()); print_help(); return false; }
    }
    p.samples_per_frame = r.fsize;
    return true;
}

// ------------------------------------------------------------
// UDP kontrol dinleyici: 127.0.0.1:25000 'STOP'|'EXIT'|'QUIT' -> stop_flag=true
class CtrlServer {
public:
#ifdef _WIN32
    using sock_t = SOCKET;
    static constexpr sock_t BAD = INVALID_SOCKET;
#else
    using sock_t = int;
    static constexpr sock_t BAD = -1;
#endif

    CtrlServer(std::atomic<bool>& stop_flag, uint16_t port=25000)
      : stop_(stop_flag), port_(port) {}

    bool start() {
#ifdef _WIN32
        wsainit();
#endif
        sock_ = ::socket(AF_INET, SOCK_DGRAM, 0);
        if (sock_ == BAD) return false;

        sockaddr_in sa{};
        sa.sin_family = AF_INET;
        sa.sin_port   = htons(port_);
        sa.sin_addr.s_addr = htonl(INADDR_LOOPBACK); // 127.0.0.1

        if (::bind(sock_, (sockaddr*)&sa, sizeof(sa)) != 0) {
            closesock(sock_); sock_=BAD; return false;
        }

#ifndef _WIN32
        int fl = fcntl(sock_, F_GETFL, 0);
        fcntl(sock_, F_SETFL, fl | O_NONBLOCK);
#else
        u_long m=1; ioctlsocket(sock_, FIONBIO, &m);
#endif
        th_ = std::thread([this]{ loop(); });
        return true;
    }

    void stop() {
        quit_.store(true, std::memory_order_release);
        if (th_.joinable()) th_.join();
        if (sock_!=BAD) { closesock(sock_); sock_=BAD; }
    }

    ~CtrlServer(){ stop(); }

private:
    void loop() {
        char buf[256];
        while (!quit_.load(std::memory_order_acquire) && !stop_.load(std::memory_order_acquire)) {
            sockaddr_in from{}; socklen_t flen=sizeof(from);
#ifdef _WIN32
            int n = ::recvfrom(sock_, buf, (int)sizeof(buf)-1, 0, (sockaddr*)&from, &flen);
#else
            int n = (int)::recvfrom(sock_, buf, sizeof(buf)-1, 0, (sockaddr*)&from, &flen);
#endif
            if (n>0) {
                buf[n]=0;
                for (int i=0;i<n;i++) if (buf[i]>='a'&&buf[i]<='z') buf[i]-=32; // upper
                if (std::strstr(buf,"STOP") || std::strstr(buf,"EXIT") || std::strstr(buf,"QUIT")) {
                    std::cout << "[CTRL] STOP komutu alindi.\n";
                    stop_.store(true, std::memory_order_release);
                    break;
                }
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(50));
        }
    }

    std::atomic<bool>& stop_;
    std::atomic<bool>  quit_{false};
    uint16_t port_;
    sock_t   sock_ = BAD;
    std::thread th_;
};

// Ctrl+C -> stop_flag
static std::atomic<bool> g_stop{false};
static void on_sigint(int){ g_stop.store(true, std::memory_order_release); }

// ------------------------------------------------------------
int main(int argc, char** argv) {
    // Sinyal yakalama
    std::signal(SIGINT,  on_sigint);
#ifdef SIGTERM
    std::signal(SIGTERM, on_sigint);
#endif

    // --- Varsayılan paramlar ---
    jd::Params p;
    p.samples_per_frame          = 4096;
    p.remove_dc                  = true;
    p.dc_alpha                   = 0.01;
    p.floor_watt                 = 1e-15;
    p.calib_db_offset            = 0.0;
    p.calib_dummy_frames         = 10;
    p.calib_time_probe_frames    = 20;
    p.calib_target_seconds       = 5.0;
    p.calib_clean_consecutive    = 10;
    p.gmm_p_low                  = 1.0;
    p.gmm_p_high                 = 99.0;
    p.gmm_max_iter               = 200;
    p.gmm_eps                    = 1e-6;
    p.detect_jammer_consecutive  = 5;
    p.detect_max_frames          = 5000;

    CliRadio r;
    if (!parse_cli(argc, argv, r, p)) {
        return (argc==1) ? 0 : 1;
    }

    // Pluto konfig
    jd::PlutoConfig pcfg;
    pcfg.uri        = r.uri;
    pcfg.center_hz  = static_cast<uint64_t>(r.freq);
    pcfg.samp_hz    = static_cast<uint64_t>(r.samp);
    pcfg.rfbw_hz    = static_cast<uint64_t>(r.rfbw);
    pcfg.frame_len  = p.samples_per_frame;
    pcfg.rx_gain_db = r.gain;

    std::cout << "[INFO] Pluto URI=" << pcfg.uri
              << " | Freq=" << pcfg.center_hz
              << " | Samp=" << pcfg.samp_hz
              << " | RFBW=" << pcfg.rfbw_hz
              << " | Gain=" << pcfg.rx_gain_db
              << " | Frame=" << pcfg.frame_len
              << "\n";

    // Sayaç + UDP
    jd::Counter  counter;
    jd::UdpIndex udp("127.0.0.1", 6000);   // veri UDP hedefi
    static uint64_t seq=0;
    bool detected_once = false;

    // Dış kontrol kanalı
    CtrlServer ctrl(g_stop, 25000);
    if (!ctrl.start()) {
        std::cerr << "[WARN] Kontrol sunucusu baslamadi (127.0.0.1:25000). Ctrl+C ile durdurabilirsiniz.\n";
    } else {
        std::cout << "[CTRL] UDP control listening on 127.0.0.1:25000 (send 'STOP').\n";
    }

    // Kaynak + Detector
    jd::PlutoSource   src(pcfg);
    jd::JammerDetector det(src, p);

    // Kalibrasyon
    auto calib = det.calibrate();
    if (!calib) {
        std::cerr << "[ERR] Kalibrasyon basarisiz. Yine de bekleme/publish dongusune gecilecek.\n";
    } else {
        std::cout << "[INFO] Threshold(dBm)=" << calib->threshold_dbm
                  << " | clean=" << (calib->clean_found ? "yes" : "no")
                  << " | mean_rx_ms=" << calib->mean_rx_ms
                  << " | mean_frame_ms=" << calib->mean_frame_ms
                  << " | frames_used=" << calib->frames_used << "\n";
    }

    // 1) Tespit asamasi (tek seferlik kosul): SustainedJammer gorunce sayaci baslat
    bool leave_detection=false;
    while (!g_stop.load(std::memory_order_acquire) && !leave_detection) {
        auto out = det.run_detection();

        if (out == jd::DetectOutcome::SourceEnded) {
            std::cout << "[WARN] Kaynak kapandi/hata. Pluto kapatilip publish moduna gecilecek.\n";
            leave_detection = true;
            break;
        }

        if (out == jd::DetectOutcome::SustainedJammer) {
            counter.start(++seq);
            udp.start(counter.seq());
            detected_once = true;
            std::cout << "[INFO] Jammer bulundu, sayaç basladi (seq=" << seq << ")\n";
            // Bir kez tespit istendi: detection'i bitirip publish moduna gec
            leave_detection = true;
            break;
        }

        // CompletedNoSustain -> tekrar dene (isterseniz burada bir kucuk bekleme koyabilirsiniz)
    }

    // 2) Pluto'yu kapat (publish modunda cihaza ihtiyac yok)
    if (src.shutdown_rx_only())
        std::cout << "[INFO] RX kapatildi (shutdown_rx_only)\n";
    else
        std::cout << "[WARN] RX kapatilirken sorun olustu (shutdown_rx_only)\n";

    src.release();
    std::cout << "[INFO] Context serbest birakildi\n";

    // 3) Publish modu: Kullanici STOP diyene kadar calis
    //    - Eğer tespit gerçekleştiyse pattern sürekli UDP'ye akar.
    //    - Tespit hiç olmadıysa idle bekler (STOP komutunu dinler).
    using namespace std::chrono_literals;
    while (!g_stop.load(std::memory_order_acquire)) {
        if (detected_once) {
            udp.tick(counter);   // pattern (1,3,5,4,2) degeri UDP'ye gider
        }
        std::this_thread::sleep_for(100ms); // publish frekansi (10 Hz)
    }

    std::cout << "[INFO] STOP istendi, cikiliyor.\n";
    return 0;
}
