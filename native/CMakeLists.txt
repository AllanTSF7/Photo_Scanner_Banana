cmake_minimum_required(VERSION 3.18...3.30)
project(banana_core LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

find_package(Python 3.12 COMPONENTS Interpreter Development.Module REQUIRED)
find_package(nanobind CONFIG REQUIRED)
find_package(OpenCV QUIET COMPONENTS core imgproc imgcodecs)

nanobind_add_module(_banana_core NB_STATIC
    src/bindings.cpp
    src/dhash.cpp
    src/blank.cpp
    src/photo_bbox.cpp
)
target_include_directories(_banana_core PRIVATE src)

if(OpenCV_FOUND)
    target_sources(_banana_core PRIVATE src/decode.cpp)
    target_compile_definitions(_banana_core PRIVATE BANANA_HAVE_OPENCV=1)
    target_link_libraries(_banana_core PRIVATE ${OpenCV_LIBS})
else()
    message(STATUS "OpenCV not found: decode_gray disabled (Python falls back to Pillow)")
endif()

if(MSVC)
    target_compile_options(_banana_core PRIVATE /W4 /O2)
else()
    target_compile_options(_banana_core PRIVATE -Wall -Wextra -O3)
endif()

install(TARGETS _banana_core LIBRARY DESTINATION banana_core)
