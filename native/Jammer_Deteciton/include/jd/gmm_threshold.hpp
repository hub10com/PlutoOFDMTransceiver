#pragma once
#include <vector>
#include <optional>

namespace jd {

struct GmmResult {
    double mu_low, mu_high, threshold;
    int n_used;
};

struct GmmConfig {
    double p_low = 1.0, p_high = 99.0; // outlier kırpma
    int max_iter = 200;
    double eps = 1e-6;
};

class GmmThreshold {
public:
    explicit GmmThreshold(const GmmConfig& cfg = {}) : cfg_(cfg) {}
    std::optional<GmmResult> fit(const std::vector<double>& power_dbm) const;

private:
    GmmConfig cfg_;
};

} // namespace jd
