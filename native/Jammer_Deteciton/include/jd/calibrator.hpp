// jd/calibrator.hpp
#pragma once
#include "jd/source.hpp"
#include "jd/power_meter.hpp"
#include "jd/gmm_threshold.hpp"
#include <optional>

namespace jd {

struct CalibConfig {
    int    dummy_frames     = 10;
    int    time_probe_frames= 20;
    double target_seconds   = 5.0;  // kalibrasyon toplam süresi
    int    clean_consecutive= 10;   // temiz ortam için ardışık frame eşiği
    bool   verbose          = true; // ayrıntılı log
    int    log_every        = 100;  // her N framede bir log (toplama aşaması)
};

struct CalibResult {
    double threshold_dbm = -100.0;
    bool   clean_found   = false;
    double mean_frame_ms = 0.0;
    double mean_rx_ms    = 0.0;
    int    frames_used   = 0;
};

class Calibrator {
public:
    Calibrator(ISource& src, PowerMeter pm, GmmThreshold gmm, CalibConfig cfg)
      : src_(src), pm_(std::move(pm)), gmm_(std::move(gmm)), cfg_(cfg) {}

    std::optional<CalibResult> run();

private:
    ISource&      src_;
    PowerMeter    pm_;
    GmmThreshold  gmm_;
    CalibConfig   cfg_;
};

} // namespace jd
