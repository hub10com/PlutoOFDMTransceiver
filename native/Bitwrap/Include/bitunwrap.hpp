#pragma once
#include <cstdint>

#if defined(_WIN32)
  #define BITUNWRAP_API extern "C" __declspec(dllexport)
#else
  #define BITUNWRAP_API extern "C"
#endif

BITUNWRAP_API int unwrap_file_bits(
    const char* in_path,
    const char* out_path,
    const char* start_flag_bits,   
    const char* end_flag_bits      
);

BITUNWRAP_API std::uint64_t get_last_start_flag_pos();
BITUNWRAP_API std::uint64_t get_last_end_flag_pos();
