"""Tests for v3.2 batch encoding, batch result handling, COM trigger and downscaling."""

import base64
import io
import json
import sys
import types
from unittest.mock import MagicMock, patch

import ezdxf
import pytest
from PIL import Image

from autocad_mcp.backends import file_ipc
from autocad_mcp.backends.base import CommandResult
from autocad_mcp.backends.file_ipc import (
    FileIPCBackend,
    batch_timeout,
    build_batch_lines,
    summarize_batch_result,
)

# ---------------------------------------------------------------------------
# build_batch_lines
# ---------------------------------------------------------------------------


class TestBuildBatchLines:
    def test_encodes_one_line_per_command(self):
        lines, err = build_batch_lines([
            {"command": "create-line", "params": {"x1": 0, "y1": 0, "x2": 1, "y2": None}},
            {"command": "create-text", "params": {"text": "Phòng\tngủ\nx"}},
        ])
        assert err is None
        assert len(lines) == 2
        cmd, pjson = lines[0].split("\t", 1)
        assert cmd == "create-line"
        assert json.loads(pjson) == {"x1": 0, "y1": 0, "x2": 1}  # None stripped
        # Non-ASCII, tab and newline are escaped, so the line stays single and ASCII
        assert "\n" not in lines[1] and lines[1].count("\t") == 1
        assert "\\u00f2" in lines[1]

    @pytest.mark.parametrize("commands", [None, "create-line", [], {"command": "x"}])
    def test_rejects_non_list(self, commands):
        lines, err = build_batch_lines(commands)
        assert lines == [] and "non-empty list" in err

    @pytest.mark.parametrize(
        "item, needle",
        [
            ("create-line", "must be an object"),
            ({"params": {}}, "invalid command name"),
            ({"command": "create-line\nundo"}, "invalid command name"),
            ({"command": "bad\tname"}, "invalid command name"),
            ({"command": "batch"}, "nested batch"),
            ({"command": "create-line", "params": [1, 2]}, "params must be an object"),
        ],
    )
    def test_rejects_invalid_items(self, item, needle):
        lines, err = build_batch_lines([{"command": "ping"}, item])
        assert lines == []
        assert "#1" in err and needle in err


# ---------------------------------------------------------------------------
# batch_timeout / summarize_batch_result
# ---------------------------------------------------------------------------


class TestBatchTimeout:
    def test_scales_with_count(self):
        assert batch_timeout(100) > batch_timeout(1) >= file_ipc.TIMEOUT

    def test_capped(self):
        assert batch_timeout(10**6) == file_ipc.BATCH_MAX_TIMEOUT


class TestSummarizeBatchResult:
    def test_all_ok_passes_through(self):
        r = CommandResult(ok=True, payload={"ok_count": 3, "error_count": 0, "errors": ""})
        assert summarize_batch_result(r) is r

    def test_errors_make_result_fail_and_keep_payload(self):
        payload = {"ok_count": 1, "error_count": 2, "errors": "create-text: bad layer"}
        r = summarize_batch_result(CommandResult(ok=True, payload=payload))
        assert r.ok is False
        assert "2 of 3" in r.error and "bad layer" in r.error
        d = r.to_dict()
        assert d["payload"] == payload

    def test_transport_failure_passes_through(self):
        r = CommandResult(ok=False, error="Timeout")
        assert summarize_batch_result(r) is r


# ---------------------------------------------------------------------------
# COM trigger
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_pythoncom(monkeypatch):
    monkeypatch.setitem(sys.modules, "pythoncom", types.SimpleNamespace(CoInitialize=lambda: None))


class TestComTrigger:
    def test_sends_escape_prefix(self, fake_pythoncom):
        b = FileIPCBackend()
        b._acad_com = MagicMock()
        b._com_capable = True
        assert b._send_com_trigger() is True
        sent = b._acad_com.ActiveDocument.SendCommand.call_args[0][0]
        assert sent.startswith("\x1b\x1b") and "(c:mcp-dispatch)" in sent

    def test_transient_failure_reacquires_and_retries(self, fake_pythoncom):
        b = FileIPCBackend()
        b._com_capable = True
        bad, good = MagicMock(), MagicMock()
        bad.ActiveDocument.SendCommand.side_effect = Exception("RPC_E_CALL_REJECTED")
        b._acad_com = bad

        def reacquire():
            b._acad_com = good

        with patch.object(b, "_acquire_com", side_effect=reacquire), patch.object(file_ipc.time, "sleep"):
            assert b._send_com_trigger() is True
        good.ActiveDocument.SendCommand.assert_called_once()

    def test_failure_does_not_disable_com_for_later_calls(self, fake_pythoncom):
        b = FileIPCBackend()
        b._com_capable = True
        b._acad_com = None
        later = MagicMock()

        with patch.object(b, "_acquire_com", side_effect=lambda: None), patch.object(file_ipc.time, "sleep"):
            assert b._send_com_trigger() is False  # falls back to WM_CHAR this time

        def reacquire():
            b._acad_com = later

        with patch.object(b, "_acquire_com", side_effect=reacquire):
            assert b._send_com_trigger() is True

    def test_lt_never_tries_com(self):
        b = FileIPCBackend()  # _com_capable False, no COM object
        with patch.object(b, "_acquire_com") as acq:
            assert b._send_com_trigger() is False
        acq.assert_not_called()


# ---------------------------------------------------------------------------
# Matplotlib downscaling
# ---------------------------------------------------------------------------


class TestMatplotlibDownscale:
    def test_respects_max_width(self, monkeypatch):
        from autocad_mcp import config
        from autocad_mcp.screenshot import MatplotlibScreenshotProvider

        monkeypatch.setattr(config, "SCREENSHOT_MAX_WIDTH", 640)
        doc = ezdxf.new("R2013")
        doc.modelspace().add_line((0, 0), (100, 50))
        b64 = MatplotlibScreenshotProvider(doc).capture()
        assert b64 is not None
        img = Image.open(io.BytesIO(base64.b64decode(b64)))
        assert img.width == 640
