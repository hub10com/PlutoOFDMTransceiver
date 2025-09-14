#pragma once
#include "jd/source.hpp"
#include <random>

namespace jd {

// Gaussian gürültü + opsiyonel burst jammer simülasyonu
class DummySource : public ISource {
public:
    DummySource(size_t n, int samples_per_frame, double noise_std=0.02,
                double jammer_prob=0.2, double jammer_amp=0.5)
      : N_(n), SPF_(samples_per_frame), noise_(0.0, noise_std),
        jam_prob_(jammer_prob), jam_amp_(jammer_amp) {}

    bool get_frame(std::vector<std::complex<float>>& out) override {
        if (N_-- == 0) return false;
        out.resize(SPF_);
        const bool jam = (uni_(rng_) < jam_prob_);
        for (int i=0; i<SPF_; ++i) {
            float i0 = (float)noise_(rng_);
            float q0 = (float)noise_(rng_);
            if (jam) { i0 += (float)jam_amp_; q0 += (float)jam_amp_; }
            out[i] = {i0, q0};
        }
        return true;
    }

private:
    size_t N_;
    int SPF_;
    std::mt19937 rng_{12345};
    std::normal_distribution<double> noise_;
    std::uniform_real_distribution<double> uni_{0.0, 1.0};
    double jam_prob_;
    double jam_amp_;
};

} // namespace jd
