from app.unraid import parse_disks_ini, read_disks

SAMPLE = '''
["parity"]
idx="0"
name="parity"
device="sdb"
status="DISK_OK"
type="Parity"
spundown="1"
temp="*"
rotational="1"
["disk1"]
name="disk1"
device="sdc"
status="DISK_OK"
type="Data"
spundown="0"
temp="34"
rotational="1"
["disk2"]
name="disk2"
device=""
status="DISK_NP"
type="Data"
["cache"]
name="cache"
device="nvme0n1"
type="Cache"
spundown="0"
rotational="0"
["flash"]
name="flash"
device="sda"
type="Flash"
'''


def test_parse_disks_ini():
    disks = parse_disks_ini(SAMPLE)
    assert [d["name"] for d in disks] == ["parity", "disk1", "cache"]
    parity, disk1, cache = disks
    assert parity["asleep"] and parity["temp"] is None
    assert not disk1["asleep"] and disk1["temp"] == "34"
    assert not cache["rotational"]


def test_read_disks_missing_file(tmp_path):
    assert read_disks(str(tmp_path / "nope.ini")) is None
