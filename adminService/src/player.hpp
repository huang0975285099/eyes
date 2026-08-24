#pragma once

#ifndef UNICODE
#define UNICODE
#endif

#include <windows.h>

#include <atomic>
#include <algorithm>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

namespace eyes {

constexpr UINT WM_EYES_FRAME = WM_APP + 11;
constexpr int kFrameWidth = 1280;
constexpr int kFrameHeight = 720;

class NativePlayer {
public:
    ~NativePlayer() { stop(); }

    bool start(HWND target, const std::wstring& ffmpegPath, const std::wstring& url) {
        stop();
        target_ = target;
        {
            std::lock_guard<std::mutex> lock(frameMutex_);
            frame_.clear();
        }

        SECURITY_ATTRIBUTES security{sizeof(SECURITY_ATTRIBUTES), nullptr, TRUE};
        HANDLE readPipe = nullptr;
        HANDLE writePipe = nullptr;
        if (!CreatePipe(&readPipe, &writePipe, &security, 4 * 1024 * 1024)) return false;
        SetHandleInformation(readPipe, HANDLE_FLAG_INHERIT, 0);

        STARTUPINFOW startup{};
        startup.cb = sizeof(startup);
        startup.dwFlags = STARTF_USESTDHANDLES | STARTF_USESHOWWINDOW;
        startup.wShowWindow = SW_HIDE;
        startup.hStdInput = GetStdHandle(STD_INPUT_HANDLE);
        startup.hStdOutput = writePipe;
        startup.hStdError = GetStdHandle(STD_ERROR_HANDLE);

        std::wstring inputOptions = L" -hide_banner -nostdin -loglevel warning -fflags nobuffer -flags low_delay ";
        if (url.rfind(L"rtmp://", 0) == 0 || url.rfind(L"rtmps://", 0) == 0) {
            inputOptions += L"-rtmp_enhanced_codecs hvc1 ";
        }
        std::wstring command = quote(ffmpegPath) + inputOptions + L"-i " + quote(url) +
            L" -map 0:v:0 -an -vf \"scale=1280:720:force_original_aspect_ratio=decrease,"
            L"pad=1280:720:(ow-iw)/2:(oh-ih)/2,format=bgra\" -pix_fmt bgra -f rawvideo pipe:1";
        std::vector<wchar_t> mutableCommand(command.begin(), command.end());
        mutableCommand.push_back(L'\0');

        PROCESS_INFORMATION process{};
        const BOOL created = CreateProcessW(ffmpegPath.c_str(), mutableCommand.data(), nullptr, nullptr, TRUE,
                                             CREATE_NO_WINDOW, nullptr, nullptr, &startup, &process);
        CloseHandle(writePipe);
        if (!created) {
            CloseHandle(readPipe);
            return false;
        }
        CloseHandle(process.hThread);
        process_ = process.hProcess;
        pipe_ = readPipe;
        running_ = true;
        reader_ = std::thread([this] { read_loop(); });
        return true;
    }

    void stop() {
        running_ = false;
        if (process_) TerminateProcess(process_, 0);
        if (pipe_) CancelIoEx(pipe_, nullptr);
        if (reader_.joinable()) reader_.join();
        if (pipe_) CloseHandle(pipe_);
        if (process_) CloseHandle(process_);
        pipe_ = nullptr;
        process_ = nullptr;
        target_ = nullptr;
    }

    bool running() const { return running_; }

    void paint(HDC dc, const RECT& target) {
        std::lock_guard<std::mutex> lock(frameMutex_);
        FillRect(dc, &target, static_cast<HBRUSH>(GetStockObject(BLACK_BRUSH)));
        if (frame_.empty()) {
            SetBkMode(dc, TRANSPARENT);
            SetTextColor(dc, RGB(145, 166, 193));
            DrawTextW(dc, L"选择视频源后双击或点击播放", -1, const_cast<RECT*>(&target),
                      DT_CENTER | DT_VCENTER | DT_SINGLELINE);
            return;
        }
        BITMAPINFO info{};
        info.bmiHeader.biSize = sizeof(BITMAPINFOHEADER);
        info.bmiHeader.biWidth = kFrameWidth;
        info.bmiHeader.biHeight = -kFrameHeight;
        info.bmiHeader.biPlanes = 1;
        info.bmiHeader.biBitCount = 32;
        info.bmiHeader.biCompression = BI_RGB;
        StretchDIBits(dc, target.left, target.top, target.right - target.left, target.bottom - target.top,
                      0, 0, kFrameWidth, kFrameHeight, frame_.data(), &info, DIB_RGB_COLORS, SRCCOPY);
    }

private:
    static std::wstring quote(const std::wstring& value) {
        return L"\"" + value + L"\"";
    }

    void read_loop() {
        const std::size_t frameSize = static_cast<std::size_t>(kFrameWidth) * kFrameHeight * 4;
        std::vector<unsigned char> next(frameSize);
        while (running_) {
            std::size_t offset = 0;
            while (running_ && offset < frameSize) {
                DWORD read = 0;
                const DWORD wanted = static_cast<DWORD>(std::min<std::size_t>(frameSize - offset, 1 << 20));
                if (!ReadFile(pipe_, next.data() + offset, wanted, &read, nullptr) || read == 0) {
                    running_ = false;
                    break;
                }
                offset += read;
            }
            if (offset == frameSize) {
                {
                    std::lock_guard<std::mutex> lock(frameMutex_);
                    frame_.swap(next);
                }
                next.resize(frameSize);
                PostMessageW(target_, WM_EYES_FRAME, 0, 0);
            }
        }
    }

    HWND target_ = nullptr;
    HANDLE process_ = nullptr;
    HANDLE pipe_ = nullptr;
    std::atomic<bool> running_{false};
    std::thread reader_;
    std::mutex frameMutex_;
    std::vector<unsigned char> frame_;
};

} // namespace eyes
