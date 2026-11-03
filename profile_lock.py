"""Cross-process, non-blocking lock for a participant's saved browser profile."""

import os


class ProfileLock:
    def __init__(self, path):
        self.path = os.fspath(path)
        self.stream = None
        self._locked = False

    def acquire(self):
        if self._locked:
            return True
        self.stream = open(self.path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self.stream.seek(0)
                if not self.stream.read(1):
                    self.stream.write(b"0")
                    self.stream.flush()
                self.stream.seek(0)
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError):
            self.stream.close()
            self.stream = None
            return False
        self._locked = True
        return True

    def release(self):
        if self.stream is None:
            return
        try:
            if self._locked and os.name == "nt":
                import msvcrt
                self.stream.seek(0)
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            elif self._locked:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
        finally:
            self._locked = False
            self.stream.close()
            self.stream = None
