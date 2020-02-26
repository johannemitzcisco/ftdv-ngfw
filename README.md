# Overview
This package is a example of using Cisco's Network Service Orchestrator (NSO), and Elastic Services Controller (ESC) to control Virtual Network Functions (VNF) running in a VMWare Infrastructure.  In this example Cisco's Firepower Threat Defense (FTD) next generation firewall can be instantiated with scaling parameters and simply policies applied with a simple user API provided by an NSO custom service.  Some of the NSO/ESC/VNF lifecycle features employed are:
  * ESC for VNF monitoring and scaling
  * NSO NFVO Core Function Pack for managing the interactions with and notifications from ESC
  * NSO service monitoring with NSO plans
  * NSO reactive-re-deploy of services for proper orchestration between VNF lifecycle events
  * NSO resource facing service (vnf-manager)
  * NSO customer facing service (firewall/ftdv-ngfw-advanced)

In this example we are using the number of users configured in the FTD as the scaling metric.  To use something different alter the
ftdload.py script.

# INSTALL INSTRUCTIONS
### Requirements (minimum versions)
* NSO version: 4.7.2
* NSO NFVO package version: 3.7.0
* ESC version: 4.3.0.121
* Pythong requests package - pip install requests

### 1. Deploy ESC

### 2. Copy the following from service package to ESC
```
/src/esc-config/ftdapicheck.py
/src/esc-config/ftdload.py
/src/esc-config/metrics.xml
```

### 3. Execute the following from the directory on ESC where the files have been copied
```
sudo cp ftdapicheck.py /opt/cisco/esc/esc-scripts/ftdapicheck.py
sudo cp ftdload.py /opt/cisco/esc/esc-scripts/ftdload.py
sudo chmod a+x /opt/cisco/esc/esc-scripts/ftdapicheck.py
sudo chmod a+x /opt/cisco/esc/esc-scripts/ftdload.py
```
Confirm that the scripts will execute
```
/opt/cisco/esc/esc-scripts/ftdapicheck.py
```
If you see the following returned (: No such file or directory) then extra carriage returns have been introduced and those need to be removed by executing:
```
sudo sed -i -e 's/\r$//' /opt/cisco/esc/esc-scripts/ftdapicheck.py
sudo sed -i -e 's/\r$//' /opt/cisco/esc/esc-scripts/ftdload.py
```
Next execute the following on ESC (API is only available locally)
```
curl -X DELETE -u admin:Cisco123 http://127.0.0.1:8080/ESCManager/internal/dynamic_mapping/metrics/FTD_API_PING ## It is ok if this errors as it doesn't exist yets
curl -X DELETE -u admin:Cisco123 http://127.0.0.1:8080/ESCManager/internal/dynamic_mapping/metrics/FTD_LOAD ## It is ok if this errors as it doesn't exist yets
curl -X POST -H "Content-Type: Application/xml" -d @metrics.xml -u admin:Cisco123 http://127.0.0.1:8080/ESCManager/internal/dynamic_mapping/metrics
```
Confirm that metric is loaded
```
curl -u admin:Cisco123 http://127.0.0.1:8080/ESCManager/internal/dynamic_mapping/metrics | python -c 'import sys;import xml.dom.minidom;s=sys.stdin.read();print(xml.dom.minidom.parseString(s).toprettyxml())' | grep ftd
```

### 4. On NSO machine, Add the following entries to ncs.conf
```
<ncs-config>
  <!-- Needed by NFVO -->
  <commit-retry-timeout>infinity</commit-retry-timeout>
  <!-- Needed to see NSO kickers -->
  <hide-group>
    <name>debug</name>
  </hide-group>
/ncs-config>
```

### 5. On NSO machine, clone and make the service package where $NSO_PROJECT_DIR is the directory where your ncs.conf file is
```
cd $NSO_PROJECT_DIR/packages
git clone https://github.com/johannemitzcisco/ftdv-ngfw
cd $NSO_PROJECT_DIR/packages/ftdv-ngfw/src
make clean all
```

### 6. Start and stop NSO to pick up ncs.conf changes and the new packages
```
[root@nso]# ncs --stop
[root@nso]# ncs --with-package-reload
```
There are xml files in the $NSO_PROJECT_DIR/packages/ftdv-ngfw/load-dir that need to be loaded.  The first time a new package is introduced to NSO these files and the data they contain will be loaded.  If you are updating the version of the package in an existing installation, 'load merge' all the \*.xml files in this directory into the system

### 7. In NSO, Register ESC device with name "ESC"

### 8. In NSO, add the following:
```
<config xmlns="http://tail-f.com/ns/config/1.0">
  <nfvo xmlns="http://tail-f.com/pkg/tailf-etsi-rel2-nfvo">
  <settings-esc xmlns="http://tail-f.com/pkg/tailf-etsi-rel2-nfvo-esc">
    <netconf-subscription>
      <username>admin</username>
      <esc-device>
        <name>ESC</name>
      </esc-device>
    </netconf-subscription>
  </settings-esc>
  </nfvo>
</config>
```

### 8. In NSO, create an authgroup named 'ftd' with the password 'C!sco123'.  These are hardcoded in the service python code:
```
admin@ncs% set devices authgroups group ftd default-map remote-name admin remote-password 'C!sco123'
[ok][2019-04-18 18:17:50]
```
```
### 10. Confirm that there is a VNFD registered with NFVO (if not, load merge the all files in $NSO_PROJECT_DIR/packages/ftdv-ngfw/load-dir)
Note that this is the location to bound the scaling count.  In this example the minimum is 1 and the maximum
is 2.  Adjust as needed
```
admin@ncs% show nfvo vnfd | display-level 1
vnfd Cisco-FTD;
```
### 11. Load following to populate the vnf-catalog
`admin@ncs% load merge $NSO_PROJECT_DIR/packages/ftdv-ngfw/test/vnf-catalog.xml`

# USAGE INSTRUCTIONS
### Site information needs to be poplated in the model see $NSO_PROJECT_DIR/packages/ftdv-ngfw/test/site.xml for sample load file
`Note that the 'admin' tenant must exist in the site data`

There are 2 ways to make VNFs spin up and down.

### 1. Populate the RFS service directly
Load or enter the following for example:
```
<config xmlns="http://tail-f.com/ns/config/1.0">
  <vnf-manager xmlns="http://example.com/ftdv-ngfw">
  <site>
    <name>CPOC-LAB</name>
      <vnf-deployment>
        <tenant>admin</tenant>
        <deployment-name>TEST1</deployment-name>
        <catalog-vnf>FTD</catalog-vnf>
        <scaling>
          <scale-up-threshold>2</scale-up-threshold>
          <scale-down-threshold>2</scale-down-threshold>
          <ip-pool-start-address>172.26.14.69</ip-pool-start-address>
          <ip-pool-end-address>172.26.14.71</ip-pool-end-address>
        </scaling>
      </vnf-deployment>
  </site>
  </vnf-manager>
</config>
```

  1. Check the status of the deployment in the vnf-manager and NFVO operation data.  Once the vnf-manager self components status is 'ready' the deployment is complete
```
admin@ncs> show vnf-manager 
admin@ncs> show nfvo 
```

  2. Add users to the device either manually by logging into the device or using the following helper action
```
admin@ncs> request vnf-manager site CTO-LAB vnf-deployment admin ADVFTD device admin-ADVFTD-ADVFTD-VMWARE-ESC-1 add-user username test password Test!123
```

  3. Check the status of the deployment in the vnf-manager and NFVO operation data.
```
admin@ncs> show vnf-manager 
admin@ncs> show nfvo 
```

  4. Remove users to the device either manually by logging into the device or using the following helper action
```
admin@ncs> request vnf-manager site CTO-LAB vnf-deployment admin ADVFTD device admin-ADVFTD-ADVFTD-VMWARE-ESC-1 delete-user username test
```

### 2. Populate the CFS service
Load or enter the following for example:
```
<config xmlns="http://tail-f.com/ns/config/1.0">
  <firewall xmlns="http://example.com/ftdv-ngfw">
    <ftdv-ngfw-advanced>
      <site>CPOC-LAB</site>
      <tenant>admin</tenant>
      <deployment-name>TEST1</deployment-name>
      <catalog-vnf>FTD</catalog-vnf>
      <scaling>
        <scale-up-threshold>2</scale-up-threshold>
        <scale-down-threshold>2</scale-down-threshold>
        <ip-pool-start-address>172.26.14.69</ip-pool-start-address>
        <ip-pool-end-address>172.26.14.71</ip-pool-end-address>
      </scaling>
    <access-rule>
      <name>TEST</name>
      <source-zone>inside_zone</source-zone>
      <source-port>HTTPS</source-port>
      <action>PERMIT</action>
    </access-rule>
  </ftdv-ngfw-advanced>
  </firewall>
```

  1. Check the status of the deployment in the service, vnf-manager and NFVO operation data.  Once the vnf-manager self components status is 'ready' the deployment is complete
```
admin@ncs> show firewall ftdv-ngfw-advanced 
admin@ncs> show vnf-manager 
admin@ncs> show nfvo 
```

  2. Add users to the device either manually by logging into the device or using the following helper action
```
admin@ncs> request vnf-manager site CTO-LAB vnf-deployment admin ADVFTD device admin-ADVFTD-ADVFTD-VMWARE-ESC-1 add-user username test password Test!123
```

  3. Check the status of the deployment in the vnf-manager and NFVO operation data.
```
admin@ncs> show firewall ftdv-ngfw-advanced 
admin@ncs> show vnf-manager 
admin@ncs> show nfvo 
```

  4. Remove users to the device either manually by logging into the device or using the following helper action
```
admin@ncs> request vnf-manager site CTO-LAB vnf-deployment admin ADVFTD device admin-ADVFTD-ADVFTD-VMWARE-ESC-1 delete-user username test
```

# TROUBLESHOOTING
**Problem**
You see the following error when executing:
```
show vnf-manager
plan error-info message "Transaction error"
```
**Solution**
Either the java-vm/service-transaction-timeout is not set or is too low (see the file $NSO_PROJECT_DIR/packages/ftdv-ngfw/load-dir/service-timeout.xml)





