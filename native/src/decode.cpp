#include <algorithm>
#include <cstring>
#include <fstream>
#include <stdexcept>

#include <opencv2/imgcodecs.hpp>
#include <opencv2/imgproc.hpp>

#include "core.hpp"

namespace banana {
namespace {

// Long side of a JPEG from its SOF marker, or -1 if the file isn't a parseable JPEG.
// OpenCV 4.6 (Ubuntu 24.04) has no header-only size query; decoding twice to find out cost ~2x.
int jpeg_long_side(const std::string& path) {
    std::ifstream in(path, std::ios::binary);
    auto byte = [&in]() { return in.get(); };
    if (byte() != 0xFF || byte() != 0xD8) return -1;
    while (in) {
        int marker = byte();
        if (marker != 0xFF) return -1;
        while (marker == 0xFF) marker = byte();  // fill bytes
        if (marker < 0 || marker == 0xD9 || marker == 0xDA) return -1;  // EOI / SOS before any SOF
        if (marker == 0x01 || (marker >= 0xD0 && marker <= 0xD7)) continue;  // no length field
        const int length = (byte() << 8) | byte();
        if (length < 2) return -1;
        const bool sof = marker >= 0xC0 && marker <= 0xCF && marker != 0xC4 && marker != 0xC8 && marker != 0xCC;
        if (sof) {
            byte();  // sample precision
            const int height = (byte() << 8) | byte();
            const int width = (byte() << 8) | byte();
            return in ? std::max(height, width) : -1;
        }
        in.seekg(length - 2, std::ios::cur);
    }
    return -1;
}

// Largest libjpeg DCT reduction that still leaves the long side >= max_side.
int reduced_flag(const std::string& path, int max_side) {
    const int long_side = jpeg_long_side(path);
    if (long_side <= 0) return cv::IMREAD_GRAYSCALE;
    if (long_side / 8 >= max_side) return cv::IMREAD_REDUCED_GRAYSCALE_8;
    if (long_side / 4 >= max_side) return cv::IMREAD_REDUCED_GRAYSCALE_4;
    if (long_side / 2 >= max_side) return cv::IMREAD_REDUCED_GRAYSCALE_2;
    return cv::IMREAD_GRAYSCALE;
}

}  // namespace

GrayImage decode_gray(const std::string& path, int max_side) {
    cv::Mat img = cv::imread(path, reduced_flag(path, max_side));
    if (img.empty()) {
        throw std::runtime_error("failed to decode " + path);
    }
    const int long_side = std::max(img.cols, img.rows);
    if (long_side > max_side) {
        const double scale = static_cast<double>(max_side) / long_side;
        cv::resize(img, img, cv::Size(), scale, scale, cv::INTER_AREA);
    }
    if (!img.isContinuous()) img = img.clone();

    GrayImage out;
    out.height = static_cast<std::size_t>(img.rows);
    out.width = static_cast<std::size_t>(img.cols);
    out.pixels.resize(out.height * out.width);
    std::memcpy(out.pixels.data(), img.data, out.pixels.size());
    return out;
}

}  // namespace banana
