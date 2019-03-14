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
URL = "https://"+ip_addr+"/api/fdm/v2/fdm/token"
# sending get request and saving the response as response object 
payload = {'grant_type': 'password','username': 'admin','password': 'cisco123'}
headers = {'Content-Type' : 'application/json', 'Accept' : 'application/json'}
try:
    r = requests.post(url=URL, headers=headers, verify=False, json=payload )
    # extracting data in json format
    print "FTDv ({}) API Check Response: {}".format(ip_addr, r.status_code)
    if r.status_code == requests.codes.ok:
        # print r.content
        data = r.json()
        print "FTDv ({}) API Check Response Token: {}".format(ip_addr, data['access_token'])
        sys.exit(int(0))
    else:
        sys.exit(int(1))
except Exception as e:
    print traceback.format_exc()
    sys.exit(int(2))


