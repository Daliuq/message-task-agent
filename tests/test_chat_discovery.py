from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from taskagent.chat_discovery import ScannerUnavailable, discover_chat_candidates, discover_wechat_chats


class ChatDiscoveryAdapterTests(unittest.TestCase):
    def test_wechat_adapter_reads_json_result_from_isolated_worker(self):
        output = (
            "diagnostic line\n"
            '__CHAT_DISCOVERY_RESULT__{"candidates":[{"account_key":"acct","conversation_key":"room"}],"warnings":[]}\n'
        )
        completed = subprocess.CompletedProcess(["python"], 0, stdout=output, stderr="")
        with patch("taskagent.chat_discovery._wechat_python", return_value=Path("wechat-python.exe")), \
             patch("taskagent.chat_discovery.subprocess.run", return_value=completed) as run:
            result = discover_wechat_chats(timeout_seconds=21)
        self.assertEqual(result["candidates"][0]["conversation_key"], "room")
        self.assertEqual(result["warnings"], [])
        self.assertEqual(run.call_args.kwargs["timeout"], 21)
        self.assertTrue(str(run.call_args.args[0][1]).endswith("wechat_discovery_worker.py"))

    def test_qq_adapter_reports_unavailable_without_starting_a_process(self):
        with patch("taskagent.qq.status", return_value={"accounts":[]}), patch("taskagent.chat_discovery.subprocess.run") as run:
            with self.assertRaisesRegex(ScannerUnavailable, "请先取钥"):
                discover_chat_candidates("qq")
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
