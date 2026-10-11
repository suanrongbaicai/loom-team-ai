"""Install a small system entry; executable hooks and state remain on E:."""
import argparse
import json
import os
import re
from pathlib import Path
import subprocess
import tomllib


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--python', required=True, help='Verified Python executable on the approved data volume')
    options = parser.parse_args()
    framework = Path(__file__).resolve().parents[1]
    target = Path(os.environ['ProgramData']) / 'OpenAI/Codex/requirements.toml'
    if target.exists():
        raise RuntimeError('Existing system requirements must be reviewed; no overwrite')
    python = str(Path(options.python).resolve(strict=True))
    # Codex inherits the selected environment shell. A quoted executable alone
    # is a string expression in PowerShell, although cmd.exe accepts it. The
    # confirmed runtime path contains no shell metacharacters or spaces and
    # can be invoked verbatim by either shell.
    if not re.fullmatch(r'[A-Za-z]:\\[A-Za-z0-9_\\.\-]+', python):
        raise RuntimeError('Runtime executable must use a shell-neutral path')
    command = python + ' -B -X utf8 "' + str(framework / 'tools/hook_entry.py') + '"'
    lines = ['# LOOM managed hook entry. Runtime and data reside on E:.',
             '# Hook dispatcher only acts for actual Windows usernames prefixed loom_.',
             '# Other clients keep their existing feature and hook-source settings.',
             '[hooks]', "windows_managed_dir = '" + str(framework / 'tools') + "'"]
    for event in ('SessionStart', 'UserPromptSubmit', 'PostCompact', 'PreToolUse'):
        lines += ['', '[[hooks.' + event + ']]', '[[hooks.' + event + '.hooks]]',
                  'type = "command"', 'command = ' + repr(command),
                  'command_windows = ' + repr(command), 'timeout = 30']
        if event in ('SessionStart', 'UserPromptSubmit'):
            lines += ['additionalContextLimit = 0']
    # TOML literal strings keep Windows backslashes unchanged.
    text = '\n'.join(lines).replace('\\\\', '\\') + '\n'
    tomllib.loads(text)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open('x', encoding='utf-8') as stream:
        stream.write(text)
    subprocess.run(['icacls.exe', str(target), '/inheritance:r', '/grant:r',
                    '*S-1-5-18:F', '*S-1-5-32-544:F', '*S-1-5-32-545:RX'], check=True,
                   capture_output=True)
    # A system hook must also be able to return its no-op response for other
    # local identities. This grants only program code, never Control or workers.
    for path, right in ((framework.parent, '*S-1-5-32-545:RX'),
                        (framework, '*S-1-5-32-545:(OI)(CI)RX'),
                        (Path(python).parent, '*S-1-5-32-545:(OI)(CI)RX')):
        subprocess.run(['icacls.exe', str(path), '/grant', right], check=True, capture_output=True)
    print(json.dumps({'check': 'LOOM-HOOKS-INSTALLED', 'path': str(target),
                      'bytes': target.stat().st_size, 'script_root': str(framework / 'tools')}))


if __name__ == '__main__':
    main()
