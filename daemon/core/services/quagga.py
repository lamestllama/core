"""
quagga.py: defines routing services provided by Quagga.
"""
from typing import Optional, Tuple

import netaddr

from core.emane.nodes import EmaneNet
from core.emulator.enumerations import LinkTypes
from core.nodes.base import CoreNode
from core.nodes.interface import DEFAULT_MTU, CoreInterface
from core.nodes.network import PtpNet, WlanNode
from core.nodes.physical import Rj45Node
from core.services.coreservices import CoreService

QUAGGA_STATE_DIR: str = "/var/run/quagga"


class Zebra(CoreService):
    name: str = "zebra"
    group: str = "Quagga"
    dirs: Tuple[str, ...] = ("/usr/local/etc/quagga", "/var/run/quagga")
    configs: Tuple[str, ...] = (
        "/usr/local/etc/quagga/Quagga.conf",
        "quaggaboot.sh",
        "/usr/local/etc/quagga/vtysh.conf",
    )
    startup: Tuple[str, ...] = ("bash quaggaboot.sh zebra",)
    shutdown: Tuple[str, ...] = ("killall zebra",)
    validate: Tuple[str, ...] = ("pidof zebra",)

    @classmethod
    def generate_config(cls, node: CoreNode, filename: str) -> str:
        """
        Return the Quagga.conf or quaggaboot.sh file contents.
        """
        if filename == cls.configs[0]:
            return cls.generate_quagga_conf(node)
        elif filename == cls.configs[1]:
            return cls.generate_quagga_boot(node)
        elif filename == cls.configs[2]:
            return cls.generate_vtysh_conf(node)
        else:
            raise ValueError(
                "file name (%s) is not a known configuration: %s", filename, cls.configs
            )

    @classmethod
    def generate_vtysh_conf(cls, node: CoreNode) -> str:
        """
        Returns configuration file text.
        """
        return "service integrated-vtysh-config\n"

    @classmethod
    def generate_quagga_conf(cls, node: CoreNode) -> str:
        """
        Returns configuration file text. Other services that depend on zebra
        will have hooks that are invoked here.
        """
        # we could verify here that filename == Quagga.conf
        cfg = ""
        for iface in node.get_ifaces():
            cfg += "interface %s\n" % iface.name
            # include control interfaces in addressing but not routing daemons
            if getattr(iface, "control", False):
                cfg += "  "
                cfg += "\n  ".join(map(cls.addrstr, iface.ips()))
                cfg += "\n"
                continue
            cfgv4 = ""
            cfgv6 = ""
            want_ipv4 = False
            want_ipv6 = False
            for s in node.services:
                if cls.name not in s.dependencies:
                    continue
                if not (isinstance(s, QuaggaService) or issubclass(s, QuaggaService)):
                    continue
                iface_config = s.generate_quagga_iface_config(node, iface)
                if s.ipv4_routing:
                    want_ipv4 = True
                if s.ipv6_routing:
                    want_ipv6 = True
                    cfgv6 += iface_config
                else:
                    cfgv4 += iface_config

            if want_ipv4:
                cfg += "  "
                cfg += "\n  ".join(map(cls.addrstr, iface.ip4s))
                cfg += "\n"
                cfg += cfgv4
            if want_ipv6:
                cfg += "  "
                cfg += "\n  ".join(map(cls.addrstr, iface.ip6s))
                cfg += "\n"
                cfg += cfgv6
            cfg += "!\n"

        for s in node.services:
            if cls.name not in s.dependencies:
                continue
            if not (isinstance(s, QuaggaService) or issubclass(s, QuaggaService)):
                continue
            cfg += s.generate_quagga_config(node)
        return cfg

    @staticmethod
    def addrstr(ip: netaddr.IPNetwork) -> str:
        """
        helper for mapping IP addresses to zebra config statements
        """
        address = str(ip.ip)
        if netaddr.valid_ipv4(address):
            return "ip address %s" % ip
        elif netaddr.valid_ipv6(address):
            return "ipv6 address %s" % ip
        else:
            raise ValueError("invalid address: %s", ip)

    @classmethod
    def generate_quagga_boot(cls, node: CoreNode) -> str:
        """
        Generate a shell script used to boot the Quagga daemons.
        """
        quagga_bin_search = node.session.options.get_config(
            "quagga_bin_search", default='"/usr/local/bin /usr/bin /usr/lib/quagga"'
        )
        quagga_sbin_search = node.session.options.get_config(
            "quagga_sbin_search", default='"/usr/local/sbin /usr/sbin /usr/lib/quagga"'
        )
        return """\
#!/bin/sh
# auto-generated by zebra service (quagga.py)
QUAGGA_CONF=%s
QUAGGA_SBIN_SEARCH=%s
QUAGGA_BIN_SEARCH=%s
QUAGGA_STATE_DIR=%s

searchforprog()
{
    prog=$1
    searchpath=$@
    ret=
    for p in $searchpath; do
        if [ -x $p/$prog ]; then
            ret=$p
            break
        fi
    done
    echo $ret
}

confcheck()
{
    CONF_DIR=`dirname $QUAGGA_CONF`
    # if /etc/quagga exists, point /etc/quagga/Quagga.conf -> CONF_DIR
    if [ "$CONF_DIR" != "/etc/quagga" ] && [ -d /etc/quagga ] && [ ! -e /etc/quagga/Quagga.conf ]; then
        ln -s $CONF_DIR/Quagga.conf /etc/quagga/Quagga.conf
    fi
    # if /etc/quagga exists, point /etc/quagga/vtysh.conf -> CONF_DIR
    if [ "$CONF_DIR" != "/etc/quagga" ] && [ -d /etc/quagga ] && [ ! -e /etc/quagga/vtysh.conf ]; then
        ln -s $CONF_DIR/vtysh.conf /etc/quagga/vtysh.conf
    fi
}

bootdaemon()
{
    QUAGGA_SBIN_DIR=$(searchforprog $1 $QUAGGA_SBIN_SEARCH)
    if [ "z$QUAGGA_SBIN_DIR" = "z" ]; then
        echo "ERROR: Quagga's '$1' daemon not found in search path:"
        echo "  $QUAGGA_SBIN_SEARCH"
        return 1
    fi

    flags=""

    if [ "$1" = "xpimd" ] && \\
        grep -E -q '^[[:space:]]*router[[:space:]]+pim6[[:space:]]*$' $QUAGGA_CONF; then
        flags="$flags -6"
    fi

    $QUAGGA_SBIN_DIR/$1 $flags -d
    if [ "$?" != "0" ]; then
        echo "ERROR: Quagga's '$1' daemon failed to start!:"
        return 1
    fi
}

bootquagga()
{
    QUAGGA_BIN_DIR=$(searchforprog 'vtysh' $QUAGGA_BIN_SEARCH)
    if [ "z$QUAGGA_BIN_DIR" = "z" ]; then
        echo "ERROR: Quagga's 'vtysh' program not found in search path:"
        echo "  $QUAGGA_BIN_SEARCH"
        return 1
    fi

    # fix /var/run/quagga permissions
    id -u quagga 2>/dev/null >/dev/null
    if [ "$?" = "0" ]; then
        chown quagga $QUAGGA_STATE_DIR
    fi

    bootdaemon "zebra"
    for r in rip ripng ospf6 ospf bgp babel; do
        if grep -q "^router \\<${r}\\>" $QUAGGA_CONF; then
            bootdaemon "${r}d"
        fi
    done

    if grep -E -q '^[[:space:]]*router[[:space:]]+pim6?[[:space:]]*$' $QUAGGA_CONF; then
        bootdaemon "xpimd"
    fi

    $QUAGGA_BIN_DIR/vtysh -b
}

if [ "$1" != "zebra" ]; then
    echo "WARNING: '$1': all Quagga daemons are launched by the 'zebra' service!"
    exit 1
fi
confcheck
bootquagga
""" % (
            cls.configs[0],
            quagga_sbin_search,
            quagga_bin_search,
            QUAGGA_STATE_DIR,
        )


class QuaggaService(CoreService):
    """
    Parent class for Quagga services. Defines properties and methods
    common to Quagga's routing daemons.
    """

    name: Optional[str] = None
    group: str = "Quagga"
    dependencies: Tuple[str, ...] = (Zebra.name,)
    meta: str = "The config file for this service can be found in the Zebra service."
    ipv4_routing: bool = False
    ipv6_routing: bool = False

    @staticmethod
    def router_id(node: CoreNode) -> str:
        """
        Helper to return the first IPv4 address of a node as its router ID.
        """
        for iface in node.get_ifaces(control=False):
            ip4 = iface.get_ip4()
            if ip4:
                return str(ip4.ip)
        return f"0.0.0.{node.id:d}"

    @staticmethod
    def rj45check(iface: CoreInterface) -> bool:
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

    @classmethod
    def generate_config(cls, node: CoreNode, filename: str) -> str:
        return ""

    @classmethod
    def generate_quagga_iface_config(cls, node: CoreNode, iface: CoreInterface) -> str:
        return ""

    @classmethod
    def generate_quagga_config(cls, node: CoreNode) -> str:
        return ""


class Ospfv2(QuaggaService):
    """
    The OSPFv2 service provides IPv4 routing for wired networks. It does
    not build its own configuration file but has hooks for adding to the
    unified Quagga.conf file.
    """

    name: str = "OSPFv2"
    shutdown: Tuple[str, ...] = ("killall ospfd",)
    validate: Tuple[str, ...] = ("pidof ospfd",)
    ipv4_routing: bool = True

    @staticmethod
    def mtu_check(iface: CoreInterface) -> str:
        """
        Helper to detect MTU mismatch and add the appropriate OSPF
        mtu-ignore command. This is needed when e.g. a node is linked via a
        GreTap device.
        """
        if iface.mtu != DEFAULT_MTU:
            # a workaround for PhysicalNode GreTap, which has no knowledge of
            # the other nodes/nets
            return "  ip ospf mtu-ignore\n"
        if not iface.net:
            return ""
        for iface in iface.net.get_ifaces():
            if iface.mtu != iface.mtu:
                return "  ip ospf mtu-ignore\n"
        return ""

    @staticmethod
    def ptp_check(iface: CoreInterface) -> str:
        """
        Helper to detect whether interface is connected to a notional
        point-to-point link.
        """
        if isinstance(iface.net, PtpNet):
            return "  ip ospf network point-to-point\n"
        return ""

    @classmethod
    def generate_quagga_config(cls, node: CoreNode) -> str:
        cfg = "router ospf\n"
        rtrid = cls.router_id(node)
        cfg += "  router-id %s\n" % rtrid
        # network 10.0.0.0/24 area 0
        for iface in node.get_ifaces(control=False):
            for ip4 in iface.ip4s:
                cfg += f"  network {ip4} area 0\n"
        cfg += "!\n"
        return cfg

    @classmethod
    def generate_quagga_iface_config(cls, node: CoreNode, iface: CoreInterface) -> str:
        cfg = cls.mtu_check(iface)
        # external RJ45 connections will use default OSPF timers
        if cls.rj45check(iface):
            return cfg
        cfg += cls.ptp_check(iface)
        return (
            cfg
            + """\
  ip ospf hello-interval 2
  ip ospf dead-interval 6
  ip ospf retransmit-interval 5
"""
        )


class Ospfv3(QuaggaService):
    """
    The OSPFv3 service provides IPv6 routing for wired networks. It does
    not build its own configuration file but has hooks for adding to the
    unified Quagga.conf file.
    """

    name: str = "OSPFv3"
    shutdown: Tuple[str, ...] = ("killall ospf6d",)
    validate: Tuple[str, ...] = ("pidof ospf6d",)
    ipv4_routing: bool = True
    ipv6_routing: bool = True

    @staticmethod
    def min_mtu(iface: CoreInterface) -> int:
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

    @classmethod
    def mtu_check(cls, iface: CoreInterface) -> str:
        """
        Helper to detect MTU mismatch and add the appropriate OSPFv3
        ifmtu command. This is needed when e.g. a node is linked via a
        GreTap device.
        """
        minmtu = cls.min_mtu(iface)
        if minmtu < iface.mtu:
            return "  ipv6 ospf6 ifmtu %d\n" % minmtu
        else:
            return ""

    @staticmethod
    def ptp_check(iface: CoreInterface) -> str:
        """
        Helper to detect whether interface is connected to a notional
        point-to-point link.
        """
        if isinstance(iface.net, PtpNet):
            return "  ipv6 ospf6 network point-to-point\n"
        return ""

    @classmethod
    def generate_quagga_config(cls, node: CoreNode) -> str:
        cfg = "router ospf6\n"
        rtrid = cls.router_id(node)
        cfg += "  instance-id 65\n"
        cfg += "  router-id %s\n" % rtrid
        for iface in node.get_ifaces(control=False):
            cfg += "  interface %s area 0.0.0.0\n" % iface.name
        cfg += "!\n"
        return cfg

    @classmethod
    def generate_quagga_iface_config(cls, node: CoreNode, iface: CoreInterface) -> str:
        return cls.mtu_check(iface)


class Ospfv3mdr(Ospfv3):
    """
    The OSPFv3 MANET Designated Router (MDR) service provides IPv6
    routing for wireless networks. It does not build its own
    configuration file but has hooks for adding to the
    unified Quagga.conf file.
    """

    name: str = "OSPFv3MDR"
    ipv4_routing: bool = True

    @classmethod
    def generate_quagga_iface_config(cls, node: CoreNode, iface: CoreInterface) -> str:
        cfg = cls.mtu_check(iface)
        if iface.net is not None and isinstance(iface.net, (WlanNode, EmaneNet)):
            return (
                cfg
                + """\
  ipv6 ospf6 hello-interval 2
  ipv6 ospf6 dead-interval 6
  ipv6 ospf6 retransmit-interval 5
  ipv6 ospf6 network manet-designated-router
  ipv6 ospf6 twohoprefresh 3
  ipv6 ospf6 adjacencyconnectivity uniconnected
  ipv6 ospf6 lsafullness mincostlsa
"""
            )
        else:
            return cfg


class Bgp(QuaggaService):
    """
    The BGP service provides interdomain routing.
    Peers must be manually configured, with a full mesh for those
    having the same AS number.
    """

    name: str = "BGP"
    shutdown: Tuple[str, ...] = ("killall bgpd",)
    validate: Tuple[str, ...] = ("pidof bgpd",)
    custom_needed: bool = True
    ipv4_routing: bool = True
    ipv6_routing: bool = True

    @classmethod
    def generate_quagga_config(cls, node: CoreNode) -> str:
        cfg = "!\n! BGP configuration\n!\n"
        cfg += "! You should configure the AS number below,\n"
        cfg += "! along with this router's peers.\n!\n"
        cfg += "router bgp %s\n" % node.id
        rtrid = cls.router_id(node)
        cfg += "  bgp router-id %s\n" % rtrid
        cfg += "  redistribute connected\n"
        cfg += "! neighbor 1.2.3.4 remote-as 555\n!\n"
        return cfg


class Rip(QuaggaService):
    """
    The RIP service provides IPv4 routing for wired networks.
    """

    name: str = "RIP"
    shutdown: Tuple[str, ...] = ("killall ripd",)
    validate: Tuple[str, ...] = ("pidof ripd",)
    ipv4_routing: bool = True

    @classmethod
    def generate_quagga_config(cls, node: CoreNode) -> str:
        cfg = """\
router rip
  redistribute static
  redistribute connected
  redistribute ospf
  network 0.0.0.0/0
!
"""
        return cfg


class Ripng(QuaggaService):
    """
    The RIP NG service provides IPv6 routing for wired networks.
    """

    name: str = "RIPNG"
    shutdown: Tuple[str, ...] = ("killall ripngd",)
    validate: Tuple[str, ...] = ("pidof ripngd",)
    ipv6_routing: bool = True

    @classmethod
    def generate_quagga_config(cls, node: CoreNode) -> str:
        cfg = """\
router ripng
  redistribute static
  redistribute connected
  redistribute ospf6
  network ::/0
!
"""
        return cfg


class Babel(QuaggaService):
    """
    The Babel service provides a loop-avoiding distance-vector routing
    protocol for IPv6 and IPv4 with fast convergence properties.
    """

    name: str = "Babel"
    shutdown: Tuple[str, ...] = ("killall babeld",)
    validate: Tuple[str, ...] = ("pidof babeld",)
    ipv6_routing: bool = True

    @classmethod
    def generate_quagga_config(cls, node: CoreNode) -> str:
        cfg = "router babel\n"
        for iface in node.get_ifaces(control=False):
            cfg += "  network %s\n" % iface.name
        cfg += "  redistribute static\n  redistribute connected\n"
        return cfg

    @classmethod
    def generate_quagga_iface_config(cls, node: CoreNode, iface: CoreInterface) -> str:
        if iface.net and iface.net.linktype == LinkTypes.WIRELESS:
            return "  babel wireless\n  no babel split-horizon\n"
        else:
            return "  babel wired\n  babel split-horizon\n"


class Xpimd(QuaggaService):
    """
    PIM multicast routing based on XORP.
    """

    name: str = "Xpimd"
    shutdown: Tuple[str, ...] = ("killall xpimd",)
    validate: Tuple[str, ...] = ("pidof xpimd",)
    ipv4_routing: bool = True

    @classmethod
    def generate_quagga_config(cls, node: CoreNode) -> str:
        ifname = "eth0"
        for iface in node.get_ifaces():
            if iface.name != "lo":
                ifname = iface.name
                break
        cfg = "router mfea\n!\n"
        cfg += "router igmp\n!\n"
        cfg += "router pim\n"
        cfg += "  !ip pim rp-address 10.0.0.1\n"
        cfg += "  ip pim bsr-candidate %s\n" % ifname
        cfg += "  ip pim rp-candidate %s\n" % ifname
        cfg += "  !ip pim spt-threshold interval 10 bytes 80000\n"
        return cfg

    @classmethod
    def generate_quagga_iface_config(cls, node: CoreNode, iface: CoreInterface) -> str:
        return "  ip mfea\n  ip igmp\n  ip pim\n"
