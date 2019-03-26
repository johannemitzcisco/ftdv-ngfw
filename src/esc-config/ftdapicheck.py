#!/usr/bin/env python
# -*- mode: python; python-indent: 4 -*-
import requests 
import sys
import traceback

print str(sys.argv)

# Functions
def get_value(key):
    i = 0
    for arg in sys.argv:
        i = i + 1
        if arg == key:
            return sys.argv[i]
    return None

def get_ip_addr():
    device_ip = get_value("vm_ip_address")
    return device_ip


# Main
ip_addr = get_ip_addr()
if ip_addr == None:
    print "IP Address property must be specified"
    sys.exit(int(3))
success_once = False
with open("/var/ftdiapi.counter", "r") as file:
    if file.read() == 's':
        success_once = True
URL = "https://"+ip_addr+"/api/fdm/v2/fdm/token"
# sending get request and saving the response as response object
payload = {'grant_type': 'password','username': 'apitester','password': 'apitester'}
headers = {'Content-Type' : 'application/json', 'Accept' : 'application/json'}
try:
    r = requests.post(url=URL, headers=headers, verify=False, json=payload )
    # extracting data in json format
    print "FTDv ({}) API Check Response: {}".format(ip_addr, r.status_code)
    # We are only checking that the API service responds, expect "unauthorized(400)"
    if r.status_code == 400:
        sys.exit(int(0))
        # Record we were successful once
        with open("/var/ftdiapi.counter", "w") as file:
            file.write('s')
    else:
        if success_once:
            # Device was once alive
            sys.exit(int(4))
        sys.exit(int(1))
except Exception as e:
    print traceback.format_exc()
    if success_once:
        # Device was once alive
        sys.exit(int(4))
    sys.exit(int(2))



