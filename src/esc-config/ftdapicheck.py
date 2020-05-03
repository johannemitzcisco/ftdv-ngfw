#!/usr/bin/env python
# -*- mode: python; python-indent: 4 -*-
import sys
import requests 
from datetime import datetime
import os
import logging
LOG_FILENAME = '/var/log/esc/mona/ftdapi.log'
logging.basicConfig(filename=LOG_FILENAME, level=logging.INFO)

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
try:
    with open("/var/ftdiapi.counter", "r") as file:
        if file.read() == 's':
            success_once = True
except Exception as e:
    pass

URL = "https://"+ip_addr+"/api/fdm/latest/fdm/token"
# sending get request and saving the response as response object
payload = {'grant_type': 'password','username': 'apitester','password': 'apitester'}
headers = {'Content-Type' : 'application/json', 'Accept' : 'application/json'}
try:
    r = requests.post(url=URL, headers=headers, verify=False, json=payload )
    # extracting data in json format
    now = datetime.now()
    # We are only checking that the API service responds, expect "unauthorized(400)"
    if r.status_code == 400:
        logging.info("{} FTDv ({}) API Check Response: {}  API Available".format(now.strftime("%m/%d/%Y %H:%M:%S:"), ip_addr, r.status_code))
        # Record we were successful once
        with open("/var/log/esc/mona/ftdapi.counter", "w") as file2:
            file2.write('s')
        logging.shutdown()
        sys.exit(int(0))
    else:
        if success_once:
            # Device was once alive
            logging.info("{} FTDv ({}) API Check Response: {}  API was Available previously".format(now.strftime("%m/%d/%Y %H:%M:%S:"), ip_addr, r.status_code))
            logging.shutdown()
            sys.exit(int(4))
        else:
            logging.info("{} FTDv ({}) API Check Response: {}  Device Available, API Unavailable".format(now.strftime("%m/%d/%Y %H:%M:%S:"), ip_addr, r.status_code))
            logging.shutdown()
            sys.exit(int(1))
except Exception as e:
    now = datetime.now()
#    logging.exception("Exception")
    status_code = unknown
    if (r):
        status_code = r.status_code
    if success_once:
        # Device was once alive
        logging.info("{} FTDv ({}) API Check Response: {}  API was available previously".format(now.strftime("%m/%d/%Y %H:%M:%S:"), ip_addr, status_code))
        logging.shutdown()
        sys.exit(int(4))
    logging.info("{} FTDv ({}) API Check Response: {}  Device Unavailable".format(now.strftime("%m/%d/%Y %H:%M:%S:"), ip_addr, status_code))
    logging.shutdown()
    sys.exit(int(2))

