"""Windows AppContainer command runner, invoked only under an ordinary worker.

Uses documented Windows APIs. No credential access or model loop.
"""
import ctypes as C
from ctypes import wintypes as W
import json
import msvcrt
import os
from pathlib import Path
import subprocess
import sys
import time
import threading


P = C.c_void_p
SIZE = C.c_size_t
k = C.WinDLL('kernel32', use_last_error=True)
a = C.WinDLL('advapi32', use_last_error=True)
u = C.WinDLL('userenv', use_last_error=True)
ui = C.WinDLL('user32', use_last_error=True)


class Startup(C.Structure):
    _fields_ = [('cb', W.DWORD), ('reserved', W.LPWSTR), ('desktop', W.LPWSTR),
                ('title', W.LPWSTR), ('x', W.DWORD), ('y', W.DWORD),
                ('xs', W.DWORD), ('ys', W.DWORD), ('xc', W.DWORD),
                ('yc', W.DWORD), ('fill', W.DWORD), ('flags', W.DWORD),
                ('show', W.WORD), ('reserved_size', W.WORD), ('reserved2', P),
                ('stdin', W.HANDLE), ('stdout', W.HANDLE), ('stderr', W.HANDLE)]


class StartupEx(C.Structure):
    _fields_ = [('startup', Startup), ('attributes', P)]


class ProcessInfo(C.Structure):
    _fields_ = [('process', W.HANDLE), ('thread', W.HANDLE),
                ('pid', W.DWORD), ('tid', W.DWORD)]


class Capabilities(C.Structure):
    _fields_ = [('sid', P), ('capabilities', P), ('count', W.DWORD), ('reserved', W.DWORD)]


class SecurityAttributes(C.Structure):
    _fields_ = [('length',W.DWORD),('descriptor',P),('inherit',W.BOOL)]


class Luid(C.Structure):
    _fields_ = [('low', W.DWORD), ('high', W.LONG)]


class Privilege(C.Structure):
    _fields_ = [('count', W.DWORD), ('luid', Luid), ('attributes', W.DWORD)]


class BasicLimits(C.Structure):
    _fields_ = [('process_time', C.c_longlong), ('job_time', C.c_longlong),
                ('flags', W.DWORD), ('min_working_set', SIZE), ('max_working_set', SIZE),
                ('active_process_limit', W.DWORD), ('affinity', SIZE),
                ('priority', W.DWORD), ('scheduling', W.DWORD)]


class IoCounters(C.Structure):
    _fields_ = [(name, C.c_ulonglong) for name in ('read_ops', 'write_ops', 'other_ops', 'read_bytes', 'write_bytes', 'other_bytes')]


class ExtendedLimits(C.Structure):
    _fields_ = [('basic', BasicLimits), ('io', IoCounters), ('process_memory', SIZE),
                ('job_memory', SIZE), ('peak_process_memory', SIZE), ('peak_job_memory', SIZE)]


def function(dll, name, restype, *args):
    f = getattr(dll, name)
    f.restype, f.argtypes = restype, args
    return f


derive = function(u, 'DeriveAppContainerSidFromAppContainerName', C.c_long, W.LPCWSTR, C.POINTER(P))
create_profile = function(u, 'CreateAppContainerProfile', C.c_long, W.LPCWSTR, W.LPCWSTR, W.LPCWSTR, P, W.DWORD, C.POINTER(P))
delete_profile = function(u, 'DeleteAppContainerProfile', C.c_long, W.LPCWSTR)
profile_folder = function(u, 'GetAppContainerFolderPath', C.c_long, W.LPCWSTR, C.POINTER(W.LPWSTR))
task_free = function(C.WinDLL('ole32'), 'CoTaskMemFree', None, P)
sid_text = function(a, 'ConvertSidToStringSidW', W.BOOL, P, C.POINTER(W.LPWSTR))
free_sid = function(a, 'FreeSid', P, P)
local_free = function(k, 'LocalFree', P, P)
init_attributes = function(k, 'InitializeProcThreadAttributeList', W.BOOL, P, W.DWORD, W.DWORD, C.POINTER(SIZE))
update_attribute = function(k, 'UpdateProcThreadAttribute', W.BOOL, P, W.DWORD, SIZE, P, SIZE, P, P)
delete_attributes = function(k, 'DeleteProcThreadAttributeList', None, P)
create = function(k, 'CreateProcessW', W.BOOL, W.LPCWSTR, W.LPWSTR, P, P, W.BOOL, W.DWORD, P, W.LPCWSTR, P, C.POINTER(ProcessInfo))
close = function(k, 'CloseHandle', W.BOOL, W.HANDLE)
set_handle = function(k, 'SetHandleInformation', W.BOOL, W.HANDLE, W.DWORD, W.DWORD)
create_job = function(k, 'CreateJobObjectW', W.HANDLE, P, W.LPCWSTR)
set_job = function(k, 'SetInformationJobObject', W.BOOL, W.HANDLE, C.c_int, P, W.DWORD)
query_job = function(k, 'QueryInformationJobObject', W.BOOL, W.HANDLE, C.c_int, P, W.DWORD, P)
assign_job = function(k, 'AssignProcessToJobObject', W.BOOL, W.HANDLE, W.HANDLE)
terminate_job = function(k, 'TerminateJobObject', W.BOOL, W.HANDLE, W.UINT)
terminate_process = function(k, 'TerminateProcess', W.BOOL, W.HANDLE, W.UINT)
resume = function(k, 'ResumeThread', W.DWORD, W.HANDLE)
wait = function(k, 'WaitForSingleObject', W.DWORD, W.HANDLE, W.DWORD)
exit_code = function(k, 'GetExitCodeProcess', W.BOOL, W.HANDLE, C.POINTER(W.DWORD))
open_token = function(a, 'OpenProcessToken', W.BOOL, W.HANDLE, W.DWORD, C.POINTER(W.HANDLE))
get_token = function(a, 'GetTokenInformation', W.BOOL, W.HANDLE, C.c_int, P, W.DWORD, C.POINTER(W.DWORD))
lookup_privilege = function(a, 'LookupPrivilegeValueW', W.BOOL, W.LPCWSTR, W.LPCWSTR, C.POINTER(Luid))
adjust_privileges = function(a, 'AdjustTokenPrivileges', W.BOOL, W.HANDLE, W.BOOL, P, W.DWORD, P, P)
convert_sddl = function(a,'ConvertStringSecurityDescriptorToSecurityDescriptorW',W.BOOL,W.LPCWSTR,W.DWORD,C.POINTER(P),P)
get_station = function(ui,'GetProcessWindowStation',W.HANDLE)
set_station = function(ui,'SetProcessWindowStation',W.BOOL,W.HANDLE)
create_station = function(ui,'CreateWindowStationW',W.HANDLE,W.LPCWSTR,W.DWORD,W.DWORD,P)
close_station = function(ui,'CloseWindowStation',W.BOOL,W.HANDLE)
create_desktop = function(ui,'CreateDesktopW',W.HANDLE,W.LPCWSTR,P,P,W.DWORD,W.DWORD,P)
close_desktop = function(ui,'CloseDesktop',W.BOOL,W.HANDLE)


def check(value, label):
    if not value:
        raise OSError(C.get_last_error(), label)
    return value


def private_desktop(name, container_sid, supervisor_sid=None):
    """New objects only: never change the service or interactive station ACL."""
    token, needed, descriptor, user = W.HANDLE(),W.DWORD(),P(),W.LPWSTR()
    station, desktop = None,None
    original = check(get_station(),'current window station')
    try:
        check(open_token(W.HANDLE(-1),8,C.byref(token)),'current user token')
        get_token(token,1,None,0,C.byref(needed))
        buffer=C.create_string_buffer(needed.value)
        check(get_token(token,1,buffer,len(buffer),C.byref(needed)),'current user SID')
        check(sid_text(P.from_buffer(buffer),C.byref(user)),'current SID text')
        # Only this supervisor and this one container can open these objects.
        # No capability granted on existing Windows desktop objects.
        owner = supervisor_sid or user.value
        sddl='D:P(A;;GA;;;SY)(A;;GA;;;'+owner+')(A;;0x0002037f;;;'+container_sid+')S:(ML;;NW;;;LW)'
        check(convert_sddl(sddl,1,C.byref(descriptor),None),'private station descriptor')
        sa=SecurityAttributes(C.sizeof(SecurityAttributes),descriptor,False)
        station=check(create_station(name,1,0xF037F,C.byref(sa)),'create private window station')
        check(set_station(station),'select private station for desktop creation')
        local_free(descriptor);descriptor=P()
        sddl='D:P(A;;GA;;;SY)(A;;GA;;;'+owner+')(A;;0x000201ff;;;'+container_sid+')S:(ML;;NW;;;LW)'
        check(convert_sddl(sddl,1,C.byref(descriptor),None),'private desktop descriptor')
        sa.descriptor=descriptor
        desktop=check(create_desktop('Default',None,None,0,0xF01FF,C.byref(sa)),'create private desktop')
        check(set_station(original),'restore original process station')
        return station,desktop,name+'\\Default'
    except Exception:
        set_station(original)
        if desktop:
            close_desktop(desktop)
        if station:
            close_station(station)
        raise
    finally:
        if token:close(token)
        if descriptor:local_free(descriptor)
        if user:local_free(user)


def identity(name):
    sid = P()
    result = derive(name, C.byref(sid))
    if result != 0:
        raise OSError(result, 'DeriveAppContainerSidFromAppContainerName')
    text = W.LPWSTR()
    try:
        check(sid_text(sid, C.byref(text)), 'ConvertSidToStringSidW')
        return text.value
    finally:
        if text:
            local_free(text)
        free_sid(sid)


def confine_traversal(process, remove=False):
    token = W.HANDLE()
    check(open_token(process, 0x28, C.byref(token)), 'open child token')
    try:
        required, is_app = W.DWORD(), W.DWORD()
        check(get_token(token, 29, C.byref(is_app), 4, C.byref(required)), 'confirm AppContainer token')
        if is_app.value != 1:
            raise RuntimeError('Child is not an AppContainer')
        luid = Luid()
        check(lookup_privilege(None, 'SeChangeNotifyPrivilege', C.byref(luid)), 'resolve traversal privilege')
        def privileges():
            get_token(token, 3, None, 0, C.byref(required))
            buf = C.create_string_buffer(required.value)
            check(get_token(token, 3, buf, len(buf), C.byref(required)), 'read child privileges')
            count = W.DWORD.from_buffer(buf).value
            return [Luid.from_buffer_copy(buf.raw[4+i*12:12+i*12]) for i in range(count)]
        present = lambda: any((p.low, p.high) == (luid.low, luid.high) for p in privileges())
        if present() and remove:
            change = Privilege(1, luid, 4)  # REMOVE, not merely disable.
            C.set_last_error(0)
            check(adjust_privileges(token, False, C.byref(change), 0, None, None), 'remove traversal bypass')
            if C.get_last_error() or present():
                raise RuntimeError('Traversal bypass privilege remains available')
        return {'appcontainer': True, 'traversal_bypass_removed': not present()}
    finally:
        close(token)


def run(name, argv, cwd, env, output=None, timeout=120, cancelled=lambda: False,
        max_output=1024*1024, memory_limit=512*1024*1024, desktop_name=None):
    """Create an offline container, bound all descendants, and always report cleanup.

    The controller must provision exact per-container ACLs before this call.
    A command receipt is usable only when cleanup_confirmed is true.
    """
    if not 1 <= timeout <= 600 or not 1024 <= max_output <= 4*1024*1024:
        raise ValueError('Invalid command bounds')
    if C.windll.shell32.IsUserAnAdmin():
        raise RuntimeError('Commands must be hosted by an ordinary Windows account')
    sid, size, pi = P(), SIZE(), ProcessInfo()
    job, attributes = None, None
    station, desktop = None, None
    created = initialized = assigned = False
    files, fds, inherited_handles, readers = [], [], [], []
    chunks, output_size = [bytearray(), bytearray()], [0]
    output_lock, overflow = threading.Lock(), threading.Event()
    result = {'state': 'failed', 'cleanup_confirmed': False, 'errors': []}
    result['started_at'] = time.time()

    def drain(fd, index):
        try:
            with os.fdopen(fd, 'rb', buffering=0) as stream:
                while True:
                    block = stream.read(8192)
                    if not block:
                        break
                    with output_lock:
                        remaining = max(0, max_output-output_size[0])
                        chunks[index].extend(block[:remaining])
                        output_size[0] += len(block)
                        if output_size[0] > max_output:
                            overflow.set()
        except OSError as error:
            result['errors'].append('output reader: '+str(error.winerror or error.errno))

    try:
        if cancelled():
            result['state'] = 'cancelled'
            return result
        hr = create_profile(name, name, 'LOOM task command', None, 0, C.byref(sid))
        if hr:
            raise OSError(hr, 'create unique AppContainer profile')
        created = True
        container_sid = identity(name)
        result['private_desktop'] = desktop_name
        folder = W.LPWSTR()
        result['profile_path_hresult'] = profile_folder(container_sid,C.byref(folder))
        if folder:
            result['profile_path'] = folder.value
            task_free(folder)
        init_attributes(None, 2, 0, C.byref(size))
        if C.get_last_error() != 122:
            raise OSError(C.get_last_error(), 'attribute sizing')
        attributes = C.create_string_buffer(size.value)
        check(init_attributes(attributes, 2, 0, C.byref(size)), 'attribute initialize')
        initialized = True
        caps = Capabilities(sid, None, 0, 0)
        check(update_attribute(attributes, 0, 0x20009, C.byref(caps), C.sizeof(caps), None, None), 'security capabilities')
        null = open(os.devnull, 'rb')
        files.append(null)
        inherited_handles.append(msvcrt.get_osfhandle(null.fileno()))
        for index in range(2):
            read_fd, write_fd = os.pipe()
            fds.append(write_fd)
            inherited_handles.append(msvcrt.get_osfhandle(write_fd))
            reader = threading.Thread(target=drain, args=(read_fd,index), daemon=True)
            readers.append(reader)
            reader.start()
        for handle in inherited_handles:
            check(set_handle(handle, 1, 1), 'inherit exact stream handle')
        inherited = (W.HANDLE * 3)(*inherited_handles)
        check(update_attribute(attributes, 0, 0x20002, inherited, C.sizeof(inherited), None, None), 'explicit handle list')
        si = StartupEx()
        si.startup.cb = C.sizeof(si)
        si.startup.flags = 0x100
        si.startup.desktop = desktop_name
        si.startup.stdin, si.startup.stdout, si.startup.stderr = inherited_handles
        si.attributes = C.cast(attributes, P)
        job = check(create_job(None, None), 'create job')
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000 | 0x200 | 0x8  # kill on close, job memory, process count
        limits.basic.active_process_limit = 16
        limits.job_memory = memory_limit
        check(set_job(job, 9, C.byref(limits), C.sizeof(limits)), 'set command resource bounds')
        environment = C.create_unicode_buffer('\0'.join(key+'='+value for key,value in sorted(env.items(),key=lambda kv:kv[0].upper()))+'\0\0')
        command = C.create_unicode_buffer(subprocess.list2cmdline(argv))
        check(create(argv[0], command, None, None, True, 0x80000 | 0x400 | 0x08000000 | 0x4,
                     environment, str(cwd), C.byref(si), C.byref(pi)), 'create suspended AppContainer')
        result['pid'] = pi.pid
        check(assign_job(job, pi.process), 'assign descendants before resume')
        assigned = True
        result['verified_token'] = confine_traversal(pi.process)
        if cancelled():
            result['state'] = 'cancelled'
        else:
            if resume(pi.thread) == 0xffffffff:
                raise OSError(C.get_last_error(), 'resume')
            # Parent write handles must close for EOF after the entire job exits.
            for fd in fds:
                os.close(fd)
            fds.clear()
            deadline = time.monotonic()+timeout
            while True:
                if cancelled():
                    result['state'] = 'cancelled'
                    break
                if overflow.is_set():
                    result['state'] = 'output_limit'
                    break
                waited = wait(pi.process, 50)
                if waited == 0:
                    code = W.DWORD()
                    check(exit_code(pi.process,C.byref(code)), 'exit code')
                    result.update(state='exited', exit_code=code.value)
                    break
                if waited != 258:
                    raise OSError(C.get_last_error(), 'wait')
                if time.monotonic() >= deadline:
                    result['state'] = 'timed_out'
                    break
    except Exception as error:
        result['errors'].append(type(error).__name__+': '+str(error))
    finally:
        # Cleanup errors are accumulated; none can skip later handle closure.
        stopped = not bool(pi.process)
        if pi.process:
            if assigned:
                if not terminate_job(job, 125):
                    result['errors'].append('terminate job failed: '+str(C.get_last_error()))
                accounting = C.create_string_buffer(48)
                for attempt in range(100):
                    if not query_job(job,1,accounting,48,None):
                        result['errors'].append('query descendants failed')
                        break
                    active = C.c_uint32.from_buffer(accounting,40).value
                    result['total_processes'] = C.c_uint32.from_buffer(accounting,36).value
                    result['descendants_remaining'] = active
                    if not active:
                        stopped = True
                        break
                    time.sleep(0.05)
            else:
                terminate_process(pi.process,125)
                stopped = wait(pi.process,5000)==0
        for handle in (pi.thread,pi.process,job):
            if handle:
                close(handle)
        if desktop and not close_desktop(desktop):
            result['errors'].append('private desktop handle close failed')
        if station and not close_station(station):
            result['errors'].append('private station handle close failed')
        for fd in fds:
            os.close(fd)
        for file in files:
            file.close()
        for reader in readers:
            reader.join(5)
        if any(reader.is_alive() for reader in readers):
            result['errors'].append('stream drain incomplete')
        if initialized:
            delete_attributes(attributes)
        if sid:
            free_sid(sid)
        if created:
            result['delete_profile_hresult'] = delete_profile(name)
        if result.get('profile_path'):
            result['profile_path_exists_after_delete'] = Path(result['profile_path']).exists()
        result['finished_at'] = time.time()
        result['cleanup_confirmed'] = stopped and not any(reader.is_alive() for reader in readers) and result.get('delete_profile_hresult',0)==0
        with output_lock:
            result['stdout'] = bytes(chunks[0]).decode('utf-8',errors='replace')
            result['stderr'] = bytes(chunks[1]).decode('utf-8',errors='replace')
            result['output_truncated'] = output_size[0] > max_output
        if result['output_truncated'] and result['state']=='exited':
            result['state'] = 'output_limit'
        if result['errors'] and result['state']=='exited':
            result['state'] = 'failed'
    return result
