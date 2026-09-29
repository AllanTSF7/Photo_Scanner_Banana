"""Find network scanners on the local network over mDNS (Epson advertises `_scanner._tcp`, port 1865).

Used so a scanning PC finds the FF-680W by name instead of a hand-typed IP that DHCP can change.
Never raises: no network, blocked multicast or a missing zeroconf package all just mean "found nothing".
"""

from __future__ import annotations

import ipaddress
import time

SERVICE = "_scanner._tcp.local."


def find_scanners(timeout: float = 2.5) -> list[dict]:
    try:
        from zeroconf import ServiceBrowser, ServiceListener, Zeroconf
    except ImportError:
        return []

    found: dict[str, dict] = {}

    class _Listener(ServiceListener):
        def add_service(self, zc, type_, name):
            info = zc.get_service_info(type_, name, timeout=1500)
            if not info:
                return
            props = {k.decode(errors="replace"): (v or b"").decode(errors="replace") for k, v in info.properties.items()}
            ipv4 = [a for a in info.parsed_addresses() if ipaddress.ip_address(a).version == 4]
            if ipv4:
                found[name] = {
                    "name": name.removesuffix("." + type_), "host": ipv4[0], "port": info.port,
                    "mfg": props.get("mfg", ""), "model": props.get("mdl", ""),
                    "available": props.get("scannerAvailable", "1") == "1",
                }

        def update_service(self, *args):
            pass

        def remove_service(self, *args):
            pass

    try:
        zc = Zeroconf()
    except Exception:  # noqa: BLE001 - no usable interface / multicast blocked
        return []
    try:
        ServiceBrowser(zc, SERVICE, _Listener())
        time.sleep(timeout)
    finally:
        zc.close()
    return list(found.values())


def pick_epson(scanners: list[dict]) -> dict | None:
    """Prefer the FF-680W, then any Epson."""
    for match in ("FF-680W", "EPSON"):
        for s in scanners:
            if match in f"{s['model']} {s['mfg']} {s['name']}".upper():
                return s
    return None
