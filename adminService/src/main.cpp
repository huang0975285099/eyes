#ifndef UNICODE
#define UNICODE
#endif
#ifndef _UNICODE
#define _UNICODE
#endif

#include <windows.h>
#include <commctrl.h>
#include <shellapi.h>

#include <algorithm>
#include <chrono>
#include <cstring>
#include <filesystem>
#include <iomanip>
#include <memory>
#include <sstream>
#include <string>
#include <thread>
#include <vector>

#include "http.hpp"
#include "json.hpp"
#include "player.hpp"

using eyes::HttpResponse;
using eyes::Json;
using eyes::NativePlayer;
using eyes::WinHttpClient;

namespace {

constexpr wchar_t kWindowClass[] = L"EyesAdminServiceWindow";
constexpr wchar_t kVideoClass[] = L"EyesAdminServiceVideo";
constexpr UINT WM_API_RESULT = WM_APP + 12;

enum ControlId {
    ID_SERVER = 100,
    ID_REFRESH,
    ID_PLAY,
    ID_STOP,
    ID_TABS,
    ID_LIST,
};

struct Row {
    std::vector<std::wstring> columns;
    std::wstring playbackUrl;
};

struct ApiResult {
    int tab = 0;
    bool ok = false;
    std::wstring error;
    Json primary;
};

HWND g_main = nullptr;
HWND g_server = nullptr;
HWND g_refresh = nullptr;
HWND g_play = nullptr;
HWND g_stop = nullptr;
HWND g_tabs = nullptr;
HWND g_list = nullptr;
HWND g_video = nullptr;
HWND g_status = nullptr;
HFONT g_font = nullptr;
int g_tab = 0;
std::vector<Row> g_rows;
NativePlayer g_player;

std::wstring utf8_to_wide(const std::string& value) {
    if (value.empty()) return {};
    const int length = MultiByteToWideChar(CP_UTF8, 0, value.data(), static_cast<int>(value.size()), nullptr, 0);
    std::wstring result(length, L'\0');
    MultiByteToWideChar(CP_UTF8, 0, value.data(), static_cast<int>(value.size()), result.data(), length);
    return result;
}

std::wstring window_text(HWND handle) {
    const int length = GetWindowTextLengthW(handle);
    std::wstring result(length + 1, L'\0');
    GetWindowTextW(handle, result.data(), length + 1);
    result.resize(length);
    return result;
}

std::wstring trim_url(std::wstring value) {
    while (!value.empty() && (value.back() == L'/' || iswspace(value.back()))) value.pop_back();
    const auto first = std::find_if_not(value.begin(), value.end(), [](wchar_t ch) { return iswspace(ch); });
    value.erase(value.begin(), first);
    return value;
}

std::filesystem::path executable_dir() {
    wchar_t path[MAX_PATH]{};
    GetModuleFileNameW(nullptr, path, MAX_PATH);
    return std::filesystem::path(path).parent_path();
}

std::wstring config_path() { return (executable_dir() / L"adminService.ini").wstring(); }
std::wstring ffmpeg_path() { return (executable_dir() / L"ffmpeg.exe").wstring(); }

void set_status(const std::wstring& text) { SetWindowTextW(g_status, text.c_str()); }

std::wstring format_size(std::int64_t bytes) {
    std::wostringstream out;
    if (bytes >= 1024LL * 1024 * 1024) out << std::fixed << std::setprecision(1) << bytes / 1073741824.0 << L" GB";
    else if (bytes >= 1024LL * 1024) out << std::fixed << std::setprecision(1) << bytes / 1048576.0 << L" MB";
    else if (bytes >= 1024) out << std::fixed << std::setprecision(1) << bytes / 1024.0 << L" KB";
    else out << bytes << L" B";
    return out.str();
}

std::wstring format_duration(double seconds) {
    const int total = static_cast<int>(seconds);
    wchar_t text[32]{};
    swprintf(text, 32, L"%02d:%02d:%02d", total / 3600, total / 60 % 60, total % 60);
    return text;
}

void clear_columns() {
    while (ListView_DeleteColumn(g_list, 0)) {}
}

void add_column(int index, const wchar_t* title, int width) {
    LVCOLUMNW column{};
    column.mask = LVCF_TEXT | LVCF_WIDTH | LVCF_SUBITEM;
    column.pszText = const_cast<wchar_t*>(title);
    column.cx = width;
    column.iSubItem = index;
    ListView_InsertColumn(g_list, index, &column);
}

void show_rows(const std::vector<std::tuple<std::wstring, int>>& columns, std::vector<Row> rows) {
    ListView_DeleteAllItems(g_list);
    clear_columns();
    int columnIndex = 0;
    for (const auto& [title, width] : columns) add_column(columnIndex++, title.c_str(), width);
    g_rows = std::move(rows);
    for (std::size_t rowIndex = 0; rowIndex < g_rows.size(); ++rowIndex) {
        auto& row = g_rows[rowIndex];
        if (row.columns.empty()) continue;
        LVITEMW item{};
        item.mask = LVIF_TEXT | LVIF_PARAM;
        item.iItem = static_cast<int>(rowIndex);
        item.pszText = row.columns[0].data();
        item.lParam = static_cast<LPARAM>(rowIndex);
        const int inserted = ListView_InsertItem(g_list, &item);
        for (std::size_t col = 1; col < row.columns.size(); ++col) {
            ListView_SetItemText(g_list, inserted, static_cast<int>(col), row.columns[col].data());
        }
    }
}

void refresh_async() {
    const int requestedTab = g_tab;
    const std::wstring base = trim_url(window_text(g_server));
    if (base.empty()) {
        set_status(L"请输入MediaService地址");
        return;
    }
    WritePrivateProfileStringW(L"server", L"mediaServiceURL", base.c_str(), config_path().c_str());
    EnableWindow(g_refresh, FALSE);
    set_status(L"正在读取MediaService…");
    std::thread([requestedTab, base] {
        auto result = std::make_unique<ApiResult>();
        result->tab = requestedTab;
        try {
            const wchar_t* path = requestedTab == 0 ? L"/api/streams" : L"/api/segments";
            HttpResponse response = WinHttpClient::request(L"GET", base + path);
            if (response.status < 200 || response.status >= 300) {
                throw std::runtime_error("MediaService returned HTTP " + std::to_string(response.status));
            }
            result->primary = Json::parse(response.body);
            result->ok = true;
        } catch (const std::exception& error) {
            result->error = utf8_to_wide(error.what());
        }
        PostMessageW(g_main, WM_API_RESULT, 0, reinterpret_cast<LPARAM>(result.release()));
    }).detach();
}

void display_result(const ApiResult& result) {
    EnableWindow(g_refresh, TRUE);
    if (!result.ok) {
        set_status(L"连接失败：" + result.error);
        return;
    }
    if (result.tab != g_tab) return;
    std::vector<Row> rows;
    if (g_tab == 0) {
        for (const Json& item : result.primary.array()) {
            const std::wstring name = utf8_to_wide(item["display_name"].string());
            const std::wstring stream = utf8_to_wide(item["stream_name"].string());
            const std::wstring codec = utf8_to_wide(item["codec"].string());
            const std::wstring resolution = std::to_wstring(item["width"].integer()) + L"×" +
                                            std::to_wstring(item["height"].integer());
            rows.push_back({{name.empty() ? stream : name, codec.empty() ? L"-" : codec, resolution,
                             utf8_to_wide(item["mac"].string()), stream},
                            utf8_to_wide(item["rtmp_url"].string())});
        }
        show_rows({{L"视频源", 150}, {L"编码", 70}, {L"分辨率", 90}, {L"MAC", 130}, {L"流名称", 240}}, std::move(rows));
    } else if (g_tab == 1) {
        const std::wstring base = trim_url(window_text(g_server));
        for (const Json& item : result.primary.array()) {
            const auto id = item["id"].integer();
            rows.push_back({{utf8_to_wide(item["started_at"].string()), format_duration(item["duration"].number()),
                             format_size(item["file_size"].integer()), utf8_to_wide(item["display_name"].string()),
                             utf8_to_wide(item["stream_name"].string())},
                            base + L"/segments/" + std::to_wstring(id) + L"/video"});
        }
        show_rows({{L"开始时间", 170}, {L"时长", 80}, {L"大小", 90}, {L"视频源", 140}, {L"流名称", 240}}, std::move(rows));
    }
    set_status(L"已连接MediaService");
}

void play_selected() {
    const int selected = ListView_GetNextItem(g_list, -1, LVNI_SELECTED);
    if (selected < 0 || static_cast<std::size_t>(selected) >= g_rows.size()) {
        set_status(L"请先选择一个视频源或录像");
        return;
    }
    const std::wstring& url = g_rows[selected].playbackUrl;
    if (url.empty()) {
        set_status(L"当前记录没有可播放地址");
        return;
    }
    if (!std::filesystem::exists(ffmpeg_path())) {
        set_status(L"缺少ffmpeg.exe，请重新执行build.ps1");
        return;
    }
    if (g_player.start(g_video, ffmpeg_path(), url)) set_status(L"正在连接：" + url);
    else set_status(L"无法启动原生视频解码器");
}

void show_tab(int tab) {
    g_tab = tab;
    refresh_async();
}

LRESULT CALLBACK video_proc(HWND window, UINT message, WPARAM wParam, LPARAM lParam) {
    if (message == eyes::WM_EYES_FRAME) {
        g_player.acknowledge_frame();
        InvalidateRect(window, nullptr, FALSE);
        return 0;
    }
    if (message == WM_ERASEBKGND) return 1;
    if (message == WM_PAINT) {
        PAINTSTRUCT paint{};
        HDC dc = BeginPaint(window, &paint);
        RECT area{};
        GetClientRect(window, &area);
        g_player.paint(dc, area);
        EndPaint(window, &paint);
        return 0;
    }
    return DefWindowProcW(window, message, wParam, lParam);
}

void layout(HWND window) {
    RECT area{};
    GetClientRect(window, &area);
    const int width = area.right;
    const int height = area.bottom;
    MoveWindow(g_server, 12, 12, std::max(220, width - 430), 28, TRUE);
    MoveWindow(g_refresh, width - 405, 12, 90, 28, TRUE);
    MoveWindow(g_play, width - 305, 12, 90, 28, TRUE);
    MoveWindow(g_stop, width - 205, 12, 90, 28, TRUE);
    MoveWindow(g_tabs, 12, 50, width - 24, 30, TRUE);
    const int listWidth = std::min(620, std::max(360, width * 45 / 100));
    MoveWindow(g_list, 12, 86, listWidth, height - 122, TRUE);
    MoveWindow(g_video, listWidth + 22, 86, width - listWidth - 34, height - 122, TRUE);
    MoveWindow(g_status, 12, height - 28, width - 24, 20, TRUE);
}

LRESULT CALLBACK window_proc(HWND window, UINT message, WPARAM wParam, LPARAM lParam) {
    switch (message) {
    case WM_CREATE: {
        g_main = window;
        g_font = CreateFontW(-16, 0, 0, 0, FW_NORMAL, FALSE, FALSE, FALSE, DEFAULT_CHARSET,
                             OUT_DEFAULT_PRECIS, CLIP_DEFAULT_PRECIS, CLEARTYPE_QUALITY,
                             DEFAULT_PITCH | FF_DONTCARE, L"Microsoft YaHei UI");
        g_server = CreateWindowExW(WS_EX_CLIENTEDGE, L"EDIT", L"", WS_CHILD | WS_VISIBLE | ES_AUTOHSCROLL,
                                   0, 0, 0, 0, window, reinterpret_cast<HMENU>(ID_SERVER), nullptr, nullptr);
        g_refresh = CreateWindowW(L"BUTTON", L"刷新", WS_CHILD | WS_VISIBLE, 0, 0, 0, 0, window,
                                  reinterpret_cast<HMENU>(ID_REFRESH), nullptr, nullptr);
        g_play = CreateWindowW(L"BUTTON", L"播放", WS_CHILD | WS_VISIBLE, 0, 0, 0, 0, window,
                               reinterpret_cast<HMENU>(ID_PLAY), nullptr, nullptr);
        g_stop = CreateWindowW(L"BUTTON", L"停止", WS_CHILD | WS_VISIBLE, 0, 0, 0, 0, window,
                               reinterpret_cast<HMENU>(ID_STOP), nullptr, nullptr);
        g_tabs = CreateWindowW(WC_TABCONTROLW, L"", WS_CHILD | WS_VISIBLE, 0, 0, 0, 0, window,
                               reinterpret_cast<HMENU>(ID_TABS), nullptr, nullptr);
        const wchar_t* tabs[] = {L"实时监控", L"录像回放"};
        for (int i = 0; i < 2; ++i) {
            TCITEMW item{};
            item.mask = TCIF_TEXT;
            item.pszText = const_cast<wchar_t*>(tabs[i]);
            TabCtrl_InsertItem(g_tabs, i, &item);
        }
        g_list = CreateWindowExW(WS_EX_CLIENTEDGE, WC_LISTVIEWW, L"",
                                 WS_CHILD | WS_VISIBLE | LVS_REPORT | LVS_SINGLESEL | LVS_SHOWSELALWAYS,
                                 0, 0, 0, 0, window, reinterpret_cast<HMENU>(ID_LIST), nullptr, nullptr);
        ListView_SetExtendedListViewStyle(g_list, LVS_EX_FULLROWSELECT | LVS_EX_DOUBLEBUFFER | LVS_EX_GRIDLINES);
        g_video = CreateWindowExW(WS_EX_CLIENTEDGE, kVideoClass, L"", WS_CHILD | WS_VISIBLE,
                                  0, 0, 0, 0, window, nullptr, nullptr, nullptr);
        g_status = CreateWindowW(L"STATIC", L"尚未连接", WS_CHILD | WS_VISIBLE, 0, 0, 0, 0, window,
                                 nullptr, nullptr, nullptr);
        for (HWND child : {g_server, g_refresh, g_play, g_stop, g_tabs, g_list, g_video, g_status}) {
            SendMessageW(child, WM_SETFONT, reinterpret_cast<WPARAM>(g_font), TRUE);
        }
        wchar_t configured[1024]{};
        GetPrivateProfileStringW(L"server", L"mediaServiceURL", L"http://10.0.20.219:22222",
                                 configured, 1024, config_path().c_str());
        SetWindowTextW(g_server, configured);
        layout(window);
        PostMessageW(window, WM_COMMAND, ID_REFRESH, 0);
        return 0;
    }
    case WM_SIZE:
        layout(window);
        return 0;
    case WM_COMMAND:
        switch (LOWORD(wParam)) {
        case ID_REFRESH: refresh_async(); return 0;
        case ID_PLAY: play_selected(); return 0;
        case ID_STOP: g_player.stop(); InvalidateRect(g_video, nullptr, FALSE); set_status(L"已停止播放"); return 0;
        }
        break;
    case WM_NOTIFY: {
        auto* header = reinterpret_cast<NMHDR*>(lParam);
        if (header->hwndFrom == g_tabs && header->code == TCN_SELCHANGE) {
            show_tab(TabCtrl_GetCurSel(g_tabs));
            return 0;
        }
        if (header->hwndFrom == g_list && header->code == NM_DBLCLK) {
            play_selected();
            return 0;
        }
        break;
    }
    case WM_API_RESULT: {
        std::unique_ptr<ApiResult> result(reinterpret_cast<ApiResult*>(lParam));
        display_result(*result);
        return 0;
    }
    case WM_DESTROY:
        g_player.stop();
        if (g_font) DeleteObject(g_font);
        PostQuitMessage(0);
        return 0;
    }
    return DefWindowProcW(window, message, wParam, lParam);
}

} // namespace

int WINAPI wWinMain(HINSTANCE instance, HINSTANCE, PWSTR, int showCommand) {
    SetProcessDPIAware();
    INITCOMMONCONTROLSEX controls{sizeof(controls), ICC_LISTVIEW_CLASSES | ICC_TAB_CLASSES};
    InitCommonControlsEx(&controls);

    WNDCLASSW videoClass{};
    videoClass.hInstance = instance;
    videoClass.lpfnWndProc = video_proc;
    videoClass.lpszClassName = kVideoClass;
    videoClass.hCursor = LoadCursor(nullptr, IDC_ARROW);
    // 视频窗口由双缓冲播放器完整绘制，禁用系统背景刷以避免WM_PAINT前闪黑。
    videoClass.hbrBackground = nullptr;
    RegisterClassW(&videoClass);

    WNDCLASSW mainClass{};
    mainClass.hInstance = instance;
    mainClass.lpfnWndProc = window_proc;
    mainClass.lpszClassName = kWindowClass;
    mainClass.hCursor = LoadCursor(nullptr, IDC_ARROW);
    mainClass.hIcon = LoadIcon(nullptr, IDI_APPLICATION);
    mainClass.hbrBackground = reinterpret_cast<HBRUSH>(COLOR_WINDOW + 1);
    RegisterClassW(&mainClass);

    HWND window = CreateWindowExW(0, kWindowClass, L"千里眼监控管理客户端",
                                  WS_OVERLAPPEDWINDOW, CW_USEDEFAULT, CW_USEDEFAULT, 1380, 820,
                                  nullptr, nullptr, instance, nullptr);
    if (!window) return 1;
    ShowWindow(window, showCommand);
    UpdateWindow(window);

    MSG message{};
    while (GetMessageW(&message, nullptr, 0, 0) > 0) {
        TranslateMessage(&message);
        DispatchMessageW(&message);
    }
    return static_cast<int>(message.wParam);
}
