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
