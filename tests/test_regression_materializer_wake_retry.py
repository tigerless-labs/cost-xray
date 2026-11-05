import os

import pytest

from cost_xray import addon


class Pipe:
    def __init__(self, broken=False):
        self.broken = broken
        self.writes = []

    def write(self, value):
        if self.broken:
            raise BrokenPipeError()
        self.writes.append(value)

    def flush(self):
        pass

    def close(self):
        pass


class Process:
    def __init__(self, broken=False):
        self.stdin = Pipe(broken)

    def poll(self):
        return None


def test_broken_pipe_wake_is_retried(monkeypatch):
    consumer = addon._Materializer()
    consumer._proc = Process(True)
    replacement = Process()
    monkeypatch.setattr(consumer, "_launch", lambda: replacement)
    consumer.send()
    assert replacement.stdin.writes == [b"\n"]


def test_buffered_pipe_close_failure_does_not_drop_wake(monkeypatch):
    read_fd, write_fd = os.pipe()
    pipe = os.fdopen(write_fd, "wb")
    os.close(read_fd)
    consumer = addon._Materializer()
    consumer._proc = Process()
    # Model poll() racing with exit, using a real buffered pipe with no reader.
    consumer._proc.stdin = pipe
    replacement = Process()
    launches = []

    def launch():
        launches.append(replacement)
        return replacement

    monkeypatch.setattr(consumer, "_launch", launch)
    try:
        consumer.send()
        assert pipe.closed
        assert launches == [replacement]
        assert replacement.stdin.writes == [b"\n"]
    finally:
        if not pipe.closed:
            try:
                pipe.close()
            except BrokenPipeError:
                pass


def test_replacement_failure_does_not_retry_forever(monkeypatch):
    consumer = addon._Materializer()
    consumer._proc = Process(True)
    launches = []

    def launch():
        replacement = Process(True)
        launches.append(replacement)
        return replacement

    monkeypatch.setattr(consumer, "_launch", launch)
    with pytest.raises(BrokenPipeError):
        consumer.send()
    assert len(launches) == 1


def test_failed_relaunch_leaves_no_closed_process_for_next_wake(monkeypatch):
    consumer = addon._Materializer()
    consumer._proc = Process(True)

    def launch_failure():
        raise OSError("fixture launch failure")

    monkeypatch.setattr(consumer, "_launch", launch_failure)
    with pytest.raises(OSError, match="fixture launch failure"):
        consumer.send()
    assert consumer._proc is None
    replacement = Process()
    monkeypatch.setattr(consumer, "_launch", lambda: replacement)
    consumer.send()
    assert replacement.stdin.writes == [b"\n"]
