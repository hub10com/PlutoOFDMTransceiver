#pragma once
#include <vector>
#include <complex>

namespace jd {

// Frame sağlayıcı arayüzü (Pluto/dosya/simülasyon hepsi buradan türesin)
class ISource {
public:
    virtual ~ISource() = default;
    // true: frame üretildi; false: kaynak bitti/hata
    virtual bool get_frame(std::vector<std::complex<float>>& out) = 0;
    virtual void release() {} // opsiyonel kaynak bırakma
};

} // namespace jd
