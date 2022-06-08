import logging

from core.emulator.coreemu import CoreEmu
from core.emulator.data import IpPrefixes
from core.emulator.enumerations import EventTypes
from core.nodes.docker import DockerNode
from core.nodes.network import SwitchNode

if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)

    coreemu = CoreEmu()
    session = coreemu.create_session()
    session.set_state(EventTypes.CONFIGURATION_STATE)

    try:
        prefixes = IpPrefixes(ip4_prefix="10.83.0.0/16")

        # create switch
        switch = session.add_node(SwitchNode)

        # node one
        options = DockerNode.create_options()
        options.image = "core"
        options.binds.append(("/tmp/testbind", "/tmp/bind"))
        options.volumes.append(("var.log", "/tmp/var.log", True, True))
        node1 = session.add_node(DockerNode, options=options)
        interface1_data = prefixes.create_iface(node1)

        # node two
        node2 = session.add_node(DockerNode, options=options)
        interface2_data = prefixes.create_iface(node2)

        # node three
        # node_three = session.add_node(CoreNode)
        # interface_three = prefixes.create_iface(node_three)

        # add links
        session.add_link(node1.id, switch.id, interface1_data)
        session.add_link(node2.id, switch.id, interface2_data)
        # session.add_link(node_three.id, switch.id, interface_three)

        # instantiate
        session.instantiate()

        print(f"{node2.name}: {node2.volumes.values()}")
    finally:
        input("continue to shutdown")
        coreemu.shutdown()
