#pragma once
#include <cstdint>

#if defined(_WIN32)
  #define BITWRAP_API extern "C" __declspec(dllexport)
#else
  #define BITWRAP_API extern "C"
#endif

BITWRAP_API int wrap_file_bits(
    const char* in_path,
    const char* out_path,
    const char* start_flag_bits,   
    const char* end_flag_bits,     
    std::uint64_t dummy_left_bits, 
    std::uint64_t dummy_right_bits,
    std::uint32_t rng_seed         
);

BITWRAP_API int wrap_file_bits_ratio(
    const char* in_path,
    const char* out_path,
    const char* start_flag_bits,
    const char* end_flag_bits,
    double ratio_divisor,         
    std::uint32_t rng_seed
);

