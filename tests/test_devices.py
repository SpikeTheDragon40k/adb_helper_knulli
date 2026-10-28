from openadb.core.devices import DeviceState, diff_devices, parse_devices_output

RG = """List of devices attached
0123456789             device usb:3-2 product:occam model:Nexus_4 device:mako transport_id:1

"""


def test_rg35xx_line_parsed():
    devs = parse_devices_output(RG)
    assert len(devs) == 1
    d = devs[0]
    assert d.serial == "0123456789"
    assert d.state is DeviceState.DEVICE
    assert d.transport == "usb"
    assert d.usb_path == "3-2"
    assert d.reported_model == "Nexus 4"
    assert d.usable


def test_multiple_and_states():
    text = """* daemon not running; starting now at tcp:5037
* daemon started successfully
List of devices attached
0123456789             device usb:3-2 product:occam model:Nexus_4 device:mako transport_id:1
192.168.1.50:5555      offline transport_id:3
R58M123ABC             unauthorized usb:1-1 transport_id:4
emulator-5554          device product:sdk model:sdk device:generic transport_id:5
ZY22                   no permissions (user in plugdev group; are your udev rules wrong?); see [http://developer.android.com/tools/device.html] usb:1-4 transport_id:6
adb-XYZ._adb-tls-connect._tcp device product:p model:Pixel_7 device:panther transport_id:7
"""
    devs = {d.serial: d for d in parse_devices_output(text)}
    assert set(devs) == {"0123456789", "192.168.1.50:5555", "R58M123ABC", "emulator-5554", "ZY22",
                         "adb-XYZ._adb-tls-connect._tcp"}
    assert devs["192.168.1.50:5555"].state is DeviceState.OFFLINE
    assert devs["192.168.1.50:5555"].transport == "tcp"
    assert not devs["192.168.1.50:5555"].usable
    assert devs["R58M123ABC"].state is DeviceState.UNAUTHORIZED
    assert devs["ZY22"].state is DeviceState.NO_PERMISSIONS
    assert devs["ZY22"].usb_path == "1-4"
    assert "plugdev" in devs["ZY22"].state_detail
    assert devs["adb-XYZ._adb-tls-connect._tcp"].transport == "tcp"
    assert devs["emulator-5554"].transport == "emulator"


def test_empty_list():
    assert parse_devices_output("List of devices attached\n\n") == []


def test_diff():
    a = parse_devices_output(RG)
    b = parse_devices_output(RG.replace(" device usb", " offline usb"))
    d = diff_devices(a, b)
    assert not d.added and not d.removed and len(d.changed) == 1
    d2 = diff_devices(a, [])
    assert d2.removed and d2.removed[0].serial == "0123456789"
    assert diff_devices([], a).added
