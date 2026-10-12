# 2024-11-29 14:17:45 by RouterOS 7.15.3
# software id = 7XKD-AB12
#
# model = RB5009UG+S+
# serial number = HD1234ABCD5
/interface bridge add admin-mac=48:A9:8A:11:22:02 auto-mac=no comment=defconf name=bridge vlan-filtering=yes
/interface vlan add comment=IoT interface=bridge name=vlan20 vlan-id=20
/ip pool add name=default-dhcp ranges=192.168.88.10-192.168.88.254
/ip address add address=192.168.88.1/24 comment=defconf interface=bridge network=192.168.88.0
/ip dhcp-server network add address=192.168.88.0/24 comment=defconf domain=lan gateway=192.168.88.1
/ip dns set allow-remote-requests=yes
/ip dns static add address=192.168.88.20 comment=Omini name=nas.lan type=A
/system identity set name=core-router
