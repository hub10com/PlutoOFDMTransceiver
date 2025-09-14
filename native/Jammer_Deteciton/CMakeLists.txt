cmake_minimum_required(VERSION 3.24)
project(JammerDetection CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# ---------- Build type: Release varsayılan ----------
if(NOT CMAKE_CONFIGURATION_TYPES AND NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release CACHE STRING "Build type" FORCE)
endif()

# ---------- Uyarılar / Optimizasyon ----------
if (MSVC)
  add_compile_options(/W3 /O2)
else()
  add_compile_options(-O3 -Wall -Wextra -Wno-unknown-pragmas)
endif()

# ---------- Vendor kökleri ----------
set(LIBIIO_ROOT ${CMAKE_SOURCE_DIR}/external/libiio)
set(OPENCV_ROOT ${CMAKE_SOURCE_DIR}/external/opencv)

# ---------- Sanity checks ----------
if(NOT EXISTS "${OPENCV_ROOT}/include/opencv4/opencv2/core.hpp")
  message(FATAL_ERROR "OpenCV headers missing: ${OPENCV_ROOT}/include/opencv4/opencv2/core.hpp not found")
endif()
if(NOT EXISTS "${LIBIIO_ROOT}/include/iio.h")
  message(FATAL_ERROR "libiio headers missing: ${LIBIIO_ROOT}/include/iio.h not found")
endif()

# ---------- Kaynaklar ----------
file(GLOB_RECURSE JD_SOURCES
  ${CMAKE_SOURCE_DIR}/src/jd/*.cpp
  ${CMAKE_SOURCE_DIR}/src/main.cpp
)
add_executable(jammer_detect ${JD_SOURCES})

# ---------- Include dizinleri ----------
target_include_directories(jammer_detect PRIVATE
  ${CMAKE_SOURCE_DIR}/include
  ${LIBIIO_ROOT}/include
  ${OPENCV_ROOT}/include/opencv4
)

# ---------- Derleme makroları ----------
target_compile_definitions(jammer_detect PRIVATE
  NOMINMAX _CRT_SECURE_NO_WARNINGS _USE_MATH_DEFINES
)

# ---------- Link kütüphaneleri ----------
find_library(LIBIIO_LIB      NAMES iio          PATHS ${LIBIIO_ROOT}/lib NO_DEFAULT_PATH REQUIRED)
# OpenCV lib isimleri sürüme göre değişebilir; birkaç olası adı aynı anda dene:
find_library(OPENCV_CORE_LIB NAMES
  opencv_core499 opencv_core498 opencv_core497 opencv_core496 opencv_core495
  opencv_core490 opencv_core485 opencv_core480 opencv_core4
  PATHS ${OPENCV_ROOT}/lib NO_DEFAULT_PATH REQUIRED)
find_library(OPENCV_ML_LIB   NAMES
  opencv_ml499 opencv_ml498 opencv_ml497 opencv_ml496 opencv_ml495
  opencv_ml490 opencv_ml485 opencv_ml480 opencv_ml4
  PATHS ${OPENCV_ROOT}/lib NO_DEFAULT_PATH REQUIRED)

target_link_libraries(jammer_detect PRIVATE
  ${LIBIIO_LIB}
  ${OPENCV_CORE_LIB}
  ${OPENCV_ML_LIB}
)

# ---------- Windows için ek link ----------
if (WIN32)
  target_link_libraries(jammer_detect PRIVATE ws2_32)
endif()

# ---------- Linux/macOS için RPATH: $ORIGIN ----------
if(UNIX AND NOT APPLE)
  set_target_properties(jammer_detect PROPERTIES
    BUILD_WITH_INSTALL_RPATH ON
    INSTALL_RPATH "\$ORIGIN"
    SKIP_BUILD_RPATH OFF
  )
elseif(APPLE)
  set_target_properties(jammer_detect PROPERTIES
    BUILD_WITH_INSTALL_RPATH ON
    INSTALL_RPATH "@loader_path"
    SKIP_BUILD_RPATH OFF
  )
endif()

# ---------- Portable Bundle (dist/) : sade top-copy yaklaşımı ----------
set(DIST_DIR "${CMAKE_BINARY_DIR}/dist")
get_filename_component(COMPILER_DIR "${CMAKE_CXX_COMPILER}" DIRECTORY)

# Yardımcı fonksiyon: klasör varsa içindeki .dll’leri kopyala
function(copy_all_dlls from_dir to_dir)
  if(EXISTS "${from_dir}")
    file(GLOB _dlls "${from_dir}/*.dll")
    foreach(_d IN LISTS _dlls)
      add_custom_command(TARGET jammer_detect POST_BUILD
        COMMAND ${CMAKE_COMMAND} -E copy_if_different "${_d}" "${to_dir}"
        VERBATIM)
    endforeach()
  endif()
endfunction()

# Build sonrası paketleme
add_custom_command(TARGET jammer_detect POST_BUILD
  COMMAND ${CMAKE_COMMAND} -E rm -rf "${DIST_DIR}"
  COMMAND ${CMAKE_COMMAND} -E make_directory "${DIST_DIR}"

  # 1) exe'yi dist/ içine kopyala
  COMMAND ${CMAKE_COMMAND} -E copy $<TARGET_FILE:jammer_detect> "${DIST_DIR}"
  VERBATIM
)

# 2) OpenCV ve libiio: bin + lib içindeki TÜM .dll’leri dist/’e kopyala
copy_all_dlls("${OPENCV_ROOT}/bin" "${DIST_DIR}")
copy_all_dlls("${OPENCV_ROOT}/lib" "${DIST_DIR}")
copy_all_dlls("${LIBIIO_ROOT}/bin" "${DIST_DIR}")
copy_all_dlls("${LIBIIO_ROOT}/lib" "${DIST_DIR}")

# 3) MinGW runtime’ları derleyici klasöründen kopyala
foreach(_mingw_dll IN ITEMS libstdc++-6.dll libgcc_s_seh-1.dll libwinpthread-1.dll)
  add_custom_command(TARGET jammer_detect POST_BUILD
    COMMAND ${CMAKE_COMMAND} -E copy_if_different "${COMPILER_DIR}/${_mingw_dll}" "${DIST_DIR}"
    VERBATIM)
endforeach()

# 4) Çalıştırma kolaylığı için run_jammer_detect.bat üret
#    (PATH'e dist'i öne alıp EXE'yi çalıştırır)
set(RUN_BAT "${DIST_DIR}/run_jammer_detect.bat")
file(WRITE "${CMAKE_BINARY_DIR}/_run_template.bat" "@echo off\r\nsetlocal\r\nset PATH=%~dp0;%PATH%\r\n\"%~dp0jammer_detect.exe\" %*\r\n")
add_custom_command(TARGET jammer_detect POST_BUILD
  COMMAND ${CMAKE_COMMAND} -E copy_if_different "${CMAKE_BINARY_DIR}/_run_template.bat" "${RUN_BAT}"
  VERBATIM
)

# (opsiyonel) install hedefi
install(DIRECTORY "${DIST_DIR}/" DESTINATION .)
