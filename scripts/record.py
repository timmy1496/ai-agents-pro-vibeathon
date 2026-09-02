"""Дописує синтетичні події стенду у data/*.json (джерело для tools get_deploys / k8s_events)."""
import json
import pathlib
import sys
import time

DATA = pathlib.Path(__file__).resolve().parent.parent / "data"


def append(filename: str, event: dict) -> None:
    path = DATA / filename
    events = json.loads(path.read_text()) if path.exists() else []
    events.append(event | {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
    path.write_text(json.dumps(events, indent=2, ensure_ascii=False) + "\n")
    print(f"{filename} += {event}")


def main(argv: list[str]) -> None:
    kind = argv[1]
    if kind == "deploy":
        _, _, service, version = argv[:4]
        append("deploys.json", {"service": service, "version": version,
                                "author": "demo", "commit": "deadbee"})
    elif kind == "k8s":
        _, _, service, reason, message = argv[:5]
        etype = "Warning" if reason in ("OOMKilling", "BackOff", "Unhealthy") else "Normal"
        append("k8s_events.json", {"service": service, "type": etype,
                                   "reason": reason, "message": message})
    else:
        sys.exit(f"unknown kind: {kind}")


if __name__ == "__main__":
    main(sys.argv)
