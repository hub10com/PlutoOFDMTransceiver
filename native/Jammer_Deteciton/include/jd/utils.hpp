#pragma once
#include <vector>
#include <algorithm>
#include <cmath>
#include <chrono>

namespace jd {

// Basit persentil (0..100), lineer interpolasyon
inline double percentile(std::vector<double> v, double p) {
    if (v.empty()) return std::nan("");
    std::sort(v.begin(), v.end());
    if (p <= 0) return v.front();
    if (p >= 100) return v.back();
    const double pos = (p/100.0) * (v.size()-1);
    const auto idx = static_cast<size_t>(std::floor(pos));
    const double frac = pos - idx;
    if (idx+1 < v.size()) return v[idx] + frac * (v[idx+1] - v[idx]);
    return v[idx];
}

struct TicToc {
    using clock = std::chrono::steady_clock;
    clock::time_point t0;
    void tic() { t0 = clock::now(); }
    double toc_ms() const {
        using namespace std::chrono;
        return duration_cast<duration<double, std::milli>>(clock::now() - t0).count();
    }
};

} // namespace jd
