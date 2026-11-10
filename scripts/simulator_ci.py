#!/usr/bin/env python3
"""Run two isolated WDA simulator channels; intended for GitHub-hosted macOS CI."""
import concurrent.futures
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import uuid

os.environ["IPHONE_USE_ANALYTICS"] = "0"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
from iphone_use import MultiPhoneRuntime
from wda_setup import WDA_COMMIT, WDA_REPOSITORY


def command(*argv, timeout=120):
    result = subprocess.run(argv, check=True, capture_output=True, text=True, timeout=timeout)
    return result.stdout.strip()


def inventory(category):
    return json.loads(command("xcrun", "simctl", "list", category, *([] if category == "devicetypes" else ["available"]), "--json", timeout=180))


def target():
    runtimes = [r for r in inventory("runtimes")["runtimes"]
                if "iOS-" in r["identifier"] and r.get("isAvailable", True)]
    if not runtimes:
        raise RuntimeError("GitHub runner has no available iOS Simulator runtime.")
    xcode = re.search(r"Xcode (\d+)", command("xcodebuild", "-version")).group(1)
    matching = [r for r in runtimes if r["identifier"].split("iOS-")[-1].split("-")[0] == xcode]
    runtime = max(matching or runtimes, key=lambda r: tuple(map(int, re.findall(r"\d+", r["version"]))))
    types = inventory("devicetypes")["devicetypes"]
    phone = next((t for t in types if t["name"] == "iPhone 16"), None)
    if phone is None:
        phone = next(t for t in types if t["name"].startswith("iPhone"))
    return phone["identifier"], runtime["identifier"]


def source_checkout(directory):
    directory.mkdir(parents=True)
    command("git", "init", str(directory))
    command("git", "-C", str(directory), "remote", "add", "origin", WDA_REPOSITORY)
    command("git", "-C", str(directory), "fetch", "--depth", "1", "origin", WDA_COMMIT, timeout=600)
    command("git", "-C", str(directory), "checkout", "--detach", "FETCH_HEAD", timeout=60)
    if command("git", "-C", str(directory), "rev-parse", "HEAD") != WDA_COMMIT:
        raise RuntimeError("Pinned WebDriverAgent revision mismatch.")
    print("Pinned WebDriverAgent available.", flush=True)


def wait_job(runtime, alias, job_id, action, timeout_seconds):
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        result = runtime.call("pua_setup", {"action": "status", "device": alias, "job_id": job_id})
        job = result["jobs"][0]
        if job["state"] in ("failed", "interrupted", "stopped"):
            raise RuntimeError(f"{alias} {action}: {job['state']}: {job.get('error')}\n{job.get('log_tail', '')[-2500:]}")
        if action == "build" and job["state"] == "succeeded":
            return
        if action == "start" and result["service"].get("ready"):
            return
        time.sleep(4)
    raise TimeoutError(f"{alias} {action}: job {job_id} not ready within integration test deadline.")


def setup_device(runtime, alias, identifier, source, port):
    data = runtime.call("pua_device", {"action": "register", "device": alias, "udid": identifier,
                                     "device_type": "simulator", "local_port": port,
                                     "source_dir": str(source)})
    assert data["ok"], data
    for action, deadline in (("build", 900), ("start", 300)):
        queued = runtime.call("pua_setup", {"action": action, "device": alias, "wait_seconds": 0})
        assert queued["ok"], queued
        job_id = queued.get("job_id") or queued.get("job", {}).get("id")
        if job_id:
            wait_job(runtime, alias, job_id, action, deadline)
        elif not queued.get("already_ready"):
            raise RuntimeError(f"{alias}: expected an owned {action} job.")
        print(alias, action, "ready", flush=True)
    for attempt in range(6):
        try:
            proof = runtime.call("pua_ready", {"device": alias, "screenshot": False, "recover": False})
            if proof["ready"]:
                print(alias, "READY", flush=True)
                return
        except Exception as exc:
            if attempt == 5:
                raise RuntimeError(f"{alias}: READY unavailable after WDA start.") from exc
        time.sleep(3)
    raise RuntimeError(f"{alias}: READY failed.")


def exercise(runtime):
    aliases = ("sim_a", "sim_b")
    apps = ("com.apple.mobilesafari", "com.apple.Preferences")

    def launch(alias, bundle):
        result = runtime.call("pua_launch_app", {"device": alias, "bundle_id": bundle, "verify": True})
        assert result["foreground_verified"], (alias, result)
        observation = runtime.call("pua_observe", {"device": alias, "mode": "both"})
        assert observation["app"] == bundle, (alias, observation.get("app"), bundle)
        assert Path(observation["image"]["path"]).is_file(), alias
        return observation["app"]

    # A and B execute separate WDA mutations concurrently, not only mocked workers.
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(launch, alias, bundle) for alias, bundle in zip(aliases, apps)]
        assert [f.result(timeout=90) for f in futures] == list(apps)

    # A -> B -> A must preserve A's routing; B should remain on Settings.
    runtime.call("pua_press_button", {"device": "sim_a", "name": "home"})
    assert runtime.call("pua_observe", {"device": "sim_b"})["app"] == apps[1]
    assert launch("sim_a", apps[0]) == apps[0]
    assert runtime.call("pua_observe", {"device": "sim_b"})["app"] == apps[1]
    print("PASS: concurrent actions, independent screenshots, A -> B -> A and session isolation.", flush=True)


def main():
    kind, os_runtime = target()
    print("Simulator target:", kind, os_runtime, flush=True)
    identifiers = []
    scratch = os.environ.get("IPHONE_USE_CI_TEMP_DIR") or os.environ.get("RUNNER_TEMP")
    with tempfile.TemporaryDirectory(prefix="iphone-use-sim-ci-", dir=scratch) as directory:
        state = Path(directory)
        runtime = None
        try:
            for alias in ("sim_a", "sim_b"):
                ident = command("xcrun", "simctl", "create", f"iphone-use-ci-{alias}-{uuid.uuid4().hex[:6]}", kind, os_runtime)
                identifiers.append(ident)
                command("xcrun", "simctl", "boot", ident, timeout=90)
            for ident in identifiers:
                command("xcrun", "simctl", "bootstatus", ident, "-b", timeout=240)
            source = state / "WebDriverAgent"
            source_checkout(source)
            runtime = MultiPhoneRuntime(state / "runtime")
            for alias, ident, port in zip(("sim_a", "sim_b"), identifiers, (18401, 18402)):
                setup_device(runtime, alias, ident, source, port)
            exercise(runtime)
        finally:
            if runtime:
                for alias in ("sim_a", "sim_b"):
                    try:
                        runtime.call("pua_setup", {"action": "stop", "device": alias})
                    except Exception:
                        pass
                runtime.close()
            for ident in reversed(identifiers):
                subprocess.run(["xcrun", "simctl", "shutdown", ident], capture_output=True)
                subprocess.run(["xcrun", "simctl", "delete", ident], capture_output=True)


if __name__ == "__main__":
    main()
