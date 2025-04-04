from typing import Any

from core import constants
from core.config import ConfigString, Configuration
from core.services.base import CoreService

GROUP_NAME: str = "Security"


class VpnClient(CoreService):
    name: str = "VPNClient"
    group: str = GROUP_NAME
    files: list[str] = ["vpnclient.sh"]
    executables: list[str] = ["openvpn", "ip"]
    startup: list[str] = ["bash vpnclient.sh"]
    validate: list[str] = ["pidof openvpn"]
    shutdown: list[str] = ["pkill -f openvpn"]
    default_configs: list[Configuration] = [
        ConfigString(
            id="keydir", label="Key Dir", default=f"{constants.CORE_CONF_DIR}/keys"
        ),
        ConfigString(id="keyname", label="Key Name", default="client1"),
        ConfigString(id="server", label="Server", default="10.0.2.10"),
    ]


class VpnServer(CoreService):
    name: str = "VPNServer"
    group: str = GROUP_NAME
    files: list[str] = ["vpnserver.sh"]
    executables: list[str] = ["openvpn", "ip"]
    startup: list[str] = ["bash vpnserver.sh"]
    validate: list[str] = ["pidof openvpn"]
    shutdown: list[str] = ["pkill -f openvpn"]
    default_configs: list[Configuration] = [
        ConfigString(
            id="keydir", label="Key Dir", default=f"{constants.CORE_CONF_DIR}/keys"
        ),
        ConfigString(id="keyname", label="Key Name", default="server"),
        ConfigString(id="subnet", label="Subnet", default="10.0.200.0"),
    ]

    def data(self) -> dict[str, Any]:
        address = None
        for iface in self.node.get_ifaces(control=False):
            ip4 = iface.get_ip4()
            if ip4:
                address = str(ip4.ip)
                break
        return dict(address=address)


class IPsec(CoreService):
    name: str = "IPsec"
    group: str = GROUP_NAME
    files: list[str] = ["ipsec.sh"]
    executables: list[str] = ["racoon", "ip", "setkey"]
    startup: list[str] = ["bash ipsec.sh"]
    validate: list[str] = ["pidof racoon"]
    shutdown: list[str] = ["pkill -f racoon"]


class Firewall(CoreService):
    name: str = "Firewall"
    group: str = GROUP_NAME
    files: list[str] = ["firewall.sh"]
    executables: list[str] = ["iptables"]
    startup: list[str] = ["bash firewall.sh"]


class Nat(CoreService):
    name: str = "NAT"
    group: str = GROUP_NAME
    files: list[str] = ["nat.sh"]
    executables: list[str] = ["iptables"]
    startup: list[str] = ["bash nat.sh"]

    def data(self) -> dict[str, Any]:
        ifnames = []
        for iface in self.node.get_ifaces(control=False):
            ifnames.append(iface.name)
        return dict(ifnames=ifnames)
