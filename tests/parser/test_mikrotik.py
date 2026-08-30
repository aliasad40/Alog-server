"""Parser tests built from real RouterOS output.

Every sample here is a shape RouterOS actually emits. When a new RouterOS
version or a new rule produces a shape we do not handle, add it here first,
then make it pass.
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "backend"))

import pytest
from app.parser import MikroTikParser

P = MikroTikParser()
LOOSE = MikroTikParser(require_nat=False)
IFACE = MikroTikParser(require_nat=False, subscriber_from_interface=True)

SPEC_1 = ("firewall,info forward: in:<pppoe-P2-musa> out:vlan2436, "
          "connection-state:new,snat proto TCP (SYN), "
          "100.68.180.230:35420->99.124.164.160:22, "
          "NAT (100.68.180.230:35420->103.125.177.119:35420)->99.124.164.160:22, len 60")

SPEC_2 = ("firewall,info forward: in:<pppoe-DT-1023-03324108448> out:vlan-3333, "
          "src-mac fc:f2:9f:e9:0f:d0, proto UDP, "
          "10.20.255.253:49196->37.111.148.20:2958, "
          "NAT (10.20.255.253:49196->103.73.100.12:49196)->37.111.148.20:2958, len 72")


def test_spec_example_one():
    log, reason = P.parse(SPEC_1)
    assert reason == ""
    assert log.private_ip == "100.68.180.230"
    assert log.private_port == 35420
    assert log.public_ip == "103.125.177.119"
    assert log.public_port == 35420
    assert log.dest_ip == "99.124.164.160"
    assert log.dest_port == 22
    assert log.protocol == "TCP"
    assert log.subscriber_id == "P2-musa"


def test_spec_example_two():
    log, reason = P.parse(SPEC_2)
    assert reason == ""
    assert log.private_ip == "10.20.255.253"
    assert log.public_ip == "103.73.100.12"
    assert log.dest_ip == "37.111.148.20"
    assert log.dest_port == 2958
    assert log.protocol == "UDP"
    assert log.subscriber_id == "DT-1023-03324108448"


def test_destination_is_not_reversed():
    """Regression guard for the correction called out in the specification."""
    log, _ = P.parse(SPEC_1)
    assert (log.dest_ip, log.dest_port) == ("99.124.164.160", 22)
    assert (log.public_ip, log.public_port) != ("99.124.164.160", 22)


def test_full_syslog_frame_with_pri_and_bsd_header():
    line = ("<134>Aug 30 18:33:17 LHE-BRAS-01 " + SPEC_1)
    log, reason = P.parse(line)
    assert reason == "" and log.public_ip == "103.125.177.119"


def test_rfc5424_frame():
    line = ("<134>1 2026-08-30T18:33:17.123456+05:00 LHE-BRAS-01 - - - - " + SPEC_2)
    log, reason = P.parse(line)
    assert reason == "" and log.subscriber_id == "DT-1023-03324108448"


@pytest.mark.parametrize("flags", ["TCP (SYN)", "TCP (ACK)", "TCP (FIN,ACK)",
                                   "TCP (SYN,ACK)", "TCP (RST)", "TCP (PSH,ACK)"])
def test_tcp_flag_variations(flags):
    line = (f"firewall,info forward: in:<pppoe-u1> proto {flags}, "
            "10.0.0.1:100->8.8.8.8:53, NAT (10.0.0.1:100->1.2.3.4:200)->8.8.8.8:53")
    log, reason = P.parse(line)
    assert reason == "" and log.protocol == "TCP" and log.public_port == 200


def test_icmp_without_ports():
    line = ("firewall,info forward: in:<pppoe-u1> proto ICMP (type 8, code 0), "
            "10.0.0.1->8.8.8.8, NAT (10.0.0.1->103.1.1.1)->8.8.8.8, len 84")
    log, reason = P.parse(line)
    assert reason == ""
    assert log.protocol == "ICMP"
    assert (log.private_port, log.public_port, log.dest_port) == (0, 0, 0)
    assert log.public_ip == "103.1.1.1"


def test_numeric_protocol_is_named():
    line = ("in:<pppoe-u1> proto 47, 10.0.0.1->8.8.8.8, NAT (10.0.0.1->1.2.3.4)->8.8.8.8")
    log, _ = P.parse(line)
    assert log.protocol == "GRE"


def test_unknown_numeric_protocol_passes_through():
    line = ("in:<pppoe-u1> proto 253, 10.0.0.1->8.8.8.8, NAT (10.0.0.1->1.2.3.4)->8.8.8.8")
    log, _ = P.parse(line)
    assert log.protocol == "253"


def test_missing_protocol_is_empty_not_an_error():
    line = "in:<pppoe-u1> 10.0.0.1:1->8.8.8.8:2, NAT (10.0.0.1:1->1.2.3.4:3)->8.8.8.8:2"
    log, reason = P.parse(line)
    assert reason == "" and log.protocol == ""


def test_non_nat_log_is_dropped_by_default():
    line = "firewall,info input: in:ether1 proto TCP (SYN), 1.1.1.1:5->2.2.2.2:80, len 60"
    log, reason = P.parse(line)
    assert log is None and reason == "no_nat"


def test_non_nat_log_accepted_in_loose_mode_public_equals_private():
    line = "firewall,info input: in:ether1 proto TCP (SYN), 1.1.1.1:5->2.2.2.2:80, len 60"
    log, reason = LOOSE.parse(line)
    assert reason == ""
    assert log.private_ip == log.public_ip == "1.1.1.1"
    assert log.private_port == log.public_port == 5
    assert log.dest_port == 80


def test_subscriber_prefix_variants():
    for iface, expected in [
        ("<pppoe-P2-musa>", "P2-musa"),
        ("<pppoe-in-cust-99>", "cust-99"),
        ("<l2tp-roaming01>", "roaming01"),
        ("<ovpn-branch-hq>", "branch-hq"),
    ]:
        line = (f"in:{iface} proto UDP, 10.0.0.1:1->8.8.8.8:2, "
                "NAT (10.0.0.1:1->1.2.3.4:3)->8.8.8.8:2")
        log, _ = P.parse(line)
        assert log.subscriber_id == expected, iface


def test_non_session_interface_yields_empty_subscriber():
    line = ("in:vlan100 proto UDP, 10.0.0.1:1->8.8.8.8:2, "
            "NAT (10.0.0.1:1->1.2.3.4:3)->8.8.8.8:2")
    log, _ = P.parse(line)
    assert log.subscriber_id == ""


def test_interface_fallback_mode_keeps_interface_name():
    line = ("in:vlan100 proto UDP, 10.0.0.1:1->8.8.8.8:2, "
            "NAT (10.0.0.1:1->1.2.3.4:3)->8.8.8.8:2")
    log, _ = IFACE.parse(line)
    assert log.subscriber_id == "vlan100"


def test_subscriber_with_dots_and_at_signs():
    line = ("in:<pppoe-user.name@isp.pk> proto UDP, 10.0.0.1:1->8.8.8.8:2, "
            "NAT (10.0.0.1:1->1.2.3.4:3)->8.8.8.8:2")
    log, _ = P.parse(line)
    assert log.subscriber_id == "user.name@isp.pk"


def test_out_of_range_octet_rejected():
    line = ("in:<pppoe-u1> proto UDP, 10.0.0.1:1->8.8.8.8:2, "
            "NAT (10.0.0.1:1->300.1.1.1:3)->8.8.8.8:2")
    log, reason = P.parse(line)
    assert log is None and reason == "no_match"


def test_out_of_range_port_rejected():
    line = ("in:<pppoe-u1> proto UDP, 10.0.0.1:1->8.8.8.8:2, "
            "NAT (10.0.0.1:1->1.2.3.4:99999)->8.8.8.8:2")
    log, reason = P.parse(line)
    assert log is None and reason == "no_match"


@pytest.mark.parametrize("junk", [
    "", "   ", "not a log at all", "<134>Aug 30 18:33:17 host kernel: oom-killer",
    "NAT (", "NAT ()->", "firewall,info forward: in:<pppoe-x> proto TCP",
    "\x00\x01\x02\xff", "a" * 8000,
])
def test_garbage_never_raises(junk):
    log, reason = P.parse(junk)
    assert log is None and reason != ""


def test_binary_payload_never_raises():
    log, reason = P.parse_bytes(b"\xff\xfe\x00binary garbage \xc3(")
    assert log is None


def test_truncated_log_is_dropped_not_half_parsed():
    line = ("in:<pppoe-u1> proto TCP, 10.0.0.1:1->8.8.8.8:2, NAT (10.0.0.1:1->1.2.3.4")
    log, reason = P.parse(line)
    assert log is None
