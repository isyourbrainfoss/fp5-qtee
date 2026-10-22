#!/usr/bin/env python3
"""Run one PAM authentication against a test service (default
fp5-fprint-test) so pam_fprintd can be tried without touching sudo.

  fp5-pam-test [service] [user]

Exit 0 = authenticated. The test service also has the password after
the fingerprint, so a failed or skipped touch falls back to it.
"""
import ctypes
import ctypes.util
import getpass
import os
import pwd
import sys

PAM_PROMPT_ECHO_OFF, PAM_PROMPT_ECHO_ON, PAM_ERROR_MSG, PAM_TEXT_INFO = 1, 2, 3, 4


class PamMessage(ctypes.Structure):
    _fields_ = [("msg_style", ctypes.c_int), ("msg", ctypes.c_char_p)]


class PamResponse(ctypes.Structure):
    _fields_ = [("resp", ctypes.c_char_p), ("resp_retcode", ctypes.c_int)]


CONV = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_int,
                        ctypes.POINTER(ctypes.POINTER(PamMessage)),
                        ctypes.POINTER(ctypes.POINTER(PamResponse)), ctypes.c_void_p)


class PamConv(ctypes.Structure):
    _fields_ = [("conv", CONV), ("appdata_ptr", ctypes.c_void_p)]


def main() -> int:
    service = sys.argv[1] if len(sys.argv) > 1 else "fp5-fprint-test"
    user = sys.argv[2] if len(sys.argv) > 2 else pwd.getpwuid(os.getuid()).pw_name
    libpam = ctypes.CDLL(ctypes.util.find_library("pam") or "libpam.so.0")
    libc = ctypes.CDLL(ctypes.util.find_library("c"))
    libc.calloc.restype = ctypes.c_void_p
    libc.strdup.restype = ctypes.c_void_p
    libc.strdup.argtypes = [ctypes.c_char_p]

    def conv(n, msgs, resp, _data):
        arr = libc.calloc(n, ctypes.sizeof(PamResponse))
        resp[0] = ctypes.cast(arr, ctypes.POINTER(PamResponse))
        for i in range(n):
            m = msgs[i].contents
            text = (m.msg or b"").decode(errors="replace")
            if m.msg_style == PAM_PROMPT_ECHO_OFF:
                answer = getpass.getpass(text)
            elif m.msg_style == PAM_PROMPT_ECHO_ON:
                answer = input(text)
            else:
                print(text, flush=True)
                continue
            resp[0][i].resp = ctypes.cast(libc.strdup(answer.encode()), ctypes.c_char_p)
            resp[0][i].resp_retcode = 0
        return 0

    handle = ctypes.c_void_p()
    pc = PamConv(CONV(conv), None)
    libpam.pam_start.argtypes = [ctypes.c_char_p, ctypes.c_char_p,
                                 ctypes.POINTER(PamConv), ctypes.POINTER(ctypes.c_void_p)]
    libpam.pam_strerror.restype = ctypes.c_char_p
    rc = libpam.pam_start(service.encode(), user.encode(), ctypes.byref(pc), ctypes.byref(handle))
    if rc != 0:
        print(f"pam_start failed rc={rc}")
        return 2
    rc = libpam.pam_authenticate(handle, 0)
    msg = libpam.pam_strerror(handle, rc).decode()
    libpam.pam_end(handle, rc)
    print(f"PAM {service} user={user}: rc={rc} ({msg})")
    return 0 if rc == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
