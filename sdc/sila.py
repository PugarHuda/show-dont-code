"""SiLA 2 server: the runs on this machine as a SiLA 2 feature (ProtocolGenerator.sila.xml), so a lab's SiLA scheduler
or orchestrator (anything that speaks SiLA 2 over gRPC) can list runs, fetch a protocol and have Opentrons' analyzer check
it at a given reaction count, next to its other instruments. Built on the official sila2 library, discoverable on the
LAN over mDNS.

  python -m sdc.sila [--port 50052]      serve (plain gRPC on the lab network; --tls for an encrypted channel)
  python -m sdc.sila --self-check        start a server, call every command and property with a SiLA client, stop
"""
import json
import sys
from pathlib import Path

from sila2.framework import Feature
from sila2.framework.errors.defined_execution_error import DefinedExecutionError
from sila2.server import FeatureImplementationBase, SilaServer

FEATURE = Feature((Path(__file__).with_name("ProtocolGenerator.sila.xml")).read_text(encoding="utf-8"))


def _error(identifier, message):
    return DefinedExecutionError(FEATURE.defined_execution_errors[identifier], message)


class ProtocolGenerator(FeatureImplementationBase):
    """Answers from the same runs and code paths as the web app and its REST API."""

    def _result(self, name):
        import app
        from fastapi import HTTPException

        if name not in app.runs():  # public runs only: an upload's name is its private link
            raise _error("UnknownRun", f"no public run named {name!r}")
        try:
            r = app.result(name)
        except HTTPException as e:
            raise _error("UnknownRun", str(e.detail))
        if not r.get("protocol") or not r.get("sim_ok"):
            raise _error("NotReady", "this run has no simulator-checked protocol yet")
        return r

    def get_Runs(self, *, metadata):
        import app

        return app.runs()

    def GetSteps(self, RunName, *, metadata):
        return json.dumps(self._result(RunName)["steps"], ensure_ascii=False)

    def GetProtocol(self, RunName, *, metadata):
        return self._result(RunName)["protocol"]

    def AnalyzeProtocol(self, RunName, Reactions, *, metadata, instance):
        import app

        r = self._result(RunName)
        if not 1 <= Reactions <= r["max_reactions"]:
            raise _error("InvalidReactions", f"reactions must be 1 to {r['max_reactions']} for this protocol")
        instance.begin_execution()
        a = app.reactions_analysis(RunName, Reactions)  # cached per protocol version and count, like the web app's
        return a["ok"], a.get("commands") or 0, json.dumps(a.get("liquid_setup", []), ensure_ascii=False)


def server():
    s = SilaServer(server_name="Show, Don't Code", server_type="ProtocolGenerator",
                   server_description="Opentrons Flex protocols made from a filmed bench protocol, checked by Opentrons' analyzer",
                   server_version="1.0", server_vendor_url="https://show-dont-code.vercel.app")
    s.set_feature_implementation(FEATURE, ProtocolGenerator(s))
    return s


def _selfcheck():
    import logging

    logging.disable(logging.INFO)
    import time

    from sila2.client import SilaClient

    def finished(inst, timeout=300):  # an observable command: wait for its execution to end, then fetch the result
        end = time.time() + timeout
        while not inst.done and time.time() < end:
            time.sleep(0.5)
        return inst.get_responses()

    import tempfile

    from sdc import pipeline, protocol

    steps = [{"start": 30, "end": 45, "action": "transfer", "description": "Add water", "reagent": "water", "volume_ul": 20,
              "destination": "PCR tube"},
             {"start": 190, "end": 238, "action": "transfer", "description": "Add premix", "reagent": "PrimeSTAR Max Premix",
              "volume_ul": 25, "destination": "PCR tube"},
             {"start": 260, "end": 266, "action": "thermocycle", "description": "Run PCR", "cycles": 35}]
    tmp = tempfile.TemporaryDirectory()
    pipeline.OUT = Path(tmp.name)  # a run of its own, so the check needs no local data (CI has none)
    for name in ("selfcheck", "u-private"):
        (pipeline.OUT / name).mkdir()
        (pipeline.OUT / name / "result.json").write_text(json.dumps(
            {"steps": steps, "protocol": protocol.generate(steps)[0], "sim_ok": True}), encoding="utf-8")
    s = server()
    s.start_insecure("127.0.0.1", 50199, enable_discovery=False)
    try:
        c = SilaClient("127.0.0.1", 50199, insecure=True)
        pg = c.ProtocolGenerator
        runs = pg.Runs.get()
        assert runs == ["selfcheck"], runs  # the upload stays private
        steps = json.loads(pg.GetSteps("selfcheck").Steps)
        assert any(st.get("volume_ul") for st in steps), steps
        assert "'robotType': 'Flex'" in pg.GetProtocol("selfcheck").Protocol
        for bad, ident in ((("nope",), "UnknownRun"), (("u-private",), "UnknownRun"), (("selfcheck", 10 ** 6), "InvalidReactions")):
            try:
                (pg.GetProtocol(*bad) if len(bad) == 1 else finished(pg.AnalyzeProtocol(*bad)))
                raise AssertionError(f"{bad} must raise {ident}")
            except DefinedExecutionError as e:
                assert e.identifier == ident, e
        a = finished(pg.AnalyzeProtocol("selfcheck", 2))  # observable: runs Opentrons' analyzer in the background
        assert a.Passed and a.Commands > 0 and json.loads(a.LiquidSetup), a
        print(f"sila self-check OK (runs, steps, protocol, 3 defined errors; analyzer at 2 reactions: {a.Commands} commands, passed)")
    finally:
        s.stop()
        tmp.cleanup()


if __name__ == "__main__":
    if "--self-check" in sys.argv:
        _selfcheck()
    else:
        port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else 50052
        s = server()
        if "--tls" in sys.argv:  # a self-signed certificate, announced over mDNS so SiLA clients can verify it
            s.start("0.0.0.0", port)
        else:
            s.start_insecure("0.0.0.0", port)
        print(f"SiLA 2 server on port {port}: feature ProtocolGenerator, {len(s.feature_servicers)} features")
        try:
            import threading
            threading.Event().wait()
        except KeyboardInterrupt:
            s.stop()
