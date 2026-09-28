import asyncio
from datetime import datetime

try:
    from bleak import BleakScanner
except ImportError:
    raise SystemExit("Bleak non installato. Esegui: pip install bleak")

async def main():
    print(f"[{datetime.now().isoformat(timespec='seconds')}] Scansione Bluetooth BLE per 15 secondi...\n")
    devices = await BleakScanner.discover(timeout=15.0, return_adv=True)

    if not devices:
        print("Nessun dispositivo BLE trovato.")
        return

    for address, (device, adv) in sorted(devices.items()):
        print("-" * 60)
        print(f"Nome:      {device.name or 'N/A'}")
        print(f"Address:   {address}")
        print(f"RSSI:      {adv.rssi}")
        print(f"LocalName: {adv.local_name or 'N/A'}")
        if adv.service_uuids:
            print(f"UUID:      {', '.join(adv.service_uuids)}")
        if adv.manufacturer_data:
            print(f"MFG data:  {adv.manufacturer_data}")

    print("\nNota: questo script vede dispositivi BLE. Se il Garmin GLO 2 usa Bluetooth Classic/SPP, potrebbe non comparire qui.")

if __name__ == '__main__':
    asyncio.run(main())