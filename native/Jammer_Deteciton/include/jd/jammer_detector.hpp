#pragma once
#include "jd/source.hpp"
#include "jd/config.hpp"
#include "jd/power_meter.hpp"
#include "jd/gmm_threshold.hpp"
#include "jd/calibrator.hpp"
#include "jd/detector.hpp"
#include <optional>

namespace jd {

struct JammerCalibSummary {
    double threshold_dbm = -100.0;
    bool   clean_found   = false;
    double mean_frame_ms = 0.0;
    double mean_rx_ms    = 0.0;
    int    frames_used   = 0;
};

class JammerDetector {
public:
    JammerDetector(ISource& src, const Params& p);

    // MATLAB'deki calibrate() eşleniği
    std::optional<JammerCalibSummary> calibrate();

    // MATLAB'deki runDetection() eşleniği
    DetectOutcome run_detection();

    double threshold_dbm() const { return threshold_dbm_; }

private:
    ISource& src_;
    Params   p_;
    double   threshold_dbm_ = -100.0;
};

} // namespace jd
