import abc
import logging
from typing import Any

from core.emane.nodes import EmaneNet
from core.nodes.base import CoreNodeBase, NodeBase
from core.nodes.interface import DEFAULT_MTU, CoreInterface
from core.nodes.network import PtpNet, WlanNode
from core.nodes.physical import Rj45Node
from core.nodes.wireless import WirelessNode
from core.services.base import CoreService

logger = logging.getLogger(__name__)
GROUP: str = "Quagga"
QUAGGA_STATE_DIR: str = "/var/run/quagga"


def is_wireless(node: NodeBase) -> bool:
    """
    Check if the node is a wireless type node.

    :param node: node to check type for
    :return: True if wireless type, False otherwise
    """
    return isinstance(node, (WlanNode, EmaneNet, WirelessNode))


def has_mtu_mismatch(iface: CoreInterface) -> bool:
    """
    Helper to detect MTU mismatch and add the appropriate OSPF
    mtu-ignore command. This is needed when e.g. a node is linked via a
    GreTap device.
    """
    if iface.mtu != DEFAULT_MTU:
        return True
    if not iface.net:
        return False
    for iface in iface.net.get_ifaces():
        if iface.mtu != iface.mtu:
            return True
    return False


def get_min_mtu(iface: CoreInterface):
    """
    Helper to discover the minimum MTU of interfaces linked with the
    given interface.
    """
    mtu = iface.mtu
    if not iface.net:
        return mtu
    for iface in iface.net.get_ifaces():
        if iface.mtu < mtu:
            mtu = iface.mtu
    return mtu


def get_router_id(node: CoreNodeBase) -> str:
    """
    Helper to return the first IPv4 address of a node as its router ID.
    """
    for iface in node.get_ifaces(control=False):
        ip4 = iface.get_ip4()
        if ip4:
            return str(ip4.ip)
    return "0.0.0.0"


def rj45_check(iface: CoreInterface) -> bool:
    """
    Helper to detect whether interface is connected an external RJ45
    link.
    """
    if iface.net:
        for peer_iface in iface.net.get_ifaces():
            if peer_iface == iface:
                continue
            if isinstance(peer_iface.node, Rj45Node):
                return True
    return False


class Zebra(CoreService):
    name: str = "zebra"
    group: str = GROUP
    directories: list[str] = ["/usr/local/etc/quagga", "/var/run/quagga"]
    files: list[str] = [
        "/usr/local/etc/quagga/Quagga.conf",
        "quaggaboot.sh",
        "/usr/local/etc/quagga/vtysh.conf",
    ]
    executables: list[str] = ["zebra"]
    startup: list[str] = ["bash quaggaboot.sh zebra"]
    validate: list[str] = ["pidof zebra"]
    shutdown: list[str] = ["pkill -f zebra"]

    def data(self) -> dict[str, Any]:
        quagga_bin_search = self.node.session.options.get(
            "quagga_bin_search", default="/usr/local/bin /usr/bin /usr/lib/quagga"
        ).strip('"')
        quagga_sbin_search = self.node.session.options.get(
            "quagga_sbin_search", default="/usr/local/sbin /usr/sbin /usr/lib/quagga"
        ).strip('"')
        quagga_state_dir = QUAGGA_STATE_DIR
        quagga_conf = self.files[0]

        services = []
        want_ip4 = False
        want_ip6 = False
        for service in self.node.services.values():
            if self.name not in service.dependencies:
                continue
            if not isinstance(service, QuaggaService):
                continue
            if service.ipv4_routing:
                want_ip4 = True
            if service.ipv6_routing:
                want_ip6 = True
            services.append(service)

        ifaces = []
        for iface in self.node.get_ifaces():
            ip4s = []
            ip6s = []
            for ip4 in iface.ip4s:
                ip4s.append(str(ip4))
            for ip6 in iface.ip6s:
                ip6s.append(str(ip6))
            configs = []
            if not iface.control:
                for service in services:
                    config = service.quagga_iface_config(iface)
                    if config:
                        configs.append(config.split("\n"))
            ifaces.append((iface, ip4s, ip6s, configs))

        return dict(
            quagga_bin_search=quagga_bin_search,
            quagga_sbin_search=quagga_sbin_search,
            quagga_state_dir=quagga_state_dir,
            quagga_conf=quagga_conf,
            ifaces=ifaces,
            want_ip4=want_ip4,
            want_ip6=want_ip6,
            services=services,
        )


class QuaggaService(abc.ABC):
    group: str = GROUP
    dependencies: list[str] = ["zebra"]
    ipv4_routing: bool = False
    ipv6_routing: bool = False

    @abc.abstractmethod
    def quagga_iface_config(self, iface: CoreInterface) -> str:
        raise NotImplementedError

    @abc.abstractmethod
    def quagga_config(self) -> str:
        raise NotImplementedError


class Ospfv2(QuaggaService, CoreService):
    """
    The OSPFv2 service provides IPv4 routing for wired networks. It does
    not build its own configuration file but has hooks for adding to the
    unified Quagga.conf file.
    """

    name: str = "OSPFv2"
    validate: list[str] = ["pidof ospfd"]
    shutdown: list[str] = ["pkill -f ospfd"]
    ipv4_routing: bool = True

    def quagga_iface_config(self, iface: CoreInterface) -> str:
        has_mtu = has_mtu_mismatch(iface)
        has_rj45 = rj45_check(iface)
        is_ptp = isinstance(iface.net, PtpNet)
        data = dict(has_mtu=has_mtu, is_ptp=is_ptp, has_rj45=has_rj45)
        text = """
        % if has_mtu:
        ip ospf mtu-ignore
        % endif
        % if has_rj45:
        <% return STOP_RENDERING %>
        % endif
        % if is_ptp:
        ip ospf network point-to-point
        % endif
        ip ospf hello-interval 2
        ip ospf dead-interval 6
        ip ospf retransmit-interval 5
        """
        return self.render_text(text, data)

    def quagga_config(self) -> str:
        router_id = get_router_id(self.node)
        addresses = []
        for iface in self.node.get_ifaces(control=False):
            for ip4 in iface.ip4s:
                addresses.append(str(ip4))
        data = dict(router_id=router_id, addresses=addresses)
        text = """
        router ospf
          router-id ${router_id}
          % for addr in addresses:
          network ${addr} area 0
          % endfor
        !
        """
        return self.render_text(text, data)


class Ospfv3(QuaggaService, CoreService):
    """
    The OSPFv3 service provides IPv6 routing for wired networks. It does
    not build its own configuration file but has hooks for adding to the
    unified Quagga.conf file.
    """

    name: str = "OSPFv3"
    shutdown: list[str] = ["pkill -f ospf6d"]
    validate: list[str] = ["pidof ospf6d"]
    ipv4_routing: bool = True
    ipv6_routing: bool = True

    def quagga_iface_config(self, iface: CoreInterface) -> str:
        mtu = get_min_mtu(iface)
        if mtu < iface.mtu:
            return f"ipv6 ospf6 ifmtu {mtu}"
        else:
            return ""

    def quagga_config(self) -> str:
        router_id = get_router_id(self.node)
        ifnames = []
        for iface in self.node.get_ifaces(control=False):
            ifnames.append(iface.name)
        data = dict(router_id=router_id, ifnames=ifnames)
        text = """
        router ospf6
          instance-id 65
          router-id ${router_id}
          % for ifname in ifnames:
          interface ${ifname} area 0.0.0.0
          % endfor
        !
        """
        return self.render_text(text, data)


class Ospfv3mdr(Ospfv3):
    """
    The OSPFv3 MANET Designated Router (MDR) service provides IPv6
    routing for wireless networks. It does not build its own
    configuration file but has hooks for adding to the
    unified Quagga.conf file.
    """

    name: str = "OSPFv3MDR"

    def quagga_iface_config(self, iface: CoreInterface) -> str:
        config = super().quagga_iface_config(iface)
        if is_wireless(iface.net):
            config = self.clean_text(
                f"""
                {config}
                ipv6 ospf6 hello-interval 2
                ipv6 ospf6 dead-interval 6
                ipv6 ospf6 retransmit-interval 5
                ipv6 ospf6 network manet-designated-router
                ipv6 ospf6 twohoprefresh 3
                ipv6 ospf6 adjacencyconnectivity uniconnected
                ipv6 ospf6 lsafullness mincostlsa
                """
            )
        return config


class Bgp(QuaggaService, CoreService):
    """
    The BGP service provides interdomain routing.
    Peers must be manually configured, with a full mesh for those
    having the same AS number.
    """

    name: str = "BGP"
    shutdown: list[str] = ["pkill -f bgpd"]
    validate: list[str] = ["pidof bgpd"]
    ipv4_routing: bool = True
    ipv6_routing: bool = True

    def quagga_config(self) -> str:
        router_id = get_router_id(self.node)
        text = f"""
        ! BGP configuration
        ! You should configure the AS number below
        ! along with this router's peers.
        router bgp {self.node.id}
          bgp router-id {router_id}
          redistribute connected
          !neighbor 1.2.3.4 remote-as 555
        !
        """
        return self.clean_text(text)

    def quagga_iface_config(self, iface: CoreInterface) -> str:
        return ""


class Rip(QuaggaService, CoreService):
    """
    The RIP service provides IPv4 routing for wired networks.
    """

    name: str = "RIP"
    shutdown: list[str] = ["pkill -f ripd"]
    validate: list[str] = ["pidof ripd"]
    ipv4_routing: bool = True

    def quagga_config(self) -> str:
        text = """
        router rip
          redistribute static
          redistribute connected
          redistribute ospf
          network 0.0.0.0/0
        !
        """
        return self.clean_text(text)

    def quagga_iface_config(self, iface: CoreInterface) -> str:
        return ""


class Ripng(QuaggaService, CoreService):
    """
    The RIP NG service provides IPv6 routing for wired networks.
    """

    name: str = "RIPNG"
    shutdown: list[str] = ["pkill -f ripngd"]
    validate: list[str] = ["pidof ripngd"]
    ipv6_routing: bool = True

    def quagga_config(self) -> str:
        text = """
        router ripng
          redistribute static
          redistribute connected
          redistribute ospf6
          network ::/0
        !
        """
        return self.clean_text(text)

    def quagga_iface_config(self, iface: CoreInterface) -> str:
        return ""


class Babel(QuaggaService, CoreService):
    """
    The Babel service provides a loop-avoiding distance-vector routing
    protocol for IPv6 and IPv4 with fast convergence properties.
    """

    name: str = "Babel"
    shutdown: list[str] = ["pkill -f babeld"]
    validate: list[str] = ["pidof babeld"]
    ipv6_routing: bool = True

    def quagga_config(self) -> str:
        ifnames = []
        for iface in self.node.get_ifaces(control=False):
            ifnames.append(iface.name)
        text = """
        router babel
          % for ifname in ifnames:
          network ${ifname}
          % endfor
          redistribute static
          redistribute connected
        !
        """
        data = dict(ifnames=ifnames)
        return self.render_text(text, data)

    def quagga_iface_config(self, iface: CoreInterface) -> str:
        if is_wireless(iface.net):
            text = """
            babel wireless
            no babel split-horizon
            """
        else:
            text = """
            babel wired
            babel split-horizon
            """
        return self.clean_text(text)


class Xpimd(QuaggaService, CoreService):
    """
    PIM multicast routing based on XORP.
    """

    name: str = "Xpimd"
    shutdown: list[str] = ["pkill -f xpimd"]
    validate: list[str] = ["pidof xpimd"]
    ipv4_routing: bool = True

    def quagga_config(self) -> str:
        ifname = "eth0"
        for iface in self.node.get_ifaces():
            if iface.name != "lo":
                ifname = iface.name
                break

        text = f"""
        router mfea
        !
        router igmp
        !
        router pim
          !ip pim rp-address 10.0.0.1
          ip pim bsr-candidate {ifname}
          ip pim rp-candidate {ifname}
          !ip pim spt-threshold interval 10 bytes 80000
        !
        """
        return self.clean_text(text)

    def quagga_iface_config(self, iface: CoreInterface) -> str:
        text = """
        ip mfea
        ip pim
        """
        return self.clean_text(text)
