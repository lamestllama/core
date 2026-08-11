from types import SimpleNamespace

import core.emane.models.tdma as tdma
from core.emane.models.tdma import (
    EmaneTdmaModel,
    TDMASchedule,
    TDMAScheduleEvent,
    build_tdma_schedule_event,
)


def write_schedule(tmp_path, name="schedule.xml", slot_duration=1000):
    schedule = tmp_path / name
    schedule.write_text(
        f"""<?xml version="1.0"?>
<emane-tdma-schedule>
  <structure frames="2" slots="2" slotoverhead="3"
             slotduration="{slot_duration}" bandwidth="1M"/>
  <multiframe frequency="225M" power="7.0" datarate="2M">
    <frame index="0" frequency="226M">
      <slot index="0" nodes="7"><tx class="3"/></slot>
      <slot index="0" nodes="8"><rx/></slot>
      <slot index="1" nodes="7,8"><rx/></slot>
    </frame>
    <frame index="1">
      <slot index="0" nodes="8"><tx class="0"/></slot>
      <slot index="0" nodes="7"><rx/></slot>
      <slot index="1" nodes="7,8"><rx/></slot>
    </frame>
  </multiframe>
</emane-tdma-schedule>
"""
    )
    return schedule


def test_build_tdma_schedule_event_selects_one_nem(tmp_path):
    schedule = write_schedule(tmp_path)

    event = build_tdma_schedule_event(schedule, 7)

    assert event.structure() == {
        "slots": 2,
        "frames": 2,
        "slotduration": 1000,
        "slotoverhead": 3,
        "bandwidth": 1000000,
    }
    frames = list(event)
    assert len(frames) == 2
    assert frames[0]["frame.frequency"] == 226000000
    assert frames[0]["slots"][0]["type"] == "tx"
    assert frames[0]["slots"][0]["service"] == 3
    assert frames[0]["slots"][1]["type"] == "rx"
    assert frames[1]["slots"][0]["type"] == "rx"
    assert frames[1]["slots"][1]["type"] == "rx"
    assert build_tdma_schedule_event(schedule, 9) is None


def test_build_matches_stock_cli_event_construction(tmp_path):
    schedule_path = write_schedule(tmp_path)
    actual = build_tdma_schedule_event(schedule_path, 7)

    # This is the event-building loop from emaneevent-tdmaschedule, restricted
    # to one target. Byte equality protects defaults, frame overrides, slot
    # types, ordering and structure while CORE changes only publication scope.
    schedule = TDMASchedule(str(schedule_path))
    expected = TDMAScheduleEvent(**schedule.defaults())
    frame_defaults = schedule.defaultsFrame()
    for frame_index, slots in schedule.info()[7].items():
        for slot_index, slot_values in slots.items():
            values = dict(slot_values)
            for key, value in frame_defaults[frame_index].items():
                values[f"frame.{key}"] = value
            expected.append(frame_index, slot_index, **values)
    expected.structure(**schedule.structure())

    assert actual.serialize() == expected.serialize()


def test_build_matches_stock_cli_for_partial_idle_schedule(tmp_path):
    schedule_path = tmp_path / "partial.xml"
    schedule_path.write_text(
        """<?xml version="1.0"?>
<emane-tdma-schedule>
  <multiframe frequency="225M" power="7.0" datarate="2M">
    <frame index="4" frequency="226M">
      <slot index="2" nodes="7">
        <tx class="2" destination="8"/>
      </slot>
      <slot index="3" nodes="7"><idle/></slot>
    </frame>
  </multiframe>
</emane-tdma-schedule>
"""
    )
    actual = build_tdma_schedule_event(schedule_path, 7)

    schedule = TDMASchedule(str(schedule_path))
    expected = TDMAScheduleEvent(**schedule.defaults())
    frame_defaults = schedule.defaultsFrame()
    for frame_index, slots in schedule.info()[7].items():
        for slot_index, slot_values in slots.items():
            values = dict(slot_values)
            for key, value in frame_defaults[frame_index].items():
                values[f"frame.{key}"] = value
            expected.append(frame_index, slot_index, **values)

    assert actual.structure() is None
    assert actual.serialize() == expected.serialize()
    frames = list(actual)
    assert frames[0]["slots"][2]["destination"] == 8
    assert frames[0]["slots"][3]["type"] == "idle"


def test_build_reports_unavailable_bindings(tmp_path):
    schedule = write_schedule(tmp_path)
    old_schedule = tdma.TDMASchedule
    old_event = tdma.TDMAScheduleEvent
    tdma.TDMASchedule = None
    tdma.TDMAScheduleEvent = None
    try:
        try:
            build_tdma_schedule_event(schedule, 7)
        except RuntimeError:
            pass
        else:
            raise AssertionError("missing EMANE bindings were accepted")
    finally:
        tdma.TDMASchedule = old_schedule
        tdma.TDMAScheduleEvent = old_event


def test_post_startup_publishes_only_current_nem(tmp_path):
    schedule = write_schedule(tmp_path)
    publications = []
    service = SimpleNamespace(
        device="ctrl0",
        events=SimpleNamespace(
            publish=lambda nem_id, event: publications.append((nem_id, event))
        ),
    )
    emane = SimpleNamespace(
        get_iface_config=lambda emane_net, iface: {"schedule": str(schedule)},
        get_nem_id=lambda iface: 7,
        event_manager=SimpleNamespace(get_service=lambda nem_id: service),
    )
    session = SimpleNamespace(
        emane=emane,
        get_node=lambda node_id, node_type: object(),
    )

    EmaneTdmaModel(session, 1).post_startup(object())

    assert len(publications) == 1
    assert publications[0][0] == 7
    assert list(publications[0][1])[0]["slots"][0]["type"] == "tx"


def test_two_interfaces_publish_only_their_own_views(tmp_path):
    schedule7 = write_schedule(tmp_path, "schedule7.xml", 1000)
    schedule8 = write_schedule(tmp_path, "schedule8.xml", 2000)
    iface7 = object()
    iface8 = object()
    publications = {7: [], 8: []}
    services = {
        nem_id: SimpleNamespace(
            device=f"ctrl{nem_id}",
            events=SimpleNamespace(
                publish=lambda target, event, owner=nem_id:
                    publications[owner].append((target, event))
            ),
        )
        for nem_id in publications
    }
    emane = SimpleNamespace(
        get_iface_config=lambda emane_net, iface: {
            "schedule": str(schedule7 if iface is iface7 else schedule8)
        },
        get_nem_id=lambda iface: 7 if iface is iface7 else 8,
        event_manager=SimpleNamespace(
            get_service=lambda nem_id: services[nem_id]
        ),
    )
    session = SimpleNamespace(
        emane=emane,
        get_node=lambda node_id, node_type: object(),
    )
    model = EmaneTdmaModel(session, 1)

    model.post_startup(iface7)
    model.post_startup(iface8)

    assert [item[0] for item in publications[7]] == [7]
    assert [item[0] for item in publications[8]] == [8]
    assert list(publications[7][0][1])[0]["slots"][0]["type"] == "tx"
    assert list(publications[8][0][1])[0]["slots"][0]["type"] == "rx"
    assert publications[7][0][1].structure()["slotduration"] == 1000
    assert publications[8][0][1].structure()["slotduration"] == 2000


def test_post_startup_does_not_publish_a_missing_nem(tmp_path):
    schedule = write_schedule(tmp_path)
    publications = []
    service = SimpleNamespace(
        device="ctrl0",
        events=SimpleNamespace(
            publish=lambda nem_id, event: publications.append((nem_id, event))
        ),
    )
    emane = SimpleNamespace(
        get_iface_config=lambda emane_net, iface: {"schedule": str(schedule)},
        get_nem_id=lambda iface: 9,
        event_manager=SimpleNamespace(get_service=lambda nem_id: service),
    )
    session = SimpleNamespace(
        emane=emane,
        get_node=lambda node_id, node_type: object(),
    )

    EmaneTdmaModel(session, 1).post_startup(object())

    assert publications == []


def test_post_startup_contains_publication_failure(tmp_path):
    schedule = write_schedule(tmp_path)

    def fail_publish(nem_id, event):
        raise OSError("event service unavailable")

    service = SimpleNamespace(
        device="ctrl0",
        events=SimpleNamespace(publish=fail_publish),
    )
    emane = SimpleNamespace(
        get_iface_config=lambda emane_net, iface: {"schedule": str(schedule)},
        get_nem_id=lambda iface: 7,
        event_manager=SimpleNamespace(get_service=lambda nem_id: service),
    )
    session = SimpleNamespace(
        emane=emane,
        get_node=lambda node_id, node_type: object(),
    )

    # A failed event socket must not abort startup of the remaining interfaces.
    EmaneTdmaModel(session, 1).post_startup(object())


def test_post_startup_contains_malformed_schedule(tmp_path):
    schedule = tmp_path / "malformed.xml"
    schedule.write_text("<emane-tdma-schedule>")
    publications = []
    service = SimpleNamespace(
        device="ctrl0",
        events=SimpleNamespace(
            publish=lambda nem_id, event: publications.append((nem_id, event))
        ),
    )
    emane = SimpleNamespace(
        get_iface_config=lambda emane_net, iface: {"schedule": str(schedule)},
        get_nem_id=lambda iface: 7,
        event_manager=SimpleNamespace(get_service=lambda nem_id: service),
    )
    session = SimpleNamespace(
        emane=emane,
        get_node=lambda node_id, node_type: object(),
    )

    EmaneTdmaModel(session, 1).post_startup(object())

    assert publications == []
