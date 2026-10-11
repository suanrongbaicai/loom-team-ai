"""Official Codex CLI invocation and scope-bound environment; no model loop."""
import json
import re
from pathlib import Path
import subprocess

from .control import identifier, within, write_durable


def sandbox_profile(runtime, workspace, home, temporary):
    """The host keeps credentials; model tools only receive scoped file access."""
    workspace = Path(workspace)
    filesystem = {':root': 'deny', ':minimal': 'read',
                  str(Path(runtime['python']).parent): 'read',
                  str(Path(runtime['framework'])): 'read',
                  str(Path(home) / 'Work' / '.agents'): 'read',
                  str(temporary): 'write',
                  str(Path(home) / 'CodexHome' / 'auth.json'): 'deny'}
    entries = ','.join(json.dumps(k) + '=' + json.dumps(v) for k, v in filesystem.items())
    return 'permissions.loom={extends=":workspace",filesystem={' + entries + '}}'


def command(runtime, workspace, home, temporary, model='example-model-b', reasoning='high'):
    # Token values are inherited, never placed on a command line.
    if not runtime['endpoint'].endswith('/rpc'):
        raise ValueError('Controller RPC endpoint required')
    if not isinstance(model,str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._-]{0,95}',model):
        raise ValueError('Explicit approved scope model required')
    if reasoning not in ('low','medium','high','xhigh','max','ultra'):
        raise ValueError('Explicit approved scope reasoning required')
    mcp = ('mcp_servers.loom={url=' + json.dumps(runtime['endpoint'][:-4]+'/mcp')
           + ',bearer_token_env_var="LOOM_TASK_TOKEN",required=true,default_tools_approval_mode="approve",startup_timeout_sec=30,tool_timeout_sec=200}')
    args = [runtime['codex'], 'exec', '--ignore-user-config', '--ignore-rules',
            '--skip-git-repo-check', '--json', '-C', str(workspace), '-c', 'default_permissions="loom"',
            '-c', sandbox_profile(runtime, workspace, home, temporary),
            '-m', model, '-c', 'model_reasoning_effort='+json.dumps(reasoning),
            '-c', 'features.hooks=true', '-c', 'features.shell_tool=false',
            '-c', 'features.view_image=false', '-c', 'features.code_mode=false',
            '-c', 'features.code_mode_only=false', '-c', 'web_search="disabled"',
            '-c', 'features.apps=false', '-c', 'features.plugins=false',
            '-c', 'windows.sandbox="elevated"', '-c', 'windows.sandbox_private_desktop=true',
            # The model host needs its proxy; offline command tools do not.
            # Inheriting it changes the official offline firewall provisioning
            # request and makes an ordinary host attempt administrative setup.
            '-c', 'shell_environment_policy.exclude=["HTTP_PROXY","HTTPS_PROXY","ALL_PROXY","http_proxy","https_proxy","all_proxy"]',
            '-c', 'approval_policy="never"', '-c', mcp, '-']
    if runtime.get('model_http_only'):
        # Use the same official account and model over HTTP when this network's
        # WebSocket TLS path is known to fail; do not retry it on every task.
        args[-1:-1] = ['-c', 'model_provider="loom-openai-http"', '-c',
            'model_providers.loom-openai-http={name="OpenAI",wire_api="responses",requires_openai_auth=true,supports_websockets=false}']
    if runtime.get('model_catalog_json'):
        args[-1:-1] = ['-c', 'model_catalog_json=' + json.dumps(runtime['model_catalog_json'])]
    return args


def make_job(ctx, token, runtime, scope, prompt):
    home = Path(scope['worker_home'])
    # A later turn must not inherit files produced under an earlier grant.
    # Durable work is retrieved through the controller under current policy.
    identifier(ctx.task)
    workspace = within(home, 'Work/tasks/' + ctx.task)
    temporary = within(home, 'Temp/tasks/' + ctx.task)
    environment = {k: str(home / folder) for k, folder in {
        'USERPROFILE': 'Profile', 'APPDATA': 'Profile/AppData/Roaming',
        'LOCALAPPDATA': 'Profile/AppData/Local', 'TEMP': 'Temp', 'TMP': 'Temp',
        'CODEX_HOME': 'CodexHome', 'XDG_CACHE_HOME': 'Cache',
        'PIP_CACHE_DIR': 'Cache/pip', 'UV_CACHE_DIR': 'Cache/uv'}.items()}
    environment.update({'LOOM_TASK_TOKEN': token, 'LOOM_ENDPOINT': runtime['endpoint'],
                        'CODEX_EXEC_SERVER_URL': 'none',
                        'TEMP': str(temporary), 'TMP': str(temporary),
                        'PYTHONPATH': runtime['framework'], 'PYTHONUTF8': '1',
                        'HTTP_PROXY': runtime.get('proxy', ''), 'HTTPS_PROXY': runtime.get('proxy', ''),
                        'NO_PROXY': '127.0.0.1,localhost', 'PYTHONDONTWRITEBYTECODE': '1'})
    prompt = ('可信运行环境说明：本任务工作目录是 ' + str(workspace) + '。'
              '命令只通过 loom.execute 执行。执行器已创建 LOOMWork 驱动器，并将 PowerShell 位置与 '
              '[Environment]::CurrentDirectory 设置到本任务工作区；不要重复创建 LOOMWork。'
              '文件工具使用本任务工作区的绝对路径。不要往 C 盘写入或通过放宽权限处理目录错误。'
              '工作资料经 LOOM 工具按当前授权查取，不读取其他任务临时文件。\n\n' + prompt)
    return {'id': ctx.task, 'os_account': scope['os_account'], 'worker_home': str(home),
            'workspace': str(workspace), 'output_dir': str(home / 'Outputs' / ctx.task),
            'environment': environment, 'argv': command(runtime, workspace, home, temporary,
                scope.get('model','example-model-b'),scope.get('reasoning','high')),
            'prompt': prompt, 'timeout_seconds': 1200, 'require_loom_preflight': True}


def launch(job, runtime):
    path = within(runtime['jobs'], job['id'] + '.json')
    write_durable(path, json.dumps(job, ensure_ascii=False, indent=2).encode())
    args = ['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                             '-File', str(Path(runtime['framework']) / 'tools/launch-job.ps1'),
                             '-JobFile', str(path), '-CredentialFile', runtime['credentials']]
    if runtime.get('headless'):
        args.append('-Headless')
    result = subprocess.run(args, capture_output=True, timeout=30)
    if result.returncode:
        diagnostic = Path(runtime['credentials']).parent / 'Diagnostics' / ('launch-' + job['id'] + '.json')
        write_durable(diagnostic, json.dumps({'returncode': result.returncode,
                      'stdout': result.stdout.decode('utf-8', errors='replace'),
                      'stderr': result.stderr.decode('utf-8', errors='replace')}, ensure_ascii=False).encode())
        raise RuntimeError('Windows scope worker launch failed; inspect protected launch diagnostic')
    return path


def reap_job(run_id, runtime):
    if not runtime.get('headless'):
        return
    args = ['powershell.exe','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass',
            '-File',str(Path(runtime['framework'])/'tools/reap-job.ps1'),'-RunId',run_id]
    result = subprocess.run(args,capture_output=True,timeout=25)
    if result.returncode:
        raise RuntimeError('Execution task cleanup incomplete')


reap_command = reap_job
