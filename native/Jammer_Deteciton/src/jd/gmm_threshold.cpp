#include "jd/gmm_threshold.hpp"
#include "jd/utils.hpp"
#include <opencv2/core.hpp>
#include <opencv2/ml.hpp>

#include <algorithm>

namespace jd {

std::optional<GmmResult> GmmThreshold::fit(const std::vector<double>& power_dbm) const {
    if (power_dbm.size() < 8) return std::nullopt;

    // Outlier kırpma
    const double lo = percentile(power_dbm, cfg_.p_low);
    const double hi = percentile(power_dbm, cfg_.p_high);
    std::vector<float> clean; clean.reserve(power_dbm.size());
    for (double x : power_dbm) if (x >= lo && x <= hi) clean.push_back(static_cast<float>(x));
    if (clean.size() < 8) return std::nullopt;

    // OpenCV EM (2 bileşen)
    cv::Mat samples((int)clean.size(), 1, CV_32F, clean.data());
    auto em = cv::ml::EM::create();
    em->setClustersNumber(2);
    em->setCovarianceMatrixType(cv::ml::EM::COV_MAT_DIAGONAL);
    em->setTermCriteria(cv::TermCriteria(cv::TermCriteria::COUNT + cv::TermCriteria::EPS,
                                         cfg_.max_iter, cfg_.eps));
    try {
        if (!em->trainEM(samples, cv::noArray(), cv::noArray(), cv::noArray()))
            return std::nullopt;

        cv::Mat means = em->getMeans(); // 2x1, CV_64F
        const double m0 = means.at<double>(0,0);
        const double m1 = means.at<double>(1,0);
        const double mu_low  = std::min(m0, m1);
        const double mu_high = std::max(m0, m1);
        return GmmResult{mu_low, mu_high, 0.5*(mu_low+mu_high), (int)clean.size()};
    } catch (...) {
        return std::nullopt;
    }
}

} // namespace jd
