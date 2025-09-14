// jd/pluto_source.hpp
#pragma once

#include "jd/source.hpp"
#include <string>
#include <vector>
#include <complex>
#include <cstdint>
#include <mutex>
#include <atomic>

extern "C" {
#include <iio.h>
}

namespace jd {

struct PlutoConfig {
    std::string uri;                 // "ip:192.168.2.1" | "usb:" | "" (default)
    uint64_t    center_hz   = 2402000000ULL; // 2.402 GHz
    uint64_t    samp_hz     = 4000000ULL;    // 4 MS/s
    uint64_t    rfbw_hz     = 4000000ULL;    // 4 MHz
    int         frame_len   = 4096;          // samples per frame
    int         rx_gain_db  = -10;           // RX manual gain (dB)
};

class PlutoSource : public ISource {
public:
    explicit PlutoSource(const PlutoConfig& cfg);
    ~PlutoSource() override;

    // ISource
    bool get_frame(std::vector<std::complex<float>>& out) override;
    void release() override;

    // Çalışırken ayar değişimi
    bool set_center_freq(uint64_t hz);
    bool set_rf_bw(uint64_t hz);
    bool set_sample_rate(uint64_t hz);
    bool set_rx_gain_db(int db);
    bool set_gain_mode(const char* mode); // "manual" | "slow_attack" | ...

    // Yalnız RX'i kapat (TX'e dokunmaz, context açık kalır)
    bool shutdown_rx_only();

    // libiio timeout (ms)
    void set_timeout_ms(int ms);

    // Teşhis/entegrasyon
    iio_context* raw_ctx()   const { return ctx_;   }
    iio_buffer*  raw_rxbuf() const { return rxbuf_; }

private:
    // Konfigürasyon ve IIO tutamaçları
    PlutoConfig  cfg_{};
    iio_context* ctx_   = nullptr;
    iio_device*  phy_   = nullptr;   // "ad9361-phy"
    iio_channel* lo_ch_ = nullptr;   // "altvoltage0/1" (RX LO)
    iio_device*  rxdev_ = nullptr;   // "cf-ad9361-lpc" (RX DMA)
    iio_channel* rx_ch_ = nullptr;   // "voltage0" (input=false)
    iio_buffer*  rxbuf_ = nullptr;

    // Eşzamanlılık/güvenlik
    std::mutex        m_;
    std::atomic<bool> rx_open_{false};

    // Kurulum adımları
    bool init_context();
    bool apply_static_config();
    bool alloc_buffer();

    // Yardımcılar
    static bool write_dev_ll (iio_device* dev,  const char* attr, long long val);
    static bool write_dev_str(iio_device* dev,  const char* attr, const char* val);
    static bool write_chan_ll(iio_channel* ch,  const char* attr, long long val);
    static bool write_chan_str(iio_channel* ch, const char* attr, const char* val);
};

} // namespace jd
