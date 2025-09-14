#pragma once

namespace jd {

struct Params {
    // Örnekleme / frame
    int    samples_per_frame        = 4096;

    // Güç ölçer
    bool   remove_dc                = true;
    double dc_alpha                 = 0.01;   // DC izleme EMA katsayısı
    double floor_watt               = 1e-15;  // sayısal taban
    double calib_db_offset          = 0.0;    // zincir kalibrasyon ofseti (dBm)

    // Kalibrasyon
    int    calib_dummy_frames       = 10;
    int    calib_time_probe_frames  = 20;
    double calib_target_seconds     = 10.0;    // ilk toplama için hedef süre
    int    calib_clean_consecutive  = 10;     // ardışık temiz çerçeve eşiği

    // Eşik (GMM)
    double gmm_p_low                = 1.0;
    double gmm_p_high               = 99.0;
    int    gmm_max_iter             = 200;
    double gmm_eps                  = 1e-6;

    // Tespit
    int    detect_jammer_consecutive= 5;      // ardışık pozitif eşiği
    int    detect_max_frames        = 1000;   // maksimum tespit döngüsü
};

} // namespace jd
