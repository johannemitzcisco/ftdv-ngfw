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

def get_data_api():
    data_api = get_value("data_api")
    if data_api == None:
        return '/object/users'
    return data_api

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

def get_baseURL(ip_address):
    return 'https://{}/api/fdm/v2'.format(ip_address)

def request(type, ip_address, url_suffix, payload, access_token):
    URL = "{}{}".format(get_baseURL(ip_address), url_suffix)
    print('URL: {}'.format(URL))
    # sending get request and saving the response as response object 
    headers = {'Content-Type' : 'application/json', 'Accept' : 'application/json'}
    if access_token is not None:
        headers.update({'Authorization': 'Bearer ' + access_token})
    try:
        if type == 'POST':
            response = requests.post(url=URL, headers=headers, verify=False, json=payload )
        elif type == 'GET':
            response = requests.get(url=URL, headers=headers, verify=False, json=payload )
        else:
            sys.exit(int(0))
        if response.status_code == requests.codes.ok:
            return response
        else:
            print('Bad status code: {}'.format(response.status_code))
            sys.exit(int())
    except Exception as e:
        print traceback.format_exc()
        sys.exit(int())

def getAccessToken(ip_address):
    payload = {'grant_type': 'password','username': get_username(),'password': get_password()}
    return request('POST', ip_address, "/fdm/token", payload, None).json()['access_token']

def get_count(ip_address):
    response = request('GET', ip_address, get_data_api(), None, getAccessToken(ip_address))
    return len(response.json()['items'])
    
# Main
ip_address = get_ip_addr()
if ip_address == None:
    print "IP Address property must be specified"
    sys.exit(int())
count = get_count(ip_address)
print('Count {}'.format(count))
sys.exit(int(count))


