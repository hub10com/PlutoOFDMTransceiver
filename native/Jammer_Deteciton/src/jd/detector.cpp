#include "jd/detector.hpp"
#include <cstdio>

namespace jd {

DetectOutcome Detector::run() {
    std::vector<std::complex<float>> frame;
    int jam_cnt = 0;

    for (int idx=1; idx<=cfg_.max_frames; ++idx) {
        if (!src_.get_frame(frame)) {
            std::printf("Source exhausted/error.\n");
            return DetectOutcome::SourceEnded;
        }
        const double pd = pm_.power_dbm(frame);

        if (pd > cfg_.threshold_dbm) {
            ++jam_cnt;
            std::printf("Frame %d - JAMMER (%.2f dBm)  [count=%d/%d]\n",
                        idx, pd, jam_cnt, cfg_.jammer_consecutive);
            if (jam_cnt >= cfg_.jammer_consecutive) {
                std::printf("Continuous JAMMER detected - exiting.\n");
                src_.release();
                return DetectOutcome::SustainedJammer;
            }
        } else {
            jam_cnt = 0;
            std::printf("Frame %d - Normal (%.2f dBm)\n", idx, pd);
        }
    }
    src_.release();
    std::printf("Scan completed; continuous jammer threshold not reached.\n");
    return DetectOutcome::CompletedNoSustain;
}

} // namespace jd
