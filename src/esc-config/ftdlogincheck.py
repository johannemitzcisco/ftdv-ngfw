#!/usr/bin/env python
# -*- mode: python; python-indent: 4 -*-
import sys
import subprocess
from datetime import datetime
import logging
LOG_FILENAME = '/var/log/esc/mona/ftdlogin.log'
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
try:
    r1 = subprocess.call(['nc', '-zv', ip_addr, '8305'])
    return_code = r1
    if r1 == 0:
        info = 'Device Login Available'
        r2 = subprocess.call(['nc', '-zv', ip_addr, '22'])
        return_code = r2
        if r2 == 0:
            info = 'Device Login Available'
        else:
            info = 'Device Login Unavailable'
    else:
        r2 = 2
        info = 'Device Login Unavailable'
except Exception as e:
    return_code = 2
    info = 'Error trying to reach device'
finally:
    now = datetime.now()
    logging.info("{} FTDv ({}) Login Check Response: {}({},{}) {}".format(now.strftime("%m/%d/%Y %H:%M:%S:"), ip_addr, return_code, r1, r2, info))
    logging.shutdown()
    sys.exit(int(return_code))

