"""验证执行器：固定检查配置，shell=False，超时终止进程树，输出有上限。

仅用于用户信任的本地仓库。项目测试/构建本身可执行代码，worktree 不是沙箱。
"""
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time

from backend.core.exceptions import InvalidInputError

PROFILES = ('pytest', 'unittest', 'npm-build')
OUTPUT_LIMIT = 16000


def verification_command(profile: str) -> list[str]:
    if profile == 'pytest':
        return [sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider']
    if profile == 'unittest':
        return [sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-v']
    if profile == 'npm-build':
        npm = shutil.which('npm.cmd' if os.name == 'nt' else 'npm')
        if not npm:
            raise InvalidInputError('未找到 npm', agent_type='code_fix')
        # Windows .cmd 需要 cmd 解析；直接使用 node + npm CLI，仍不经过 shell。
        if os.name == 'nt':
            cli = Path(npm).parent / 'node_modules/npm/bin/npm-cli.js'
            node = shutil.which('node')
            if not node or not cli.is_file():
                raise InvalidInputError('无法定位 Node/npm CLI', agent_type='code_fix')
            return [node, str(cli), 'run', 'build']
        return [npm, 'run', 'build']
    raise InvalidInputError('不支持的验证配置', agent_type='code_fix')


def _kill(process: subprocess.Popen) -> None:
    if os.name == 'nt':
        subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                       capture_output=True, timeout=15)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        process.kill()
    process.wait(timeout=15)


def verify(workspace_path: str, profile: str, timeout: int = 120,
           cancel=None) -> dict:
    started = time.monotonic()
    argv = verification_command(profile)
    path = Path(workspace_path)
    result = dict(argv=argv, exit_code=None, status='error', output='',
                  duration_ms=0, error=None)
    if profile == 'unittest' and not (path / 'tests').is_dir():
        result.update(status='insufficient', error='未找到 tests 目录')
        return result
    env = os.environ.copy()
    for key in list(env):
        if key.startswith('GIT_') or key in ('PYTHONPATH', 'PYTHONHOME'):
            del env[key]
    env.update(PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8', CI='true',
               PYTEST_DISABLE_PLUGIN_AUTOLOAD='1')
    # 临时文件避免 stdout 管道阻塞或在内存无限累积。
    with tempfile.TemporaryFile() as output:
        process = None
        try:
            process = subprocess.Popen(argv, cwd=path, env=env, shell=False,
                                       stdin=subprocess.DEVNULL, stdout=output,
                                       stderr=subprocess.STDOUT,
                                       start_new_session=os.name != 'nt')
            while process.poll() is None:
                if cancel and cancel():
                    _kill(process)
                    result['error'] = '任务已取消'
                    break
                if time.monotonic() - started > timeout:
                    _kill(process)
                    result['error'] = f'验证超时（{timeout}s）'
                    break
                if output.tell() > 2_000_000:
                    _kill(process)
                    result['error'] = '验证输出超过 2MB，已终止'
                    break
                time.sleep(0.1)
            result['exit_code'] = process.returncode
            if not result['error']:
                result['status'] = 'passed' if process.returncode == 0 else 'failed'
                if profile == 'pytest' and process.returncode == 5:
                    result['status'] = 'insufficient'
            output.seek(0)
            data = output.read(OUTPUT_LIMIT + 1)
            result['output'] = data[:OUTPUT_LIMIT].decode('utf-8', errors='replace')
            if len(data) > OUTPUT_LIMIT:
                result['output'] += '\n[输出已截断]'
            if profile == 'unittest' and 'Ran 0 tests' in result['output']:
                result['status'] = 'insufficient'
        except (OSError, subprocess.SubprocessError) as exc:
            if process and process.poll() is None:
                _kill(process)
            result['error'] = str(exc)[:1000]
    result['duration_ms'] = int((time.monotonic() - started) * 1000)
    return result
