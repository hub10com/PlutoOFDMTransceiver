#pragma once
#include "jd/source.hpp"
#include "jd/power_meter.hpp"

namespace jd {

struct DetectConfig {
    double threshold_dbm = -50.0;
    int jammer_consecutive = 5; // ardışık pozitif sayacı eşiği
    int max_frames = 1000;
};

enum class DetectOutcome {
    CompletedNoSustain,  // tarama bitti, ardışık eşik aşımına ulaşılmadı
    SustainedJammer,     // ardışık eşik aşımı eşiği sağlandı
    SourceEnded          // kaynak bitti/hata
};

class Detector {
public:
    Detector(ISource& src, PowerMeter pm, DetectConfig cfg)
      : src_(src), pm_(std::move(pm)), cfg_(cfg) {}

    DetectOutcome run();

private:
    ISource& src_;
    PowerMeter pm_;
    DetectConfig cfg_;
};

} // namespace jd
