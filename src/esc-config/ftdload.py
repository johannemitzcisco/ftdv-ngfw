#!/usr/bin/env python
# -*- mode: python; python-indent: 4 -*-
import requests 
import sys
from datetime import datetime
import traceback
import logging
import paramiko
LOG_FILENAME = '/var/log/esc/mona/ftdload.log'
logging.basicConfig(filename=LOG_FILENAME, level=logging.DEBUG)


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

def get_username():
    username = get_value("username")
    if username == None:
        return 'admin'
    return username

def get_password():
    password = get_value("password")
    if password == None:
        return 'C!sco123'
    return password

def get_stats(ip, username, password):
    logging.info("\nStarting ===========================================")
    pre_ssh=paramiko.SSHClient()
    pre_ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    pre_ssh.connect(ip, username=username, password=password,
                        look_for_keys=False, allow_agent=False)
    logging.info("Connected: {}".format(ip))
    ssh = pre_ssh.invoke_shell()
    logging.info("Sending Request")
    ssh.send('show conn count\n')
    data = ""
    while True:
        data_part = ssh.recv(1024)
#        logging.info(str(count)+' ---:'+data_part)
        if len(data_part) == 0:
            logging.info("*** Connection terminated")
            read_data = False
            break
        data += data_part
        logging.info(data)
        logging.info('**** Checking ****')
        if 'preserve-connection' in data:
            connections = data.split('> show conn count')[1].split(' ')[0].lstrip()
            logging.info('connections: '+connections)
            ssh.close()
            return connections

# Main
try:
    now = datetime.now()
    ip_address = get_ip_addr()
    if ip_address == None:
        print "IP Address property must be specified"
        sys.exit(int(3))
    count = get_stats(ip_address, get_username(), get_password())
    count = 1
#    logging.error("{} FTDv ({}) Load Check: {}".format(now.strftime("%m/%d/%Y %H:%M:%S:"), ip_address, count))
    sys.exit(int(count))
except Exception as e:
    logging.error(traceback.format_exc())
