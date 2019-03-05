#
#ptf --test-dir ptftests fast-reboot --qlen=1000 --platform remote -t 'verbose=True;dut_username="admin";dut_hostname="10.0.0.243";reboot_limit_in_seconds=30;portchannel_ports_file="/tmp/portchannel_interfaces.json";vlan_ports_file="/tmp/vlan_interfaces.json";ports_file="/tmp/ports.json";dut_mac="4c:76:25:f5:48:80";default_ip_range="192.168.0.0/16";vlan_ip_range="172.0.0.0/22";arista_vms="[\"10.0.0.200\",\"10.0.0.201\",\"10.0.0.202\",\"10.0.0.203\"]"' --platform-dir ptftests --disable-vxlan --disable-geneve --disable-erspan --disable-mpls --disable-nvgre
#
#
# This test checks that DUT is able to make FastReboot procedure
#
# This test supposes that fast-reboot/warm-reboot initiates by running /usr/bin/{fast,warm}-reboot command.
#
# The test uses "pings". The "pings" are packets which are sent through dataplane in two directions
# 1. From one of vlan interfaces to T1 device. The source ip, source interface, and destination IP are chosen randomly from valid choices. Number of packet is 100.
# 2. From all of portchannel ports to all of vlan ports. The source ip, source interface, and destination IP are chosed sequentially from valid choices.
#    Currently we have 500 distrinct destination vlan addresses. Our target to have 1000 of them.
#
# The test sequence is following:
# 1. Check that DUT is stable. That means that "pings" work in both directions: from T1 to servers and from servers to T1.
# 2. If DUT is stable the test starts continiously pinging DUT in both directions.
# 3. The test runs '/usr/bin/{fast,warm}-reboot' on DUT remotely. The ssh key supposed to be uploaded by ansible before the test
# 4. As soon as it sees that ping starts failuring in one of directions the test registers a start of dataplace disruption
# 5. As soon as the test sees that pings start working for DUT in both directions it registers a stop of dataplane disruption
# 6. If the length of the disruption is less than 30 seconds (if not redefined by parameter) - the test passes
# 7. If there're any drops, when control plane is down - the test fails
# 8. When test start reboot procedure it connects to all VM (which emulates T1) and starts fetching status of BGP and LACP
#    LACP is supposed to be down for one time only, if not - the test fails
#    if default value of BGP graceful restart timeout is less than 120 seconds the test fails
#    if BGP graceful restart is not enabled on DUT the test fails
#    If BGP graceful restart timeout value is almost exceeded (less than 15 seconds) the test fails
#    if BGP routes disappeares more then once, the test failed
#
# The test expects you're running the test with link state propagation helper.
# That helper propagate a link state from fanout switch port to corresponding VM port
#

import ptf
from ptf.base_tests import BaseTest
from ptf import config
import ptf.testutils as testutils
from ptf.testutils import *
from ptf.dataplane import match_exp_pkt
import datetime
import time
import subprocess
from ptf.mask import Mask
import socket
import ptf.packet as scapy
import thread
import threading
from multiprocessing.pool import ThreadPool, TimeoutError
import os
import signal
import random
import struct
import socket
from pprint import pprint
from fcntl import ioctl
import sys
import json
import re
from collections import defaultdict
import json
import paramiko
import Queue
import pickle
from operator import itemgetter
import scapy.all as scapyall


class Arista(object):
    DEBUG = False
    def __init__(self, ip, queue, test_params, login='admin', password='123456'):
        self.ip = ip
        self.queue = queue
        self.login = login
        self.password = password
        self.conn = None
        self.hostname = None
        self.v4_routes = [test_params['vlan_ip_range'], test_params['lo_prefix']]
        self.v6_routes = [test_params['lo_v6_prefix']]
        self.fails = set()
        self.info = set()
        self.min_bgp_gr_timeout = int(test_params['min_bgp_gr_timeout'])

    def __del__(self):
        self.disconnect()

    def connect(self):
        self.conn = paramiko.SSHClient()
        self.conn.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.conn.connect(self.ip, username=self.login, password=self.password, allow_agent=False, look_for_keys=False)
        self.shell = self.conn.invoke_shell()

        first_prompt = self.do_cmd(None, prompt = '>')
        self.hostname = self.extract_hostname(first_prompt)

        self.do_cmd('enable')
        self.do_cmd('terminal length 0')

        return self.shell

    def extract_hostname(self, first_prompt):
        lines = first_prompt.split('\n')
        prompt = lines[-1]
        return prompt.strip().replace('>', '#')

    def do_cmd(self, cmd, prompt = None):
        if prompt == None:
            prompt = self.hostname

        if cmd is not None:
            self.shell.send(cmd + '\n')

        input_buffer = ''
        while prompt not in input_buffer:
            input_buffer += self.shell.recv(16384)

        return input_buffer

    def disconnect(self):
        if self.conn is not None:
            self.conn.close()
            self.conn = None

        return

    def run(self):
        data = {}
        debug_data = {}
        run_once = False
        log_first_line = None
        quit_enabled = False
        v4_routing_ok = False
        v6_routing_ok = False
        routing_works = True
        self.connect()

        cur_time = time.time()
        sample = {}
        samples = {}
        portchannel_output = self.do_cmd("show interfaces po1 | json")
        portchannel_output = "\n".join(portchannel_output.split("\r\n")[1:-1])
        sample["po_changetime"] = json.loads(portchannel_output, strict=False)['interfaces']['Port-Channel1']['lastStatusChangeTimestamp']
        samples[cur_time] = sample

        while not (quit_enabled and v4_routing_ok and v6_routing_ok):
            cmd = self.queue.get()
            if cmd == 'quit':
                quit_enabled = True
                continue
            cur_time = time.time()
            info = {}
            debug_info = {}
            lacp_output = self.do_cmd('show lacp neighbor')
            info['lacp'] = self.parse_lacp(lacp_output)
            bgp_neig_output = self.do_cmd('show ip bgp neighbors')
            info['bgp_neig'] = self.parse_bgp_neighbor(bgp_neig_output)

            bgp_route_v4_output = self.do_cmd('show ip route bgp | json')
            v4_routing_ok = self.parse_bgp_route(bgp_route_v4_output, self.v4_routes)
            info['bgp_route_v4'] = v4_routing_ok

            bgp_route_v6_output = self.do_cmd("show ipv6 route bgp | json")
            v6_routing_ok = self.parse_bgp_route(bgp_route_v6_output, self.v6_routes)
            info["bgp_route_v6"] = v6_routing_ok

            portchannel_output = self.do_cmd("show interfaces po1 | json")
            portchannel_output = "\n".join(portchannel_output.split("\r\n")[1:-1])
            sample["po_changetime"] = json.loads(portchannel_output, strict=False)['interfaces']['Port-Channel1']['lastStatusChangeTimestamp']

            if not run_once:
                self.ipv4_gr_enabled, self.ipv6_gr_enabled, self.gr_timeout = self.parse_bgp_neighbor_once(bgp_neig_output)
                if self.gr_timeout is not None:
                    log_first_line = "session_begins_%f" % cur_time
                    self.do_cmd("send log message %s" % log_first_line)
                    run_once = True

            data[cur_time] = info
            samples[cur_time] = sample
            if self.DEBUG:
                debug_data[cur_time] = {
                    'show lacp neighbor' : lacp_output,
                    'show ip bgp neighbors' : bgp_neig_output,
                    'show ip route bgp' : bgp_route_v4_output,
                    'show ipv6 route bgp' : bgp_route_v6_output,
                }

        attempts = 60
        for _ in range(attempts):
            log_output = self.do_cmd("show log | begin %s" % log_first_line)
            log_lines = log_output.split("\r\n")[1:-1]
            log_data = self.parse_logs(log_lines)
            if len(log_data) != 0:
                break
            time.sleep(1) # wait until logs are populated

        if len(log_data) == 0:
            log_data['error'] = 'Incomplete output'

        self.disconnect()

        # save data for troubleshooting
        with open("/tmp/%s.data.pickle" % self.ip, "w") as fp:
            pickle.dump(data, fp)

        # save debug data for troubleshooting
        if self.DEBUG:
            with open("/tmp/%s.raw.pickle" % self.ip, "w") as fp:
                pickle.dump(debug_data, fp)
            with open("/tmp/%s.logging" % self.ip, "w") as fp:
                fp.write("\n".join(log_lines))

        self.check_gr_peer_status(data)
        cli_data = {}
        cli_data['lacp']   = self.check_series_status(data, "lacp",         "LACP session")
        cli_data['bgp_v4'] = self.check_series_status(data, "bgp_route_v4", "BGP v4 routes")
        cli_data['bgp_v6'] = self.check_series_status(data, "bgp_route_v6", "BGP v6 routes")
        cli_data['po']     = self.check_change_time(samples, "po_changetime", "PortChannel interface")

        route_timeout             = log_data['route_timeout']
        cli_data['route_timeout'] = route_timeout

        # {'10.0.0.38': [(0, '4200065100)')], 'fc00::2d': [(0, '4200065100)')]}
        for nei in route_timeout.keys():
            asn = route_timeout[nei][0][-1]
            msg = 'BGP route GR timeout: neighbor %s (ASN %s' % (nei, asn)
            self.fails.add(msg)

        return self.fails, self.info, cli_data, log_data

    def extract_from_logs(self, regexp, data):
        raw_data = []
        result = defaultdict(list)
        initial_time = -1
        re_compiled = re.compile(regexp)
        for line in data:
            m = re_compiled.match(line)
            if not m:
                continue
            raw_data.append((datetime.datetime.strptime(m.group(1), "%b %d %X"), m.group(2), m.group(3)))

        if len(raw_data) > 0:
            initial_time = raw_data[0][0]
            for when, what, status in raw_data:
                offset = (when - initial_time if when > initial_time else initial_time - when).seconds
                result[what].append((offset, status))

        return result, initial_time

    def parse_logs(self, data):
        result = {}
        bgp_r = r'^(\S+\s+\d+\s+\S+) \S+ Rib: %BGP-5-ADJCHANGE: peer (\S+) .+ (\S+)$'
        result_bgp, initial_time_bgp = self.extract_from_logs(bgp_r, data)
        if_r = r'^(\S+\s+\d+\s+\S+) \S+ Ebra: %LINEPROTO-5-UPDOWN: Line protocol on Interface (\S+), changed state to (\S+)$'
        result_if, initial_time_if = self.extract_from_logs(if_r, data)

        route_r = r'^(\S+\s+\d+\s+\S+) \S+ Rib: %BGP-5-BGP_GRACEFUL_RESTART_TIMEOUT: Deleting stale routes from peer (\S+) .+ (\S+)$'
        result_rt, initial_time_rt = self.extract_from_logs(route_r, data)

        result['route_timeout'] = result_rt

        if initial_time_bgp == -1 or initial_time_if == -1:
            return result

        for events in result_bgp.values():
            if events[-1][1] != 'Established':
                return result

        # first state is Idle, last state is Established
        for events in result_bgp.values():
            if len(events) > 1:
                assert(events[0][1] != 'Established')

            assert(events[-1][1] == 'Established')

        # first state is down, last state is up
        for events in result_if.values():
            assert(events[0][1] == 'down')
            assert(events[-1][1] == 'up')

        po_name = [ifname for ifname in result_if.keys() if 'Port-Channel' in ifname][0]
        neigh_ipv4 = [neig_ip for neig_ip in result_bgp.keys() if '.' in neig_ip][0]

        result['PortChannel was down (seconds)'] = result_if[po_name][-1][0] - result_if[po_name][0][0]
        for if_name in sorted(result_if.keys()):
            result['Interface %s was down (times)' % if_name] = map(itemgetter(1), result_if[if_name]).count("down")

        for neig_ip in result_bgp.keys():
            key = "BGP IPv6 was down (seconds)" if ':' in neig_ip else "BGP IPv4 was down (seconds)"
            result[key] = result_bgp[neig_ip][-1][0] - result_bgp[neig_ip][0][0]

        for neig_ip in result_bgp.keys():
            key = "BGP IPv6 was down (times)" if ':' in neig_ip else "BGP IPv4 was down (times)"
            result[key] = map(itemgetter(1), result_bgp[neig_ip]).count("Idle")

        bgp_po_offset = (initial_time_if - initial_time_bgp if initial_time_if > initial_time_bgp else initial_time_bgp - initial_time_if).seconds
        result['PortChannel went down after bgp session was down (seconds)'] = bgp_po_offset + result_if[po_name][0][0]

        for neig_ip in result_bgp.keys():
            key = "BGP IPv6 was gotten up after Po was up (seconds)" if ':' in neig_ip else "BGP IPv4 was gotten up after Po was up (seconds)"
            result[key] = result_bgp[neig_ip][-1][0] - bgp_po_offset - result_if[po_name][-1][0]

        return result

    def parse_lacp(self, output):
        return output.find('Bundled') != -1

    def parse_bgp_neighbor_once(self, output):
        is_gr_ipv4_enabled = False
        is_gr_ipv6_enabled = False
        restart_time = None
        for line in output.split('\n'):
            if '     Restart-time is' in line:
                restart_time = int(line.replace('       Restart-time is ', ''))
                continue

            if 'is enabled, Forwarding State is' in line:
                if 'IPv6' in line:
                    is_gr_ipv6_enabled = True
                elif 'IPv4' in line:
                    is_gr_ipv4_enabled = True

        return is_gr_ipv4_enabled, is_gr_ipv6_enabled, restart_time

    def parse_bgp_neighbor(self, output):
        gr_active = None
        gr_timer = None
        for line in output.split('\n'):
            if 'Restart timer is' in line:
                gr_active = 'is active' in line
                gr_timer = str(line[-9:-1])

        return gr_active, gr_timer

    def parse_bgp_route(self, output, expects):
        prefixes = set()
        data = "\n".join(output.split("\r\n")[1:-1])
        obj = json.loads(data)

        if "vrfs" in obj and "default" in obj["vrfs"]:
            obj = obj["vrfs"]["default"]
        for prefix, attrs in obj["routes"].items():
            if "routeAction" not in attrs or attrs["routeAction"] != "forward":
                continue
            if all("Port-Channel" in via["interface"] for via in attrs["vias"]):
                prefixes.add(prefix)

        return set(expects) == prefixes

    def check_gr_peer_status(self, output):
        # [0] True 'ipv4_gr_enabled', [1] doesn't matter 'ipv6_enabled', [2] should be >= 120
        if not self.ipv4_gr_enabled:
            self.fails.add("bgp ipv4 graceful restart is not enabled")
        if not self.ipv6_gr_enabled:
            pass # ToDo:
        if self.gr_timeout < 120: # bgp graceful restart timeout less then 120 seconds
            self.fails.add("bgp graceful restart timeout is less then 120 seconds")

        for when, other in sorted(output.items(), key = lambda x : x[0]):
            gr_active, timer = other['bgp_neig']
            # wnen it's False, it's ok, wnen it's True, check that inactivity timer not less then self.min_bgp_gr_timeout seconds
            if gr_active and datetime.datetime.strptime(timer, '%H:%M:%S') < datetime.datetime(1900, 1, 1, second = self.min_bgp_gr_timeout):
                self.fails.add("graceful restart timer is almost finished. Less then %d seconds left" % self.min_bgp_gr_timeout)

    def check_series_status(self, output, entity, what):
        # find how long anything was down
        # Input parameter is a dictionary when:status
        # constraints:
        # entity must be down just once
        # entity must be up when the test starts
        # entity must be up when the test stops

        sorted_keys = sorted(output.keys())
        if not output[sorted_keys[0]][entity]:
            self.fails.add("%s must be up when the test starts" % what)
            return 0, 0
        if not output[sorted_keys[-1]][entity]:
            self.fails.add("%s must be up when the test stops" % what)
            return 0, 0

        start = sorted_keys[0]
        cur_state = True
        res = defaultdict(list)
        for when in sorted_keys[1:]:
            if cur_state != output[when][entity]:
                res[cur_state].append(when - start)
                start = when
                cur_state = output[when][entity]
        res[cur_state].append(when - start)

        is_down_count = len(res[False])

        if is_down_count > 1:
            self.info.add("%s must be down just for once" % what)

        return is_down_count, sum(res[False]) # summary_downtime

    def check_change_time(self, output, entity, what):
        # find last changing time updated, if no update, the entity is never changed
        # Input parameter is a dictionary when:last_changing_time
        # constraints:
        # the dictionary `output` cannot be empty
        sorted_keys = sorted(output.keys())
        if not output:
            self.fails.add("%s cannot be empty" % what)
            return 0, 0

        start = sorted_keys[0]
        prev_time = output[start]
        change_count = 0
        for when in sorted_keys[1:]:
            if prev_time != output[when][entity]:
                prev_time = output[when][entity]
                change_count += 1

        if change_count > 0:
            self.info.add("%s state changed %d times" % (what, change_count))

        # Note: the first item is a placeholder
        return 0, change_count


class StateMachine():
    def __init__(self, init_state='init'):
        self.state_lock = threading.RLock()
        self.state_time = {} # Recording last time when entering a state
        self.state      = None
        self.flooding   = False
        self.set(init_state)


    def set(self, state):
        with self.state_lock:
            self.state             = state
            self.state_time[state] = datetime.datetime.now()


    def get(self):
        with self.state_lock:
            cur_state = self.state
        return cur_state


    def get_state_time(self, state):
        with self.state_lock:
            time = self.state_time[state]
        return time


    def set_flooding(self, flooding):
        with self.state_lock:
            self.flooding = flooding


    def is_flooding(self):
        with self.state_lock:
            flooding = self.flooding

        return flooding


class ReloadTest(BaseTest):
    TIMEOUT = 0.5
    def __init__(self):
        BaseTest.__init__(self)
        self.fails = {}
        self.info = {}
        self.cli_info = {}
        self.logs_info = {}
        self.log_lock = threading.RLock()
        self.test_params = testutils.test_params_get()
        self.check_param('verbose', False,   required = False)
        self.check_param('dut_username', '', required = True)
        self.check_param('dut_hostname', '', required = True)
        self.check_param('reboot_limit_in_seconds', 30, required = False)
        self.check_param('reboot_type', 'fast-reboot', required = False)
        self.check_param('graceful_limit', 180, required = False)
        self.check_param('portchannel_ports_file', '', required = True)
        self.check_param('vlan_ports_file', '', required = True)
        self.check_param('ports_file', '', required = True)
        self.check_param('dut_mac', '', required = True)
        self.check_param('dut_vlan_ip', '', required = True)
        self.check_param('default_ip_range', '', required = True)
        self.check_param('vlan_ip_range', '', required = True)
        self.check_param('lo_prefix', '10.1.0.32/32', required = False)
        self.check_param('lo_v6_prefix', 'fc00:1::/64', required = False)
        self.check_param('arista_vms', [], required = True)
        self.check_param('min_bgp_gr_timeout', 15, required = False)
        self.check_param('warm_up_timeout_secs', 180, required = False)
        self.check_param('dut_stabilize_secs', 20, required = False)
        self.check_param('docker_name', '', required = False)

        self.docker_name = ''
        self.log_file_name = '/tmp/%s.log' % self.test_params['reboot_type']
        self.log_fp = open(self.log_file_name, 'w')

        # Default settings
        self.ping_dut_pkts = 10
        self.arp_ping_pkts = 1
        self.nr_pc_pkts = 100
        self.nr_tests = 3
        self.reboot_delay = 10
        self.task_timeout = 300   # Wait up to 5 minutes for tasks to complete
        self.max_nr_vl_pkts = 500 # FIXME: should be 1000.
                                  # But ptf is not fast enough + swss is slow for FDB and ARP entries insertions
        self.timeout_thr = None

        self.time_to_listen = 180.0     # Listen for more then 180 seconds, to be used in sniff_in_background method.
        #   Inter-packet interval, to be used in send_in_background method.
        #   Improve this interval to gain more precision of disruptions.
        self.send_interval = 0.0035
        self.packets_to_send = min(int(self.time_to_listen / (self.send_interval + 0.0015)), 45000) # How many packets to be sent in send_in_background method

        # State watcher attributes
        self.watching            = False
        self.cpu_state           = StateMachine('init')
        self.asic_state          = StateMachine('init')
        self.vlan_state          = StateMachine('init')
        self.vlan_lock           = threading.RLock()
        self.asic_state_time     = {} # Recording last asic state entering time
        self.asic_vlan_reach     = [] # Recording asic vlan reachability
        self.recording           = False # Knob for recording asic_vlan_reach
        # light_probe:
        #    True : when one direction probe fails, don't probe another.
        #    False: when one direction probe fails, continue probe another.
        self.light_probe         = False

        return

    def read_json(self, name):
        with open(self.test_params[name]) as fp:
          content = json.load(fp)

        return content

    def read_port_indices(self):
        self.port_indices = self.read_json('ports_file')

        return

    def read_portchannel_ports(self):
        content = self.read_json('portchannel_ports_file')
        pc_ifaces = []
        for pc in content.values():
            pc_ifaces.extend([self.port_indices[member] for member in pc['members']])

        return pc_ifaces

    def read_vlan_ports(self):
        content = self.read_json('vlan_ports_file')
        if len(content) > 1:
            raise Exception("Too many vlans")
        return [self.port_indices[ifname] for ifname in content.values()[0]['members']]

    def check_param(self, param, default, required = False):
        if param not in self.test_params:
            if required:
                raise Exception("Test parameter '%s' is required" % param)
            self.test_params[param] = default

    def random_ip(self, ip):
        net_addr, mask = ip.split('/')
        n_hosts = 2**(32 - int(mask))
        random_host = random.randint(2, n_hosts - 2)
        return self.host_ip(ip, random_host)

    def host_ip(self, net_ip, host_number):
        src_addr, mask = net_ip.split('/')
        n_hosts = 2**(32 - int(mask))
        if host_number > (n_hosts - 2):
            raise Exception("host number %d is greater than number of hosts %d in the network %s" % (host_number, n_hosts - 2, net_ip))
        src_addr_n = struct.unpack(">I", socket.inet_aton(src_addr))[0]
        net_addr_n = src_addr_n & (2**32 - n_hosts)
        host_addr_n = net_addr_n + host_number
        host_ip = socket.inet_ntoa(struct.pack(">I", host_addr_n))

        return host_ip

    def random_port(self, ports):
        return random.choice(ports)

    def log(self, message, verbose=False):
        current_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self.log_lock:
            if verbose and self.test_params['verbose'] or not verbose:
                print "%s : %s" % (current_time, message)
            self.log_fp.write("%s : %s\n" % (current_time, message))

    def timeout(self, seconds, message):
        def timeout_exception(self, message):
            self.log('Timeout is reached: %s' % message)
            self.tearDown()
            os.kill(os.getpid(), signal.SIGINT)

        if self.timeout_thr is None:
            self.timeout_thr = threading.Timer(seconds, timeout_exception, args=(self, message))
            self.timeout_thr.start()
        else:
            raise Exception("Timeout already set")

    def cancel_timeout(self):
        if self.timeout_thr is not None:
            self.timeout_thr.cancel()
            self.timeout_thr = None

    def setUp(self):
        self.read_port_indices()
        self.portchannel_ports = self.read_portchannel_ports()
        vlan_ip_range = self.test_params['vlan_ip_range']
        self.vlan_ports = self.read_vlan_ports()

        self.docker_name = self.test_params['docker_name']

        self.limit = datetime.timedelta(seconds=self.test_params['reboot_limit_in_seconds'])
        self.reboot_type = self.test_params['reboot_type']
        if self.reboot_type not in ['fast-reboot', 'warm-reboot']:
            raise ValueError('Not supported reboot_type %s' % self.reboot_type)
        self.dut_ssh = self.test_params['dut_username'] + '@' + self.test_params['dut_hostname']
        self.dut_mac = self.test_params['dut_mac']
        #
        self.generate_from_t1()
        self.generate_from_vlan()
        self.generate_ping_dut_lo()
        self.generate_arp_ping_packet()

        self.log("Test params:")
        self.log("DUT ssh: %s" % self.dut_ssh)
        self.log("DUT reboot limit in seconds: %s" % self.limit)
        self.log("DUT mac address: %s" % self.dut_mac)

        self.log("From server src addr: %s" % self.from_server_src_addr)
        self.log("From server src port: %s" % self.from_server_src_port)
        self.log("From server dst addr: %s" % self.from_server_dst_addr)
        self.log("From server dst ports: %s" % self.from_server_dst_ports)
        self.log("From upper layer number of packets: %d" % self.nr_vl_pkts)
        self.log("VMs: %s" % str(self.test_params['arista_vms']))

        self.log("docker_name: %s" % self.docker_name)

        self.log("Reboot type is %s" % self.reboot_type)

        if self.reboot_type == 'warm-reboot':
            # Pre-generate list of packets to be sent in send_in_background method.
            generate_start = datetime.datetime.now()
            self.generate_bidirectional()
            self.log("%d packets are ready after: %s" % (len(self.packets_list), str(datetime.datetime.now() - generate_start)))

        self.dataplane = ptf.dataplane_instance
        for p in self.dataplane.ports.values():
            port = p.get_packet_source()
            port.socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1000000)

        self.dataplane.flush()
        if config["log_dir"] != None:
            filename = os.path.join(config["log_dir"], str(self)) + ".pcap"
            self.dataplane.start_pcap(filename)

        self.log("Enabling arp_responder")
        self.cmd(["supervisorctl", "restart", "arp_responder"])

        return

    def tearDown(self):
        self.log("Disabling arp_responder")
        self.cmd(["supervisorctl", "stop", "arp_responder"])

        # Stop watching DUT
        self.watching = False

        if config["log_dir"] != None:
            self.dataplane.stop_pcap()
        self.log_fp.close()

    def get_if(self, iff, cmd):
        s = socket.socket()
        ifreq = ioctl(s, cmd, struct.pack("16s16x",iff))
        s.close()

        return ifreq

    def get_mac(self, iff):
        SIOCGIFHWADDR = 0x8927          # Get hardware address
        return ':'.join(['%02x' % ord(char) for char in self.get_if(iff, SIOCGIFHWADDR)[18:24]])

    def generate_from_t1(self):
        self.from_t1 = []

        vlan_ip_range = self.test_params['vlan_ip_range']

        _, mask = vlan_ip_range.split('/')
        n_hosts = min(2**(32 - int(mask)) - 3, self.max_nr_vl_pkts)

        dump = defaultdict(dict)
        counter = 0
        for i in xrange(2, n_hosts + 2):
            from_t1_src_addr = self.random_ip(self.test_params['default_ip_range'])
            from_t1_src_port = self.random_port(self.portchannel_ports)
            from_t1_dst_addr = self.host_ip(vlan_ip_range, i)
            from_t1_dst_port = self.vlan_ports[i % len(self.vlan_ports)]
            from_t1_if_name = "eth%d" % from_t1_dst_port
            from_t1_if_addr = "%s/%s" % (from_t1_dst_addr, vlan_ip_range.split('/')[1])
            vlan_mac_hex = '72060001%04x' % counter
            lag_mac_hex = '5c010203%04x' % counter
            mac_addr = ':'.join(lag_mac_hex[i:i+2] for i in range(0, len(lag_mac_hex), 2))
            packet = simple_tcp_packet(
                      eth_src=mac_addr,
                      eth_dst=self.dut_mac,
                      ip_src=from_t1_src_addr,
                      ip_dst=from_t1_dst_addr,
                      ip_ttl=255,
                      tcp_dport=5000
            )
            self.from_t1.append((from_t1_src_port, str(packet)))
            dump[from_t1_if_name][from_t1_dst_addr] = vlan_mac_hex
            counter += 1

        exp_packet = simple_tcp_packet(
                      ip_src="0.0.0.0",
                      ip_dst="0.0.0.0",
                      tcp_dport=5000,
        )

        self.from_t1_exp_packet = Mask(exp_packet)
        self.from_t1_exp_packet.set_do_not_care_scapy(scapy.Ether, "src")
        self.from_t1_exp_packet.set_do_not_care_scapy(scapy.Ether, "dst")
        self.from_t1_exp_packet.set_do_not_care_scapy(scapy.IP, "src")
        self.from_t1_exp_packet.set_do_not_care_scapy(scapy.IP, "dst")
        self.from_t1_exp_packet.set_do_not_care_scapy(scapy.IP, "chksum")
        self.from_t1_exp_packet.set_do_not_care_scapy(scapy.TCP, "chksum")
        self.from_t1_exp_packet.set_do_not_care_scapy(scapy.IP, "ttl")

        # save data for arp_replay process
        with open("/tmp/from_t1.json", "w") as fp:
            json.dump(dump, fp)

        random_vlan_iface = random.choice(dump.keys())
        self.from_server_src_port = int(random_vlan_iface.replace('eth',''))
        self.from_server_src_addr = random.choice(dump[random_vlan_iface].keys())
        self.from_server_dst_addr = self.random_ip(self.test_params['default_ip_range'])
        self.from_server_dst_ports = self.portchannel_ports

        self.nr_vl_pkts = n_hosts

        return

    def generate_from_vlan(self):
        packet = simple_tcp_packet(
                      eth_dst=self.dut_mac,
                      ip_src=self.from_server_src_addr,
                      ip_dst=self.from_server_dst_addr,
                      tcp_dport=5000
                 )
        exp_packet = simple_tcp_packet(
                      ip_src=self.from_server_src_addr,
                      ip_dst=self.from_server_dst_addr,
                      ip_ttl=63,
                      tcp_dport=5000,
                     )

        self.from_vlan_exp_packet = Mask(exp_packet)
        self.from_vlan_exp_packet.set_do_not_care_scapy(scapy.Ether,"src")
        self.from_vlan_exp_packet.set_do_not_care_scapy(scapy.Ether,"dst")

        self.from_vlan_packet = str(packet)

        return

    def generate_ping_dut_lo(self):
        dut_lo_ipv4 = self.test_params['lo_prefix'].split('/')[0]
        packet = simple_icmp_packet(eth_dst=self.dut_mac,
                                    ip_src=self.from_server_src_addr,
                                    ip_dst=dut_lo_ipv4)

        exp_packet = simple_icmp_packet(eth_src=self.dut_mac,
                                        ip_src=dut_lo_ipv4,
                                        ip_dst=self.from_server_src_addr,
                                        icmp_type='echo-reply')


        self.ping_dut_exp_packet  = Mask(exp_packet)
        self.ping_dut_exp_packet.set_do_not_care_scapy(scapy.Ether, "dst")
        self.ping_dut_exp_packet.set_do_not_care_scapy(scapy.IP, "id")
        self.ping_dut_exp_packet.set_do_not_care_scapy(scapy.IP, "chksum")

        self.ping_dut_packet = str(packet)

    def generate_arp_ping_packet(self):
        vlan_ip_range = self.test_params['vlan_ip_range']

        vlan_port_canadiates = range(len(self.vlan_ports))
        vlan_port_canadiates.remove(0) # subnet prefix
        vlan_port_canadiates.remove(1) # subnet IP on dut
        src_idx  = random.choice(vlan_port_canadiates)
        vlan_port_canadiates.remove(src_idx)
        dst_idx  = random.choice(vlan_port_canadiates)
        src_port = self.vlan_ports[src_idx]
        dst_port = self.vlan_ports[dst_idx]
        src_mac  = self.get_mac('eth%d' % src_port)
        src_addr = self.host_ip(vlan_ip_range, src_idx)
        dst_addr = self.host_ip(vlan_ip_range, dst_idx)
        packet   = simple_arp_packet(eth_src=src_mac, arp_op=1, ip_snd=src_addr, ip_tgt=dst_addr, hw_snd=src_mac)
        expect   = simple_arp_packet(eth_dst=src_mac, arp_op=2, ip_snd=dst_addr, ip_tgt=src_addr, hw_tgt=src_mac)
        self.log("ARP ping: src idx %d port %d mac %s addr %s" % (src_idx, src_port, src_mac, src_addr))
        self.log("ARP ping: dst idx %d port %d addr %s" % (dst_idx, dst_port, dst_addr))
        self.arp_ping = str(packet)
        self.arp_resp = Mask(expect)
        self.arp_resp.set_do_not_care_scapy(scapy.Ether, 'src')
        self.arp_resp.set_do_not_care_scapy(scapy.ARP,   'hwtype')
        self.arp_resp.set_do_not_care_scapy(scapy.ARP,   'hwsrc')
        self.arp_src_port = src_port

    def generate_bidirectional(self, packets_to_send = None):
        """
        This method is used to pre-generate packets to be sent in background thread.
        Packets are composed into a list, and present a bidirectional flow as next:
        five packet from T1, one packet from vlan.
        Each packet has sequential UDP Payload - to be identified later.
        """
        if packets_to_send:
            self.packets_to_send = packets_to_send
            self.send_interval = self.time_to_listen / self.packets_to_send
        else:
            packets_to_send = self.packets_to_send
        vlan_ip_range = self.test_params['vlan_ip_range']
        _, mask = vlan_ip_range.split('/')
        n_hosts = min(2**(32 - int(mask)) - 3, self.max_nr_vl_pkts)
        counter = 0
        self.packets_list = list()
        for i in xrange(packets_to_send):
            payload = '0' * 60 + str(i)
            if (i % 5) == 0 :   # From vlan to T1.
                packet = simple_udp_packet(
                    eth_dst = self.dut_mac,
                    ip_src = self.from_server_src_addr,
                    ip_dst = self.from_server_dst_addr,
                    udp_sport = 1234,
                    udp_dport = 5000,
                    udp_payload = payload)
                from_port = self.from_server_src_port
            else:   # From T1 to vlan.
                from_t1_src_addr = self.random_ip(self.test_params['default_ip_range'])
                from_t1_src_port = self.random_port(self.portchannel_ports)
                from_t1_dst_addr = self.host_ip(vlan_ip_range, (counter%(n_hosts-2))+2)
                lag_mac_hex = '5c010203%04x' % counter
                mac_addr = ':'.join(lag_mac_hex[i:i+2] for i in range(0, len(lag_mac_hex), 2))
                counter += 1
                packet = simple_udp_packet(
                        eth_src = mac_addr,
                        eth_dst = self.dut_mac,
                        ip_src = from_t1_src_addr,
                        ip_dst = from_t1_dst_addr,
                        ip_ttl = 255,
                        udp_dport = 5000,
                        udp_payload = payload)
                from_port = from_t1_src_port
            self.packets_list.append((from_port, str(packet)))

    def runTest(self):
        self.reboot_start = None
        no_routing_start = None
        no_routing_stop = None
        no_cp_replies = None

        arista_vms = self.test_params['arista_vms'][1:-1].split(",")
        ssh_targets = []
        for vm in arista_vms:
            if (vm.startswith("'") or vm.startswith('"')) and (vm.endswith("'") or vm.endswith('"')):
                ssh_targets.append(vm[1:-1])
            else:
                ssh_targets.append(vm)

        self.log("Converted addresses VMs: %s" % str(ssh_targets))

        self.ssh_jobs = []
        for addr in ssh_targets:
            q = Queue.Queue()
            thr = threading.Thread(target=self.peer_state_check, kwargs={'ip': addr, 'queue': q})
            thr.setDaemon(True)
            self.ssh_jobs.append((thr, q))
            thr.start()

        thr = threading.Thread(target=self.reboot_dut)
        thr.setDaemon(True)

        try:
            self.fails['dut'] = set()

            pool = ThreadPool(processes=3)
            self.log("Starting reachability state watch thread...")
            self.watching    = True
            self.light_probe = False
            watcher = pool.apply_async(self.reachability_watcher)
            self.watcher_is_stopped = threading.Event() # Waiter Event for the Watcher state is stopped.
            self.watcher_is_running = threading.Event() # Waiter Event for the Watcher state is running.
            self.watcher_is_stopped.set()               # By default the Watcher is not running.
            self.watcher_is_running.clear()             # By default its required to wait for the Watcher started.
            # Give watch thread some time to wind up
            #time.sleep(5)

            self.log("Check that device is alive and pinging")
            time.sleep(5)
            self.fails['dut'].add('DUT is not ready for test')
            self.assertTrue(self.wait_dut_to_warm_up(), 'DUT is not stable')
            self.fails['dut'].clear()

            self.log("Schedule to reboot the remote switch in %s sec" % self.reboot_delay)
            thr.start()

            if not self.docker_name:
                self.log("Wait until Control plane is down")
                self.timeout(self.task_timeout, "DUT hasn't shutdown in %d seconds" % self.task_timeout)
                self.wait_until_cpu_port_down()
                self.cancel_timeout()

            if self.reboot_type == 'fast-reboot':
                self.light_probe = True

            self.reboot_start = datetime.datetime.now()
            self.log("Dut reboots: reboot start %s" % str(self.reboot_start))

            if self.reboot_type == 'fast-reboot':
                self.log("Check that device is still forwarding data plane traffic")
                self.fails['dut'].add('Data plane has a forwarding problem')
                self.assertTrue(self.check_alive(), 'DUT is not stable')
                self.fails['dut'].clear()

                self.log("Wait until control plane up")
                async_cpu_up = pool.apply_async(self.wait_until_cpu_port_up)

                self.log("Wait until data plane stops")
                async_forward_stop = pool.apply_async(self.check_forwarding_stop)

                try:
                    async_cpu_up.get(timeout=self.task_timeout)
                except TimeoutError as e:
                    self.log("DUT hasn't bootup in %d seconds" % self.task_timeout)
                    self.fails['dut'].add("DUT hasn't booted up in %d seconds" % self.task_timeout)
                    raise

                try:
                    no_routing_start, upper_replies = async_forward_stop.get(timeout=self.task_timeout)
                    self.log("Data plane was stopped, Waiting until it's up. Stop time: %s" % str(no_routing_start))
                except TimeoutError:
                    self.log("Data plane never stop")
                    no_routing_start = datetime.datetime.min

                if no_routing_start is not None:
                    self.timeout(self.task_timeout, "DUT hasn't started to work for %d seconds" % self.task_timeout)
                    no_routing_stop, _ = self.check_forwarding_resume()
                    self.cancel_timeout()
                else:
                    no_routing_stop = datetime.datetime.min

                # Stop watching DUT
                self.watching = False

            if self.reboot_type == 'warm-reboot':
                # Stop watching DUT
                self.watching = False
                self.log("Stopping reachability state watch thread.")
                self.watcher_is_stopped.wait(timeout = 10)  # Wait for the Watcher stopped.
                self.send_and_sniff()

                examine_start = datetime.datetime.now()
                self.log("Packet flow examine started %s after the reboot" % str(examine_start - self.reboot_start))
                self.examine_flow()
                self.log("Packet flow examine finished after %s" % str(datetime.datetime.now() - examine_start))

                if self.lost_packets:
                    no_routing_stop, no_routing_start = datetime.datetime.fromtimestamp(self.no_routing_stop), datetime.datetime.fromtimestamp(self.no_routing_start)
                    self.log("The longest disruption lasted %.3f seconds. %d packet(s) lost." % (self.max_disrupt_time, self.max_lost_id))
                    self.log("Total disruptions count is %d. All disruptions lasted %.3f seconds. Total %d packet(s) lost" % \
                        (self.disrupts_count, self.total_disrupt_time, self.total_disrupt_packets))
                else:
                    no_routing_start = self.reboot_start
                    no_routing_stop  = self.reboot_start

            # wait until all bgp session are established
            self.log("Wait until bgp routing is up on all devices")
            for _, q in self.ssh_jobs:
                q.put('quit')

            self.timeout(self.task_timeout, "SSH threads haven't finished for %d seconds" % self.task_timeout)
            while any(thr.is_alive() for thr, _ in self.ssh_jobs):
                for _, q in self.ssh_jobs:
                    q.put('go')
                time.sleep(self.TIMEOUT)

            for thr, _ in self.ssh_jobs:
                thr.join()
            self.cancel_timeout()

            self.log("Data plane works again. Start time: %s" % str(no_routing_stop))
            self.log("")

            if self.reboot_type == 'fast-reboot':
                no_cp_replies = self.extract_no_cpu_replies(upper_replies)

            if no_routing_stop - no_routing_start > self.limit:
                self.fails['dut'].add("Downtime must be less then %s seconds. It was %s" \
                        % (self.test_params['reboot_limit_in_seconds'], str(no_routing_stop - no_routing_start)))
            if no_routing_stop - self.reboot_start > datetime.timedelta(seconds=self.test_params['graceful_limit']):
                self.fails['dut'].add("%s cycle must be less than graceful limit %s seconds" % (self.reboot_type, self.test_params['graceful_limit']))
            if self.reboot_type == 'fast-reboot' and no_cp_replies < 0.95 * self.nr_vl_pkts:
                self.fails['dut'].add("Dataplane didn't route to all servers, when control-plane was down: %d vs %d" % (no_cp_replies, self.nr_vl_pkts))

        finally:
            # Stop watching DUT
            self.watching = False

            # Generating report
            self.log("="*50)
            self.log("Report:")
            self.log("="*50)

            self.log("LACP/BGP were down for (extracted from cli):")
            self.log("-"*50)
            for ip in sorted(self.cli_info.keys()):
                self.log("    %s - lacp: %7.3f (%d) po_events: (%d) bgp v4: %7.3f (%d) bgp v6: %7.3f (%d)" \
                         % (ip, self.cli_info[ip]['lacp'][1],   self.cli_info[ip]['lacp'][0], \
                                self.cli_info[ip]['po'][1], \
                                self.cli_info[ip]['bgp_v4'][1], self.cli_info[ip]['bgp_v4'][0],\
                                self.cli_info[ip]['bgp_v6'][1], self.cli_info[ip]['bgp_v6'][0]))

            self.log("-"*50)
            self.log("Extracted from VM logs:")
            self.log("-"*50)
            for ip in sorted(self.logs_info.keys()):
                self.log("Extracted log info from %s" % ip)
                for msg in sorted(self.logs_info[ip].keys()):
                    if not msg in [ 'error', 'route_timeout' ]:
                        self.log("    %s : %d" % (msg, self.logs_info[ip][msg]))
                    else:
                        self.log("    %s" % self.logs_info[ip][msg])
                self.log("-"*50)

            self.log("Summary:")
            self.log("-"*50)

            if no_routing_stop:
                self.log("Downtime was %s" % str(no_routing_stop - no_routing_start))
                self.log("Reboot time was %s" % str(no_routing_stop - self.reboot_start))
                self.log("Expected downtime is less then %s" % self.limit)

            if self.reboot_type == 'fast-reboot' and no_cp_replies:
                self.log("How many packets were received back when control plane was down: %d Expected: %d" % (no_cp_replies, self.nr_vl_pkts))

            has_info = any(len(info) > 0 for info in self.info.values())
            if has_info:
                self.log("-"*50)
                self.log("Additional info:")
                self.log("-"*50)
                for name, info in self.info.items():
                    for entry in info:
                        self.log("INFO:%s:%s" % (name, entry))
                self.log("-"*50)

            is_good = all(len(fails) == 0 for fails in self.fails.values())

            errors = ""
            if not is_good:
                self.log("-"*50)
                self.log("Fails:")
                self.log("-"*50)

                errors = "\n\nSomething went wrong. Please check output below:\n\n"
                for name, fails in self.fails.items():
                    for fail in fails:
                        self.log("FAILED:%s:%s" % (name, fail))
                        errors += "FAILED:%s:%s\n" % (name, fail)

            self.log("="*50)

            self.assertTrue(is_good, errors)

    def extract_no_cpu_replies(self, arr):
      """
      This function tries to extract number of replies from dataplane, when control plane is non working
      """
      # remove all tail zero values
      non_zero = filter(lambda x : x > 0, arr)

      # check that last value is different from previos
      if len(non_zero) > 1 and non_zero[-1] < non_zero[-2]:
          return non_zero[-2]
      else:
          return non_zero[-1]

    def reboot_dut(self):
        time.sleep(self.reboot_delay)

        if self.docker_name:
            self.log("%s docker %s remote side" % (self.reboot_type, self.docker_name))

            if self.reboot_type == "warm-reboot":
                stdout, stderr, return_code = self.cmd(["ssh", "-oStrictHostKeyChecking=no", self.dut_ssh, \
                    "sudo config warm_restart enable " + self.docker_name])

                if stderr != []:
                    self.log("stderr from \"sudo config warm_restart enable %s\": %s" % (self.docker_name, str(stderr)))

                if self.docker_name == "teamd":
                    stdout, stderr, return_code = self.cmd(["ssh", "-oStrictHostKeyChecking=no", self.dut_ssh, \
                        "docker exec -i teamd pkill -USR1 teamd"])
                elif self.docker_name == "swss":
                    stdout, stderr, return_code = self.cmd(["ssh", "-oStrictHostKeyChecking=no", self.dut_ssh, \
                        "docker exec -i swss orchagent_restart_check -w 1000"])
                elif self.docker_name == "bgp":
                    stdout, stderr, return_code = self.cmd(["ssh", "-oStrictHostKeyChecking=no", self.dut_ssh, \
                        "docker exec -i bgp pkill -9 zebra && docker exec -i bgp pkill -9 bgpd"])
                else:
                    self.log("Warm restart for %s is not supported, proceed to cold restart" % self.docker_name);

                if stdout != []:
                    self.log("stdout from %s %s pre processing: %s" % (self.reboot_type, self.docker_name, str(stdout)))
                if stderr != []:
                    self.log("stderr from %s %s pre processing: %s" % (self.reboot_type, self.docker_name, str(stderr)))

                time.sleep(2) # wait for pre-processing to settle down.

            stdout, stderr, return_code = self.cmd(["ssh", "-oStrictHostKeyChecking=no", self.dut_ssh, \
                "sudo systemctl restart " + self.docker_name])

            if stdout != []:
                self.log("stdout from %s %s: %s" % (self.reboot_type, self.docker_name, str(stdout)))
            if stderr != []:
                self.log("stderr from %s %s: %s" % (self.reboot_type, self.docker_name, str(stderr)))
            self.log("return code from %s %s: %s" % (self.reboot_type, self.docker_name, str(return_code)))
        else:
            self.log("Rebooting remote side")
            stdout, stderr, return_code = self.cmd(["ssh", "-oStrictHostKeyChecking=no", self.dut_ssh, "sudo " + self.reboot_type])
            if stdout != []:
                self.log("stdout from %s: %s" % (self.reboot_type, str(stdout)))
            if stderr != []:
                self.log("stderr from %s: %s" % (self.reboot_type, str(stderr)))
            self.log("return code from %s: %s" % (self.reboot_type, str(return_code)))

        # Note: a timeout reboot in ssh session will return a 255 code
        if return_code not in [0, 255]:
            thread.interrupt_main()

        return

    def cmd(self, cmds):
        process = subprocess.Popen(cmds,
                                   shell=False,
                                   stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE)
        stdout, stderr = process.communicate()
        return_code = process.returncode

        return stdout, stderr, return_code

    def peer_state_check(self, ip, queue):
        ssh = Arista(ip, queue, self.test_params)
        self.fails[ip], self.info[ip], self.cli_info[ip], self.logs_info[ip] = ssh.run()

    def wait_until_cpu_port_down(self):
        while True:
            for _, q in self.ssh_jobs:
                q.put('go')
            if self.cpu_state.get() == 'down':
                break
            time.sleep(self.TIMEOUT)

    def wait_until_cpu_port_up(self):
        while True:
            for _, q in self.ssh_jobs:
                q.put('go')
            if self.cpu_state.get() == 'up':
                break
            time.sleep(self.TIMEOUT)

    def send_in_background(self, packets_list = None, interval = None):
        """
        This method sends predefined list of packets with predefined interval.
        """
        if not interval:
            interval = self.send_interval
        if not packets_list:
            packets_list = self.packets_list
        self.sniffer_started.wait(timeout=10)
        sender_start = datetime.datetime.now()
        self.log("Sender started at %s" % str(sender_start))
        for entry in packets_list:
            time.sleep(interval)
            testutils.send_packet(self, *entry)
        self.log("Sender has been running for %s" % str(datetime.datetime.now() - sender_start))

    def sniff_in_background(self, wait = None):
        """
        This function listens on all ports, in both directions, for the UDP src=1234 dst=5000 packets, until timeout.
        Once found, all packets are dumped to local pcap file,
        and all packets are saved to self.packets as scapy type.
        The native scapy.snif() is used as a background thread, to allow delayed start for the send_in_background().
        """
        if not wait:
            wait = self.time_to_listen + 30
        sniffer_start = datetime.datetime.now()
        self.log("Sniffer started at %s" % str(sniffer_start))
        filename = '/tmp/capture.pcap'
        sniff_filter = "udp and udp dst port 5000 and udp src port 1234 and not icmp"
        scapy_sniffer = threading.Thread(target=self.scapy_sniff, kwargs={'wait': wait, 'sniff_filter': sniff_filter})
        scapy_sniffer.start()
        time.sleep(2)               # Let the scapy sniff initialize completely.
        self.sniffer_started.set()  # Unblock waiter for the send_in_background.
        scapy_sniffer.join()
        self.log("Sniffer has been running for %s" % str(datetime.datetime.now() - sniffer_start))
        self.sniffer_started.clear()
        if self.packets:
            scapyall.wrpcap(filename, self.packets)
            self.log("Pcap file dumped to %s" % filename)
        else:
            self.log("Pcap file is empty.")

    def scapy_sniff(self, wait = 180, sniff_filter = ''):
        """
        This method exploits native scapy sniff() method.
        """
        self.packets = scapyall.sniff(timeout = wait, filter = sniff_filter)

    def send_and_sniff(self):
        """
        This method starts two background threads in parallel:
        one for sending, another for collecting the sent packets.
        """
        self.sender_thr = threading.Thread(target = self.send_in_background)
        self.sniff_thr = threading.Thread(target = self.sniff_in_background)
        self.sniffer_started = threading.Event()    # Event for the sniff_in_background status.
        self.sniff_thr.start()
        self.sender_thr.start()
        self.sniff_thr.join()
        self.sender_thr.join()

    def check_udp_payload(self, packet):
        """
        This method is used by examine_flow() method.
        It returns True if a packet is not corrupted and has a valid UDP sequential UDP Payload, as created by generate_bidirectional() method'.
        """
        try:
            int(str(packet[scapyall.UDP].payload)) in range(self.packets_to_send)
            return True
        except Exception as err:
            return False

    def no_flood(self, packet):
        """
        This method filters packets which are unique (i.e. no floods).
        """
        if (not int(str(packet[scapyall.UDP].payload)) in self.unique_id) and (packet[scapyall.Ether].src == self.dut_mac):
            # This is a unique (no flooded) received packet.
            self.unique_id.append(int(str(packet[scapyall.UDP].payload)))
            return True
        elif packet[scapyall.Ether].dst == self.dut_mac:
            # This is a sent packet.
            return True
        else:
            return False

    def examine_flow(self, filename = None):
        """
        This method examines pcap file (if given), or self.packets scapy file.
        The method compares UDP payloads of the packets one by one (assuming all payloads are consecutive integers),
        and the losses if found - are treated as disruptions in Dataplane forwarding.
        All disruptions are saved to self.lost_packets dictionary, in format:
        disrupt_start_id = (missing_packets_count, disrupt_time, disrupt_start_timestamp, disrupt_stop_timestamp)
        """
        if filename:
            all_packets = scapyall.rdpcap(filename)
        elif self.packets:
            all_packets = self.packets
        else:
            self.log("Filename and self.packets are not defined.")
            self.fails['dut'].add("Filename and self.packets are not defined")
            return None
        # Filter out packets and remove floods:
        self.unique_id = list()     # This list will contain all unique Payload ID, to filter out received floods.
        filtered_packets = [ pkt for pkt in all_packets if
            scapyall.UDP in pkt and
            not scapyall.ICMP in pkt and
            pkt[scapyall.UDP].sport == 1234 and
            pkt[scapyall.UDP].dport == 5000 and
            self.check_udp_payload(pkt) and
            self.no_flood(pkt)
            ]
        # Re-arrange packets, if delayed, by Payload ID and Timestamp:
        packets = sorted(filtered_packets, key = lambda packet: (int(str(packet[scapyall.UDP].payload)), packet.time ))
        self.lost_packets = dict()
        self.max_disrupt, self.total_disruption = 0, 0
        sent_packets = dict()
        self.fails['dut'].add("Sniffer failed to capture any traffic")
        self.assertTrue(packets, "Sniffer failed to capture any traffic")
        self.fails['dut'].clear()
        if packets:
            prev_payload, prev_time = 0, 0
            sent_payload = 0
            received_counter = 0    # Counts packets from dut.
            self.disruption_start, self.disruption_stop = None, None
            for packet in packets:
                if packet[scapyall.Ether].dst == self.dut_mac:
                    # This is a sent packet - keep track of it as payload_id:timestamp.
                    sent_payload = int(str(packet[scapyall.UDP].payload))
                    sent_packets[sent_payload] = packet.time
                    continue
                if packet[scapyall.Ether].src == self.dut_mac:
                    # This is a received packet.
                    received_time = packet.time
                    received_payload = int(str(packet[scapyall.UDP].payload))
                    received_counter += 1
                if not (received_payload and received_time):
                    # This is the first valid received packet.
                    prev_payload = received_payload
                    prev_time = received_time
                    continue
                if received_payload - prev_payload > 1:
                    # Packets in a row are missing, a disruption.
                    lost_id = (received_payload -1) - prev_payload # How many packets lost in a row.
                    disrupt = (sent_packets[received_payload] - sent_packets[prev_payload + 1]) # How long disrupt lasted.
                    # Add disrupt to the dict:
                    self.lost_packets[prev_payload] = (lost_id, disrupt, received_time - disrupt, received_time)
                    self.log("Disruption between packet ID %d and %d. For %.4f " % (prev_payload, received_payload, disrupt))
                    if not self.disruption_start:
                        self.disruption_start = datetime.datetime.fromtimestamp(prev_time)
                    self.disruption_stop = datetime.datetime.fromtimestamp(received_time)
                prev_payload = received_payload
                prev_time = received_time
        self.fails['dut'].add("Sniffer failed to filter any traffic from DUT")
        self.assertTrue(received_counter, "Sniffer failed to filter any traffic from DUT")
        self.fails['dut'].clear()
        if self.lost_packets:
            self.disrupts_count = len(self.lost_packets) # Total disrupt counter.
            # Find the longest loss with the longest time:
            max_disrupt_from_id, (self.max_lost_id, self.max_disrupt_time, self.no_routing_start, self.no_routing_stop) = \
                max(self.lost_packets.items(), key = lambda item:item[1][0:2])
            self.total_disrupt_packets = sum([item[0] for item in self.lost_packets.values()])
            self.total_disrupt_time = sum([item[1] for item in self.lost_packets.values()])
            self.log("Disruptions happen between %s and %s after the reboot." % \
                (str(self.disruption_start - self.reboot_start), str(self.disruption_stop - self.reboot_start)))
        else:
            self.log("Gaps in forwarding not found.")
        self.log("Total incoming packets captured %d" % received_counter)
        if packets:
            filename = '/tmp/capture_filtered.pcap'
            scapyall.wrpcap(filename, packets)
            self.log("Filtered pcap dumped to %s" % filename)

    def check_forwarding_stop(self):
        self.asic_start_recording_vlan_reachability()

        while True:
            state = self.asic_state.get()
            for _, q in self.ssh_jobs:
                q.put('go')
            if state == 'down':
                break
            time.sleep(self.TIMEOUT)


        self.asic_stop_recording_vlan_reachability()
        return self.asic_state.get_state_time(state), self.get_asic_vlan_reachability()

    def check_forwarding_resume(self):
        while True:
            state = self.asic_state.get()
            if state != 'down':
                break
            time.sleep(self.TIMEOUT)

        return self.asic_state.get_state_time(state), self.get_asic_vlan_reachability()

    def ping_data_plane(self, light_probe=True):
        replies_from_servers = self.pingFromServers()
        if replies_from_servers > 0 or not light_probe:
            replies_from_upper = self.pingFromUpperTier()
        else:
            replies_from_upper = 0

        return replies_from_servers, replies_from_upper

    def wait_dut_to_warm_up(self):
        # When the DUT is freshly rebooted, it appears that it needs to warm
        # up towards PTF docker. In practice, I've seen this warm up taking
        # up to ~70 seconds.

        dut_stabilize_secs   = int(self.test_params['dut_stabilize_secs'])
        warm_up_timeout_secs = int(self.test_params['warm_up_timeout_secs'])

        start_time = datetime.datetime.now()

        # First wait until DUT data/control planes are up
        while True:
            dataplane = self.asic_state.get()
            ctrlplane = self.cpu_state.get()
            elapsed   = (datetime.datetime.now() - start_time).total_seconds()
            if dataplane == 'up' and ctrlplane == 'up' and elapsed > dut_stabilize_secs:
                break;
            if elapsed > warm_up_timeout_secs:
                # Control plane didn't come up within warm up timeout
                return False
            time.sleep(1)

        # check until flooding is over. Flooding happens when FDB entry of
        # certain host is not yet learnt by the ASIC, therefore it sends
        # packet to all vlan ports.
        uptime = datetime.datetime.now()
        while True:
            elapsed = (datetime.datetime.now() - start_time).total_seconds()
            if not self.asic_state.is_flooding() and elapsed > dut_stabilize_secs:
                break
            if elapsed > warm_up_timeout_secs:
                # Control plane didn't stop flooding within warm up timeout
                return False
            time.sleep(1)

        dataplane = self.asic_state.get()
        ctrlplane = self.cpu_state.get()
        if not dataplane == 'up' or not ctrlplane == 'up':
            # Either control or data plane went down while we were waiting
            # for the flooding to stop.
            return False

        if (self.asic_state.get_state_time('up') > uptime or
            self.cpu_state.get_state_time('up')  > uptime):
           # Either control plane or data plane flapped while we were
           # waiting for the warm up.
           return False

        # Everything is good
        return True


    def check_alive(self):
        # This function checks that DUT routes the packets in the both directions.
        #
        # Sometimes first attempt failes because ARP responses to DUT are not so fast.
        # But after this the function expects to see steady "replies".
        # If the function sees that there is an issue with the dataplane after we saw
        # successful replies it considers that the DUT is not healthy
        #
        # Sometimes I see that DUT returns more replies then requests.
        # I think this is because of not populated FDB table
        # The function waits while it's done

        uptime = None
        for counter in range(self.nr_tests * 2):
            state = self.asic_state.get()
            if state == 'up':
                if not uptime:
                    uptime = self.asic_state.get_state_time(state)
            else:
                if uptime:
                    return False # Stopped working after it working for sometime?
            time.sleep(2)

        # wait, until FDB entries are populated
        for _ in range(self.nr_tests * 10): # wait for some time
            if not self.asic_state.is_flooding():
                return True
            time.sleep(2)

        return False                        # we still see extra replies


    def get_asic_vlan_reachability(self):
        return self.asic_vlan_reach


    def asic_start_recording_vlan_reachability(self):
        with self.vlan_lock:
            self.asic_vlan_reach = []
            self.recording       = True


    def asic_stop_recording_vlan_reachability(self):
        with self.vlan_lock:
            self.recording = False


    def try_record_asic_vlan_recachability(self, t1_to_vlan):
        with self.vlan_lock:
            if self.recording:
                self.asic_vlan_reach.append(t1_to_vlan)


    def log_asic_state_change(self, reachable, partial=False, t1_to_vlan=0, flooding=False):
        old = self.asic_state.get()

        if reachable:
            state = 'up' if not partial else 'partial'
        else:
            state = 'down'

        self.try_record_asic_vlan_recachability(t1_to_vlan)

        self.asic_state.set_flooding(flooding)

        if old != state:
            self.log("Data plane state transition from %s to %s (%d)" % (old, state, t1_to_vlan))
            self.asic_state.set(state)


    def log_cpu_state_change(self, reachable, partial=False, flooding=False):
        old = self.cpu_state.get()

        if reachable:
            state = 'up' if not partial else 'partial'
        else:
            state = 'down'

        self.cpu_state.set_flooding(flooding)

        if old != state:
            self.log("Control plane state transition from %s to %s" % (old, state))
            self.cpu_state.set(state)


    def log_vlan_state_change(self, reachable):
        old = self.vlan_state.get()

        if reachable:
            state = 'up'
        else:
            state = 'down'

        if old != state:
            self.log("VLAN ARP state transition from %s to %s" % (old, state))
            self.vlan_state.set(state)


    def reachability_watcher(self):
        # This function watches the reachability of the CPU port, and ASIC. It logs the state
        # changes for future analysis
        self.watcher_is_stopped.clear() # Watcher is running.
        while self.watching:
            vlan_to_t1, t1_to_vlan = self.ping_data_plane(self.light_probe)
            reachable              = (t1_to_vlan  > self.nr_vl_pkts * 0.7 and
                                      vlan_to_t1  > self.nr_pc_pkts * 0.7)
            partial                = (reachable and
                                      (t1_to_vlan < self.nr_vl_pkts or
                                       vlan_to_t1 < self.nr_pc_pkts))
            flooding               = (reachable and
                                      (t1_to_vlan  > self.nr_vl_pkts or
                                       vlan_to_t1  > self.nr_pc_pkts))
            self.log_asic_state_change(reachable, partial, t1_to_vlan, flooding)
            total_rcv_pkt_cnt      = self.pingDut()
            reachable              = total_rcv_pkt_cnt > 0 and total_rcv_pkt_cnt > self.ping_dut_pkts * 0.7
            partial                = total_rcv_pkt_cnt > 0 and total_rcv_pkt_cnt < self.ping_dut_pkts
            flooding                = reachable and total_rcv_pkt_cnt > self.ping_dut_pkts
            self.log_cpu_state_change(reachable, partial, flooding)
            total_rcv_pkt_cnt      = self.arpPing()
            reachable              = total_rcv_pkt_cnt >= self.arp_ping_pkts
            self.log_vlan_state_change(reachable)
            self.watcher_is_running.set()   # Watcher is running.
        self.watcher_is_stopped.set()       # Watcher has stopped.
        self.watcher_is_running.clear()     # Watcher has stopped.


    def pingFromServers(self):
        for i in xrange(self.nr_pc_pkts):
            testutils.send_packet(self, self.from_server_src_port, self.from_vlan_packet)

        total_rcv_pkt_cnt = testutils.count_matched_packets_all_ports(self, self.from_vlan_exp_packet, self.from_server_dst_ports, timeout=self.TIMEOUT)

        self.log("Send %5d Received %5d servers->t1" % (self.nr_pc_pkts, total_rcv_pkt_cnt), True)

        return total_rcv_pkt_cnt

    def pingFromUpperTier(self):
        for entry in self.from_t1:
            testutils.send_packet(self, *entry)

        total_rcv_pkt_cnt = testutils.count_matched_packets_all_ports(self, self.from_t1_exp_packet, self.vlan_ports, timeout=self.TIMEOUT)

        self.log("Send %5d Received %5d t1->servers" % (self.nr_vl_pkts, total_rcv_pkt_cnt), True)

        return total_rcv_pkt_cnt

    def pingDut(self):
        for i in xrange(self.ping_dut_pkts):
            testutils.send_packet(self, self.random_port(self.vlan_ports), self.ping_dut_packet)

        total_rcv_pkt_cnt = testutils.count_matched_packets_all_ports(self, self.ping_dut_exp_packet, self.vlan_ports, timeout=self.TIMEOUT)

        self.log("Send %5d Received %5d ping DUT" % (self.ping_dut_pkts, total_rcv_pkt_cnt), True)

        return total_rcv_pkt_cnt

    def arpPing(self):
        for i in xrange(self.arp_ping_pkts):
            testutils.send_packet(self, self.arp_src_port, self.arp_ping)
        total_rcv_pkt_cnt = testutils.count_matched_packets_all_ports(self, self.arp_resp, [self.arp_src_port], timeout=self.TIMEOUT)
        self.log("Send %5d Received %5d arp ping" % (self.arp_ping_pkts, total_rcv_pkt_cnt), True)
        return total_rcv_pkt_cnt
