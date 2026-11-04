"""Local capability and process-ownership boundaries; standard library only."""
import os
import stat
from pathlib import Path


def validate_command(value):
    value = str(value or '').strip().strip('"')
    if not value:
        return ''
    path = Path(value)
    if not path.is_absolute():
        raise ValueError('CLI 路径必须是现有普通文件的绝对路径')
    path = path.resolve(strict=True)
    if not path.is_file():
        raise ValueError('CLI 路径必须是普通文件')
    if os.name == 'posix':
        for item in (path, *path.parents):
            info = item.stat()
            if (item in (path, path.parent) and info.st_uid not in (0, os.getuid())) or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                raise ValueError('CLI 文件及所在目录必须由当前用户或 root 所有；路径上的目录不可由其他用户写入')
    return str(path)


class DataDirectoryLock:
    """Hold a kernel lock; never unlink it or decide ownership from a PID."""
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.fd = None

    def __enter__(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        if os.name == 'posix':
            info = self.directory.stat()
            if info.st_uid not in (0, os.getuid()) or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                raise RuntimeError('数据目录必须由当前用户或 root 所有，且不可由其他用户写入')
        flags = os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0)
        fd = os.open(str(self.directory / '.instance.lock'), flags, 0o600)
        try:
            info = os.fstat(fd)
            if os.name == 'posix' and (info.st_uid not in (0, os.getuid()) or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)):
                raise OSError('unsafe lock ownership or permissions')
            if not stat.S_ISREG(info.st_mode):
                raise OSError('lock is not a regular file')
            if os.name == 'nt':
                import msvcrt
                # Ensure a byte exists before locking it on Windows.
                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"0")
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.fd = fd
            return self
        except BaseException as exc:
            os.close(fd)
            raise RuntimeError('数据目录已被另一实例占用，或无法取得独占锁：%s' % self.directory) from exc

    def __exit__(self, *args):
        if self.fd is not None:
            fd, self.fd = self.fd, None
            os.close(fd)  # OS releases the lock, including after abnormal process exit.
