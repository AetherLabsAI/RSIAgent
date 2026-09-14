"""Trusted Windows guest helper: exact output, process-tree timeout, restricted verifier.

Uses CreateRestrictedToken/CreateProcessAsUser and a kill-on-close Job Object.
Reference: https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-createprocessasusera
This file contains no task data, model credentials, or evaluator code.
"""

import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path


def run(spec):
    import msvcrt

    import win32api
    import win32con
    import win32event
    import win32job
    import win32process
    import win32security
    import win32service

    token = None
    job = win32job.CreateJobObject(None, "")
    info = win32job.QueryInformationJobObject(
        job, win32job.JobObjectExtendedLimitInformation
    )
    info["BasicLimitInformation"]["LimitFlags"] |= (
        win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    )
    win32job.SetInformationJobObject(
        job, win32job.JobObjectExtendedLimitInformation, info
    )
    output_path = Path(spec["output"])
    process = thread = None
    rc = 125
    timed_out = False
    preserve_children = False
    start = time.monotonic()
    try:
        with output_path.open("wb") as output, open(os.devnull, "rb") as incoming:
            out_handle = msvcrt.get_osfhandle(output.fileno())
            in_handle = msvcrt.get_osfhandle(incoming.fileno())
            os.set_handle_inheritable(out_handle, True)
            os.set_handle_inheritable(in_handle, True)
            si = win32process.STARTUPINFO()
            si.dwFlags = win32con.STARTF_USESTDHANDLES | win32con.STARTF_USESHOWWINDOW
            si.wShowWindow = win32con.SW_HIDE
            si.hStdOutput = si.hStdError = out_handle
            si.hStdInput = in_handle
            si.lpDesktop = r"winsta0\default"
            environment = os.environ.copy()
            environment.update(spec.get("env") or {})
            environment["PYTHONIOENCODING"] = "utf-8"
            environment["PYTHONUNBUFFERED"] = "1"
            flags = (
                win32con.CREATE_SUSPENDED
                | win32con.CREATE_UNICODE_ENVIRONMENT
                | win32con.CREATE_NO_WINDOW
            )
            args = (
                None,
                subprocess.list2cmdline(spec["argv"]),
                None,
                None,
                True,
                flags,
                environment,
                spec.get("cwd"),
                si,
            )
            if spec.get("restricted"):
                current = win32security.OpenProcessToken(
                    win32api.GetCurrentProcess(), win32con.TOKEN_ALL_ACCESS
                )
                try:
                    user_sid = win32security.GetTokenInformation(
                        current, win32security.TokenUser
                    )[0]
                    groups = win32security.GetTokenInformation(
                        current, win32security.TokenGroups
                    )
                    logon_sid = next(
                        (
                            sid
                            for sid, attrs in groups
                            if attrs & 0xC0000000 == 0xC0000000
                        ),
                        user_sid,
                    )
                    admins = win32security.CreateWellKnownSid(
                        win32security.WinBuiltinAdministratorsSid, None
                    )
                    local_admins = win32security.ConvertStringSidToSid("S-1-5-114")
                    token = win32security.CreateRestrictedToken(
                        current, 1, [(admins, 0), (local_admins, 0)], None, None
                    )
                finally:
                    current.Close()
                # The image starts CUA under an elevated logon. Once the Admin
                # group is disabled, explicitly authorize that logon to the
                # interactive desktop (the documented CreateProcessAsUser flow).
                handles = [
                    win32service.OpenWindowStation(
                        "winsta0", False, win32con.READ_CONTROL | win32con.WRITE_DAC
                    ),
                    win32service.OpenDesktop(
                        "default", 0, False, win32con.READ_CONTROL | win32con.WRITE_DAC
                    ),
                ]
                try:
                    for handle in handles:
                        sd = win32security.GetUserObjectSecurity(
                            handle, win32security.DACL_SECURITY_INFORMATION
                        )
                        acl = sd.GetSecurityDescriptorDacl()
                        acl.AddAccessAllowedAce(
                            win32security.ACL_REVISION, win32con.GENERIC_ALL, logon_sid
                        )
                        sd.SetSecurityDescriptorDacl(True, acl, False)
                        win32security.SetUserObjectSecurity(
                            handle, win32security.DACL_SECURITY_INFORMATION, sd
                        )
                finally:
                    handles[1].CloseDesktop()
                    handles[0].CloseWindowStation()
                acl = win32security.ACL()
                acl.AddAccessAllowedAce(
                    win32security.ACL_REVISION, win32con.GENERIC_ALL, user_sid
                )
                acl.AddAccessAllowedAce(
                    win32security.ACL_REVISION,
                    win32con.GENERIC_ALL,
                    win32security.CreateWellKnownSid(
                        win32security.WinLocalSystemSid, None
                    ),
                )
                win32security.SetTokenInformation(
                    token, win32security.TokenDefaultDacl, acl
                )
                process, thread, pid, tid = win32process.CreateProcessAsUser(
                    token, *args
                )
            else:
                process, thread, pid, tid = win32process.CreateProcess(*args)
            # No model code executes until it belongs to the bounded process tree.
            win32job.AssignProcessToJobObject(job, process)
            win32process.ResumeThread(thread)
            waited = win32event.WaitForSingleObject(
                process, int(spec["timeout"] * 1000)
            )
            if waited == win32con.WAIT_TIMEOUT:
                timed_out = True
                rc = 124
            elif waited == win32con.WAIT_OBJECT_0:
                rc = win32process.GetExitCodeProcess(process)
                # Normal Programs may launch persistent applications/services,
                # just as RSIAgent's Linux GNU timeout wrapper allows. Only an
                # expired/failed harness action kills the whole process tree.
                info["BasicLimitInformation"][
                    "LimitFlags"
                ] &= ~win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                win32job.SetInformationJobObject(
                    job, win32job.JobObjectExtendedLimitInformation, info
                )
                preserve_children = True
            else:
                raise RuntimeError(f"Unexpected process wait result: {waited}")
            if timed_out:
                win32job.TerminateJobObject(job, rc)
            win32event.WaitForSingleObject(process, 10000)
    finally:
        # Includes the CREATE_SUSPENDED failure path; never leave a child running.
        if process is not None and not preserve_children:
            try:
                win32process.TerminateProcess(process, rc)
            except Exception:
                pass
        job.Close()
        for handle in (thread, process, token):
            if handle is not None:
                handle.Close()
    result = {
        "exit_code": rc,
        "timed_out": timed_out,
        "seconds": time.monotonic() - start,
        "output_b64": base64.b64encode(output_path.read_bytes()).decode("ascii"),
    }
    print("RSIAGENT_WINDOWS_RESULT:" + json.dumps(result), flush=True)


if __name__ == "__main__":
    run(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")))
