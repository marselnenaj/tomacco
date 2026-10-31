#!/usr/bin/env python3
"""Check the real board's pin6/IRQ/timer with QTest; no iOS CPU execution."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from ios_dart_integration import connect
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"tools"))
from prepare_ios_paravirt import ap_cpu_count, decode
from run_ios_paravirt import QMP, file_digest, stop_child


def check(engine, receipt):
    record=json.loads(receipt.read_text())
    if record.get("machine") != "tomacco-vphone":
        raise ValueError("An owned tomacco-vphone input receipt is required")
    inputs={}
    for name in ("bootkc", "sptm", "txm", "dtree", "ramdisk", "tc",
                 *(key for key in ("sep", "sep-manifest") if key in record["inputs"])):
        row=record["inputs"][name]
        path=Path(row["path"]).resolve(strict=True)
        if file_digest(path) != row["sha256"]:
            raise ValueError("Changed board input: "+name)
        inputs[name]=path
    tree=decode(inputs["dtree"].read_bytes())
    memory=int.from_bytes(tree.child("chosen").get("dram-size"),"little")
    machine="tomacco-vphone,"+",".join(k+"="+str(p).replace(",",",,") for k,p in inputs.items())
    if "sep" in inputs:
        machine+=",sep-role="+record.get("sep_boot_role","normal")
        machine+=",sep-handoff="+record.get("sep_handoff","preloaded")
    with tempfile.TemporaryDirectory(prefix="tomacco-power-pin6-") as temporary:
        root=Path(temporary)
        disk=root/"root.img"
        disk.write_bytes(bytes(8192))
        cpus=ap_cpu_count(tree)
        command=[str(engine),"-M",machine,"-m",f"{memory >> 30}G","-smp",
                 f"{cpus},maxcpus={cpus+1}" if "sep" in inputs else str(cpus),
                 "-accel","qtest","-S","-display","none","-serial","null","-monitor","none",
                 "-qmp",f"unix:{root}/qmp,server=on,wait=off",
                 "-qtest",f"unix:{root}/test,server=on,wait=off","-qtest-log","/dev/null",
                 "-drive",f"if=none,id=tomacco-root,format=raw,readonly=on,file={disk}"]
        if "sep" in inputs:
            sep_disk=root/"sep.img"
            sep_disk.write_bytes(bytes(512*1024))
            command += ["-drive",f"if=none,id=tomacco-sep-storage,format=raw,file={sep_disk}"]
        with (root/"host.log").open("wb") as log:
            child=subprocess.Popen(command,stdout=log,stderr=log,start_new_session=True,
                env={**os.environ,"REIMS_VGPU_RAIL":"vulkan","REIMS_VGPU_WINDOW":"0"})
            client=None
            try:
                with connect(root/"qmp",child):
                    pass
                client=QMP(root/"qmp")
                interrupts={}
                with connect(root/"test",child) as peer, peer.makefile("rwb",buffering=0) as wire:
                    def bus(command):
                        wire.write((command+"\n").encode())
                        for _ in range(128):
                            reply=wire.readline().decode().split()
                            if reply and reply[0]=="IRQ":
                                interrupts[int(reply[2])]=reply[1]=="raise"
                                continue
                            assert reply and reply[0]=="OK",(command,reply)
                            return int(reply[1],0) if len(reply)>1 else None
                        raise RuntimeError("QTest interrupt budget exceeded")
                    bus("irq_intercept_in /machine/interrupts")
                    # QTest enables virtual timers without executing a guest CPU.
                    assert "return" in client.call("cont")
                    bus("writel 0x2006040c 0x40")
                    bus("writel 0x20060410 0x40")
                    assert bus("readl 0x20060100")==0
                    assert "return" in client.call("system_powerdown")
                    assert bus("readl 0x20060100")==0x40
                    assert bus("readl 0x20060020")==0
                    assert bus("readl 0x20060418")==0x40 and interrupts.get(5) is True
                    bus("clock_step 99999999")
                    assert bus("readl 0x20060100")==0x40
                    bus("clock_step 1")
                    assert bus("readl 0x20060100")==0
                    # Edge remains latched until the guest's ICR acknowledgement.
                    assert bus("readl 0x20060418")==0x40
                    bus("writel 0x2006041c 0x40")
                    assert bus("readl 0x20060418")==0 and interrupts.get(5) is False
                assert "return" in client.call("quit")
                assert child.wait(timeout=5)==0
            except BaseException as error:
                raise RuntimeError(str(error)+"\n"+(root/"host.log").read_text(errors="replace")) from error
            finally:
                if client is not None:
                    client.close()
                stop_child(child)
    return {"state":"passed","engine_sha256":file_digest(engine),
            "board_input_receipt":str(receipt),"board_input_receipt_sha256":file_digest(receipt),
            "backend":"qtest","ios_cpu_execution":False,"prepared_writable_media_used":False,
            "pin":6,"mask":64,"unrelated_pin3_low":True,"gpio_pulse_ms":100,
            "irq":37,"irq_acknowledged":True,"native_screen_response_verified":False,
            "production_ready":False}


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine",type=Path,required=True)
    parser.add_argument("--request",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    result=check(args.engine.resolve(strict=True),args.request.resolve(strict=True))
    args.output.write_text(json.dumps(result,indent=2)+"\n")
    print(json.dumps(result))
