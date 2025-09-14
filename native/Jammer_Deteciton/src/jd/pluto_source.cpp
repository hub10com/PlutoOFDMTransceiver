// jd/pluto_source.cpp
#include "jd/pluto_source.hpp"
#include <cstdio>
#include <cstring>
#include <string>

namespace jd {

static void log_err(const char* msg) { std::fprintf(stderr, "[Pluto] %s\n", msg); }

bool PlutoSource::write_dev_ll(iio_device* dev, const char* attr, long long val) {
    if (!dev) return false;
    return iio_device_attr_write_longlong(dev, attr, val) >= 0;
}
bool PlutoSource::write_dev_str(iio_device* dev, const char* attr, const char* val) {
    if (!dev) return false;
    return iio_device_attr_write(dev, attr, val) >= 0;
}
bool PlutoSource::write_chan_ll(iio_channel* ch, const char* attr, long long val) {
    if (!ch) return false;
    return iio_channel_attr_write_longlong(ch, attr, val) >= 0;
}
bool PlutoSource::write_chan_str(iio_channel* ch, const char* attr, const char* val) {
    if (!ch) return false;
    return iio_channel_attr_write(ch, attr, val) >= 0;
}

PlutoSource::PlutoSource(const PlutoConfig& cfg) : cfg_(cfg) {
    if (!init_context())            { log_err("Context oluşturulamadı."); return; }
    if (!apply_static_config())     { log_err("Ayarlar uygulanamadı.");  return; }
    if (!alloc_buffer())            { log_err("RX buffer ayrılamadı.");  return; }
}

PlutoSource::~PlutoSource() { release(); }

bool PlutoSource::init_context() {
    // 1) Context
    ctx_ = cfg_.uri.empty() ? iio_create_default_context()
                            : iio_create_context_from_uri(cfg_.uri.c_str());
    if (!ctx_) { log_err("iio context null"); return false; }

    // (opsiyonel) refill/kapanış bloklarına karşı timeout
    iio_context_set_timeout(ctx_, 1000); // ms

    // 2) Cihazları yaz (teşhis)
    const int ndev = iio_context_get_devices_count(ctx_);
    std::fprintf(stderr, "[Pluto] context devices (%d):\n", ndev);
    for (int i=0; i<ndev; ++i) {
        auto* d = iio_context_get_device(ctx_, i);
        const char* name = iio_device_get_name(d);
        std::fprintf(stderr, "  - %s\n", name ? name : "(null)");
    }

    // 3) ad9361-phy
    phy_ = iio_context_find_device(ctx_, "ad9361-phy");
    if (!phy_) {
        for (int i=0; i<ndev; ++i) {
            auto* d = iio_context_get_device(ctx_, i);
            const char* nm = iio_device_get_name(d);
            if (nm && std::string(nm).find("ad9361-phy") != std::string::npos) { phy_ = d; break; }
        }
    }

    // 4) RX DMA: cf-ad9361-lpc
    rxdev_ = iio_context_find_device(ctx_, "cf-ad9361-lpc");
    if (!rxdev_) {
        for (int i=0; i<ndev; ++i) {
            auto* d = iio_context_get_device(ctx_, i);
            const char* nm = iio_device_get_name(d);
            if (nm && std::string(nm).find("cf-ad9361") != std::string::npos) { rxdev_ = d; break; }
        }
    }

    // 5) RX LO: altvoltage0 (yoksa 1)
    lo_ch_ = phy_ ? iio_device_find_channel(phy_, "altvoltage0", true) : nullptr;
    if (!lo_ch_ && phy_) lo_ch_ = iio_device_find_channel(phy_, "altvoltage1", true);

    if (!phy_ || !rxdev_ || !lo_ch_) {
        log_err("ad9361-phy/altvoltage*/cf-ad9361* bulunamadı.");
        return false;
    }

    // 6) RX data kanalı: voltage0 (input=false)
    rx_ch_ = iio_device_find_channel(rxdev_, "voltage0", false);
    if (!rx_ch_) { log_err("RX dev üzerinde 'voltage0' kanalı yok."); return false; }
    iio_channel_enable(rx_ch_);
    return true;
}

bool PlutoSource::apply_static_config() {
    // Kanallar
    iio_channel* phy_rx_ch = iio_device_find_channel(phy_, "voltage0", false); // RX input
    iio_channel* phy_tx_ch = iio_device_find_channel(phy_, "voltage0", true);  // bazı FW'lerde gerekir

    auto try_set_rate = [&](long long hz) -> bool {
        if (phy_rx_ch && iio_channel_attr_write_longlong(phy_rx_ch, "sampling_frequency", hz) >= 0) return true;
        if (phy_tx_ch && iio_channel_attr_write_longlong(phy_tx_ch, "sampling_frequency", hz) >= 0) return true;
        if (iio_device_attr_write_longlong(phy_, "sampling_frequency", hz) >= 0) return true;
        return false;
    };
    auto try_set_rfbw = [&](long long hz) -> bool {
        if (phy_rx_ch && iio_channel_attr_write_longlong(phy_rx_ch, "rf_bandwidth", hz) >= 0) return true;
        if (phy_tx_ch && iio_channel_attr_write_longlong(phy_tx_ch, "rf_bandwidth", hz) >= 0) return true;
        if (iio_device_attr_write_longlong(phy_, "rf_bandwidth", hz) >= 0) return true;
        return false;
    };

    // 1) Sample rate
    if (!try_set_rate(static_cast<long long>(cfg_.samp_hz))) {
        std::fprintf(stderr, "[Pluto] sampling_frequency yazılamadı.\n");
        return false;
    }
    // 2) RF bandwidth
    if (!try_set_rfbw(static_cast<long long>(cfg_.rfbw_hz))) {
        std::fprintf(stderr, "[Pluto] rf_bandwidth yazılamadı.\n");
        return false;
    }
    // 3) RX LO freq
    if (!write_chan_ll(lo_ch_, "frequency", static_cast<long long>(cfg_.center_hz))) {
        log_err("RX LO frequency yazılamadı.");
        return false;
    }
    // 4) Gain: manual + dB
    iio_channel* gain_ch = phy_rx_ch ? phy_rx_ch : iio_device_find_channel(phy_, "voltage0", false);
    if (!gain_ch) { log_err("gain channel bulunamadı"); return false; }
    if (!write_chan_str(gain_ch, "gain_control_mode", "manual")) { log_err("gain_control_mode=manual yazılamadı."); return false; }
    if (!write_chan_ll (gain_ch, "hardwaregain", static_cast<long long>(cfg_.rx_gain_db))) { log_err("hardwaregain yazılamadı."); return false; }

    return true;
}

bool PlutoSource::alloc_buffer() {
    rxbuf_ = iio_device_create_buffer(rxdev_, cfg_.frame_len, false);
    if (!rxbuf_) { log_err("iio_device_create_buffer() başarısız."); return false; }
    return true;
}

bool PlutoSource::get_frame(std::vector<std::complex<float>>& out) {
    if (!rxbuf_) return false;

    const ssize_t nbytes = iio_buffer_refill(rxbuf_);
    if (nbytes <= 0) return false;

    auto* start = reinterpret_cast<int16_t*>(iio_buffer_start(rxbuf_));
    auto* end   = reinterpret_cast<int16_t*>(iio_buffer_end(rxbuf_));

    const size_t nsamples = (end - start) / 2; // I+Q
    const size_t take = (static_cast<size_t>(cfg_.frame_len) <= nsamples)
                        ? static_cast<size_t>(cfg_.frame_len) : nsamples;

    out.resize(static_cast<size_t>(cfg_.frame_len));
    const float scale = 1.0f / 32768.0f;

    size_t i = 0;
    for (; i < take; ++i) {
        const int16_t i16 = start[2*i + 0];
        const int16_t q16 = start[2*i + 1];
        out[i] = { i16 * scale, q16 * scale };
    }
    for (; i < static_cast<size_t>(cfg_.frame_len); ++i) out[i] = {0.0f, 0.0f};

    return true;
}

void PlutoSource::release() {
    std::lock_guard<std::mutex> lk(m_);

    if (rxbuf_) {
        iio_buffer_cancel(rxbuf_);      // refill varsa kes
        iio_buffer_destroy(rxbuf_);
        rxbuf_ = nullptr;
    }
    if (rx_ch_) {
        iio_channel_disable(rx_ch_);
        rx_ch_ = nullptr;
    }
    rxdev_ = nullptr;

    // Referansları bırak
    phy_ = nullptr;
    lo_ch_ = nullptr;
    rx_open_.store(false);

    // En sonda context'i kapat
    if (ctx_) {
        iio_context_destroy(ctx_);
        ctx_ = nullptr;
    }
}

// --- Yalnız RX'i kapat (TX ve context dokunulmaz) ---
bool PlutoSource::shutdown_rx_only() {
    std::lock_guard<std::mutex> lk(m_);

    // Idempotent: RX zaten kapalıysa başarı say
    if (!ctx_ || !rx_open_.load()) {
        return true;
    }

    // 1) Refill bekliyorsa iptal et (eski/yeni libiio’da güvenli yol)
    if (rxbuf_) {
        iio_buffer_cancel(rxbuf_);
    }

    // 2) Buffer'ı bırak
    if (rxbuf_) {
        iio_buffer_destroy(rxbuf_);
        rxbuf_ = nullptr;
    }

    // 3) Capture device ve PHY RX kanallarını disable et (varsa in/out)
    iio_device* cap = rxdev_ ? rxdev_ : iio_context_find_device(ctx_, "cf-ad9361-lpc");
    if (cap) {
        if (auto* ch = iio_device_find_channel(cap, "voltage0", false)) iio_channel_disable(ch);
        if (auto* ch = iio_device_find_channel(cap, "voltage1", false)) iio_channel_disable(ch);
        if (auto* ch = iio_device_find_channel(cap, "voltage0", true )) iio_channel_disable(ch);
        if (auto* ch = iio_device_find_channel(cap, "voltage1", true )) iio_channel_disable(ch);
    } else {
        std::fprintf(stderr, "[Pluto] RX capture device bulunamadı.\n");
    }

    iio_device* phy = phy_ ? phy_ : iio_context_find_device(ctx_, "ad9361-phy");
    if (phy) {
        if (auto* ch = iio_device_find_channel(phy, "voltage0", false)) iio_channel_disable(ch);
        if (auto* ch = iio_device_find_channel(phy, "voltage1", false)) iio_channel_disable(ch);
        if (auto* ch = iio_device_find_channel(phy, "voltage0", true )) iio_channel_disable(ch);
        if (auto* ch = iio_device_find_channel(phy, "voltage1", true )) iio_channel_disable(ch);
    } else {
        std::fprintf(stderr, "[Pluto] PHY bulunamadı.\n");
    }

    rx_ch_ = nullptr;
    rx_open_.store(false);
    return true;
}

void PlutoSource::set_timeout_ms(int ms) {
    if (ctx_) iio_context_set_timeout(ctx_, ms < 0 ? 0 : ms);
}

bool PlutoSource::set_center_freq(uint64_t hz) {
    if (!lo_ch_) return false;
    if (!write_chan_ll(lo_ch_, "frequency", static_cast<long long>(hz))) return false;
    cfg_.center_hz = hz;
    return true;
}
bool PlutoSource::set_rf_bw(uint64_t hz) {
    iio_channel* phy_rx_ch = iio_device_find_channel(phy_, "voltage0", false);
    if (phy_rx_ch) {
        if (!write_chan_ll(phy_rx_ch, "rf_bandwidth", static_cast<long long>(hz))) return false;
    } else {
        if (!write_dev_ll(phy_, "rf_bandwidth", static_cast<long long>(hz))) return false;
    }
    cfg_.rfbw_hz = hz;
    return true;
}
bool PlutoSource::set_sample_rate(uint64_t hz) {
    if (!write_dev_ll(phy_, "sampling_frequency", static_cast<long long>(hz))) return false;
    cfg_.samp_hz = hz;
    return true;
}
bool PlutoSource::set_rx_gain_db(int db) {
    iio_channel* phy_rx_ch = iio_device_find_channel(phy_, "voltage0", false);
    if (!phy_rx_ch) return false;
    if (!write_chan_str(phy_rx_ch, "gain_control_mode", "manual")) return false;
    if (!write_chan_ll (phy_rx_ch, "hardwaregain", static_cast<long long>(db))) return false;
    cfg_.rx_gain_db = db;
    return true;
}
bool PlutoSource::set_gain_mode(const char* mode) {
    iio_channel* phy_rx_ch = iio_device_find_channel(phy_, "voltage0", false);
    if (!phy_rx_ch) return false;
    return write_chan_str(phy_rx_ch, "gain_control_mode", mode);
}

} // namespace jd
