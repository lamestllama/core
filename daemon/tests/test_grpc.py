import time
from pathlib import Path
from queue import Queue

import grpc
import pytest
from mock import patch

from core.api.grpc import wrappers
from core.api.grpc.client import CoreGrpcClient, InterfaceHelper, MoveNodesStreamer
from core.api.grpc.server import CoreGrpcServer
from core.api.grpc.wrappers import (
    ConfigOption,
    ConfigOptionType,
    EmaneModelConfig,
    Event,
    Geo,
    Hook,
    Interface,
    Link,
    LinkOptions,
    MobilityAction,
    MoveNodesRequest,
    Node,
    NodeType,
    Position,
    ServiceAction,
    ServiceData,
    SessionLocation,
    SessionState,
)
from core.emane.models.ieee80211abg import EmaneIeee80211abgModel
from core.emane.nodes import EmaneNet
from core.emulator.data import IpPrefixes, NodeData
from core.emulator.enumerations import AlertLevels, EventTypes, MessageFlags
from core.errors import CoreError
from core.location.mobility import BasicRangeModel, Ns2ScriptedMobility
from core.nodes.base import CoreNode
from core.nodes.network import SwitchNode, WlanNode
from core.services.defaults.utilservices.services import DefaultRouteService
from core.xml.corexml import CoreXmlWriter


class TestGrpc:
    @pytest.mark.parametrize("definition", [False, True])
    def test_start_session(self, grpc_server: CoreGrpcServer, definition):
        # given
        client = CoreGrpcClient()
        with client.context_connect():
            session = client.create_session()
        position = Position(x=50, y=100)
        node1 = session.add_node(1, position=position)
        position = Position(x=100, y=100)
        node2 = session.add_node(2, position=position)
        position = Position(x=200, y=200)
        wlan_node = session.add_node(3, _type=NodeType.WIRELESS_LAN, position=position)
        iface_helper = InterfaceHelper(ip4_prefix="10.83.0.0/16")
        iface1_id = 0
        iface1 = iface_helper.create_iface(node1.id, iface1_id)
        iface2_id = 0
        iface2 = iface_helper.create_iface(node2.id, iface2_id)
        link = Link(node1_id=node1.id, node2_id=node2.id, iface1=iface1, iface2=iface2)
        session.links = [link]
        hook = Hook(state=SessionState.RUNTIME, file="echo.sh", data="echo hello")
        session.hooks = {hook.file: hook}
        location_x = 5
        location_y = 10
        location_z = 15
        location_lat = 20
        location_lon = 30
        location_alt = 40
        location_scale = 5
        session.location = SessionLocation(
            x=location_x,
            y=location_y,
            z=location_z,
            lat=location_lat,
            lon=location_lon,
            alt=location_alt,
            scale=location_scale,
        )

        # setup wlan config
        wlan_config_key = "range"
        wlan_config_value = "333"
        wlan_node.set_wlan({wlan_config_key: wlan_config_value})

        # setup mobility config
        mobility_config_key = "refresh_ms"
        mobility_config_value = "60"
        wlan_node.set_mobility({mobility_config_key: mobility_config_value})

        # setup service config
        service_name = DefaultRouteService.name
        file_name = DefaultRouteService.files[0]
        file_data = "hello world"
        service_data = ServiceData(templates={file_name: file_data})
        node1.service_configs[service_name] = service_data

        # setup session option
        option_key = "controlnet"
        option_value = "172.16.0.0/24"
        session.set_options({option_key: option_value})

        # when
        with patch.object(CoreXmlWriter, "write"):
            with client.context_connect():
                client.start_session(session, definition=definition)

        # then
        real_session = grpc_server.coreemu.sessions[session.id]
        if definition:
            state = EventTypes.DEFINITION_STATE
        else:
            state = EventTypes.RUNTIME_STATE
        assert real_session.state == state
        assert node1.id in real_session.nodes
        assert node2.id in real_session.nodes
        assert wlan_node.id in real_session.nodes
        assert iface1_id in real_session.nodes[node1.id].ifaces
        assert iface2_id in real_session.nodes[node2.id].ifaces
        hooks = real_session.hook_manager.script_hooks[EventTypes.RUNTIME_STATE]
        real_hook = hooks[hook.file]
        assert real_hook == hook.data
        assert real_session.location.refxyz == (location_x, location_y, location_z)
        assert real_session.location.refgeo == (
            location_lat,
            location_lon,
            location_alt,
        )
        assert real_session.location.refscale == location_scale
        set_wlan_config = real_session.mobility.get_model_config(
            wlan_node.id, BasicRangeModel.name
        )
        assert set_wlan_config[wlan_config_key] == wlan_config_value
        set_mobility_config = real_session.mobility.get_model_config(
            wlan_node.id, Ns2ScriptedMobility.name
        )
        assert set_mobility_config[mobility_config_key] == mobility_config_value
        real_node1 = real_session.get_node(node1.id, CoreNode)
        real_service = real_node1.services[service_name]
        real_templates = real_service.get_templates()
        real_template_data = real_templates[file_name]
        assert file_data == real_template_data

    @pytest.mark.parametrize("session_id", [None, 6013])
    def test_create_session(self, grpc_server: CoreGrpcServer, session_id: int | None):
        # given
        client = CoreGrpcClient()

        # when
        with client.context_connect():
            created_session = client.create_session(session_id)

        # then
        assert isinstance(created_session, wrappers.Session)
        session = grpc_server.coreemu.sessions.get(created_session.id)
        assert session is not None
        if session_id is not None:
            assert created_session.id == session_id
            assert session.id == session_id

    @pytest.mark.parametrize("session_id, expected", [(None, True), (6013, False)])
    def test_delete_session(
        self, grpc_server: CoreGrpcServer, session_id: int | None, expected: bool
    ):
        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()
        if session_id is None:
            session_id = session.id

        # then
        with client.context_connect():
            result = client.delete_session(session_id)

        # then
        assert result is expected
        assert grpc_server.coreemu.sessions.get(session_id) is None

    def test_get_session(self, grpc_server: CoreGrpcServer):
        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()
        session.add_node(CoreNode)
        session.set_state(EventTypes.DEFINITION_STATE)

        # then
        with client.context_connect():
            session = client.get_session(session.id)

        # then
        assert session.state == SessionState.DEFINITION
        assert len(session.nodes) == 1
        assert len(session.links) == 0

    def test_get_sessions(self, grpc_server: CoreGrpcServer):
        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()

        # then
        with client.context_connect():
            sessions = client.get_sessions()

        # then
        found_session = None
        for current_session in sessions:
            if current_session.id == session.id:
                found_session = current_session
                break
        assert len(sessions) == 1
        assert found_session is not None

    def test_add_node(self, grpc_server: CoreGrpcServer):
        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()

        # then
        with client.context_connect():
            position = Position(x=0, y=0)
            node = Node(id=1, name="n1", type=NodeType.DEFAULT, position=position)
            node_id = client.add_node(session.id, node)

        # then
        assert node_id is not None
        assert session.get_node(node_id, CoreNode) is not None

    def test_get_node(self, grpc_server: CoreGrpcServer):
        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()
        node = session.add_node(CoreNode)

        # then
        with client.context_connect():
            get_node, ifaces, links = client.get_node(session.id, node.id)

        # then
        assert node.id == get_node.id
        assert len(ifaces) == 0
        assert len(links) == 0

    def test_move_node_pos(self, grpc_server: CoreGrpcServer):
        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()
        node = session.add_node(CoreNode)
        position = Position(x=100.0, y=50.0)

        # then
        with client.context_connect():
            result = client.move_node(session.id, node.id, position=position)

        # then
        assert result is True
        assert node.position.x == position.x
        assert node.position.y == position.y

    def test_move_node_geo(self, grpc_server: CoreGrpcServer):
        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()
        node = session.add_node(CoreNode)
        geo = Geo(lon=0.0, lat=0.0, alt=0.0)

        # then
        with client.context_connect():
            result = client.move_node(session.id, node.id, geo=geo)

        # then
        assert result is True
        assert node.position.lon == geo.lon
        assert node.position.lat == geo.lat
        assert node.position.alt == geo.alt

    def test_move_node_exception(self, grpc_server: CoreGrpcServer):
        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()
        node = session.add_node(CoreNode)

        # then and when
        with pytest.raises(CoreError), client.context_connect():
            client.move_node(session.id, node.id)

    def test_edit_node(self, grpc_server: CoreGrpcServer):
        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()
        node = session.add_node(CoreNode)
        icon = "test.png"

        # then
        with client.context_connect():
            result = client.edit_node(session.id, node.id, icon)

        # then
        assert result is True
        assert node.icon == icon

    @pytest.mark.parametrize("node_id, expected", [(1, True), (2, False)])
    def test_delete_node(
        self, grpc_server: CoreGrpcServer, node_id: int, expected: bool
    ):
        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()
        node = session.add_node(CoreNode)

        # then
        with client.context_connect():
            result = client.delete_node(session.id, node_id)

        # then
        assert result is expected
        if expected is True:
            with pytest.raises(CoreError):
                assert session.get_node(node.id, CoreNode)

    def test_node_command(self, request, grpc_server: CoreGrpcServer):
        if request.config.getoption("mock"):
            pytest.skip("mocking calls")

        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()
        session.set_state(EventTypes.CONFIGURATION_STATE)
        node = session.add_node(CoreNode)
        session.instantiate()
        expected_output = "hello world"
        expected_status = 0

        # then
        command = f"echo {expected_output}"
        with client.context_connect():
            output = client.node_command(session.id, node.id, command)

        # then
        assert (expected_status, expected_output) == output

    def test_get_node_terminal(self, grpc_server: CoreGrpcServer):
        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()
        session.set_state(EventTypes.CONFIGURATION_STATE)
        node = session.add_node(CoreNode)
        session.instantiate()

        # then
        with client.context_connect():
            terminal = client.get_node_terminal(session.id, node.id)

        # then
        assert terminal is not None

    def test_save_xml(self, grpc_server: CoreGrpcServer, tmp_path: Path):
        # given
        xml_dir = tmp_path / "xml"
        xml_dir.mkdir()
        xml_file = xml_dir / "text.xml"
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()

        # then
        with client.context_connect():
            client.save_xml(session.id, str(xml_file))

        # then
        assert xml_file.exists()

    def test_open_xml_hook(self, grpc_server: CoreGrpcServer, tmp_path: Path):
        # given
        xml_dir = tmp_path / "xml"
        xml_dir.mkdir()
        xml_file = xml_dir / "text.xml"
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()
        session.save_xml(xml_file)

        # then
        with client.context_connect():
            result, session_id = client.open_xml(xml_file)

        # then
        assert result is True
        assert session_id is not None

    def test_add_link(self, grpc_server: CoreGrpcServer):
        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()
        switch = session.add_node(SwitchNode)
        node = session.add_node(CoreNode)
        assert len(session.link_manager.links()) == 0
        iface = InterfaceHelper("10.0.0.0/24").create_iface(node.id, 0)
        link = Link(node.id, switch.id, iface1=iface)

        # then
        with client.context_connect():
            result, iface1, _ = client.add_link(session.id, link)

        # then
        assert result is True
        assert len(session.link_manager.links()) == 1
        assert iface1.id == iface.id
        assert iface1.ip4 == iface.ip4

    def test_add_link_exception(self, grpc_server: CoreGrpcServer):
        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()
        node = session.add_node(CoreNode)

        # then
        link = Link(node.id, 3)
        with pytest.raises(grpc.RpcError):
            with client.context_connect():
                client.add_link(session.id, link)

    def test_edit_link(self, grpc_server: CoreGrpcServer, ip_prefixes: IpPrefixes):
        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()
        session.set_state(EventTypes.CONFIGURATION_STATE)
        switch = session.add_node(SwitchNode)
        node = session.add_node(CoreNode)
        iface_data = ip_prefixes.create_iface(node)
        iface, _ = session.add_link(node.id, switch.id, iface_data)
        session.instantiate()
        options = LinkOptions(bandwidth=30000)
        assert iface.options.bandwidth != options.bandwidth
        link = Link(node.id, switch.id, iface1=Interface(id=iface.id), options=options)

        # then
        with client.context_connect():
            result = client.edit_link(session.id, link)

        # then
        assert result is True
        assert options.bandwidth == iface.options.bandwidth

    def test_delete_link(self, grpc_server: CoreGrpcServer, ip_prefixes: IpPrefixes):
        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()
        node1 = session.add_node(CoreNode)
        iface1 = ip_prefixes.create_iface(node1)
        node2 = session.add_node(CoreNode)
        iface2 = ip_prefixes.create_iface(node2)
        session.add_link(node1.id, node2.id, iface1, iface2)
        assert len(session.link_manager.links()) == 1
        link = Link(
            node1.id,
            node2.id,
            iface1=Interface(id=iface1.id),
            iface2=Interface(id=iface2.id),
        )

        # then
        with client.context_connect():
            result = client.delete_link(session.id, link)

        # then
        assert result is True
        assert len(session.link_manager.links()) == 0

    def test_get_wlan_config(self, grpc_server: CoreGrpcServer):
        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()
        wlan = session.add_node(WlanNode)

        # then
        with client.context_connect():
            config = client.get_wlan_config(session.id, wlan.id)

        # then
        assert len(config) > 0

    def test_set_wlan_config(self, grpc_server: CoreGrpcServer):
        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()
        session.set_state(EventTypes.CONFIGURATION_STATE)
        wlan = session.add_node(WlanNode)
        wlan.setmodel(BasicRangeModel, BasicRangeModel.default_values())
        session.instantiate()
        range_key = "range"
        range_value = "50"

        # then
        with client.context_connect():
            result = client.set_wlan_config(
                session.id,
                wlan.id,
                {
                    range_key: range_value,
                    "delay": "0",
                    "loss": "0",
                    "bandwidth": "50000",
                    "error": "0",
                    "jitter": "0",
                },
            )

        # then
        assert result is True
        config = session.mobility.get_model_config(wlan.id, BasicRangeModel.name)
        assert config[range_key] == range_value
        assert wlan.wireless_model.range == int(range_value)

    def test_set_emane_model_config(self, grpc_server: CoreGrpcServer):
        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()
        session.set_location(47.57917, -122.13232, 2.00000, 1.0)
        options = EmaneNet.create_options()
        options.emane_model = EmaneIeee80211abgModel.name
        emane_network = session.add_node(EmaneNet, options=options)
        session.emane.node_models[emane_network.id] = EmaneIeee80211abgModel.name
        config_key = "bandwidth"
        config_value = "900000"
        option = ConfigOption(
            label=config_key,
            name=config_key,
            value=config_value,
            type=ConfigOptionType.INT32,
            group="Default",
        )
        config = EmaneModelConfig(
            emane_network.id, EmaneIeee80211abgModel.name, config={config_key: option}
        )

        # then
        with client.context_connect():
            result = client.set_emane_model_config(session.id, config)

        # then
        assert result is True
        config = session.emane.get_config(emane_network.id, EmaneIeee80211abgModel.name)
        assert config[config_key] == config_value

    def test_get_emane_model_config(self, grpc_server: CoreGrpcServer):
        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()
        session.set_location(47.57917, -122.13232, 2.00000, 1.0)
        options = EmaneNet.create_options()
        options.emane_model = EmaneIeee80211abgModel.name
        emane_network = session.add_node(EmaneNet, options=options)
        session.emane.node_models[emane_network.id] = EmaneIeee80211abgModel.name

        # then
        with client.context_connect():
            config = client.get_emane_model_config(
                session.id, emane_network.id, EmaneIeee80211abgModel.name
            )

        # then
        assert len(config) > 0

    def test_get_mobility_config(self, grpc_server: CoreGrpcServer):
        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()
        wlan = session.add_node(WlanNode)
        session.mobility.set_model_config(wlan.id, Ns2ScriptedMobility.name, {})

        # then
        with client.context_connect():
            config = client.get_mobility_config(session.id, wlan.id)

        # then
        assert len(config) > 0

    def test_set_mobility_config(self, grpc_server: CoreGrpcServer):
        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()
        wlan = session.add_node(WlanNode)
        config_key = "refresh_ms"
        config_value = "60"

        # then
        with client.context_connect():
            result = client.set_mobility_config(
                session.id, wlan.id, {config_key: config_value}
            )

        # then
        assert result is True
        config = session.mobility.get_model_config(wlan.id, Ns2ScriptedMobility.name)
        assert config[config_key] == config_value

    def test_mobility_action(self, grpc_server: CoreGrpcServer):
        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()
        wlan = session.add_node(WlanNode)
        session.mobility.set_model_config(wlan.id, Ns2ScriptedMobility.name, {})
        session.instantiate()

        # then
        with client.context_connect():
            result = client.mobility_action(session.id, wlan.id, MobilityAction.STOP)

        # then
        assert result is True

    def test_service_action(self, grpc_server: CoreGrpcServer):
        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()
        node = session.add_node(CoreNode)
        service_name = "DefaultRoute"

        # then
        with client.context_connect():
            result = client.service_action(
                session.id, node.id, service_name, ServiceAction.STOP
            )

        # then
        assert result is True

    def test_node_events(self, grpc_server: CoreGrpcServer):
        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()
        node = session.add_node(CoreNode)
        node.position.lat = 10.0
        node.position.lon = 20.0
        node.position.alt = 5.0
        queue = Queue()

        def handle_event(event: Event) -> None:
            assert event.session_id == session.id
            assert event.node_event is not None
            event_node = event.node_event.node
            assert event_node.geo.lat == node.position.lat
            assert event_node.geo.lon == node.position.lon
            assert event_node.geo.alt == node.position.alt
            queue.put(event)

        # then
        with client.context_connect():
            client.events(session.id, handle_event)
            time.sleep(0.1)
            session.broadcast_node(node)

            # then
            queue.get(timeout=5)

    def test_link_events(self, grpc_server: CoreGrpcServer, ip_prefixes: IpPrefixes):
        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()
        wlan = session.add_node(WlanNode)
        node = session.add_node(CoreNode)
        iface_data = ip_prefixes.create_iface(node)
        session.add_link(node.id, wlan.id, iface_data)
        core_link = list(session.link_manager.links())[0]
        link_data = core_link.get_data(MessageFlags.ADD)

        queue = Queue()

        def handle_event(event: Event) -> None:
            assert event.session_id == session.id
            assert event.link_event is not None
            queue.put(event)

        # then
        with client.context_connect():
            client.events(session.id, handle_event)
            time.sleep(0.1)
            session.broadcast_link(link_data)

            # then
            queue.get(timeout=5)

    def test_throughputs(self, request, grpc_server: CoreGrpcServer):
        if request.config.getoption("mock"):
            pytest.skip("mocking calls")

        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()
        queue = Queue()

        def handle_event(event_data):
            assert event_data.session_id == session.id
            queue.put(event_data)

        # then
        with client.context_connect():
            client.throughputs(session.id, handle_event)
            time.sleep(0.1)

            # then
            queue.get(timeout=5)

    def test_session_events(self, grpc_server: CoreGrpcServer):
        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()
        queue = Queue()

        def handle_event(event: Event) -> None:
            assert event.session_id == session.id
            assert event.session_event is not None
            queue.put(event)

        # then
        with client.context_connect():
            client.events(session.id, handle_event)
            time.sleep(0.1)
            session.broadcast_event(EventTypes.RUNTIME_STATE)

            # then
            queue.get(timeout=5)

    def test_alert_events(self, grpc_server: CoreGrpcServer):
        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()
        queue = Queue()
        alert_level = AlertLevels.FATAL
        source = "test"
        node_id = None
        text = "alert message"

        def handle_event(event: Event) -> None:
            assert event.session_id == session.id
            assert event.alert_event is not None
            alert_event = event.alert_event
            assert alert_event.level.value == alert_level.value
            assert alert_event.node_id == 0
            assert alert_event.source == source
            assert alert_event.text == text
            queue.put(event)

        # then
        with client.context_connect():
            client.events(session.id, handle_event)
            time.sleep(0.1)
            session.broadcast_alert(alert_level, source, text, node_id)

            # then
            queue.get(timeout=5)

    def test_move_nodes(self, grpc_server: CoreGrpcServer):
        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()
        node = session.add_node(CoreNode)
        x, y = 10.0, 15.0
        streamer = MoveNodesStreamer(session.id)
        streamer.send_position(node.id, x, y)
        streamer.stop()

        # then
        with client.context_connect():
            client.move_nodes(streamer)

        # assert
        assert node.position.x == x
        assert node.position.y == y

    def test_move_nodes_geo(self, grpc_server: CoreGrpcServer):
        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()
        node = session.add_node(CoreNode)
        lon, lat, alt = 10.0, 15.0, 5.0
        streamer = MoveNodesStreamer(session.id)
        streamer.send_geo(node.id, lon, lat, alt)
        streamer.stop()
        queue = Queue()

        def node_handler(node_data: NodeData):
            n = node_data.node
            assert n.position.lon == lon
            assert n.position.lat == lat
            assert n.position.alt == alt
            queue.put(node_data)

        session.broadcast_manager.add_handler(NodeData, node_handler)

        # then
        with client.context_connect():
            client.move_nodes(streamer)

        # assert
        assert queue.get(timeout=5)
        assert node.position.lon == lon
        assert node.position.lat == lat
        assert node.position.alt == alt

    def test_move_nodes_exception(self, grpc_server: CoreGrpcServer):
        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()
        streamer = MoveNodesStreamer(session.id)
        request = MoveNodesRequest(session.id + 1, 1)
        streamer.send(request)
        streamer.stop()

        # then
        with pytest.raises(grpc.RpcError):
            with client.context_connect():
                client.move_nodes(streamer)

    def test_wlan_link(self, grpc_server: CoreGrpcServer, ip_prefixes: IpPrefixes):
        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()
        session.set_state(EventTypes.CONFIGURATION_STATE)
        wlan = session.add_node(WlanNode)
        node1 = session.add_node(CoreNode)
        node2 = session.add_node(CoreNode)
        iface1_data = ip_prefixes.create_iface(node1)
        iface2_data = ip_prefixes.create_iface(node2)
        session.add_link(node1.id, wlan.id, iface1_data)
        session.add_link(node2.id, wlan.id, iface2_data)
        session.instantiate()
        assert len(session.link_manager.links()) == 2

        # when
        with client.context_connect():
            result1 = client.wlan_link(session.id, wlan.id, node1.id, node2.id, True)
            result2 = client.wlan_link(session.id, wlan.id, node1.id, node2.id, False)

        # then
        assert result1 is True
        assert result2 is True
