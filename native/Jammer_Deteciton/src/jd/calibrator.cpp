// jd/calibrator.cpp
#include "jd/calibrator.hpp"
#include "jd/utils.hpp"   // TicToc
#include <cstdio>
#include <chrono>
#include <vector>
#include <complex>
#include <algorithm>
#include <optional>   // <-- eksikti, eklendi

namespace jd {

std::optional<CalibResult> Calibrator::run() {
    CalibResult res{};

    // 1) Dummy RX
    if (cfg_.verbose)
        std::printf("[CAL] Receiving Dummy RX (%d)...\n", cfg_.dummy_frames);

    std::vector<std::complex<float>> frame;
    for (int k = 0; k < cfg_.dummy_frames; ++k) {
        if (!src_.get_frame(frame)) return std::nullopt;
    }

    // 2) Zaman ölçümü (RX vs toplam) - hafif örnekleme
    TicToc ttot, trx;
    double sum_total_ms = 0.0, sum_rx_ms = 0.0;
    const int Nprobe = std::max(1, cfg_.time_probe_frames);

    for (int i = 0; i < Nprobe; ++i) {
        ttot.tic();
        trx.tic();
        if (!src_.get_frame(frame)) return std::nullopt;
        const double rx_ms  = trx.toc_ms();
        (void)pm_.power_dbm(frame);
        const double tot_ms = ttot.toc_ms();
        sum_rx_ms    += rx_ms;
        sum_total_ms += tot_ms;

        const int probe_log_stride = std::max(1, cfg_.log_every / 10);
        if (cfg_.verbose && ((i + 1) % probe_log_stride == 0)) {
            std::printf("[CAL] Probe %d  RX: %.3f ms  TOTAL: %.3f ms\n",
                        i + 1, rx_ms, tot_ms);
        }
    }

    res.mean_rx_ms    = sum_rx_ms    / Nprobe;
    res.mean_frame_ms = sum_total_ms / Nprobe;

    // 3) Hedef süreye göre veri toplama
    using clock = std::chrono::steady_clock;
    const double Tgoal = std::max(0.1, cfg_.target_seconds);

    if (cfg_.verbose)
        std::printf("[CAL] Initial calibration starting. Target duration: %.2f s (approximately %.2f ms/frame)\n",
            Tgoal, res.mean_frame_ms);

    std::vector<double> p_dbm;
    const double est_fps = (res.mean_frame_ms > 0.0) ? (1000.0 / res.mean_frame_ms) : 1000.0;
    p_dbm.reserve(static_cast<size_t>(Tgoal * est_fps * 1.2));

    auto t0 = clock::now();
    size_t k = 0;
    while (std::chrono::duration<double>(clock::now() - t0).count() < Tgoal) {
        if (!src_.get_frame(frame)) break;
        p_dbm.push_back(pm_.power_dbm(frame));
        ++k;

        if (cfg_.verbose && cfg_.log_every > 0 && (k % cfg_.log_every == 0)) {
            const double elapsed = std::chrono::duration<double>(clock::now() - t0).count();
            std::printf("[CAL] progress: %zu frames, elapsed=%.2fs\n", k, elapsed);
        }
    }

    const double elapsed = std::chrono::duration<double>(clock::now() - t0).count();
    res.frames_used      = static_cast<int>(p_dbm.size());
    if (res.frames_used > 0) {
        res.mean_frame_ms = 1000.0 * elapsed / res.frames_used;
    }

    if (cfg_.verbose) {
        const double fps = (elapsed > 0.0) ? (res.frames_used / elapsed) : 0.0;
        std::printf("[CAL] Collection finished: elapsed=%.3fs, frames=%d, fps=%.1f\n",
                    elapsed, res.frames_used, fps);
    }

    if (res.frames_used < 8) {
        if (cfg_.verbose) std::printf("[CAL] Insufficient data (frames=%d). Cancelled.\n", res.frames_used);
        return std::nullopt;
    }

    // 4) GMM threshold
    auto g = gmm_.fit(p_dbm);
    if (!g) {
        if (cfg_.verbose) std::printf("[CAL] GMM failed. Cancelled.\n");
        return std::nullopt;
    }
    res.threshold_dbm = g->threshold;

    if (cfg_.verbose) {
        std::printf("[CAL] GMM: mu_low=%.2f  mu_high=%.2f  threshold=%.2f dBm  (n=%d)\n",
                    g->mu_low, g->mu_high, g->threshold, g->n_used);
    }

    // 5) Clean environment kontrolü
    const int look = std::max(5, res.frames_used / 10);
    int consecutive = 0;
    if (cfg_.verbose) std::printf("[CAL] Clean environment check (%d frame)...\n", look);

    const int probe_stride = std::max(1, cfg_.log_every / 10);
    for (int i = 0; i < look; ++i) {
        if (!src_.get_frame(frame)) break;
        const double pd = pm_.power_dbm(frame);

        if (cfg_.verbose && ((i + 1) % probe_stride == 0)) {
            std::printf("[CAL] Probe %d  Power=%.2f dBm\n", i + 1, pd);
        }

        if (pd < res.threshold_dbm) {
            if (++consecutive >= cfg_.clean_consecutive) {
                res.clean_found = true;
                if (cfg_.verbose)
                    std::printf("[CAL] Clean environment found (frame=%d).\n", i + 1);
                break;
            }
        } else {
            consecutive = 0;
        }
    }

    if (!res.clean_found && cfg_.verbose)
        std::printf("[CAL] Clean environment not found; jammer likely.\n");

    return res;
}

} // namespace jd
