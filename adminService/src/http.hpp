#pragma once

#ifndef UNICODE
#define UNICODE
#endif

#include <windows.h>
#include <winhttp.h>

#include <stdexcept>
#include <string>
#include <iterator>

namespace eyes {

struct HttpResponse {
    DWORD status = 0;
    std::string body;
};

class WinHttpClient {
public:
    static HttpResponse request(const std::wstring& method, const std::wstring& url,
                                const std::string& body = {}) {
        URL_COMPONENTS parts{};
        parts.dwStructSize = sizeof(parts);
        wchar_t host[256]{};
        wchar_t path[4096]{};
        wchar_t extra[2048]{};
        parts.lpszHostName = host;
        parts.dwHostNameLength = static_cast<DWORD>(std::size(host));
        parts.lpszUrlPath = path;
        parts.dwUrlPathLength = static_cast<DWORD>(std::size(path));
        parts.lpszExtraInfo = extra;
        parts.dwExtraInfoLength = static_cast<DWORD>(std::size(extra));
        if (!WinHttpCrackUrl(url.c_str(), static_cast<DWORD>(url.size()), 0, &parts)) {
            throw std::runtime_error("invalid MediaService URL");
        }

        HINTERNET session = WinHttpOpen(L"EyesAdminService/1.0", WINHTTP_ACCESS_TYPE_AUTOMATIC_PROXY,
                                        WINHTTP_NO_PROXY_NAME, WINHTTP_NO_PROXY_BYPASS, 0);
        if (!session) throw std::runtime_error("WinHttpOpen failed");
        WinHttpSetTimeouts(session, 5000, 5000, 5000, 15000);
        HINTERNET connect = WinHttpConnect(session, std::wstring(parts.lpszHostName, parts.dwHostNameLength).c_str(),
                                           parts.nPort, 0);
        if (!connect) {
            WinHttpCloseHandle(session);
            throw std::runtime_error("WinHttpConnect failed");
        }
        std::wstring target(parts.lpszUrlPath, parts.dwUrlPathLength);
        if (parts.dwExtraInfoLength) target.append(parts.lpszExtraInfo, parts.dwExtraInfoLength);
        const DWORD flags = parts.nScheme == INTERNET_SCHEME_HTTPS ? WINHTTP_FLAG_SECURE : 0;
        HINTERNET request = WinHttpOpenRequest(connect, method.c_str(), target.c_str(), nullptr,
                                               WINHTTP_NO_REFERER, WINHTTP_DEFAULT_ACCEPT_TYPES, flags);
        if (!request) {
            WinHttpCloseHandle(connect);
            WinHttpCloseHandle(session);
            throw std::runtime_error("WinHttpOpenRequest failed");
        }
        const wchar_t* headers = body.empty() ? WINHTTP_NO_ADDITIONAL_HEADERS
                                               : L"Content-Type: application/json; charset=utf-8\r\n";
        const DWORD headerLength = body.empty() ? 0 : static_cast<DWORD>(-1L);
        void* data = body.empty() ? WINHTTP_NO_REQUEST_DATA : const_cast<char*>(body.data());
        const DWORD dataLength = static_cast<DWORD>(body.size());
        bool ok = WinHttpSendRequest(request, headers, headerLength, data, dataLength, dataLength, 0) &&
                  WinHttpReceiveResponse(request, nullptr);
        if (!ok) {
            WinHttpCloseHandle(request);
            WinHttpCloseHandle(connect);
            WinHttpCloseHandle(session);
            throw std::runtime_error("MediaService request failed");
        }

        HttpResponse result;
        DWORD statusSize = sizeof(result.status);
        WinHttpQueryHeaders(request, WINHTTP_QUERY_STATUS_CODE | WINHTTP_QUERY_FLAG_NUMBER,
                            WINHTTP_HEADER_NAME_BY_INDEX, &result.status, &statusSize, WINHTTP_NO_HEADER_INDEX);
        for (;;) {
            DWORD available = 0;
            if (!WinHttpQueryDataAvailable(request, &available) || available == 0) break;
            const std::size_t offset = result.body.size();
            result.body.resize(offset + available);
            DWORD read = 0;
            if (!WinHttpReadData(request, result.body.data() + offset, available, &read)) break;
            result.body.resize(offset + read);
        }
        WinHttpCloseHandle(request);
        WinHttpCloseHandle(connect);
        WinHttpCloseHandle(session);
        return result;
    }
};

} // namespace eyes
