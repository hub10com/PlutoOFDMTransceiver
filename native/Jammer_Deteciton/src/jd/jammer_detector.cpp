#include "jd/jammer_detector.hpp"
#include <cstdio>

namespace jd {

JammerDetector::JammerDetector(ISource& src, const Params& p)
    : src_(src), p_(p) {}

std::optional<JammerCalibSummary> JammerDetector::calibrate() {
    // PowerMeter
    PowerMeter pm({
        p_.remove_dc,
        p_.dc_alpha,
        p_.floor_watt,
        p_.calib_db_offset
    });

    // GMM
    GmmThreshold gmm({
        p_.gmm_p_low, p_.gmm_p_high,
        p_.gmm_max_iter, p_.gmm_eps
    });

    // Calibrator
    Calibrator calib(
        src_, pm, gmm,
        { p_.calib_dummy_frames,
          p_.calib_time_probe_frames,
          p_.calib_target_seconds,
          p_.calib_clean_consecutive }
    );

    auto res = calib.run();
    if (!res) return std::nullopt;

    threshold_dbm_ = res->threshold_dbm;

    JammerCalibSummary s;
    s.threshold_dbm = res->threshold_dbm;
    s.clean_found   = res->clean_found;
    s.mean_frame_ms = res->mean_frame_ms;
    s.mean_rx_ms    = res->mean_rx_ms;
    s.frames_used   = res->frames_used;
    return s;
}

DetectOutcome JammerDetector::run_detection() {
    // PowerMeter (aynı paramlarla)
    PowerMeter pm({
        p_.remove_dc,
        p_.dc_alpha,
        p_.floor_watt,
        p_.calib_db_offset
    });

    // Detector
    DetectConfig dc;
    dc.threshold_dbm     = threshold_dbm_;                // kalibrasyondan geliyor
    dc.jammer_consecutive= p_.detect_jammer_consecutive;
    dc.max_frames        = p_.detect_max_frames;

    Detector det(src_, pm, dc);
    return det.run();
}

} // namespace jd
