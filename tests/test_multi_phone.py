"""Multi-device targeting and concurrency tests without a connected iPhone."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"server"))
from iphone_use import MultiPhoneRuntime, serve, tool_result
from wda_client import WDAError


class MultiPhoneTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory=Path(temp.name)
        self.runtime=MultiPhoneRuntime(temp.name)
        self.addCleanup(self.runtime.close)

    def register(self,alias,udid,port):
        return self.runtime.call("pua_device",{
            "action":"register","device":alias,"udid":udid,
            "team_id":"ABCDE12345","bundle_id":"com.example.wdarunner",
            "local_port":port})

    def test_registration_persists_without_overwriting_old_device(self):
        self.register("apple","TEST-DEVICE-00001",18101)
        self.register("sam","TEST-DEVICE-00002",18102)
        devices=self.runtime.call("pua_device",{"action":"list"})["devices"]
        self.assertEqual([item["device"] for item in devices],["apple","sam"])
        apple=self.runtime._context("apple")
        sam=self.runtime._context("sam")
        self.assertNotEqual(apple.state_dir,sam.state_dir)
        self.assertNotEqual(apple.base_url,sam.base_url)
        self.assertNotEqual(apple.state_dir/"operation.lock",sam.state_dir/"operation.lock")
        self.assertNotEqual(apple.state_dir/"session.json",sam.state_dir/"session.json")
        self.assertEqual(apple.setup_manager.config["udid"],"TEST-DEVICE-00001")
        self.assertEqual(sam.setup_manager.config["udid"],"TEST-DEVICE-00002")
        other=MultiPhoneRuntime(str(self.directory))
        try:
            self.assertEqual(len(other.call("pua_device",{"action":"list"})["devices"]),2)
        finally:other.close()

    def test_duplicate_id_port_and_unsafe_alias_cannot_target_another_phone(self):
        self.register("apple","TEST-DEVICE-00001",18101)
        for alias,udid,port in (
            ("sam","TEST-DEVICE-00001",18102),
            ("sam","TEST-DEVICE-00002",18101),
            ("sam","TEST-DEVICE-00002",18100),
            ("apple","TEST-DEVICE-00003",18103),
            ("../bad","TEST-DEVICE-00003",18103)
        ):
            with self.subTest(alias=alias,port=port),self.assertRaises(WDAError):
                self.register(alias,udid,port)
        self.assertEqual(self.runtime._context("apple").setup_manager.config["udid"],"TEST-DEVICE-00001")
        for alias in ("../bad","default","/tmp/phone","missing"):
            with self.subTest(alias=alias),self.assertRaises(WDAError):
                self.runtime.call("pua_observe",{"device":alias})
        self.assertFalse((self.directory/"devices"/"sam"/"config.json").exists())

    def test_reconfigure_cannot_steal_another_devices_udid_or_port(self):
        self.register("apple","TEST-DEVICE-00001",18101)
        self.register("sam","TEST-DEVICE-00002",18102)
        for udid,port in (("TEST-DEVICE-00001",18102),("TEST-DEVICE-00002",18101)):
            with self.subTest(udid=udid,port=port),self.assertRaises(WDAError) as caught:
                self.runtime.call("pua_setup",{
                    "action":"configure","device":"sam","udid":udid,
                    "team_id":"ABCDE12345","bundle_id":"com.example.wdarunner",
                    "local_port":port})
            self.assertEqual(caught.exception.code,"device_conflict")
        self.assertEqual(self.runtime._context("sam").setup_manager.config["udid"],"TEST-DEVICE-00002")

    def test_explicit_target_is_isolated_from_legacy_default(self):
        self.register("apple","TEST-DEVICE-00001",18101)
        apple=self.runtime._context("apple")
        with patch.object(apple,"call",return_value={"phone":"apple"}) as left, \
             patch.object(self.runtime,"_call",return_value={"phone":"default"}) as root:
            self.assertEqual(self.runtime.call("pua_metrics",{"device":"apple"})["phone"],"apple")
            self.assertEqual(self.runtime.call("pua_metrics",{})["phone"],"default")
            left.assert_called_once_with("pua_metrics",{})
            root.assert_called_once_with("pua_metrics",{})

    def test_mixed_device_batch_and_wrong_preview_are_rejected(self):
        self.register("apple","TEST-DEVICE-00001",18101)
        with self.assertRaises(WDAError) as caught:
            self.runtime.call("pua_batch",{"device":"apple","steps":[
                {"op":"tap","args":{"device":"default","x":10,"y":20}}
            ]})
        self.assertEqual(caught.exception.code,"invalid_argument")
        with self.assertRaises(WDAError) as caught:
            self.runtime.call("pua_screen",{"device":"apple"})
        self.assertEqual(caught.exception.code,"preview_not_supported")
        self.assertFalse(self.runtime.preview_enabled({"device":"apple"}))
        self.assertTrue(self.runtime.preview_enabled({}))
        result=tool_result(self.runtime,{"name":"pua_ready","arguments":{"device":"apple"}})
        self.assertNotIn("_meta",result)

    def test_different_device_tool_calls_are_concurrent(self):
        self.register("apple","TEST-DEVICE-00001",18101)
        self.register("sam","TEST-DEVICE-00002",18102)
        meeting=threading.Barrier(2)
        def fake_call(name,arguments):
            meeting.wait(timeout=3)
            return {"ok":True}
        calls=[
            {"jsonrpc":"2.0","id":number,"method":"tools/call",
             "params":{"name":"pua_metrics","arguments":{"device":alias}}}
            for number,alias in ((1,"apple"),(2,"sam"))]
        output=io.StringIO()
        with patch.object(self.runtime._context("apple"),"call",side_effect=fake_call), \
             patch.object(self.runtime._context("sam"),"call",side_effect=fake_call), \
             patch.object(sys,"stdin",io.StringIO("".join(json.dumps(x)+"\n" for x in calls))), \
             contextlib.redirect_stdout(output):
            serve(self.runtime)
        answers=[json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual({item["id"] for item in answers},{1,2})
        self.assertTrue(all(not item["result"]["isError"] for item in answers))


    def test_simulator_registration_without_signing_and_independent_ports(self):
        first=self.runtime.call("pua_device",{
            "action":"register","device":"sim_a","device_type":"simulator",
            "udid":"ABCD1234-1234-5678-ABCD-123456789012","local_port":18201})
        second=self.runtime.call("pua_device",{
            "action":"register","device":"sim_b","device_type":"simulator",
            "udid":"DCBA1234-1234-5678-ABCD-123456789012","local_port":18202})
        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        self.assertEqual(self.runtime._context("sim_a").setup_manager.config["device_type"],"simulator")
        self.assertEqual(self.runtime._context("sim_a").base_url,"http://127.0.0.1:18201")
        self.assertEqual(self.runtime._context("sim_b").base_url,"http://127.0.0.1:18202")
        self.assertEqual({v["device_type"] for v in self.runtime.call("pua_device",{"action":"list"})["devices"]},{"simulator"})


if __name__=="__main__":
    unittest.main()
