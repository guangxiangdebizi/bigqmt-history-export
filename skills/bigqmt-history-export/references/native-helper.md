# Native Helper Discovery and Invocation

## Contents

1. Preconditions
2. Process and module discovery
3. Helper signature discovery
4. MSVC `std::string` layout
5. Windows x64 call stub
6. Memory lifecycle
7. Version drift and diagnostics
8. Security properties

## 1. Preconditions

- Run on 64-bit Windows with a 64-bit Python process.
- Start and log in to BigQMT before scanning.
- Run the script at the same integrity level as `XtItClient.exe`.
- Expect exactly one target process unless `--pid` is supplied.
- Treat the implementation as version-sensitive; validate after every QMT update.

`--locate-only` performs discovery and opens the target process but never allocates remote call data or creates a remote thread.

## 2. Process and module discovery

The script uses Toolhelp snapshots:

- `TH32CS_SNAPPROCESS` to find `XtItClient.exe`
- `TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32` to find `FormulaLib.dll`

It records module base, image size and module path, then opens the process with:

```text
PROCESS_CREATE_THREAD
PROCESS_QUERY_INFORMATION
PROCESS_VM_OPERATION
PROCESS_VM_READ
PROCESS_VM_WRITE
```

The helper address is always calculated as:

```text
helper_address = live_module_base + helper_rva
```

Never persist `helper_address`. ASLR changes the live base between processes.

## 3. Helper signature discovery

The public DLL export table does not expose the four-string Python helper. The implementation instead follows its Boost.Python registration:

1. Read the live `FormulaLib.dll` image.
2. Find all occurrences of `download_history_data\0`.
3. Find RIP-relative `lea rcx, [rip+disp32]` instructions targeting that string.
4. Search shortly backward for a RIP-relative `lea rbx, [rip+disp32]` used to load the registered function.
5. Resolve that target RVA.
6. Verify the candidate starts with the expected saved-register prologue, allowing an optional redundant REX prefix.
7. Require exactly one unique candidate.

The verified prologue pattern represents:

```text
push rbp
push rbx
push rsi
push rdi
push r12
push r13
push r14
push r15
```

If no candidate or multiple candidates remain, stop. Do not select the first match or fall back to a historical hard-coded RVA.

## 4. MSVC `std::string` layout

The helper accepts four `std::string` values by value:

```text
code, period, start, end
```

All supported arguments fit MSVC's short-string optimization (SSO):

```text
offset  size  value
0x00    16    inline bytes, NUL padded
0x10    8     string length
0x18    8     capacity = 15
```

The bundled builder accepts ASCII values of at most 15 bytes. This covers codes such as `600000.SH`, periods such as `1m`, and 8/14-digit dates. Reject longer or non-ASCII strings rather than guessing heap-owned `std::string` internals.

Four objects occupy offsets `0x00`, `0x20`, `0x40`, `0x60` in one remote allocation.

## 5. Windows x64 call stub

Windows x64 passes the first four arguments in:

```text
RCX, RDX, R8, R9
```

The generated stub:

1. subtracts `0x28` from RSP for shadow space and alignment;
2. loads the four remote object addresses into argument registers;
3. loads the live helper address into RAX;
4. calls RAX;
5. zero-extends the boolean return into EAX;
6. restores RSP and returns.

`CreateRemoteThread` starts at the stub. The thread exit code becomes the helper's boolean result.

Return value `1` means the helper accepted the request. It does not guarantee that the server authorized the range, the cache is complete, or any bars exist.

## 6. Memory lifecycle

For every trigger:

1. Allocate one remote region with `VirtualAllocEx`.
2. Write four SSO strings and the call stub with `WriteProcessMemory`.
3. Verify the complete byte count was written.
4. Create and wait for the remote thread with a finite timeout.
5. Read the thread exit code.
6. Close the thread handle.
7. Free the remote allocation with `VirtualFreeEx(..., MEM_RELEASE)` in a `finally` block.

The reusable session owns one process handle and closes it on context exit. A QMT restart invalidates the handle and all resolved addresses; create a new session.

## 7. Version drift and diagnostics

| Failure | Action |
|---|---|
| process not running | start/login QMT; do not start GUI automation |
| multiple processes | identify the active one and pass `--pid` |
| module not loaded | wait for QMT initialization or verify edition/version |
| helper not unique | capture module version/hash and update signature logic |
| access denied | align integrity levels; do not disable unrelated OS protections blindly |
| remote thread timeout | stop the batch, verify QMT responsiveness, restart from a clean session |
| result 0 | validate arguments and QMT runtime; do not treat it as a data result |

After changing signature logic, first run `--locate-only`, then one code/one week, then a four-code stress smoke. Never begin a full universe from an unverified candidate.

## 8. Security properties

The script operates only on a local process chosen by executable name or explicit PID. It does not download or execute third-party shellcode, expose a listener, persist injected code, or modify QMT files on disk.

The call stub is deterministic and limited to one already-loaded function. Even so, process memory access is privileged and version-sensitive. Inspect the source, keep QMT and the script under the same user account, and publish only module-relative discovery logic, never a live absolute address or process dump.
