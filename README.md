# INSTALL / USAGE INSTRUCTIONS

### 1. Deploy ESC

### 2. Copy the following from service package to ESC
```
/src/esc-config/ftdapicheck.py
/src/esc-config/metrics.xml
```

### 3. Execute the following from the directory on ESC where the files have been copied
```
sudo cp ftdapicheck.py /opt/cisco/esc/esc-scripts/ftdapicheck.py
sudo chmod a+x /opt/cisco/esc/esc-scripts/ftdapicheck.py
```
These have to be executed from ESC (API is only available locally)
```
curl -X DELETE -u admin:Cisco123 http://127.0.0.1:8080/ESCManager/internal/dynamic_mapping/metrics/FTD_API_PING ## It is ok if this errors as it doesn't exist yets
curl -X POST -H "Content-Type: Application/xml" -d @metrics.xml -u admin:Cisco123 http://127.0.0.1:8080/ESCManager/internal/dynamic_mapping/metrics
```
Check to see that metric is loaded
```
curl -u admin:Cisco123 http://127.0.0.1:8080/ESCManager/internal/dynamic_mapping/metrics | python -c 'import sys;import xml.dom.minidom;s=sys.stdin.read();print(xml.dom.minidom.parseString(s).toprettyxml())'
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

### 6. In NSO, reload packages

### 5. In NSO, Register ESC device with name "ESC"

### 6. In NSO, add the following:
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
### 7. Confirm that there is a VNFD registered with NFVO (if not, load merge the all files in src/loaddata)
```
admin@ncs% show nfvo vnfd | display-level 1
vnfd Cisco-FTD;
```
### 8. The service model is more complicated now, load following to populate the vnf-catalog
`admin@ncs% load merge $NSO_PROJECT_DIR/packages/ftdv-ngfw/test/vnf-catalog.xml`

### 9. Site information needs to be poplated in the model see $NSO_PROJECT_DIR/packages/ftdv-ngfw/test/site.xml for sample load file
`admin tenant must exist in the site`

### 10. To deploy a new VM populate NSO with this (always use admin as the tenant name, comes from the site/tenant part of the model):
```
<config xmlns="http://tail-f.com/ns/config/1.0">
  <vnf-manager xmlns="http://example.com/ftdv-ngfw">
  <site>
    <name>CTO-LAB</name>
      <vnf-deployment>
        <deployment-name>FTD</deployment-name>
        <tenant>admin</tenant>
        <catalog-vnf>FTD</catalog-vnf>
      </vnf-deployment>
  </site>
  </vnf-manager>
</config>
```
### 11. Check Deployment status
`admin@ncs> show nfvo vnf-info`






