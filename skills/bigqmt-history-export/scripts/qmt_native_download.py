#!/usr/bin/env python3
"""Trigger Big-QMT's in-process ``download_history_data`` without GUI use.

The public FormulaLib export does not expose the four-string Python helper.
This tool locates the helper from its live Boost.Python registration and calls
it in XtItClient.exe with four MSVC ``std::string`` objects.  The lookup is
signature based so it is not tied to one ASLR base or one hard-coded RVA.
"""

import argparse
import ctypes
import os
import struct
from ctypes import wintypes


PROCESS_CREATE_THREAD = 0x0002
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_OPERATION = 0x0008
PROCESS_VM_READ = 0x0010
PROCESS_VM_WRITE = 0x0020
MEM_COMMIT = 0x1000
MEM_RESERVE = 0x2000
MEM_RELEASE = 0x8000
PAGE_EXECUTE_READWRITE = 0x40
WAIT_OBJECT_0 = 0
TH32CS_SNAPPROCESS = 0x00000002
TH32CS_SNAPMODULE = 0x00000008
TH32CS_SNAPMODULE32 = 0x00000010
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


class MODULEENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("th32ModuleID", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("GlblcntUsage", wintypes.DWORD),
        ("ProccntUsage", wintypes.DWORD),
        ("modBaseAddr", ctypes.POINTER(ctypes.c_byte)),
        ("modBaseSize", wintypes.DWORD),
        ("hModule", wintypes.HMODULE),
        ("szModule", wintypes.WCHAR * 256),
        ("szExePath", wintypes.WCHAR * 260),
    ]


def configure_api():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(PROCESSENTRY32W),
    ]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(PROCESSENTRY32W),
    ]
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.Module32FirstW.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(MODULEENTRY32W),
    ]
    kernel32.Module32FirstW.restype = wintypes.BOOL
    kernel32.Module32NextW.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(MODULEENTRY32W),
    ]
    kernel32.Module32NextW.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.ReadProcessMemory.argtypes = [
        wintypes.HANDLE,
        wintypes.LPCVOID,
        wintypes.LPVOID,
        ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    kernel32.ReadProcessMemory.restype = wintypes.BOOL
    kernel32.VirtualAllocEx.argtypes = [
        wintypes.HANDLE,
        wintypes.LPVOID,
        ctypes.c_size_t,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    kernel32.VirtualAllocEx.restype = wintypes.LPVOID
    kernel32.VirtualFreeEx.argtypes = [
        wintypes.HANDLE,
        wintypes.LPVOID,
        ctypes.c_size_t,
        wintypes.DWORD,
    ]
    kernel32.VirtualFreeEx.restype = wintypes.BOOL
    kernel32.WriteProcessMemory.argtypes = [
        wintypes.HANDLE,
        wintypes.LPVOID,
        wintypes.LPCVOID,
        ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    kernel32.WriteProcessMemory.restype = wintypes.BOOL
    kernel32.CreateRemoteThread.argtypes = [
        wintypes.HANDLE,
        wintypes.LPVOID,
        ctypes.c_size_t,
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.CreateRemoteThread.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.GetExitCodeThread.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeThread.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    return kernel32


def find_process(kernel32, executable_name):
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())
    matches = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        available = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while available:
            if entry.szExeFile.lower() == executable_name.lower():
                matches.append(entry.th32ProcessID)
            available = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    if not matches:
        raise RuntimeError("process not running: %s" % executable_name)
    if len(matches) != 1:
        raise RuntimeError("multiple %s processes found: %s" % (executable_name, matches))
    return matches[0]


def find_module(kernel32, pid, module_name):
    snapshot = kernel32.CreateToolhelp32Snapshot(
        TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid
    )
    if snapshot == INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        entry = MODULEENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        available = kernel32.Module32FirstW(snapshot, ctypes.byref(entry))
        while available:
            if entry.szModule.lower() == module_name.lower():
                return (
                    ctypes.cast(entry.modBaseAddr, ctypes.c_void_p).value,
                    entry.modBaseSize,
                    entry.szExePath,
                )
            available = kernel32.Module32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    raise RuntimeError("module not loaded in target: %s" % module_name)


def read_memory(kernel32, process, address, size):
    buffer = ctypes.create_string_buffer(size)
    received = ctypes.c_size_t()
    if not kernel32.ReadProcessMemory(
        process,
        ctypes.c_void_p(address),
        buffer,
        size,
        ctypes.byref(received),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    if received.value != size:
        raise RuntimeError("short ReadProcessMemory: %d of %d" % (received.value, size))
    return buffer.raw


def find_all(data, needle):
    offset = 0
    while True:
        offset = data.find(needle, offset)
        if offset < 0:
            return
        yield offset
        offset += 1


def rip_target(image, instruction_rva):
    displacement = struct.unpack_from("<i", image, instruction_rva + 3)[0]
    return instruction_rva + 7 + displacement


def locate_download_helper(image):
    string_rvas = list(find_all(image, b"download_history_data\0"))
    candidates = []
    for string_rva in string_rvas:
        for xref_rva in find_all(image, b"\x48\x8d\x0d"):
            if rip_target(image, xref_rva) != string_rva:
                continue
            start = max(0, xref_rva - 0x100)
            prior = image.rfind(b"\x48\x8d\x1d", start, xref_rva)
            if prior < 0:
                continue
            function_rva = rip_target(image, prior)
            prologue = image[function_rva : function_rva + 13]
            expected = b"\x55\x53\x56\x57\x41\x54\x41\x55\x41\x56\x41\x57"
            # MSVC may emit a redundant REX prefix before ``push rbp``.
            if prologue.startswith(expected) or prologue.startswith(b"\x40" + expected):
                candidates.append(function_rva)
    candidates = sorted(set(candidates))
    if len(candidates) != 1:
        raise RuntimeError(
            "could not uniquely locate download_history_data helper: %s"
            % [hex(item) for item in candidates]
        )
    return candidates[0]


def msvc_string(value):
    raw = value.encode("ascii")
    if len(raw) > 15:
        raise ValueError("native helper argument exceeds MSVC SSO limit: %r" % value)
    return raw.ljust(16, b"\0") + struct.pack("<QQ", len(raw), 15)


def build_stub(function_address, arguments):
    return b"".join(
        [
            b"\x48\x83\xec\x28",  # sub rsp, 0x28
            b"\x48\xb9" + struct.pack("<Q", arguments[0]),
            b"\x48\xba" + struct.pack("<Q", arguments[1]),
            b"\x49\xb8" + struct.pack("<Q", arguments[2]),
            b"\x49\xb9" + struct.pack("<Q", arguments[3]),
            b"\x48\xb8" + struct.pack("<Q", function_address),
            b"\xff\xd0",  # call rax
            b"\x0f\xb6\xc0",  # movzx eax, al
            b"\x48\x83\xc4\x28\xc3",
        ]
    )


def run_remote_thread(kernel32, process, address, timeout_ms):
    thread_id = wintypes.DWORD()
    thread = kernel32.CreateRemoteThread(
        process,
        None,
        0,
        ctypes.c_void_p(address),
        None,
        0,
        ctypes.byref(thread_id),
    )
    if not thread:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        wait_result = kernel32.WaitForSingleObject(thread, timeout_ms)
        if wait_result != WAIT_OBJECT_0:
            raise TimeoutError("native QMT download helper did not return")
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeThread(thread, ctypes.byref(exit_code)):
            raise ctypes.WinError(ctypes.get_last_error())
        return exit_code.value
    finally:
        kernel32.CloseHandle(thread)


class NativeDownloadSession:
    """Reuse one XtItClient handle and one resolved helper across many symbols."""

    def __init__(self, pid=None):
        self.kernel32 = configure_api()
        self.pid = pid or find_process(self.kernel32, "XtItClient.exe")
        self.module_base, self.module_size, self.module_path = find_module(
            self.kernel32, self.pid, "FormulaLib.dll"
        )
        access = (
            PROCESS_CREATE_THREAD
            | PROCESS_QUERY_INFORMATION
            | PROCESS_VM_OPERATION
            | PROCESS_VM_READ
            | PROCESS_VM_WRITE
        )
        self.process = self.kernel32.OpenProcess(access, False, self.pid)
        if not self.process:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            image = read_memory(
                self.kernel32,
                self.process,
                self.module_base,
                self.module_size,
            )
            self.helper_rva = locate_download_helper(image)
            self.helper_address = self.module_base + self.helper_rva
        except Exception:
            self.close()
            raise

    def close(self):
        process = getattr(self, "process", None)
        if process:
            self.kernel32.CloseHandle(process)
            self.process = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def trigger(self, code, period, start, end, timeout_ms=30000):
        if not self.process:
            raise RuntimeError("native download session is closed")
        objects = b"".join(
            msvc_string(value) for value in (code, period, start, end)
        )
        allocation_size = len(objects) + 256
        remote = self.kernel32.VirtualAllocEx(
            self.process,
            None,
            allocation_size,
            MEM_COMMIT | MEM_RESERVE,
            PAGE_EXECUTE_READWRITE,
        )
        if not remote:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            remote_address = int(remote)
            arguments = [
                remote_address + offset for offset in (0, 0x20, 0x40, 0x60)
            ]
            stub = build_stub(self.helper_address, arguments)
            stub_address = remote_address + len(objects)
            payload = objects + stub
            written = ctypes.c_size_t()
            if not self.kernel32.WriteProcessMemory(
                self.process,
                ctypes.c_void_p(remote_address),
                payload,
                len(payload),
                ctypes.byref(written),
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            if written.value != len(payload):
                raise RuntimeError("short WriteProcessMemory")
            result = run_remote_thread(
                self.kernel32,
                self.process,
                stub_address,
                timeout_ms,
            )
        finally:
            self.kernel32.VirtualFreeEx(
                self.process, remote, 0, MEM_RELEASE
            )
        return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("code", nargs="?", help="QMT code such as 600000.SH")
    parser.add_argument("--period", default="1m")
    parser.add_argument("--start", help="YYYYMMDD or YYYYMMDDhhmmss")
    parser.add_argument("--end", help="YYYYMMDD or YYYYMMDDhhmmss")
    parser.add_argument("--pid", type=int)
    parser.add_argument("--timeout-ms", type=int, default=30000)
    parser.add_argument(
        "--locate-only",
        action="store_true",
        help="Locate the live helper and exit without triggering a download.",
    )
    args = parser.parse_args()

    if args.locate_only:
        with NativeDownloadSession(args.pid) as session:
            print(
                "pid=%d module=%s module_size=%d helper_rva=0x%x located=1"
                % (
                    session.pid,
                    os.path.basename(session.module_path),
                    session.module_size,
                    session.helper_rva,
                )
            )
        return

    if not args.code or not args.start or not args.end:
        parser.error("code, --start and --end are required unless --locate-only is used")

    code = args.code.strip().upper()
    if not code or "." not in code:
        raise ValueError("code must include its QMT market suffix")
    if args.period not in ("1m", "5m", "1d"):
        raise ValueError("unsupported period: %s" % args.period)
    for name, value in (("start", args.start), ("end", args.end)):
        if len(value) not in (8, 14) or not value.isdigit():
            raise ValueError("%s must be YYYYMMDD or YYYYMMDDhhmmss" % name)

    with NativeDownloadSession(args.pid) as session:
        result = session.trigger(
            code,
            args.period,
            args.start,
            args.end,
            timeout_ms=args.timeout_ms,
        )

    print(
        "pid=%d module=%s helper_rva=0x%x code=%s period=%s result=%d"
        % (
            session.pid,
            os.path.basename(session.module_path),
            session.helper_rva,
            code,
            args.period,
            result,
        )
    )
    if result != 1:
        raise RuntimeError("QMT download_history_data rejected the request")


if __name__ == "__main__":
    main()
