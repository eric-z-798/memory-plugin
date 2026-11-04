"""Fake executables must satisfy production validation, even in /tmp checkouts."""
import os
import stat
import tempfile
from pathlib import Path


WORKSPACE = Path('/workspace')


def secure_temporary_directory():
    root = Path(__file__).resolve().parents[1]
    candidates = [Path(os.environ['XINGSHU_TEST_SECURE_ROOT'])] if os.environ.get('XINGSHU_TEST_SECURE_ROOT') else []
    candidates += [WORKSPACE, root, root.parent]
    errors = []
    try:
        candidates.append(Path.home())
    except (RuntimeError, OSError) as exc:
        errors.append(str(exc))  # HOME is optional; existing safe candidates still work.
    for base in candidates:
        try:
            base = base.resolve(strict=True)
            if os.name == 'posix' and any(p.stat().st_mode & (stat.S_IWGRP | stat.S_IWOTH) for p in (base, *base.parents)):
                continue
            directory = tempfile.TemporaryDirectory(prefix='xingshu-fake-cli-', dir=str(base))
            return directory
        except OSError as exc:
            errors.append(str(exc))
    raise RuntimeError('没有可写且祖先不可由其他用户写入的测试夹具目录；请设置 XINGSHU_TEST_SECURE_ROOT 为允许写入的安全路径。' + '; '.join(errors))
