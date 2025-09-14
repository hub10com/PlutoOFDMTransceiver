#include "jd/power_meter.hpp"
#include <cmath>

namespace jd {

double PowerMeter::power_dbm(const std::vector<std::complex<float>>& frame) {
    if (frame.empty()) return -300.0;
    double acc = 0.0;
    if (cfg_.remove_dc) {
        for (const auto& s : frame) {
            dc_.real(dc_.real() + cfg_.dc_alpha * (s.real() - dc_.real()));
            dc_.imag(dc_.imag() + cfg_.dc_alpha * (s.imag() - dc_.imag()));
            const double i = s.real() - dc_.real();
            const double q = s.imag() - dc_.imag();
            acc += i*i + q*q;
        }
    } else {
        for (const auto& s : frame) {
            const double i = s.real();
            const double q = s.imag();
            acc += i*i + q*q;
        }
    }
    const double mean_watt = std::max(acc / static_cast<double>(frame.size()), cfg_.floor_watt);
    return 10.0 * std::log10(mean_watt) + 30.0 + cfg_.calib_db;
}

} // namespace jd
