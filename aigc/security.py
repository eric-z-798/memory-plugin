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


def _mkdir_private(path):
    if not path.exists():
        _mkdir_private(path.parent)
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass  # Concurrent creation is checked by the caller, never chmodded.


class DataDirectoryLock:
    """Hold a kernel lock; never unlink it or decide ownership from a PID."""
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.fd = None

    def __enter__(self):
        _mkdir_private(self.directory)
        if os.name == 'posix':
            info = self.directory.stat()
            if info.st_uid not in (0, os.getuid()) or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                raise RuntimeError('数据目录不安全：%s；请使用新的本人私有目录，或先核对属主和共享需求，再由本人移除组/其他用户写权限（chmod go-w）。程序不会自动修改既有权限。' % self.directory)
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


class PrivateStartupFiles:
    """Per-process POSIX bootstrap, never a reusable persistent credential.

    Only a private file path is passed to the browser. SIGKILL can leave files;
    their capability expires with the server and is never reused on restart.
    """
    def __init__(self, directory, url):
        self.directory = Path(directory).resolve()
        self.url = url
        self.path = None
        self.files = {}

    def __enter__(self):
        import tempfile
        import html
        if os.name != 'posix':
            raise RuntimeError('无法验证此平台的私有启动文件 ACL；请在交互终端启动并手动打开链接')
        for ancestor in self.directory.parents:
            info = ancestor.stat()
            if ancestor == Path(ancestor.anchor):
                if info.st_mode & 0o022:
                    raise RuntimeError('文件系统根目录可由其他用户写入，无法安全创建启动文件')
                continue  # Filesystem root is an OS boundary, not a replaceable parent.
            sticky_root = info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX)
            if info.st_uid not in (0, os.getuid()) or (info.st_mode & 0o022 and not sticky_root):
                raise RuntimeError('启动文件路径祖先可被其他用户替换；请使用本人私有数据路径')
        info = self.directory.stat()
        if info.st_uid not in (0, os.getuid()) or info.st_mode & 0o022:
            raise RuntimeError('启动文件的父目录不安全；请使用本人私有数据目录')
        self.path = Path(tempfile.mkdtemp(prefix='.startup-', dir=str(self.directory)))
        self.identity = self.path.stat().st_ino
        try:
            info = self.path.stat()
            if info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise RuntimeError('无法创建私有启动目录')
            self.link = self.path / 'startup-url.txt'
            self.page = self.path / 'open.html'
            target = html.escape(self.url, quote=True)
            contents = [(self.link, self.url + '\n'),
                        (self.page, '<!doctype html><meta charset="utf-8"><meta name="referrer" content="no-referrer">'
                         '<meta http-equiv="refresh" content="0;url=' + target + '"><a href="' + target + '">打开星枢</a>')]
            for path, content in contents:
                fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
                self.files[path] = os.fstat(fd).st_ino
                with os.fdopen(fd, 'w', encoding='utf-8') as f:
                    info = os.fstat(f.fileno())
                    if info.st_uid != os.getuid() or info.st_mode & 0o077 or not stat.S_ISREG(info.st_mode):
                        raise RuntimeError('启动文件权限不安全')
                    f.write(content)
                    f.flush()
            return self
        except BaseException:
            self.__exit__()
            raise

    def __exit__(self, *args):
        if self.path is None:
            return
        try:
            info = self.path.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_ino != self.identity:
                return  # Do not follow replacement paths or remove someone else's files.
            for path, inode in self.files.items():
                try:
                    if path.lstat().st_ino == inode:
                        path.unlink()
                except FileNotFoundError:
                    pass
            self.path.rmdir()
        except OSError:
            pass  # Crash/cleanup remnants are private; tokens are never reused.
