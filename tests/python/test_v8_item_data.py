from __future__ import annotations

import os
import struct
from collections import Counter
from pathlib import Path

import pytest

import ezdgn
from ezdgn.v8 import (
    V8_ITEM_INSTANCE_ENTRY_TYPE,
    V8_MAX_AUXILIARY_ENTRIES,
    V8_MAX_ITEM_BYTES,
    decode_auxiliary_entries,
    decode_item_instance,
    decode_item_instances,
)


DATA = Path(__file__).parents[1] / "data" / "dgn"
V8 = DATA / "v8" / "test_dgnv8.dgn"

HEAD = bytes.fromhex("0000010115")
SIGNATURE = b"\x01BECXML\x00\xff\x0a\x0d\x01\x00\x00\x03\x00\x00\x30"
ROOT, CHILD, END, ATTRIBUTE, CONTENT, VALUE = 0x03, 0x02, 0x04, 0x05, 0x06, 0x11
ATTRIBUTES = (("instanceId", ""), ("xmlns", "Sample__x0020__Library.01.00"))


def _table_string(text: str) -> bytes:
    if all(ord(character) < 0x100 for character in text):
        return bytes([0xFA, len(text)]) + text.encode("latin-1")
    encoded = text.encode("utf-16-le")
    return bytes([0xFB, len(encoded) // 2]) + encoded


def _item_data(
    schema_element_id: int,
    class_name: str,
    attributes: tuple[tuple[str, str], ...],
    properties: tuple[tuple[str, str], ...],
) -> bytes:
    """Build one instance by hand: equal strings share one table entry."""

    strings: list[str] = []

    def number(text: str) -> int:
        if text not in strings:
            strings.append(text)
        return strings.index(text)

    tokens = [ROOT, number(class_name)]
    for name, value in attributes:
        tokens += [ATTRIBUTE, number(name), VALUE, number(value)]
    tokens.append(CONTENT)
    for name, value in properties:
        tokens += [CHILD, number(name), VALUE, number(value), END]
    tokens.append(END)
    table = b"".join(_table_string(text) for text in strings)
    return (
        HEAD
        + struct.pack("<I", schema_element_id)
        + SIGNATURE
        + bytes([len(strings)])
        + table
        + bytes(tokens)
    )


def _entry(type_id: int, entry_id: int, data: bytes, *, flag: bool = True) -> bytes:
    size = len(data) | (0x8000_0000 if flag else 0)
    return struct.pack("<IIII", type_id, entry_id, size, 0) + data


def _payload(*entries: bytes) -> bytes:
    return b"".join(entries) + b"\x01\x00\x00\x00"


def test_auxiliary_payload_splits_into_entries() -> None:
    item = _item_data(900, "Pump", ATTRIBUTES, (("Tag", "P-101"),))
    payload = _payload(
        _entry(0x5709_0000, 0, b"\x03\x00\x00\x00", flag=False),
        _entry(V8_ITEM_INSTANCE_ENTRY_TYPE, 2, item),
    )
    entries = decode_auxiliary_entries(payload, 2)
    assert entries is not None
    assert [(entry.index, entry.offset) for entry in entries] == [(0, 0), (1, 20)]
    first, second = entries
    assert (first.type_id, first.type_major, first.type_minor) == (0x5709_0000, 0x5709, 0)
    assert (first.entry_id, first.size_word, first.size_flag) == (0, 4, False)
    assert first.data == b"\x03\x00\x00\x00"
    assert (second.type_id, second.entry_id, second.size_flag) == (0x56FF_0000, 2, True)
    assert second.size_word == 0x8000_0000 | len(item)
    assert second.reserved == 0
    assert second.data == item
    assert decode_auxiliary_entries(b"\x01\x00\x00\x00", 0) == ()


def test_a_payload_with_more_entries_than_the_limit_stays_raw() -> None:
    assert V8_MAX_AUXILIARY_ENTRIES == 65_536
    empty = _entry(0x5709_0000, 0, b"", flag=False)
    at_limit = decode_auxiliary_entries(
        _payload(empty * V8_MAX_AUXILIARY_ENTRIES), V8_MAX_AUXILIARY_ENTRIES
    )
    assert at_limit is not None and len(at_limit) == V8_MAX_AUXILIARY_ENTRIES
    over = V8_MAX_AUXILIARY_ENTRIES + 1
    assert decode_auxiliary_entries(_payload(empty * over), over) is None


@pytest.mark.parametrize(
    ("payload", "count"),
    [
        (b"", 0),
        (b"", 1),
        (_payload(_entry(1, 1, b"abcd")), 0),
        (_payload(_entry(1, 1, b"abcd")), 2),
        (_payload(_entry(1, 1, b"abcd")), 0xFFFF_FFFF),
        (_payload(_entry(1, 1, b"abcd")), -1),
        (_payload(_entry(1, 1, b"abcd")), 1 << 32),
        (_entry(1, 1, b"abcd"), 1),
        (_payload(_entry(1, 1, b"abcd")) + b"\x00", 1),
        (_entry(1, 1, b"abcd") + b"\x02\x00\x00\x00", 1),
        (struct.pack("<IIII", 1, 1, 0x7FFF_FFFF, 0) + b"abcd\x01\x00\x00\x00", 1),
    ],
)
def test_auxiliary_payloads_that_do_not_match_the_layout_stay_raw(
    payload: bytes, count: int
) -> None:
    assert decode_auxiliary_entries(payload, count) is None


def test_item_instance_exposes_class_attributes_and_properties() -> None:
    properties = (
        ("Tag", "P-101"),
        ("Duty", ""),
        ("Standby", "P-101"),
        ("Bore", "1½\""),
        ("Wide", "2ƒ\" \U0001f600"),
    )
    data = _item_data(0x0001_02B3, "Pump", ATTRIBUTES, properties)
    # one-byte strings store a code point per byte; wide strings are UTF-16LE
    assert b"\xfa\x031\xbd\"" in data
    assert b"\xfb\x062\x00\x92\x01" in data
    (entry,) = decode_auxiliary_entries(
        _payload(_entry(V8_ITEM_INSTANCE_ENTRY_TYPE, 7, data)), 1
    )
    item = decode_item_instance(entry)
    assert item == ezdgn.V8ItemInstance(
        entry_id=7,
        schema_element_id=0x0001_02B3,
        class_name="Pump",
        attributes=ATTRIBUTES,
        properties=properties,
    )
    assert item.schema_reference == "Sample__x0020__Library.01.00"
    assert item.attribute("instanceId") == ""
    assert item.attribute("missing") is None
    assert item.get("Standby") == "P-101"
    assert item.get("Missing") is None
    assert item.get("Missing", "-") == "-"
    assert item.as_dict() == dict(properties)


def test_only_item_entries_with_the_observed_layout_are_decoded() -> None:
    data = _item_data(1, "Pump", ATTRIBUTES, (("Tag", "P-101"),))

    def decode(type_id: int, blob: bytes) -> ezdgn.V8ItemInstance | None:
        (entry,) = decode_auxiliary_entries(_payload(_entry(type_id, 1, blob)), 1)
        return decode_item_instance(entry)

    assert decode(V8_ITEM_INSTANCE_ENTRY_TYPE, data) is not None
    assert decode(0x5709_0000, data) is None
    assert decode(V8_ITEM_INSTANCE_ENTRY_TYPE | 1, data) is None
    # truncated at every length
    for end in range(len(data)):
        assert decode(V8_ITEM_INSTANCE_ENTRY_TYPE, data[:end]) is None, end
    # a changed head or signature byte
    for offset in (*range(5), *range(9, 27)):
        changed = bytearray(data)
        changed[offset] ^= 1
        assert decode(V8_ITEM_INSTANCE_ENTRY_TYPE, bytes(changed)) is None, offset
    # a second property is the observed layout; the cases after it are not
    second_property = data[:-1] + bytes([CHILD, 0, VALUE, 0, END, END])
    assert decode(V8_ITEM_INSTANCE_ENTRY_TYPE, second_property) is not None
    # trailing bytes, a nested element, and a property without a value
    assert decode(V8_ITEM_INSTANCE_ENTRY_TYPE, data + b"\x00") is None
    nested = data[:-2] + bytes([CHILD, 0, VALUE, 0, END, END, END])
    assert decode(V8_ITEM_INSTANCE_ENTRY_TYPE, nested) is None
    no_value = data[:-1] + bytes([CHILD, 0, END, END])
    assert decode(V8_ITEM_INSTANCE_ENTRY_TYPE, no_value) is None
    # a count, a length or a string number that needs more than seven bits
    wide_count = bytearray(data)
    wide_count[27] = 0x80
    assert decode(V8_ITEM_INSTANCE_ENTRY_TYPE, bytes(wide_count)) is None
    long_string = (
        HEAD
        + struct.pack("<I", 1)
        + SIGNATURE
        + bytes([1, 0xFA, 0x80])
        + b"a" * 0x80
        + bytes([ROOT, 0, ATTRIBUTE, 0, VALUE, 0, CONTENT, END])
    )
    assert decode(V8_ITEM_INSTANCE_ENTRY_TYPE, long_string) is None
    high_number = data[:-1] + bytes([CHILD, 0x80, VALUE, 0, END, END])
    assert decode(V8_ITEM_INSTANCE_ENTRY_TYPE, high_number) is None


def _record(*entries: bytes) -> ezdgn.V8AuxiliaryRecord:
    payload = _payload(*entries)
    return ezdgn.V8AuxiliaryRecord(
        index=0,
        stream_path="made-up",
        inflated_offset=0,
        magic=0,
        kind=170,
        reserved=0,
        element_id=1,
        flags=len(entries),
        raw_bytes=payload,
        payload=payload,
    )


def _text_bytes(items: tuple[ezdgn.V8ItemInstance, ...]) -> int:
    return sum(
        len(text.encode())
        for item in items
        for text in (item.class_name, *(part for pair in item.attributes for part in pair))
    )


def test_item_decoding_is_held_to_the_decoded_byte_limit() -> None:
    assert V8_MAX_ITEM_BYTES == 1_048_576
    # One table string of 127 three-byte characters. Each attribute pair is
    # four stored bytes that ask for two copies of it.
    long = "\u3042" * 127
    assert len(long.encode()) == 381

    def amplifying(pairs: int) -> bytes:
        return _item_data(1, long, ((long, long),) * pairs, ())

    def decode(blob: bytes) -> ezdgn.V8ItemInstance | None:
        (entry,) = decode_auxiliary_entries(
            _payload(_entry(V8_ITEM_INSTANCE_ENTRY_TYPE, 1, blob)), 1
        )
        return decode_item_instance(entry)

    # more than half of the limit, and inside it: decoded
    large = amplifying(700)
    assert len(large) < 4 * 1024
    item = decode(large)
    assert item is not None and len(item.attributes) == 700
    assert V8_MAX_ITEM_BYTES // 2 < _text_bytes((item,)) <= V8_MAX_ITEM_BYTES
    # twice that text is over the limit: the entry stays raw
    over = amplifying(1_400)
    assert len(over) < 8 * 1024
    assert decode(over) is None

    # One call for all records of an element shares one limit: the second
    # large entry and the item entries after it are counted, not decoded.
    small = _item_data(1, "Pump", ATTRIBUTES, (("Tag", "P-101"),))
    item_type = V8_ITEM_INSTANCE_ENTRY_TYPE
    records = (
        _record(_entry(item_type, 1, large), _entry(0x5709_0000, 2, small)),
        _record(_entry(item_type, 3, large), _entry(item_type, 4, small)),
        _record(_entry(item_type, 5, small)),
    )
    decoded = decode_item_instances(records)
    assert isinstance(decoded, ezdgn.V8ItemInstances)
    assert [item.entry_id for item in decoded.instances] == [1]
    assert decoded.over_limit == 3
    assert _text_bytes(decoded.instances) <= V8_MAX_ITEM_BYTES

    # without the limit in reach every item entry is listed, in stored order
    decoded = decode_item_instances(
        (_record(_entry(item_type, 1, small)), _record(), _record(_entry(item_type, 2, small)))
    )
    assert [item.entry_id for item in decoded.instances] == [1, 2]
    assert decoded.over_limit == 0
    assert decode_item_instances(()) == ezdgn.V8ItemInstances(instances=(), over_limit=0)


def test_fixture_auxiliary_record_holds_one_entry_and_no_item_data() -> None:
    document = ezdgn.read_v8(V8)
    (page,) = document.raw.named_auxiliary_pages
    (record,) = page.records
    assert record.flags == 1
    assert record.entries is not None
    (entry,) = record.entries
    assert (entry.type_id, entry.entry_id, entry.size_word) == (0x5709_0000, 0, 102)
    assert not entry.size_flag
    assert entry.data[:10] == bytes.fromhex("03000000c0000000789c")
    assert record.payload == (
        struct.pack("<IIII", entry.type_id, 0, 102, 0) + entry.data + b"\x01\x00\x00\x00"
    )
    assert decode_item_instance(entry) is None
    assert all(
        element.item_instances == ()
        for model in document.models
        for element in model.elements
    )


# The checks below read a public file that the repository does not
# redistribute: the "Water distribution mains" support example recorded in
# docs/v8/PROVENANCE.md. Set EZDGN_PUBLIC_V8_DIR to the folder that holds it.
PUBLIC_DIR = os.environ.get("EZDGN_PUBLIC_V8_DIR")
WATER_MAINS = "Water_distribution_mains.dgn"
WATER_MAINS_SHA256 = "f34bbb1b408b583d9cfc9d280e4d5782559cfa824b4b51210a03cf5c88235b64"


@pytest.mark.skipif(
    not PUBLIC_DIR or not (Path(PUBLIC_DIR or ".") / WATER_MAINS).is_file(),
    reason="set EZDGN_PUBLIC_V8_DIR to a folder with the public water mains file",
)
def test_public_water_mains_item_instances() -> None:
    import hashlib

    path = Path(PUBLIC_DIR or ".") / WATER_MAINS
    assert hashlib.sha256(path.read_bytes()).hexdigest() == WATER_MAINS_SHA256
    document = ezdgn.read_v8(path)
    (model,) = document.models
    assert len(model.elements) == 66_053

    records = [record for element in model.elements for record in element.auxiliary_records]
    assert len(records) == 66_053
    assert all(record.entries is not None and len(record.entries) == 2 for record in records)
    assert Counter(
        (entry.type_id, entry.entry_id, entry.size_flag, entry.reserved)
        for record in records
        for entry in record.entries or ()
    ) == {(0x56FF_0000, 1, False, 0): 66_053, (0x56FF_0000, 2, False, 0): 66_053}

    assert Counter(
        tuple((item.entry_id, item.class_name) for item in element.item_instances)
        for element in model.elements
    ) == {((1, "WaterMain"), (2, "Metadata")): 66_053}
    items = [item for element in model.elements for item in element.item_instances]
    assert Counter(item.attributes[0] for item in items) == {("instanceId", ""): 2 * 66_053}
    assert Counter(item.schema_reference for item in items) == {
        "DgnCustomItemTypes_Default__x0020__Library.01.00": 2 * 66_053
    }
    assert Counter(
        tuple(name for name, _ in item.properties) for item in items
    ) == {
        ("GaugeMM", "LiningMaterial", "Material"): 66_053,
        ("InstallationDate",): 66_053,
    }
    assert len({item.schema_element_id for item in items}) == 1
    schema_holder = next(
        item
        for page in document.raw.named_pages
        for item in page.objects
        if item.element_id == items[0].schema_element_id
    )
    assert schema_holder.element_type == 39

    first = model.elements[0].item_instances
    assert first[0].as_dict() == {
        "GaugeMM": "150",
        "LiningMaterial": "Cement Lined",
        "Material": "Ductile Iron",
    }
    assert first[1].as_dict() == {"InstallationDate": "1986-01-01"}
