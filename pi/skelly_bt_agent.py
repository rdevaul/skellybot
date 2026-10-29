#!/usr/bin/env python3
"""skelly_bt_agent — D-Bus Bluetooth agent that auto-responds to PIN requests.

Spawned by skelly_orchestrator.py to handle classic BT pairing with PIN '1234'.
Registers as KeyboardDisplay agent with BlueZ and answers RequestPinCode.

Usage: python3 skelly_bt_agent.py <PIN>
Stops when killed (SIGTERM/SIGINT).
"""
import signal
import sys

from dbus_next.aio import MessageBus
from dbus_next.service import ServiceInterface, method

PIN = sys.argv[1] if len(sys.argv) > 1 else "1234"
AGENT_PATH = "/org/bluez/skelly_pin_agent"

class PinAgent(ServiceInterface):
    def __init__(self):
        super().__init__('org.bluez.Agent1')

    @method()
    def Release(self):
        pass

    @method()
    def RequestPinCode(self, device: 'o') -> 's':
        print(f"[agent] PIN requested for {device} → {PIN}", flush=True)
        return PIN

    @method()
    def DisplayPinCode(self, device: 'o', pincode: 's'):
        pass

    @method()
    def RequestPasskey(self, device: 'o') -> 'u':
        return 0

    @method()
    def DisplayPasskey(self, device: 'o', passkey: 'u', entered: 'q'):
        pass

    @method()
    def RequestConfirmation(self, device: 'o', passkey: 'u'):
        pass

    @method()
    def RequestAuthorization(self, device: 'o'):
        pass

    @method()
    def AuthorizeService(self, device: 'o', uuid: 's'):
        pass

    @method()
    def Cancel(self):
        pass

async def main():
    bus = await MessageBus(bus_type='system').connect()
    agent = PinAgent()
    bus.export(AGENT_PATH, agent)

    # Register with BlueZ
    obj = await bus.introspect("org.bluez", "/org/bluez")
    manager = bus.get_proxy_object("org.bluez", "/org/bluez")
    manager_iface = manager.get_interface("org.bluez.AgentManager1")

    try:
        await manager_iface.call_register_agent(AGENT_PATH, "KeyboardDisplay")
        print(f"[agent] registered as KeyboardDisplay with PIN={PIN}", flush=True)
    except Exception as e:
        # May already be registered — that's fine
        print(f"[agent] register note: {e}", flush=True)

    # Run until killed
    stop = asyncio.Future()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: stop.set_result(True) if not stop.done() else None)
    await stop

    # Unregister on exit
    try:
        await manager_iface.call_unregister_agent(AGENT_PATH)
    except Exception:
        pass

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
