"""Windows task launcher that runs the interactive application without elevation."""

import ctypes
import ctypes.wintypes as wt
import subprocess


class STARTUPINFO(ctypes.Structure):
    _fields_ = [
        ("cb", wt.DWORD), ("lpReserved", wt.LPWSTR), ("lpDesktop", wt.LPWSTR),
        ("lpTitle", wt.LPWSTR), ("dwX", wt.DWORD), ("dwY", wt.DWORD),
        ("dwXSize", wt.DWORD), ("dwYSize", wt.DWORD),
        ("dwXCountChars", wt.DWORD), ("dwYCountChars", wt.DWORD),
        ("dwFillAttribute", wt.DWORD), ("dwFlags", wt.DWORD),
        ("wShowWindow", wt.WORD), ("cbReserved2", wt.WORD),
        ("lpReserved2", ctypes.c_void_p), ("hStdInput", wt.HANDLE),
        ("hStdOutput", wt.HANDLE), ("hStdError", wt.HANDLE),
    ]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", wt.HANDLE), ("hThread", wt.HANDLE),
                ("dwProcessId", wt.DWORD), ("dwThreadId", wt.DWORD)]


def run_without_elevation(command: list[str], logger) -> int | None:
    """Return None when unelevated; otherwise use this user's interactive shell token."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    security = ctypes.WinDLL("advapi32", use_last_error=True)
    userenv = ctypes.WinDLL("userenv", use_last_error=True)
    desktop = ctypes.WinDLL("user32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wt.HANDLE
    kernel.CloseHandle.argtypes = [wt.HANDLE]
    kernel.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
    kernel.WaitForSingleObject.restype = wt.DWORD
    kernel.GetExitCodeProcess.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
    kernel.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    kernel.OpenProcess.restype = wt.HANDLE
    desktop.GetShellWindow.restype = wt.HWND
    desktop.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
    security.OpenProcessToken.argtypes = [wt.HANDLE, wt.DWORD, ctypes.POINTER(wt.HANDLE)]
    security.GetTokenInformation.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                            wt.DWORD, ctypes.POINTER(wt.DWORD)]
    security.CreateProcessWithTokenW.argtypes = [
        wt.HANDLE, wt.DWORD, wt.LPCWSTR, wt.LPWSTR, wt.DWORD, ctypes.c_void_p,
        wt.LPCWSTR, ctypes.POINTER(STARTUPINFO), ctypes.POINTER(PROCESS_INFORMATION),
    ]
    security.EqualSid.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    security.DuplicateTokenEx.argtypes = [wt.HANDLE, wt.DWORD, ctypes.c_void_p,
                                        ctypes.c_int, ctypes.c_int, ctypes.POINTER(wt.HANDLE)]
    userenv.CreateEnvironmentBlock.argtypes = [ctypes.POINTER(ctypes.c_void_p), wt.HANDLE, wt.BOOL]
    userenv.DestroyEnvironmentBlock.argtypes = [ctypes.c_void_p]
    token = wt.HANDLE()
    shell_token = wt.HANDLE()
    standard = wt.HANDLE()
    shell_process = None
    environment = ctypes.c_void_p()
    process = PROCESS_INFORMATION()
    try:
        if not security.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
            raise ctypes.WinError(ctypes.get_last_error())
        size = wt.DWORD()
        elevated = wt.DWORD()
        if not security.GetTokenInformation(token, 20, ctypes.byref(elevated),
                                            ctypes.sizeof(elevated), ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not elevated.value:
            return None
        shell = desktop.GetShellWindow()
        if not shell:
            raise RuntimeError("The user's interactive desktop is not ready")
        shell_pid = wt.DWORD()
        desktop.GetWindowThreadProcessId(shell, ctypes.byref(shell_pid))
        shell_process = kernel.OpenProcess(0x1000, False, shell_pid.value)
        if not shell_process or not security.OpenProcessToken(shell_process, 0x000B, ctypes.byref(shell_token)):
            raise ctypes.WinError(ctypes.get_last_error())

        def token_user(handle):
            size = wt.DWORD()
            security.GetTokenInformation(handle, 1, None, 0, ctypes.byref(size))
            buffer = ctypes.create_string_buffer(size.value)
            if not security.GetTokenInformation(handle, 1, buffer, len(buffer), ctypes.byref(size)):
                raise ctypes.WinError(ctypes.get_last_error())
            return buffer

        own_user = token_user(token)
        shell_user = token_user(shell_token)
        own_sid = ctypes.cast(own_user, ctypes.POINTER(ctypes.c_void_p))[0]
        shell_sid = ctypes.cast(shell_user, ctypes.POINTER(ctypes.c_void_p))[0]
        if not security.EqualSid(own_sid, shell_sid):
            raise RuntimeError("The interactive desktop belongs to a different user")
        if not security.GetTokenInformation(shell_token, 20, ctypes.byref(elevated),
                                            ctypes.sizeof(elevated), ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        if elevated.value:
            raise RuntimeError("A standard user token is required for the translator")
        if not security.DuplicateTokenEx(shell_token, 0x02000000, None, 2, 1, ctypes.byref(standard)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not userenv.CreateEnvironmentBlock(ctypes.byref(environment), standard, False):
            raise ctypes.WinError(ctypes.get_last_error())
        startup = STARTUPINFO()
        startup.cb = ctypes.sizeof(startup)
        startup.lpDesktop = "winsta0\\default"
        startup.dwFlags = 1
        startup.wShowWindow = 0
        arguments = ctypes.create_unicode_buffer(subprocess.list2cmdline(command))
        if not security.CreateProcessWithTokenW(
                standard, 0, command[0], arguments, 0x08000400, environment,
                None, ctypes.byref(startup), ctypes.byref(process)):
            raise ctypes.WinError(ctypes.get_last_error())
        logger.info("scheduled standard-user process started pid=%s", process.dwProcessId)
        if kernel.WaitForSingleObject(process.hProcess, 0xFFFFFFFF) == 0xFFFFFFFF:
            raise ctypes.WinError(ctypes.get_last_error())
        code = wt.DWORD()
        if not kernel.GetExitCodeProcess(process.hProcess, ctypes.byref(code)):
            raise ctypes.WinError(ctypes.get_last_error())
        return code.value
    finally:
        if environment:
            userenv.DestroyEnvironmentBlock(environment)
        for handle in (process.hThread, process.hProcess, standard, shell_token, shell_process, token):
            if handle:
                kernel.CloseHandle(handle)
