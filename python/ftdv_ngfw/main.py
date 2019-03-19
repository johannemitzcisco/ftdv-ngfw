# -*- mode: python; python-indent: 4 -*-
import ncs
from ncs.application import Service
from ncs.dp import Action, NCS_SERVICE_UPDATE
import _ncs.dp
import requests 
import traceback
              
#TODO Handle VNF recovery scenario

class ScalableService(Service):

    @Service.create
    def cb_create(self, tctx, root, service, proplist):
        self.log.info('Service create(service=', service._path, ')')
        site = service._parent._parent
        vnf_catalog = root.vnf_manager.vnf_catalog
        vnf_deployment_name = service.tenant+'-'+service.deployment_name
        self.log.info('Site Name: ', service._parent._parent.name)
        self.log.info('Tenant-Deployment Name: ', vnf_deployment_name)
        proplistdict = dict(proplist)
        self.log.info('==== Service Reactive-Redeploy Properties ====')
        for k, v in proplistdict.iteritems():
            self.log.info(k, ' ', v)
        self.log.info('==============================================')

        # Step 1: VNF Deployment - this may deploy multiple VMs depending on associated nfvo/vnfd
        vars = ncs.template.Variables()
        vars.add('SITE-NAME', service._parent._parent.name);
        vars.add('DEPLOYMENT-TENANT', service.tenant);
        vars.add('DEPLOYMENT-NAME', service.deployment_name);
        vars.add('MONITORS-ENABLED', 'true');
        vars.add('VNF-PASSWORD', 'Adm!n123');
        vars.add('DEPLOY-PASSWORD', 'Adm!n123');
        vars.add('IMAGE-NAME', root.nfvo.vnfd[vnf_catalog[service.catalog_vnf].descriptor_name]
                                .vdu[vnf_catalog[service.catalog_vnf].descriptor_vdu]
                                .software_image_descriptor.image);
        # Set the context of the template to /vnf-manager
        template = ncs.template.Template(service._parent._parent._parent._parent)
        template.apply('vnf-deployment', vars)

        # Step 2: Check if VNF is Ready to be configured
        op_status = 'not-reached'
        with ncs.maapi.single_read_trans('admin', 'system',
                                  db=ncs.OPERATIONAL) as trans:
            try:
                op_root = ncs.maagic.get_root(trans)
                op_status = op_root.nfvo.vnf_info.nfvo_rel2_esc__esc \
                             .vnf_deployment[service.tenant, service.deployment_name, site.elastic_services_controller] \
                             .plan.component['self'].state['ready'].status
            except KeyError:
                # op_status will not exist the first pass through the service logic
                self.log.info('Cannot find operational status of the deployment')
                pass
        if op_status == 'reached':
            self.log.info('VNFs\' APIs are available')
            proplistdict['Alive'] = 'True'
        else:
            # Service VNFs are not ready, add kicker and return to wait from re-deploy
            self.log.info('VNFs\' APIs are NOT not available - Creating kicker to watch status')
            applyKicker(root, self.log, vnf_deployment_name, site.name, service.tenant, service.deployment_name, site.elastic_services_controller)
            service.status = 'Deploying'
            proplistdict["Alive"] = "False"
            proplist = [(k,v) for k,v in proplistdict.iteritems()]
            return proplist

        # Step 3: Turn off monitoring and change password so that provisioning doesn't error 
        #         out because of too many failed logins
        vm_devices = root.nfvo.vnf_info.esc.vnf_deployment_result[service.tenant, \
                service.deployment_name, site.elastic_services_controller] \
                .vdu[service.deployment_name, vnf_catalog[service.catalog_vnf].descriptor_vdu] \
                .vm_device
        vm_count = proplistdict.get('VMCount', 0)
        new_vm_count = len(vm_devices)
        if new_vm_count > vm_count and proplistdict.get('Monitored', 'True') == 'True':
            # Turn off monitoring and change the monitoring password to the service-supplied one
            self.log.info('Device is not provisioned, turn off monitoring')
            vars = ncs.template.Variables()
            vars.add('SITE-NAME', service._parent._parent.name);
            vars.add('DEPLOYMENT-TENANT', service.tenant);
            vars.add('DEPLOYMENT-NAME', service.deployment_name);
            vars.add('MONITORS-ENABLED', 'false'); # This is changing
            vars.add('DEPLOY-PASSWORD', 'Adm!n123');
            vars.add('VNF-PASSWORD', 'C!sco123'); # This is changing
            vars.add('IMAGE-NAME', root.nfvo.vnfd[vnf_catalog[service.catalog_vnf].descriptor_name]
                                    .vdu[vnf_catalog[service.catalog_vnf].descriptor_vdu]
                                    .software_image_descriptor.image);
            # Set the context of the template to /vnf-manager
            template = ncs.template.Template(service._parent._parent._parent._parent)
            template.apply('vnf-deployment', vars)
            applyKicker(root, self.log, vnf_deployment_name, site.name, service.tenant, service.deployment_name, site.elastic_services_controller)
            proplistdict['Monitored'] = "False"
            proplistdict['Provisioned'] = "False"
            proplist = [(k,v) for k,v in proplistdict.iteritems()]
            return proplist

        # Step 4: Call device provisioning API and register the device with NSO and re-enable 
        #         monitoring
        if proplistdict['Provisioned'] == 'False':
            self.log.info('Provision Device and re-enable monitoring')
            # Do initial provisitioning of each device and re-enable monitoring and register device with NSO
            for device in vm_devices:
                if proplistdict.get(device.device_name, 'Not Provisioned') != 'Provisioned':
                    ip_address = device.interface['1'].ip_address
                    device_status = service.device.create(device.name)
                    device_status.management_ip_address = ip_address
                    # Call the device provisioning API directly
                    provisionFTD(self.log, ip_address, 'admin', 'Adm!n123', 'C!sco123')
                    vars = ncs.template.Variables()
                    vars.add('DEVICE-NAME', device.device_name);
                    vars.add('IP-ADDRESS', ip_address);
                    vars.add('PORT', 443);
                    vars.add('AUTHGROUP', 'default');
                    # Context doesn't matter, only variables are used in the template
                    template = ncs.template.Template(service)
                    # Register the device in NSO
                    template.apply('nso-device', vars)
                    proplistdict[device.device_name] = 'Provisioned'
            # Turn monitoring back on
            vars = ncs.template.Variables()
            vars.add('SITE-NAME', service._parent._parent.name);
            vars.add('DEPLOYMENT-TENANT', service.tenant);
            vars.add('DEPLOYMENT-NAME', service.deployment_name);
            vars.add('MONITORS-ENABLED', 'true'); # This is changing
            vars.add('DEPLOY-PASSWORD', 'Adm!n123');
            vars.add('VNF-PASSWORD', 'C!sco123');
            vars.add('IMAGE-NAME', root.nfvo.vnfd[vnf_catalog[service.catalog_vnf].descriptor_name]
                                    .vdu[vnf_catalog[service.catalog_vnf].descriptor_vdu]
                                    .software_image_descriptor.image);
            # Set the context of the template to /vnf-manager
            template = ncs.template.Template(service._parent._parent._parent._parent)
            template.apply('vnf-deployment', vars)
            service.status = 'Deployed'
            proplistdict["Provisioned"] = "True"
            proplistdict["Monitored"] = "True"
            proplist = [(k,v) for k,v in proplistdict.iteritems()]
            # Apply kicker to watch for service status changing to configurable.
            #  This changed should happen in the post_modification call
            kick_monitor_node = ("/vnf-manager/site[name='{}']/vnf-deployment[tenant='{}'][deployment-name='{}'].status").format(
                                site.name, service.tenant, service.deployment_name)
            self.log.info('Creating Kicker Monitor on: ', kick_monitor_node)
            kicker = root.kickers.data_kicker.create('ftdv_ngfw-'+vnf_deployment_name+'-'+tenant)
            kicker.monitor = kick_monitor_node
            kicker.kick_node = ("/vnf-manager/site[name='{}']/vnf-deployment[tenant='{}'][deployment-name='{}']").format(
                                site.name, service.tenant, service.deployment_name)
            kicker.action_name = 'reactive-re-deploy'
            return proplist

        # Step 5: When more than one VNF is running, apply ITD load balancing
        if vm_count > 1:
            self.log.info("Now's the time to configure the device and ITD")

    @Service.post_modification
    def cb_post_modification(self, tctx, op, kp, root, proplist):
        self.log.info('Service postmod(service=', kp, ' ', op, ')')
        # If the device is in the Deployed state, sync-from device
        if op == NCS_SERVICE_UPDATE: # This should only happen after the intial servce run
            with ncs.maapi.single_read_trans('admin', 'system',
                                              db=ncs.OPERATIONAL) as trans:
                service = ncs.maagic.get_node(trans, kp)
                self.log.info('Deployment Status: ', service.status)
                if service.status == "Deployed":
                    vm_devices = root.nfvo.vnf_info.esc.vnf_deployment_result[service.tenant, \
                                    service.deployment_name, site.elastic_services_controller] \
                                    .vdu[service.deployment_name, vnf_catalog[service.catalog_vnf].descriptor_vdu] \
                                    .vm_device
                    for device in vm_devices:
                        self.log.info('Syncing device: ', device.device_name)
                        # output.result = root.devices.device[device.device_name].sync_from.result
                        self.log.info('Sync Result: ', output.result)



def applyKicker(root, log, vnf_deployment_name, site_name, tenant, service_deployment_name, esc_device_name):
    kick_monitor_node = ("/nfvo/vnf-info/nfvo-rel2-esc:esc" 
                      "/vnf-deployment[tenant='{}'][deployment-name='{}'][esc='{}']" 
                      "/plan/component[name='self']/state[name='ncs:ready']/status").format(
                      tenant, service_deployment_name, esc_device_name)
    log.info('Creating Kicker Monitor on: ', kick_monitor_node)
    kicker = root.kickers.data_kicker.create('ftdv_ngfw-'+vnf_deployment_name+'-'+tenant)
    kicker.monitor = kick_monitor_node
    kicker.kick_node = ("/vnf-manager/site[name='{}']/vnf-deployment[tenant='{}'][deployment-name='{}']").format(
                        site_name, tenant, service_deployment_name)
    kicker.action_name = 'reactive-re-deploy'

def provisionFTD(log, ip_address, username, current_password, new_password):
    access_token = getAccessToken(log, ip_address, username, current_password)
    URL = "https://{}/api/fdm/v2/devices/default/action/provision".format(ip_address)
    headers = {'Content-Type' : 'application/json', 'Accept' : 'application/json', 
            'Authorization': 'Bearer ' + access_token}
    payload = provisionPayload.format(current_password, new_password)
    log.info(payload)
    r = requests.post(url=URL, headers=headers, verify=False, json=payload )
    log.info('Provision Response:', r.status_code)
    log.info('Provision Response:', r.text)


def getAccessToken(log, ip_address, username='admin', password='cisco123'):
    URL = "https://"+ip_address+"/api/fdm/v2/fdm/token"
    payload = {'grant_type': 'password','username': username,'password': password}
    headers = {'Content-Type' : 'application/json', 'Accept' : 'application/json'}
    r = requests.post(url=URL, headers=headers, verify=False, json=payload )
    data = r.json()
    access_token = data['access_token']
    log.info('AccessToken: ', access_token)
    return access_token

# class GetDeviceData(Action):
#     @Action.action
#     def cb_action(self, uinfo, name, kp, input, output):
#         self.log.info('action name: ', name)
#         with ncs.maapi.single_write_trans('admin', 'system',
#                                           db=ncs.OPERATIONAL) as trans:
#             service = ncs.maagic.get_node(trans, kp)
#             if service.state.port is not None:
#                 service.state.port.delete()
#             if service.state.zone is not None:
#                 service.state.zone.delete()

#             URL = "https://"+service.ip_address+"/api/fdm/v2/object/tcpports?limit=0"
#             access_token = getAccessToken(self.log, service.ip_address)
#             headers = {'Content-Type' : 'application/json', 'Accept' : 'application/json', 
#                         'Authorization': 'Bearer ' + access_token}
#             r = requests.get(url=URL, headers=headers, verify=False)
#             data = r.json()
#             self.log.info(data)
#             for item in data['items']:
#                 self.log.info(item['name'], ' ', item['id'])
#                 port = service.state.port.create(str(item['name']))
#                 port.id = item['id']

#             URL = "https://"+service.ip_address+"/api/fdm/v2/object/securityzones?limit=0"
#             # access_token = getAccessToken(self.log)
#             headers = {'Content-Type' : 'application/json', 'Accept' : 'application/json', 
#                         'Authorization': 'Bearer ' + access_token}
#             r = requests.get(url=URL, headers=headers, verify=False)
#             data = r.json()
#             self.log.info(data)
#             for item in data['items']:
#                 self.log.info(item['name'], ' ', item['id'])
#                 zone = service.state.zone.create(str(item['name']))
#                 zone.id = item['id']

#             output.result = "Ok"
#             trans.apply()

class GetDeviceData(Action):
    @Action.action
    def cb_action(self, uinfo, name, kp, input, output):
        self.log.info('action name: ', name)

        with ncs.maapi.single_write_trans('admin', 'system',
                                          db=ncs.OPERATIONAL) as trans:
            device = ncs.maagic.get_node(trans, kp)
            ip_address = device.management_ip_address

            if device.state.port is not None:
                device.state.port.delete()
            if device.state.zone is not None:
                devices.state.zone.delete()

            access_token = getAccessToken(self.log, ip_address)
            headers = {'Content-Type' : 'application/json', 'Accept' : 'application/json', 
                        'Authorization': 'Bearer ' + access_token}

            URL = "https://"+ip_address+"/api/fdm/v2/object/tcpports?limit=0"
            r = requests.get(url=URL, headers=headers, verify=False)
            data = r.json()
            self.log.info(data)
            for item in data['items']:
                self.log.info(item['name'], ' ', item['id'])
                port = service.state.port.create(str(item['name']))
                port.id = item['id']

            URL = "https://"+ip_address+"/api/fdm/v2/object/securityzones?limit=0"
            r = requests.get(url=URL, headers=headers, verify=False)
            data = r.json()
            self.log.info(data)
            for item in data['items']:
                self.log.info(item['name'], ' ', item['id'])
                zone = service.state.zone.create(str(item['name']))
                zone.id = item['id']

            output.result = "Ok"
            trans.apply()

class NGFWAdvancedService(Service):
    @Service.create
    def cb_create(self, tctx, root, service, proplist):
        self.log.info('Service create(service=', service._path, ')')

        vars = ncs.template.Variables()
        template = ncs.template.Template(service)
        template.apply('vnf-manager-vnf-deployment', vars)

        # Check VNF-Manger service deployment status
        status = 'Unknown'
        with ncs.maapi.single_read_trans('admin', 'system',
                                  db=ncs.OPERATIONAL) as trans:
            try:
                op_root = ncs.maagic.get_root(trans)
                deployment = op_root.vnf_manager.site[service.site].vnf_deployment[service.deployment_name]
            except KeyError:
                # op_status will not exist the first pass through the service logic
                self.log.info('Cannot find operational status of the deployment')
                pass

        if deployment.status != 'Configurable':
            # Create a kicker to be alerted when the VNFs are deployed
            kick_monitor_node = ("/vnf-manager/site[name='{}']/vnf-deployment[tenant='{}'][deployment-name='{}'].status").format(
                                service.site, service.tenant, service.deployment_name)
            self.log.info('Creating Kicker Monitor on: ', kick_monitor_node)
            kicker = root.kickers.data_kicker.create('ftdv_ngfw-'+vnf_deployment_name+'-'+tenant)
            kicker.monitor = kick_monitor_node
            kicker.kick_node = ("/firewall/ftdv-ngfw-advanced[site='{}'][tenant='{}'][deployment-name='{}']").format(
                                service.site, service.tenant, service.deployment_name)
            kicker.action_name = 'reactive-re-deploy'
            return proplist


        # Apply policies
        # This will be replaced with a template against the FTD NED when available
        for device in service.device:
            self.log.info('Configuring device: ', device.name)
            access_token = getAccessToken(self.log, device.management_ip_address, 'admin', 'C!sco123')
            headers = {'Content-Type' : 'application/json', 'Accept' : 'application/json', 
                        'Authorization': 'Bearer ' + access_token}
            # Make sure that the Device OPs data is synced
            device.get_device_data()

            # Now apply the rules specified
            for rule in service.access_rule:
                URL = "https://"+device.management_ip_address+"/api/fdm/v2/policy/accesspolicies/c78e66bc-cb57-43fe-bcbf-96b79b3475b3/accessrules"
                r = requests.get(url=URL, headers=headers, verify=False)
                zoneid = service.state.zone[rule.source_zone].id
                portid = service.state.port[rule.source_port].id
                self.log.info('Deployment Exists ', rule.source_zone, ' ', rule.source_port)
                self.log.info('Deployment Exists ', zoneid, ' ', portid)
                URL = "https://"+service.ip_address+"/api/fdm/v2/policy/accesspolicies/c78e66bc-cb57-43fe-bcbf-96b79b3475b3/accessrules"
                payload = {"name": rule.name,
                           "sourceZones": [ {"id": zoneid,
                                             "type": "securityzone"} ],
                           "sourcePorts": [ {"id": portid,
                                             "type": "tcpportobject"} ],
                           "ruleAction": str(rule.action),
                           "eventLogAction": "LOG_NONE",
                           "type": "accessrule" }
                self.log.info(str(payload))
                if not found:
                    r = requests.post(url=URL, headers=headers, verify=False, json=payload )
                self.log.info('Got here 2')
                self.log.info(r.content)


class NGFWBasicService(Service):

    @Service.create
    def cb_create(self, tctx, root, service, proplist):
        self.log.info('Service create(service=', service._path, ')')

        vars = ncs.template.Variables()
        template = ncs.template.Template(service)
        template.apply('esc-ftd-deployment', vars)

        try:
            with ncs.maapi.single_read_trans(tctx.uinfo.username, 'system',
                                              db=ncs.RUNNING) as trans:
                servicetest = ncs.maagic.get_node(trans, service._path)
                self.log.info('Deployment Exists - RUNNING')
        except Exception as e:
            self.log.info('Deployment does not exist!')
            # self.log.info(traceback.format_exc())
            return
        # service = ncs.maagic.get_node(root, kp)
        access_token = getAccessToken(self.log, service)
        headers = {'Content-Type' : 'application/json', 'Accept' : 'application/json', 
                    'Authorization': 'Bearer ' + access_token}
        for rule in service.access_rule:
            URL = "https://"+service.ip_address+"/api/fdm/v2/policy/accesspolicies/c78e66bc-cb57-43fe-bcbf-96b79b3475b3/accessrules"
            r = requests.get(url=URL, headers=headers, verify=False)
            data = r.json()
            found = False
            for item in data['items']:
                if item['name'] == rule.name:
                    found = True
                    self.log.info('Found')
            self.log.info('Got here')
            # with ncs.maapi.single_read_trans(tctx.uinfo.username, tctx.uinfo.context,
            #                             db=ncs.OPERATIONAL) as trans2:
                # opservice = ncs.maagic.get_node(trans2, kp)
            self.log.info('Deployment Exists')
            zoneid = service.state.zone[rule.source_zone].id
            portid = service.state.port[rule.source_port].id
            self.log.info('Deployment Exists ', rule.source_zone, ' ', rule.source_port)
            self.log.info('Deployment Exists ', zoneid, ' ', portid)
            URL = "https://"+service.ip_address+"/api/fdm/v2/policy/accesspolicies/c78e66bc-cb57-43fe-bcbf-96b79b3475b3/accessrules"
            payload = {"name": rule.name,
                       "sourceZones": [ {"id": zoneid,
                                         "type": "securityzone"} ],
                       "sourcePorts": [ {"id": portid,
                                         "type": "tcpportobject"} ],
                       "ruleAction": str(rule.action),
                       "eventLogAction": "LOG_NONE",
                       "type": "accessrule" }
            self.log.info(str(payload))
            if not found:
                r = requests.post(url=URL, headers=headers, verify=False, json=payload )
            self.log.info('Got here 2')
            self.log.info(r.content)
    # The pre_modification() and post_modification() callbacks are optional,
    # and are invoked outside FASTMAP. pre_modification() is invoked before
    # create, update, or delete of the service, as indicated by the enum
    # ncs_service_operation op parameter. Conversely
    # post_modification() is invoked after create, update, or delete
    # of the service. These functions can be useful e.g. for
    # allocations that should be stored and existing also when the
    # service instance is removed.

    # @Service.pre_lock_create
    # def cb_pre_lock_create(self, tctx, root, service, proplist):
    #     self.log.info('Service plcreate(service=', service._path, ')')

    # @Service.pre_modification
    # def cb_pre_modification(self, tctx, op, kp, root, proplist):
    #     self.log.info('Service premod(service=', kp, ')')

    @Service.post_modification
    def cb_post_modification(self, tctx, op, kp, root, proplist):
        self.log.info('Service postmod(service=', kp, ' ', op, ')')


# ---------------------------------------------
# COMPONENT THREAD THAT WILL BE STARTED BY NCS.
# ---------------------------------------------
class Main(ncs.application.Application):
    def setup(self):
        # The application class sets up logging for us. It is accessible
        # through 'self.log' and is a ncs.log.Log instance.
        self.log.info('Main RUNNING')

        # Service callbacks require a registration for a 'service point',
        # as specified in the corresponding data model.
        #
        self.register_service('ftdv-ngfw-servicepoint', NGFWBasicService)
        self.register_service('ftdv-ngfw-advanced-servicepoint', NGFWAdvancedService)
        self.register_action('ftdv-ngfw-getDeviceData-action', GetDeviceData)
        self.register_service('ftdv-ngfw-scalable-servicepoint', ScalableService)
#        self.register_service('ftdv-ngfw-access-rule-servicepoint', AccessRuleService)

        # If we registered any callback(s) above, the Application class
        # took care of creating a daemon (related to the service/action point).

        # When this setup method is finished, all registrations are
        # considered done and the application is 'started'.

    def teardown(self):
        # When the application is finished (which would happen if NCS went
        # down, packages were reloaded or some error occurred) this teardown
        # method will be called.

        self.log.info('Main FINISHED')

provisionPayload = """   {{
      "acceptEULA": true,
"eulaText": "End User License Agreement\n\nEffective: May 22, 2017\n\nThis is an agreement between You and Cisco Systems, Inc. or its affiliates\n(\"Cisco\") and governs your Use of Cisco Software. \"You\" and \"Your\" means the\nindividual or legal entity licensing the Software under this EULA. \"Use\" or\n\"Using\" means to download, install, activate, access or otherwise use the\nSoftware. \"Software\" means the Cisco computer programs and any Upgrades made\navailable to You by an Approved Source and licensed to You by Cisco.\n\"Documentation\" is the Cisco user or technical manuals, training materials,\nspecifications or other documentation applicable to the Software and made\navailable to You by an Approved Source. \"Approved Source\" means (i) Cisco or\n(ii) the Cisco authorized reseller, distributor or systems integrator from whom\nyou acquired the Software. \"Entitlement\" means the license detail; including\nlicense metric, duration, and quantity provided in a product ID (PID) published\non Cisco's price list, claim certificate or right to use notification.\n\"Upgrades\" means all updates, upgrades, bug fixes, error corrections,\nenhancements and other modifications to the Software and backup copies thereof.\n\nThis agreement, any supplemental license terms and any specific product terms\nat www.cisco.com/go/softwareterms (collectively, the \"EULA\") govern Your Use of\nthe Software.\n\n1. Acceptance of Terms. By Using the Software, You agree to be bound by the\nterms of the EULA. If you are entering into this EULA on behalf of an entity,\nyou represent that you have authority to bind that entity. If you do not have\nsuch authority or you do not agree to the terms of the EULA, neither you nor\nthe entity may Use the Software and it may be returned to the Approved Source\nfor a refund within thirty (30) days of the date you acquired the Software or\nCisco product. Your right to return and refund applies only if you are the\noriginal end user licensee of the Software.\n\n2. License. Subject to payment of the applicable fees and compliance with this\nEULA, Cisco grants You a limited, non-exclusive and non-transferable license to\nUse object code versions of the Software and the Documentation solely for Your\ninternal operations and in accordance with the Entitlement and the\nDocumentation. Cisco licenses You the right to Use only the Software You\nacquire from an Approved Source. Unless contrary to applicable law, You are not\nlicensed to Use the Software on secondhand or refurbished Cisco equipment not\nauthorized by Cisco, or on Cisco equipment not purchased through an Approved\nSource. In the event that Cisco requires You to register as an end user, Your\nlicense is valid only if the registration is complete and accurate. The\nSoftware may contain open source software, subject to separate license terms\nmade available with the Cisco Software or Documentation.\n\nIf the Software is licensed for a specified term, Your license is valid solely\nfor the applicable term in the Entitlement. Your right to Use the Software\nbegins on the date the Software is made available for download or installation\nand continues until the end of the specified term, unless otherwise terminated\nin accordance with this Agreement.\n\n3. Evaluation License. If You license the Software or receive Cisco product(s)\nfor evaluation purposes or other limited, temporary use as authorized by Cisco\n(\"Evaluation Product\"), Your Use of the Evaluation Product is only permitted\nfor the period limited by the license key or otherwise stated by Cisco in\nwriting. If no evaluation period is identified by the license key or in\nwriting, then the evaluation license is valid for thirty (30) days from the\ndate the Software or Cisco product is made available to You. You will be\ninvoiced for the list price of the Evaluation Product if You fail to return or\nstop Using it by the end of the evaluation period. The Evaluation Product is\nlicensed \"AS-IS\" without support or warranty of any kind, expressed or implied.\nCisco does not assume any liability arising from any use of the Evaluation\nProduct. You may not publish any results of benchmark tests run on the\nEvaluation Product without first obtaining written approval from Cisco. You\nauthorize Cisco to use any feedback or ideas You provide Cisco in connection\nwith Your Use of the Evaluation Product.\n\n4. Ownership. Cisco or its licensors retain ownership of all intellectual\nproperty rights in and to the Software, including copies, improvements,\nenhancements, derivative works and modifications thereof. Your rights to Use\nthe Software are limited to those expressly granted by this EULA. No other\nrights with respect to the Software or any related intellectual property rights\nare granted or implied.\n\n5. Limitations and Restrictions. You will not and will not allow a third party\nto:\n\na. transfer, sublicense, or assign Your rights under this license to any other\nperson or entity (except as expressly provided in Section 12 below), unless\nexpressly authorized by Cisco in writing;\n\nb. modify, adapt or create derivative works of the Software or Documentation;\n\nc. reverse engineer, decompile, decrypt, disassemble or otherwise attempt to\nderive the source code for the Software, except as provided in Section 16\nbelow;\n\nd. make the functionality of the Software available to third parties, whether\nas an application service provider, or on a rental, service bureau, cloud\nservice, hosted service, or other similar basis unless expressly authorized by\nCisco in writing;\n\ne. Use Software that is licensed for a specific device, whether physical or\nvirtual, on another device, unless expressly authorized by Cisco in writing; or\n\nf. remove, modify, or conceal any product identification, copyright,\nproprietary, intellectual property notices or other marks on or within the\nSoftware.\n\n6. Third Party Use of Software. You may permit a third party to Use the\nSoftware licensed to You under this EULA if such Use is solely (i) on Your\nbehalf, (ii) for Your internal operations, and (iii) in compliance with this\nEULA. You agree that you are liable for any breach of this EULA by that third\nparty.\n\n7. Limited Warranty and Disclaimer.\n\na. Limited Warranty. Cisco warrants that the Software will substantially\nconform to the applicable Documentation for the longer of (i) ninety (90) days\nfollowing the date the Software is made available to You for your Use or (ii)\nas otherwise set forth at www.cisco.com/go/warranty. This warranty does not\napply if the Software, Cisco product or any other equipment upon which the\nSoftware is authorized to be used: (i) has been altered, except by Cisco or its\nauthorized representative, (ii) has not been installed, operated, repaired, or\nmaintained in accordance with instructions supplied by Cisco, (iii) has been\nsubjected to abnormal physical or electrical stress, abnormal environmental\nconditions, misuse, negligence, or accident; (iv) is licensed for beta,\nevaluation, testing or demonstration purposes or other circumstances for which\nthe Approved Source does not receive a payment of a purchase price or license\nfee; or (v) has not been provided by an Approved Source. Cisco will use\ncommercially reasonable efforts to deliver to You Software free from any\nviruses, programs, or programming devices designed to modify, delete, damage or\ndisable the Software or Your data.\n\nb. Exclusive Remedy. At Cisco's option and expense, Cisco shall repair,\nreplace, or cause the refund of the license fees paid for the non-conforming\nSoftware. This remedy is conditioned on You reporting the non-conformance in\nwriting to Your Approved Source within the warranty period. The Approved Source\nmay ask You to return the Software, the Cisco product, and/or Documentation as\na condition of this remedy. This Section is Your exclusive remedy under the\nwarranty.\n\nc. Disclaimer.\n\nExcept as expressly set forth above, Cisco and its licensors provide Software\n\"as is\" and expressly disclaim all warranties, conditions or other terms,\nwhether express, implied or statutory, including without limitation,\nwarranties, conditions or other terms regarding merchantability, fitness for a\nparticular purpose, design, condition, capacity, performance, title, and\nnon-infringement. Cisco does not warrant that the Software will operate\nuninterrupted or error-free or that all errors will be corrected. In addition,\nCisco does not warrant that the Software or any equipment, system or network on\nwhich the Software is used will be free of vulnerability to intrusion or\nattack.\n\n8. Limitations and Exclusions of Liability. In no event will Cisco or its\nlicensors be liable for the following, regardless of the theory of liability or\nwhether arising out of the use or inability to use the Software or otherwise,\neven if a party been advised of the possibility of such damages: (a) indirect,\nincidental, exemplary, special or consequential damages; (b) loss or corruption\nof data or interrupted or loss of business; or (c) loss of revenue, profits,\ngoodwill or anticipated sales or savings. All liability of Cisco, its\naffiliates, officers, directors, employees, agents, suppliers and licensors\ncollectively, to You, whether based in warranty, contract, tort (including\nnegligence), or otherwise, shall not exceed the license fees paid by You to any\nApproved Source for the Software that gave rise to the claim. This limitation\nof liability for Software is cumulative and not per incident. Nothing in this\nAgreement limits or excludes any liability that cannot be limited or excluded\nunder applicable law.\n\n9. Upgrades and Additional Copies of Software. Notwithstanding any other\nprovision of this EULA, You are not permitted to Use Upgrades unless You, at\nthe time of acquiring such Upgrade:\n\na. already hold a valid license to the original version of the Software, are in\ncompliance with such license, and have paid the applicable fee for the Upgrade;\nand\n\nb. limit Your Use of Upgrades or copies to Use on devices You own or lease; and\n\nc. unless otherwise provided in the Documentation, make and Use additional\ncopies solely for backup purposes, where backup is limited to archiving for\nrestoration purposes.\n\n10. Audit. During the license term for the Software and for a period of three\n(3) years after its expiration or termination, You will take reasonable steps\nto maintain complete and accurate records of Your use of the Software\nsufficient to verify compliance with this EULA. No more than once per twelve\n(12) month period, You will allow Cisco and its auditors the right to examine\nsuch records and any applicable books, systems (including Cisco product(s) or\nother equipment), and accounts, upon reasonable advanced notice, during Your\nnormal business hours. If the audit discloses underpayment of license fees, You\nwill pay such license fees plus the reasonable cost of the audit within thirty\n(30) days of receipt of written notice.\n\n11. Term and Termination. This EULA shall remain effective until terminated or\nuntil the expiration of the applicable license or subscription term. You may\nterminate the EULA at any time by ceasing use of or destroying all copies of\nSoftware. This EULA will immediately terminate if You breach its terms, or if\nYou fail to pay any portion of the applicable license fees and You fail to cure\nthat payment breach within thirty (30) days of notice. Upon termination of this\nEULA, You shall destroy all copies of Software in Your possession or control.\n\n12. Transferability. You may only transfer or assign these license rights to\nanother person or entity in compliance with the current Cisco\nRelicensing/Transfer Policy (www.cisco.com/c/en/us/products/\ncisco_software_transfer_relicensing_policy.html). Any attempted transfer or,\nassignment not in compliance with the foregoing shall be void and of no effect.\n\n13. US Government End Users. The Software and Documentation are \"commercial\nitems,\" as defined at Federal Acquisition Regulation (\"FAR\") (48 C.F.R.) 2.101,\nconsisting of \"commercial computer software\" and \"commercial computer software\ndocumentation\" as such terms are used in FAR 12.212. Consistent with FAR 12.211\n(Technical Data) and FAR 12.212 (Computer Software) and Defense Federal\nAcquisition Regulation Supplement (\"DFAR\") 227.7202-1 through 227.7202-4, and\nnotwithstanding any other FAR or other contractual clause to the contrary in\nany agreement into which this EULA may be incorporated, Government end users\nwill acquire the Software and Documentation with only those rights set forth in\nthis EULA. Any license provisions that are inconsistent with federal\nprocurement regulations are not enforceable against the U.S. Government.\n\n14. Export. Cisco Software, products, technology and services are subject to\nlocal and extraterritorial export control laws and regulations. You and Cisco\neach will comply with such laws and regulations governing use, export,\nre-export, and transfer of Software, products and technology and will obtain\nall required local and extraterritorial authorizations, permits or licenses.\nSpecific export information may be found at: tools.cisco.com/legal/export/pepd/\nSearch.do\n\n15. Survival. Sections 4, 5, the warranty limitation in 7(a), 7(b) 7(c), 8, 10,\n11, 13, 14, 15, 17 and 18 shall survive termination or expiration of this EULA.\n\n16. Interoperability. To the extent required by applicable law, Cisco shall\nprovide You with the interface information needed to achieve interoperability\nbetween the Software and another independently created program. Cisco will\nprovide this interface information at Your written request after you pay\nCisco's licensing fees (if any). You will keep this information in strict\nconfidence and strictly follow any applicable terms and conditions upon which\nCisco makes such information available.\n\n17. Governing Law, Jurisdiction and Venue.\n\nIf You acquired the Software in a country or territory listed below, as\ndetermined by reference to the address on the purchase order the Approved\nSource accepted or, in the case of an Evaluation Product, the address where\nProduct is shipped, this table identifies the law that governs the EULA\n(notwithstanding any conflict of laws provision) and the specific courts that\nhave exclusive jurisdiction over any claim arising under this EULA.\n\n\nCountry or Territory     | Governing Law           | Jurisdiction and Venue\n=========================|=========================|===========================\nUnited States, Latin     | State of California,    | Federal District Court,\nAmerica or the           | United States of        | Northern District of\nCaribbean                | America                 | California or Superior\n                         |                         | Court of Santa Clara\n                         |                         | County, California\n-------------------------|-------------------------|---------------------------\nCanada                   | Province of Ontario,    | Courts of the Province of\n                         | Canada                  | Ontario, Canada\n-------------------------|-------------------------|---------------------------\nEurope (excluding        | Laws of England         | English Courts\nItaly), Middle East,     |                         |\nAfrica, Asia or Oceania  |                         |\n(excluding Australia)    |                         |\n-------------------------|-------------------------|---------------------------\nJapan                    | Laws of Japan           | Tokyo District Court of\n                         |                         | Japan\n-------------------------|-------------------------|---------------------------\nAustralia                | Laws of the State of    | State and Federal Courts\n                         | New South Wales         | of New South Wales\n-------------------------|-------------------------|---------------------------\nItaly                    | Laws of Italy           | Court of Milan\n-------------------------|-------------------------|---------------------------\nChina                    | Laws of the People's    | Hong Kong International\n                         | Republic of China       | Arbitration Center\n-------------------------|-------------------------|---------------------------\nAll other countries or   | State of California     | State and Federal Courts\nterritories              |                         | of California\n-------------------------------------------------------------------------------\n\n\nThe parties specifically disclaim the application of the UN Convention on\nContracts for the International Sale of Goods. In addition, no person who is\nnot a party to the EULA shall be entitled to enforce or take the benefit of any\nof its terms under the Contracts (Rights of Third Parties) Act 1999. Regardless\nof the above governing law, either party may seek interim injunctive relief in\nany court of appropriate jurisdiction with respect to any alleged breach of\nsuch party's intellectual property or proprietary rights.\n\n18. Integration. If any portion of this EULA is found to be void or\nunenforceable, the remaining provisions of the EULA shall remain in full force\nand effect. Except as expressly stated or as expressly amended in a signed\nagreement, the EULA constitutes the entire agreement between the parties with\nrespect to the license of the Software and supersedes any conflicting or\nadditional terms contained in any purchase order or elsewhere, all of which\nterms are excluded. The parties agree that the English version of the EULA will\ngovern in the event of a conflict between it and any version translated into\nanother language.\n\n\nCisco and the Cisco logo are trademarks or registered trademarks of Cisco\nand/or its affiliates in the U.S. and other countries. To view a list of Cisco\ntrademarks, go to this URL: www.cisco.com/go/trademarks. Third-party trademarks\nmentioned are the property of their respective owners. The use of the word\npartner does not imply a partnership relationship between Cisco and any other\ncompany. (1110R)\n",
      "currentPassword": "{}",
      "newPassword": "{}",
      "type": "initialprovision"
    }}"""

