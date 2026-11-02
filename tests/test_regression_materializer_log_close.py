from cost_xray import addon


def test_parent_log_is_closed_after_launch(monkeypatch, tmp_path):
    monkeypatch.setattr(addon, "OUT", tmp_path)
    handles = []

    def popen(*args, **kwargs):
        handles.append(kwargs["stdout"])
        assert not handles[-1].closed
        return object()

    monkeypatch.setattr(addon.subprocess, "Popen", popen)
    addon._Materializer()._launch()
    assert handles[0].closed
