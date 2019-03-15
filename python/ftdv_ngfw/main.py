# -*- mode: python; python-indent: 4 -*-
import ncs
from ncs.application import Service
from ncs.dp import Action
import _ncs.dp
import requests 
import traceback
              

class ScalableService(Service):

    @Service.create
    def cb_create(self, tctx, root, service, proplist):
        self.log.info('Service create(service=', service._path, ')')
        self.log.info('Site Name: ', service._parent._parent.name)
        self.log.info('Deployment Name: ', service.deployment_name)

        vars = ncs.template.Variables()
        vars.add("SITE-NAME", service._parent._parent.name);
        vars.add("VNF-DEPLOYMENT-NAME", service.deployment_name);
        vars.add("IMAGE-NAME", root.nfvo.vnfd[root.vnf_manager.vnf_catalog[service.catalog_vnf].descriptor_name]
                                .vdu[root.vnf_manager.vnf_catalog[service.catalog_vnf].descriptor_vdu]
                                .software_image_descriptor.image);
        template = ncs.template.Template(service._parent._parent._parent._parent)
        template.apply('vnf-deployment', vars)

class BasicService(Service):

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
                self.log.info("Deployment Exists - RUNNING")
        except Exception as e:
            self.log.info("Deployment does not exist!")
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
                    self.log.info("Found")
            self.log.info("Got here")
            # with ncs.maapi.single_read_trans(tctx.uinfo.username, tctx.uinfo.context,
            #                             db=ncs.OPERATIONAL) as trans2:
                # opservice = ncs.maagic.get_node(trans2, kp)
            self.log.info("Deployment Exists")
            zoneid = service.state.zone[rule.source_zone].id
            portid = service.state.port[rule.source_port].id
            self.log.info("Deployment Exists ", rule.source_zone, ' ', rule.source_port)
            self.log.info("Deployment Exists ", zoneid, ' ', portid)
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
            self.log.info("Got here 2")
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


def getAccessToken(log, service):
    URL = "https://"+service.ip_address+"/api/fdm/v2/fdm/token"
    # defining a params dict for the parameters to be sent to the API 
#            PARAMS = {'address':location} 
    # sending get request and saving the response as response object 
    payload = {'grant_type': 'password','username': 'admin','password': 'cisco123'}
    headers = {'Content-Type' : 'application/json', 'Accept' : 'application/json'}
    r = requests.post(url=URL, headers=headers, verify=False, json=payload )
#            r = requests.get(url = URL, params = PARAMS, json=payload) 
    # extracting data in json format 
    log.info(r.content)
    data = r.json()
    # log.info(data)
    access_token = data['access_token']
    log.info("AccessToken: ", access_token)
    return access_token

class GetDeviceData(Action):
    '''Test the connectivity with Ping from A devices the B devices both from
       over the regular interface and the Tunnel'''
    @Action.action
    def cb_action(self, uinfo, name, kp, input, output):
        self.log.info('action name: ', name)
        with ncs.maapi.single_write_trans('admin', 'system',
                                          db=ncs.OPERATIONAL) as trans:
            service = ncs.maagic.get_node(trans, kp)
            if service.state.port is not None:
                service.state.port.delete()
            if service.state.zone is not None:
                service.state.zone.delete()

            URL = "https://"+service.ip_address+"/api/fdm/v2/object/tcpports?limit=0"
            access_token = getAccessToken(self.log, service)
            headers = {'Content-Type' : 'application/json', 'Accept' : 'application/json', 
                        'Authorization': 'Bearer ' + access_token}
            r = requests.get(url=URL, headers=headers, verify=False)
            data = r.json()
            self.log.info(data)
            for item in data['items']:
                self.log.info(item['name'], ' ', item['id'])
                port = service.state.port.create(str(item['name']))
                port.id = item['id']

            URL = "https://"+service.ip_address+"/api/fdm/v2/object/securityzones?limit=0"
            # access_token = getAccessToken(self.log)
            headers = {'Content-Type' : 'application/json', 'Accept' : 'application/json', 
                        'Authorization': 'Bearer ' + access_token}
            r = requests.get(url=URL, headers=headers, verify=False)
            data = r.json()
            self.log.info(data)
            for item in data['items']:
                self.log.info(item['name'], ' ', item['id'])
                zone = service.state.zone.create(str(item['name']))
                zone.id = item['id']

            output.result = "Ok"
            trans.apply()


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
        self.register_service('ftdv-ngfw-servicepoint', BasicService)
        self.register_action('ftdv-ngfw-getDeviceData-action', GetDeviceData)
        self.register_service('ftdv-ngfw-scalable-servicepoint', ScalableService)

        # If we registered any callback(s) above, the Application class
        # took care of creating a daemon (related to the service/action point).

        # When this setup method is finished, all registrations are
        # considered done and the application is 'started'.

    def teardown(self):
        # When the application is finished (which would happen if NCS went
        # down, packages were reloaded or some error occurred) this teardown
        # method will be called.

        self.log.info('Main FINISHED')
