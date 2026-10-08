/* MIT license. Small Windows launcher; the controller remains editable Python. */
#ifndef UNICODE
#define UNICODE
#endif
#include <windows.h>
#include <stdio.h>
#include <wchar.h>

static int exists(const wchar_t *path) {
    DWORD attributes = GetFileAttributesW(path);
    return attributes != INVALID_FILE_ATTRIBUTES && !(attributes & FILE_ATTRIBUTE_DIRECTORY);
}

static int registered_python(HKEY hive, REGSAM view, wchar_t *result) {
    HKEY root;
    if (RegOpenKeyExW(hive, L"Software\\Python\\PythonCore", 0, KEY_READ | view, &root) != ERROR_SUCCESS)
        return 0;
    for (DWORD index = 0; ; ++index) {
        wchar_t version[128];
        DWORD length = 128;
        LONG code = RegEnumKeyExW(root, index, version, &length, NULL, NULL, NULL, NULL);
        if (code == ERROR_NO_MORE_ITEMS) break;
        if (code != ERROR_SUCCESS) continue;
        int major = 0, minor = 0;
        if (swscanf(version, L"%d.%d", &major, &minor) != 2 || major != 3 || minor < 10) continue;
        wchar_t key[256];
        swprintf(key, 256, L"%ls\\InstallPath", version);
        HKEY install;
        if (RegOpenKeyExW(root, key, 0, KEY_READ | view, &install) != ERROR_SUCCESS) continue;
        DWORD type = 0, bytes = 32768 * sizeof(wchar_t);
        code = RegQueryValueExW(install, L"WindowedExecutablePath", NULL, &type, (BYTE *)result, &bytes);
        if (code == ERROR_SUCCESS && type == REG_SZ && exists(result)) {
            RegCloseKey(install);
            RegCloseKey(root);
            return 1;
        }
        wchar_t directory[32768];
        bytes = sizeof(directory);
        code = RegQueryValueExW(install, NULL, NULL, &type, (BYTE *)directory, &bytes);
        RegCloseKey(install);
        if (code == ERROR_SUCCESS && type == REG_SZ && directory[0] && wcslen(directory) < 32600) {
            swprintf(result, 32768, L"%ls%lspythonw.exe", directory,
                     directory[wcslen(directory)-1] == L'\\' ? L"" : L"\\");
            if (exists(result)) {
                RegCloseKey(root);
                return 1;
            }
        }
    }
    RegCloseKey(root);
    return 0;
}

int WINAPI wWinMain(HINSTANCE instance, HINSTANCE previous, PWSTR arguments, int show) {
    (void)instance; (void)previous; (void)show;
    wchar_t directory[32768], script[32768], python[32768], command[65536];
    DWORD length = GetModuleFileNameW(NULL, directory, 32768);
    if (!length || length >= 32768) return 1;
    wchar_t *last = wcsrchr(directory, L'\\');
    if (!last) return 1;
    *last = L'\0';
    if (wcslen(directory) > 32500) return 1;
    swprintf(script, 32768, L"%ls\\AXON Control.pyw", directory);
    if (!exists(script)) {
        MessageBoxW(NULL, L"AXON Control.pyw is missing. Keep the launcher and source files in the same folder.",
                    L"AXON Control", MB_ICONERROR);
        return 1;
    }
    int found = registered_python(HKEY_CURRENT_USER, KEY_WOW64_64KEY, python)
             || registered_python(HKEY_LOCAL_MACHINE, KEY_WOW64_64KEY, python)
             || registered_python(HKEY_CURRENT_USER, KEY_WOW64_32KEY, python)
             || registered_python(HKEY_LOCAL_MACHINE, KEY_WOW64_32KEY, python);
    if (!found) {
        MessageBoxW(NULL, L"Python 3.10 or newer is required, including Tkinter. See README.md.",
                    L"AXON Control", MB_ICONINFORMATION);
        return 1;
    }
    swprintf(command, 65536, L"\"%ls\" \"%ls\" %ls", python, script, arguments);
    STARTUPINFOW startup = {0};
    PROCESS_INFORMATION process = {0};
    startup.cb = sizeof(startup);
    if (!CreateProcessW(python, command, NULL, NULL, FALSE, CREATE_NO_WINDOW,
                        NULL, directory, &startup, &process)) {
        MessageBoxW(NULL, L"Python could not be started. See README.md.", L"AXON Control", MB_ICONERROR);
        return 1;
    }
    CloseHandle(process.hThread);
    CloseHandle(process.hProcess);
    return 0;
}
