#pragma once
#include <vector>
#include <complex>

namespace jd {

struct PowerConfig {
    bool   remove_dc  = true;
    double dc_alpha   = 0.01;
    double floor_watt = 1e-15;
    double calib_db   = 0.0;
};

class PowerMeter {
public:
    explicit PowerMeter(const PowerConfig& cfg = {}) 
        : cfg_(cfg), dc_(0.0, 0.0) {}

    double power_dbm(const std::vector<std::complex<float>>& frame);

private:
    PowerConfig cfg_;
    std::complex<double> dc_;
}; 

} // namespace jd
