# Standard Libraries
import pathlib
# Third party packages
import pandas as pd
import numpy as np
from pydantic.typing import Tuple
# Local package
from net_models.loaders import BaseLoader
from net_models.models import (
    VLANModel
)
from net_models.models.interfaces import *
from net_models.inventory import *
# Local module


META_KEYS = ["use"]
GLOBAL_VRFS = [None, 'global']

class ExcelLoader(BaseLoader):

    def __init__(self, input_file: pathlib.Path):
        super(ExcelLoader, self).__init__()
        self.input_file = self.resolve_path(path=input_file)
        self.templates = {}

    def resolve_path(self, path: pathlib.Path):
        if not isinstance(path, pathlib.Path):
            path = pathlib.Path(path)
        path = path.resolve()
        if not path.is_file():
            raise FileNotFoundError(f"Path {path} is not a valid path to file.")
        if not path.exists():
            raise FileNotFoundError(f"File with specified path does not exist.")
        return path

    def load_excel(self, path: pathlib.Path, sheet_name: str, index_column: str = None, columns_rename: dict =None, **kwargs) -> pd.DataFrame:
        self.logger.info("Loading file: '{}' Sheet: '{}' as DF".format(path, sheet_name))
        path = self.resolve_path(path=path)
        df = pd.read_excel(io=path, sheet_name=sheet_name, index_col=index_column, dtype=object, engine="openpyxl", **kwargs)
        df = df.where(pd.notnull(df), None)
        # Drop Unnamed columns
        # https://stackoverflow.com/a/58158544/6198445
        df.drop(df.filter(regex="Unname"),axis=1, inplace=True)
        if columns_rename is not None:
            df = df.rename(columns=columns_rename)
        return df

    def use_filter(self, df: pd.DataFrame):
        df["use"] = df["use"].astype(bool)
        df["use"] = df["use"].map(lambda x: False if x is None else x)
        df["use"] = df["use"].map(lambda x: True if x == 1 else x)
        df = df[df["use"]]
        return df

    def row_to_model(self, row, model) -> BaseNetModel:

        return model(**{k:v for k, v in row.items() if k in model.__fields__.keys() and v is not None})

    def get_hosts_from_link(self, link) -> Tuple[Host, Host]:
        a_host = self.get_host(host_name=link.a_host)
        z_host = self.get_host(host_name=link.z_host)
        return a_host, z_host

    def get_interfaces_from_link(self, link) -> Tuple[InterfaceModel, InterfaceModel]:
        a_interface = self.get_interface(host_name=link.a_host, interface_name=link.a_interface)
        z_interface = self.get_interface(host_name=link.z_host, interface_name=link.z_interface)
        return a_interface, z_interface

    def load_vlan_definitions(self):
        columns_rename = {
            "Use": "use",
            "ID": "vlan_id",
            "Name": "name"
        }
        include_keys = set(columns_rename.values()).difference(set(META_KEYS))
        vlan_definitions = {}
        df = self.load_excel(path=self.input_file, sheet_name="vlan_definitions", columns_rename=columns_rename)
        df = self.use_filter(df)
        df = df.sort_values("vlan_id")
        switches_group = self.get_group(group_name="switches")
        if switches_group.config.vlan_definitions is None:
            switches_group.config.vlan_definitions = []
        for index, row in df.iterrows():
            switches_group.config.vlan_definitions.append(
                VLANModel(**{k:v for k,v in row.items() if k in include_keys and v is not None})
            )

    def load_physical_links(self, default_lag_mode: LAG_MODE = 'active'):
        self.logger.info("Loading Physical Links")
        columns_rename = {k:k for k in ['use', 'a_host', 'a_interface', 'a_description', 'a_lag_group', 'a_lag_mode', 'z_host', 'z_interface', 'z_description', 'z_lag_group', 'z_lag_mode']}
        include_keys = set(columns_rename.values()).difference(set(META_KEYS))
        df = self.load_excel(path=self.input_file, sheet_name="physical_links", columns_rename=columns_rename)
        df = self.use_filter(df)
        for index, row in df.iterrows():
            # print(row)
            link = PhysicalLink(**{k:v for k,v in row.items() if k in include_keys and v is not None})
            # print(link)
            a_host = self.get_host(host_name=link.a_host)
            a_interface = self.get_interface(host_name=a_host.name, interface_name=link.a_interface)
            z_host = self.get_host(host_name=link.z_host)
            z_interface = self.get_interface(host_name=z_host.name, interface_name=link.z_interface)
            a_interface.neighbor = InterfaceNeighbor(host=z_host.name, interface=z_interface.name)
            z_interface.neighbor = InterfaceNeighbor(host=a_host.name, interface=a_interface.name)

            # CDP Section
            if 'cdp_enabled' in row.keys():
                if row['cdp_enabled'] is None:
                    pass
                else:
                    cdp_config = None
                    if row['cdp_enabled'] is True:
                        cdp_config = InterfaceCdpConfig(enabled=True)
                    elif row['cdp_enabled'] is False:
                        cdp_config = InterfaceCdpConfig(enabled=False)
                    for interface in [a_interface, z_interface]:
                        if interface.discovery_protocols is None:
                            interface.discovery_protocols = InterfaceDiscoveryProtocols(cdp=cdp_config.clone())


            # LAG Section
            a_lag = None
            z_lag = None
            if link.a_lag_group is not None:
                a_lag = self.get_interface(host_name=a_host.name, interface_name=f"Port-channel{link.a_lag_group}")
                lag_mode = link.a_lag_mode if link.a_lag_mode is not None else default_lag_mode
                a_interface.lag_member = InterfaceLagMemberConfig(group=link.a_lag_group, mode=lag_mode)

            if link.z_lag_group is not None:
                z_lag = self.get_interface(host_name=z_host.name, interface_name=f"Port-channel{link.z_lag_group}")
                lag_mode = link.z_lag_mode if link.z_lag_mode is not None else default_lag_mode
                z_interface.lag_member = InterfaceLagMemberConfig(group=link.z_lag_group, mode=lag_mode)

            if all([a_lag, z_lag]):
                self.logger.debug("Both sides of link are LAG-Members")
                a_lag.neighbor = InterfaceNeighbor(host=z_host.name, interface=z_lag.name)
                z_lag.neighbor = InterfaceNeighbor(host=a_host.name, interface=a_lag.name)

    def load_ospf_templates(self):
        self.logger.info("Loading OSPF Templates")
        columns_rename = {k:k for k in ['use', 'template_name', 'process_id', 'area', 'network_type', 'cost', 'priority']}
        include_keys = set(columns_rename.values()).difference(set(META_KEYS)).difference(set(['template_name']))
        df = self.load_excel(path=self.input_file, sheet_name="templates_ospf", columns_rename=columns_rename)
        df = self.use_filter(df)
        if 'ospf' not in self.templates.keys():
            self.templates['ospf'] = {}
        for index, row in df.iterrows():
            self.templates['ospf'][row['template_name']] = self.row_to_model(row=row, model=InterfaceOspfConfig)

    def load_bfd_templates(self):

        raise NotImplementedError


    def load_l3_links(self):
        self.logger.info("Loading L3 Links")
        columns_rename = {k:k for k in ['use', 'a_host', 'a_interface', 'a_description', 'a_vrf', 'a_ipv4_address', 'z_host', 'z_interface', 'z_description', 'z_vrf', 'z_ipv4_address', 'ipv4_network', 'ospf_template', 'bfd_template']}
        include_keys = set(columns_rename.values()).difference(set(META_KEYS))
        df = self.load_excel(path=self.input_file, sheet_name="l3_links", columns_rename=columns_rename)
        df = self.use_filter(df)
        for index, row in df.iterrows():
            link: L3Link = self.row_to_model(row=row, model=L3Link)
            a_host, z_host = self.get_hosts_from_link(link=link)
            a_interface, z_interface = self.get_interfaces_from_link(link=link)
            a_interface.neighbor = InterfaceNeighbor(host=z_host.name, interface=z_interface.name)
            z_interface.neighbor = InterfaceNeighbor(host=a_host.name, interface=a_interface.name)
            interface_list = [a_interface, z_interface]
            for interface in interface_list:
                if interface.l3_port is None:
                    interface.l3_port = InterfaceRouteportModel()

            GLOBAL_VRFS = [None, 'global']

            if link.a_vrf not in GLOBAL_VRFS:
                a_interface.l3_port.vrf = link.a_vrf
            if link.z_vrf not in GLOBAL_VRFS:
                z_interface.l3_port.vrf = link.z_vrf

            # Create IPv4 Container if necessary
            if all([link.a_ipv4_address, link.z_ipv4_address]) or link.ipv4_network:
                for interface in interface_list:
                    if interface.l3_port.ipv4 is None:
                        interface.l3_port.ipv4 = InterfaceIPv4Container()
            # Assign Given addresses
            if all([link.a_ipv4_address, link.z_ipv4_address]):
                a_interface.l3_port.add_ipv4_address(link.a_ipv4_address)
                z_interface.l3_port.add_ipv4_address(link.z_ipv4_address)
            elif link.ipv4_network is not None:
                usable_ips = list(link.ipv4_network.hosts())
                a_interface.l3_port.add_ipv4_address(ipaddress.IPv4Interface(f"{usable_ips[0]}/{link.ipv4_network.prefixlen}"))
                z_interface.l3_port.add_ipv4_address(ipaddress.IPv4Interface(f"{usable_ips[-1]}/{link.ipv4_network.prefixlen}"))


            if row.get('ospf_template') is not None:
                ospf_template = self.templates['ospf'].get(row.get('ospf_template'))
                a_interface.l3_port.ospf = ospf_template.clone()
                z_interface.l3_port.ospf = ospf_template.clone()
            if row.get('bfd_template') is not None:
                ospf_template = self.templates['ospf'].get(row.get('ospf_template'))
                a_interface.l3_port.bfd = InterfaceBfdConfig(template=row.get('bfd_template'))
                z_interface.l3_port.bfd = InterfaceBfdConfig(template=row.get('bfd_template'))




    def load_l3_ports(self):
        self.logger.info("Loading L3 Ports")

        df = self.load_excel(path=self.input_file, sheet_name="l3_ports")
        df = self.use_filter(df)
        for index, row in df.iterrows():
            host = self.get_host(host_name=row['host'])
            interface = self.get_interface(host_name=host.name, interface_name=row['interface'])
            if interface.l3_port is None:
                interface.l3_port = InterfaceRouteportModel()
            if interface.description is None and row['description'] is not None:
                interface.description = row['description']
            if row['vrf'] not in  GLOBAL_VRFS:
                interface.l3_port.vrf = row['vrf']
            if row['ipv4_address'] is not None:
                interface.l3_port.add_ipv4_address(address=row['ipv4_address'])


    def load_bgp_routers(self):
        self.logger.info("Loading BGP Routers")

        df = self.load_excel(path=self.input_file, sheet_name="bgp_routers")
        df = self.use_filter(df)
        for index, row in df.iterrows():
            host = self.get_host(host_name=row['host'])
            if host.config is None:
                host.config = HostConfig(interfaces={})
            if host.config.routing is None:
                host.config.routing = RoutingConfig()
            if host.config.routing.bgp is None:
                host.config.routing.bgp = RoutingBgpProcess(asn=row['asn'])

    def load_bgp_peer_groups(self) -> List[BgpPeerGroup]:
        self.logger.info("Loading BGP PeerGroups")

        df = self.load_excel(path=self.input_file, sheet_name="bgp_peer_groups")
        df = self.use_filter(df)
        peer_groups = []
        for index, row in df.iterrows():
            peer_group = BgpPeerGroup(**{k:v for k, v in row.items() if k in BgpPeerGroup.__fields__.keys() and v is not None})
            peer_groups.append(peer_group)
        return peer_groups

    def load_bgp_neighbors(self):
        self.logger.info("Loading BGP Neighbors")

        df = self.load_excel(path=self.input_file, sheet_name="bgp_neighbors")
        df = self.use_filter(df)
        peer_groups = self.load_bgp_peer_groups()
        for index, row in df.iterrows():
            host = self.get_host(host_name=row['host'])
            if host.config.routing.bgp.neighbors is None:
                host.config.routing.bgp.neighbors = []
            neighbor = BgpNeighbor(**{k:v for k, v in row.items() if k in BgpNeighbor.__fields__.keys() and v is not None})
            print(neighbor)
            # Assign peer group to host if needed
            if neighbor.peer_group is not None:
                peer_group = list(filter(lambda x: x.name == neighbor.peer_group, peer_groups))[0]
                print(peer_group)
                if host.config.routing.bgp.peer_groups is None:
                    host.config.routing.bgp.peer_groups = []
                host.config.routing.bgp.peer_groups.append(peer_group)
            # Append neighbor
            host.config.routing.bgp.neighbors.append(neighbor)



