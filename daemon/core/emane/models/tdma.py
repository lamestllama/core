"""
tdma.py: EMANE TDMA model bindings for CORE
"""

import logging
from pathlib import Path

from core import constants
from core.config import ConfigString
from core.emane import emanemodel
from core.emane.nodes import EmaneNet
from core.nodes.interface import CoreInterface

try:
    from emane.events import TDMASchedule, TDMAScheduleEvent
except ImportError:
    try:
        from emanesh.events import TDMASchedule, TDMAScheduleEvent
    except ImportError:
        TDMASchedule = None
        TDMAScheduleEvent = None

logger = logging.getLogger(__name__)


def build_tdma_schedule_event(schedule_path: Path, nem_id: int):
    """Build the configured schedule view addressed to one NEM."""
    if TDMASchedule is None or TDMAScheduleEvent is None:
        raise RuntimeError("compatible EMANE Python bindings are not installed")

    schedule = TDMASchedule(str(schedule_path))
    nem_schedule = schedule.info().get(nem_id)
    if nem_schedule is None:
        return None

    event = TDMAScheduleEvent(**schedule.defaults())
    frame_defaults = schedule.defaultsFrame()
    for frame_index, slots in nem_schedule.items():
        for slot_index, slot_values in slots.items():
            values = dict(slot_values)
            for key, value in frame_defaults.get(frame_index, {}).items():
                values[f"frame.{key}"] = value
            event.append(frame_index, slot_index, **values)

    structure = schedule.structure()
    if structure is not None:
        event.structure(**structure)

    return event


class EmaneTdmaModel(emanemodel.EmaneModel):
    # model name
    name: str = "emane_tdma"

    # mac configuration
    mac_library: str = "tdmaeventschedulerradiomodel"
    mac_xml: str = "tdmaeventschedulerradiomodel.xml"

    # add custom schedule options and ignore it when writing emane xml
    schedule_name: str = "schedule"
    default_schedule: Path = (
        constants.CORE_DATA_DIR / "examples" / "tdma" / "schedule.xml"
    )
    config_ignore: set[str] = {schedule_name}

    @classmethod
    def load(cls, emane_prefix: Path) -> None:
        cls.mac_defaults["pcrcurveuri"] = str(
            emane_prefix
            / "share/emane/xml/models/mac/tdmaeventscheduler/tdmabasemodelpcr.xml"
        )
        super().load(emane_prefix)
        config_item = ConfigString(
            id=cls.schedule_name,
            default=str(cls.default_schedule),
            label="TDMA schedule file (core)",
        )
        cls.mac_config.insert(0, config_item)

    def post_startup(self, iface: CoreInterface) -> None:
        """Publish only this interface's NEM view of the configured schedule.

        CORE's event channel is shared by every EMANE model in a session. The
        stock command-line publisher walks every NEM named by an XML document,
        so invoking it once per interface can overwrite unrelated waveforms.
        Addressing one parsed view through the interface's existing event
        service preserves all TDMA semantics without that global side effect.
        """
        emane_net = self.session.get_node(self.id, EmaneNet)
        config = self.session.emane.get_iface_config(emane_net, iface)
        schedule = Path(config[self.schedule_name])
        if not schedule.is_file():
            logger.error("ignoring invalid tdma schedule: %s", schedule)
            return

        nem_id = self.session.emane.get_nem_id(iface)
        if not nem_id:
            logger.error("could not find nem for interface")
            return

        service = self.session.emane.event_manager.get_service(nem_id)
        if not service:
            return

        try:
            event = build_tdma_schedule_event(schedule, nem_id)
        except SystemExit as error:
            logger.error(
                "invalid tdma schedule %s (parser exit %s); publishing nothing",
                schedule,
                error.code,
            )
            return
        except Exception:
            logger.exception(
                "failed to build tdma schedule %s for nem(%s)", schedule, nem_id
            )
            return

        if event is None:
            logger.error(
                "tdma schedule %s has no entry for nem(%s); publishing nothing",
                schedule,
                nem_id,
            )
            return

        try:
            service.events.publish(nem_id, event)
        except Exception:
            logger.exception(
                "failed to publish tdma schedule %s for nem(%s)",
                schedule,
                nem_id,
            )
            return

        logger.info(
            "published scoped tdma schedule: schedule(%s) nem(%s) device(%s)",
            schedule,
            nem_id,
            service.device,
        )
