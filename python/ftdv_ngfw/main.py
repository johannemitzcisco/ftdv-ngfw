# -*- mode: python; python-indent: 4 -*-
import ncs
import ncs.maapi
from ncs.application import Service, PlanComponent
from ncs.dp import Action
import _ncs.dp
import requests 
import traceback
from time import sleep
import collections
import netaddr
import _ncs

#TODO Handle VNF recovery scenario
#TODO Investigate reactive-redeploy on error condition from NFVO
#TODO API check script needs to be split or adding/deleting to 
# to the actions of the rule needs to be investigated so
# that there is not an immeadiate recovering when the API 
# check fails immeadiately but recovery is supported in future

default_timeout = 600
ftd_api_port = 443

class ScalableService(Service):

    managed = False
    
    @Service.create
    def cb_create(self, tctx, root, service, proplist):
        self.log.info('')
        self.log.info('**** Service create(service=', service._path, ') ****')
        # This data should be valid based on the model
        site = service._parent._parent
        vnf_catalog = root.vnf_manager.vnf_catalog[service.catalog_vnf]
        vnf_deployment_name = service.tenant+'-'+service.deployment_name
        nfv_vnfd = root.nfv.vnfd[vnf_catalog.descriptor_name]
        nfv_deployment_name = site.elastic_services_controller + '-vnf-info-' \
                                + service.deployment_name
        nfv_vm_group = service.deployment_name + '-' + vnf_catalog.descriptor_vdu
        if service.manager.name:
            self.managed = True
        else:
            self.managed = False

        # This is internal service data that is persistant between reactive-re-deploy's
        proplistdict = dict(proplist)
        self.log.info('Service Properties: ', proplistdict)
        # These are for presenting the status and timings of the service deployment
        #  Even if there is a failure or exit early, this data will be written to
        #  the service's operational model
        planinfo = {}
        planinfo['devices'] = {}
        planinfo['failure'] = {}
        planinfo_devices = planinfo['devices']

        try:
            ((vnf_day0_authgroup, vnf_day0_username, vnf_day0_password), 
             (vnf_day1_authgroup, vnf_day1_username, vnf_day1_password)) = getVNFPasswords(self.log, service)
        except Exception as e:
            self.log.error('VNF user/password initialization failed: {}'.format(e))
            self.log.error(traceback.format_exc())
            self.addPlanFailure(planinfo, 'service', 'init')
            service.status_message = 'VNF user/password initialization failed: {}'.format(e)
            raise Exception('VNF authentication initialization failed')

        # Initialize variables for this service deployment run
        nfv_deployment_status = 'Initializing'

        # Every time the service is re-run it starts with a network model just
        # as it was the very first time, this means that any changes that where made
        # in a previous run that need to be preserved must be run again.
        # NSO will detect that we are updating something to the same thing and
        # ignore when when it commits at the end of the service run, but if something
        # is not repeated, it will be considered deleted and NSO will attempt
        # to delete from the model, with all that that implies
        try:
            self.log.info('Site Name: ', site.name)
            self.log.info('Tenant Name: ', service.tenant)
            self.log.info('Deployment Name: ', service.deployment_name)
            self.log.info('Managed: ', self.managed)

            # First step is to make sure that the sites ip address pools are instantiated
            planinfo['ip-addressing'] = 'NOT COMPLETED'
            for network in service.scaling.networks.network:
                site_network = site.networks.network[network.name]
                site_network.resource_pool.name = "{}_{}".format(site.name, network.name)
                output = site_network.initialize_ip_address_pool()
                if 'Error' in output.result:
                    raise Exception(output.result)
            # Calculate the subnet size
            max_vnf_count = root.nfv.vnfd[vnf_catalog.descriptor_name] \
                            .df[vnf_catalog.descriptor_flavor] \
                            .vdu_profile[vnf_catalog.descriptor_vdu] \
                            .max_number_of_instances
            # Allocate IP Addresses
            for network in service.scaling.networks.network:
                site_network = site.networks.network[network.name]
                network.resource_pool_allocation.name = "{}_{}_{}".format(service.tenant, service.deployment_name,
                                                                          network.name)
                inputs = network.resource_pool_allocation.allocate_ip_addresses.get_input()
                inputs.network_keypath = site_network._path
                inputs.allocating_service = ("/vnf-manager/site[name='{}']/vnf-deployment[tenant='{}']"
                                             "[deployment-name='{}']").format(site.name, service.tenant, service.deployment_name)
                inputs.address_count = max_vnf_count
                output = network.resource_pool_allocation.allocate_ip_addresses(inputs)
                if 'Error' in output.result:
                    raise Exception(output.result)
                self.log.info("Network Initialized: ", network.name)

            # Check and see if the ip pools have been instantiated
            planinfo['ip-addressing'] = 'COMPLETED'
            failure = False
            with ncs.maapi.single_read_trans(tctx.uinfo.username, 'itd',
                                      db=ncs.OPERATIONAL) as trans:
                op_root = ncs.maagic.get_root(trans)
                for network in service.scaling.networks.network:
                    try:
                        site_network = site.networks.network[network.name]
                        inputs = network.resource_pool_allocation.check_ready.get_input()
                        inputs.network_keypath = site_network._path
                        output = network.resource_pool_allocation.check_ready(inputs)
                        if 'Not Allocated' in output.result:
                            self.log.info("Network {} not Allocated".format(network.name))
                            planinfo['ip-addressing'] = 'NOT COMPLETED'
                        else:
                            self.log.info("Network {} Allocated".format(network.name))
                    except Exception as e:
                        failure = True
                        self.log.error('Network ip pools initialization failed: {}'.format(e))
                        self.log.error(traceback.format_exc())
            if failure:
                planinfo['ip-addressing'] = 'FAILURE'
                service.status_message = 'IP addressing failed, please check that ip resource pools are not exhausted'
            elif planinfo['ip-addressing'] == 'NOT COMPLETED':
                # There are pools that need to be configured
                self.log.info('Network ip pools are being configured, wait for resource manager to call back')

            # VNF Deployment with Scale Monitors not configured
            planinfo['vnfs-deployed'] = 'NOT COMPLETED'
            try:
                if (self.service_status_good(planinfo) and planinfo['ip-addressing'] == 'COMPLETED'):
                    vars = ncs.template.Variables()
                    vars.add('SITE-NAME', service._parent._parent.name)
                    vars.add('DEPLOYMENT-TENANT', service.tenant)
                    vars.add('DEPLOYMENT-NAME', service.deployment_name)
                    vars.add('DEPLOY-PASSWORD', vnf_day0_password) # admin password to set when deploy
                    vars.add('MONITOR-TYPE', 'alive')
                    vars.add('MONITORS-ENABLED', 'true')
                    vars.add('MONITOR-USERNAME', vnf_day1_username)
                    vars.add('MAX-INSTANCES', nfv_vnfd.df[vnf_catalog.descriptor_flavor]
                                               .vdu_profile[vnf_catalog.descriptor_vdu]
                                               .max_number_of_instances)
                    vars.add('IMAGE-NAME', root.nfv.vnfd[vnf_catalog.descriptor_name]
                                            .sw_image_desc[root.nfv.vnfd[vnf_catalog.descriptor_name]
                                                           .vdu[vnf_catalog.descriptor_vdu]
                                                           .sw_image_desc]
                                            .image);
                    if self.managed:
                        vars.add('MANAGER-IP-ADDRESS', root.devices.device[service.manager.name].address)
                        vars.add('MONITOR-PASSWORD', vnf_day0_password)
                    else:
                        vars.add('MANAGER-IP-ADDRESS', '')
                        vars.add('MONITOR-PASSWORD', vnf_day1_password)

                    # Set the context of the template to /vnf-manager
                    template = ncs.template.Template(service._parent._parent._parent._parent)
                    self.log.info('Applying template: vnf-deployment')
                    template.apply('vnf-deployment', vars)
            except Exception as e:
                self.log.error(e)
                self.log.error(traceback.format_exc())
                self.addPlanFailure(planinfo, 'service', 'vnfs-deployed')

            # Initialize plug-ins
            # Load balancer
            if self.service_status_good(planinfo):
                planinfo['load-balancing-configured'] = 'DISABLED'
                for loadbalancer in service.scaling.load_balance:
                    if str(loadbalancer) != 'ftdv-ngfw:load-balancer':
                        try:
                            service.scaling.load_balance.__getitem__(loadbalancer).initialize()
                            planinfo['load-balancing-configured'] = 'INITIALIZED'
                            service.scaling.load_balance.status = 'Initialized'
                            break
                        except Exception as e:
                            self.log.error(e)
                            self.log.error(traceback.format_exc())
                            self.addPlanFailure(planinfo, 'service', 'load-balancing-configured')
                            service.scaling.load_balance.status == 'Failed'
                            service.status.message = "{} Plug-in failed to initialize".format(loadbalancer)
                if planinfo['load-balancing-configured'] == 'DISABLED':
                    service.scaling.load_balance.status = 'Disabled'
   
            # Check if this is not the initial service call
            with ncs.maapi.single_write_trans(tctx.uinfo.username, 'itd',
                                      db=ncs.OPERATIONAL) as trans:
                try:
                    op_root = ncs.maagic.get_root(trans)
                    # This should only be present after the intial service call
                    op_root.nfv.vnf_info_plan[service.deployment_name]
                    nfv_deployment_status = 'deploying'
                    nfv_deployment_status = op_root.nfv.internal.netconf_deployment_result[nfv_deployment_name].status
                except KeyError:
                    # Deployment plan will not exist on the first pass through the service logic
                    pass
            if planinfo['ip-addressing'] == 'COMPLETED' and nfv_deployment_status == 'Initializing':
                # Service has just been called, have not committed NFVO information yet
                self.log.info('Initial Service Call - wait for NFVO to report back')

            self.log.info("NFVO Deployment Status: ", nfv_deployment_status)

            vm_count = None
            new_vm_count = None
            # VNF deployment exists in NFVO, collect additional information
            if nfv_deployment_status != 'Initializing':
                vm_devices = None
                try:
                    vm_devices = root.nfv.internal.netconf_deployment_result[nfv_deployment_name] \
                                  .vm_group[nfv_vm_group].vm_device
                except KeyError as e:
                    pass
                # This is the number of devices that the service has provisioned possibly from a 
                #  previous re-deploy, initialize if neccessary if this is the first time the service
                #  has been called
                vm_count = int(proplistdict.get('ProvisionedVMCount', 0)) 
                new_vm_count = 0
                if vm_devices is not None: # This will happen during the ip-addressing stage
                    new_vm_count = len(vm_devices) # This is the number of devices that NFVO reports it is aware of
                self.log.info('Current VM Count: '+str(vm_count), ' New VM Count: '+str(new_vm_count))
                # Reset the device tracking
                # Device goes through Not Provisioned -> Not Registered -> Provisioned -> Not Registered -> Provisioned...
                # 'Not Provisioned' devices still have to be initially provisioned, all others will still need
                # to be registered
                if vm_devices is not None:
                    for nfv_device in vm_devices:
                        # Initialize the plan status information for the device
                        planinfo_devices[nfv_device.device_name] = {}
                        # Keep track of Device's and the IP addresses in the service operational model
                        dev_status = []
                        for status in nfv_device.status:
                            self.log.info(status.type)
                            dev_status.append(status.type)
                        self.log.info('Creating Device: ', nfv_device.device_name, ' Status: ', dev_status[0])
                        service_device = service.device.create(nfv_device.device_name)
                        service_device.vm_name = nfv_device.name
                        service_device.vmid = nfv_device.id
                        for network in service.scaling.networks.network:
                            interface_id = nfv_vnfd.vdu[vnf_catalog.descriptor_vdu] \
                                            .int_cpd[network.catalog_descriptor_vdu_id].interface_id
                            device_network = service_device.networks.network.create(network.name)
                            device_network.ip_address = nfv_device.interface[interface_id].ip_address
                            if nfv_vnfd.vdu[vnf_catalog.descriptor_vdu] \
                                            .int_cpd[network.catalog_descriptor_vdu_id].management:
                                device_network.management.create()
                        service_device.status = 'Deploying'
                    all_vnfs_deployed = True
                    all_vnfs_alive = True
                    for nfv_device in vm_devices:
                        planinfo_devices[nfv_device.device_name]['deployed'] = 'NOT COMPLETED'
                        planinfo_devices[nfv_device.device_name]['api-available'] = 'NOT COMPLETED'
                        dev_status = []
                        for status in nfv_device.status:
                            dev_status.append(status.type)
                        if 'deployed' in dev_status:
                            planinfo_devices[nfv_device.device_name]['deployed'] = 'COMPLETED'
                        else:
                            all_vnfs_deployed = False
                        if 'alive' in dev_status:
                            planinfo_devices[nfv_device.device_name]['api-available'] = 'COMPLETED'
                        else:
                            all_vnfs_alive = False
                    if all_vnfs_deployed:
                        planinfo['vnfs-deployed'] = 'COMPLETED'
                    if all_vnfs_alive:
                        planinfo['vnfs-api-available'] = 'COMPLETED'
            self.log.info('==== Service Reactive-Redeploy Properties ====')
            od = collections.OrderedDict(sorted(proplistdict.items()))
            for k, v in od.iteritems(): self.log.info(k, ' ', v)
            for device in service.device:
                for network in device.networks.network:
                    if network.management:
                        self.log.info(device.name, ': ', network.ip_address, ' ', device.status)
            self.log.info('==============================================')

            if nfv_deployment_status == 'failed':
                self.log.info('!! Service failure condition encountered !!')
                self.log.info('Error: ' + nfv_deployment_status.error)
                raise Exception('Error: ' + nfv_deployment_status.error)
                return
            elif nfv_deployment_status == 'error':
                if nfv_deployment_status == 'error':
                    self.addPlanFailure(planinfo, 'service', 'vnfs-deployed')
                    with ncs.maapi.single_read_trans(tctx.uinfo.username, 'itd') as t:
                        service.status_message = str(t.get_elem("/nfv/vnf-info/nfv-rel2-esc:esc/" +
                                                    "vnf-deployment-result{{{} {} {}}}/status/error".format(
                                                    service.tenant, service.deployment_name, 
                                                    site.elastic_services_controller)))
                    raise Exception('VNF Error Condition from NFVO reported: ', service.status_message)

            if self.managed:
                failure = False
                all_vnfs_registered = True
                planinfo['vnfs-registered-with-manager'] = 'NOT COMPLETED'
                if len(service.device) > 0:
                    with ncs.maapi.single_read_trans(tctx.uinfo.username, 'itd',
                                                      db=ncs.RUNNING) as trans:
                        op_root = ncs.maagic.get_root(trans)
                        for device in service.device:
                            planinfo_devices[device.name]['registered-with-manager'] = 'NOT COMPLETED'
                            if planinfo_devices[device.name]['api-available'] == 'COMPLETED':
                                try:
                                    if op_root.devices.device[service.manager.name].config.devices.devicerecords[device.vm_name]:
                                        planinfo_devices[device.name]['registered-with-manager'] = 'COMPLETED'
                                except KeyError as e:
                                    # This is expected when in the manager registration phase
                                    all_vnfs_registered = False
#                                    self.log.error(traceback.format_exc())
                                    pass
                                except Exception as e:
                                    self.log.error(traceback.format_exc())
                                    failure = True
                                    service.status_message = e
                                    self.addPlanFailure(planinfo, device.name, 'registered-with-manager')
                                    self.addPlanFailure(planinfo, 'service', 'vnfs-registered-with-manager')
                            else:
                                all_vnfs_registered = False
                else:
                    all_vnfs_registered = False
                if not failure and all_vnfs_registered:
                    planinfo['vnfs-registered-with-manager'] = 'COMPLETED'

                # Register devices with Manager
                failure = False
                all_vnfs_registration_ready = True
                for device in service.device:
                    if planinfo_devices[device.name]['api-available'] != 'COMPLETED':
                        all_vnfs_registration_ready = False
                if all_vnfs_registration_ready == True:
                    for device in service.device:
                        try:
                            self.log.info('Registering device with manager: ', device.name)
                            vars = ncs.template.Variables()
                            vars.add('TENANT-NAME', service.tenant)
                            vars.add('DEPLOYMENT-NAME', service.deployment_name)
                            vars.add('DEVICE-NAME', device.name)
                            template = ncs.template.Template(site)
                            post_registration_template = None
                            for catalog_template in vnf_catalog.templates.template:
                                if catalog_template.target.manager_type:
                                    if catalog_template.target.manager_type.string == 'device-registration':
                                        registration_template = catalog_template
                                    elif catalog_template.target.manager_type.string == 'device-post-registration':
                                        post_registration_template = catalog_template
                            if registration_template is None:
                                raise Exception('No registration template found for vnf-catalog {}'.format(vnf_catalog.templates.template.name)) 
                            if planinfo_devices[device.name]['registered-with-manager'] == 'COMPLETED' and \
                              post_registration_template is not None:
                                self.log.info('Applying template: ', post_registration_template.name)
                                template.apply(post_registration_template.name, vars)
                            else:
                                self.log.info('Applying template: ', registration_template.name)
                                template.apply(registration_template.name, vars)
                            self.log.info('Device Registration Completed: ', device.name)
                            planinfo_devices[device.name]['registered-with-manager'] = 'COMPLETED'
                        except Exception as e:
                            self.log.error(traceback.format_exc())
                            failure = True
                            service.status_message = e
                            self.addPlanFailure(planinfo, device.name, 'registered-with-manager')
                            self.addPlanFailure(planinfo, 'service', 'vnfs-registered-with-manager')
                    if not failure and len(service.device) > 0:
                        planinfo['vnfs-registered-with-manager'] = 'COMPLETED'

                failure = False
                all_vnfs_synced = True
                planinfo['vnfs-synchronized-with-manager'] = 'NOT COMPLETED'
                for device in service.device:
                    try:
                        with ncs.maapi.single_read_trans(tctx.uinfo.username, 'itd',
                                                     db=ncs.RUNNING) as trans:
                            run_root = ncs.maagic.get_root(trans)
                            try:
                                planinfo_devices[device.name]['synchronized-with-manager'] = 'NOT COMPLETED'
                                planinfo_devices[device.name]['configurable'] = 'NOT COMPLETED'
                                if run_root.devices.device[service.manager.name].config.devices.devicerecords[device.vm_name].physicalinterfaces['Diagnostic0/0'] \
                                 is not None: 
                                    self.log.info('Device Synced with Manager: ', device.name)
                                    planinfo_devices[device.name]['synchronized-with-manager'] = 'COMPLETED'
                                    planinfo_devices[device.name]['configurable'] = 'COMPLETED'
                                    device.status = 'Configurable'
                                    proplistdict['ProvisionedVMCount'] = str(int(proplistdict.get('ProvisionedVMCount', 0)) + 1)
                                else:
                                    self.log.info('Device NOT synced with Manager: ', device.name)
                                    all_vnfs_synced = False
                            except KeyError as error:
#                                self.log.error(traceback.format_exc())
                                self.log.info('Device NOT synced with Manager (Device not registered): ', device.name)
                                all_vnfs_synced = False
                    except Exception as e:
                        failure = True
                        self.log.error(e)
                        self.log.error(traceback.format_exc())
                        self.addPlanFailure(planinfo, device.name, 'synchronized-with-manager')
                        self.addPlanFailure(planinfo, 'service', 'vnfs-synchronized-with-manager')
                    if new_vm_count is not None and new_vm_count !=0 and not failure and all_vnfs_synced:
                        planinfo['vnfs-synchronized-with-manager'] = 'COMPLETED'
            
            if not self.managed:
                # Device should be registered by a trigger on the nfv device being deployed
                # Check to see if the device exists in NSO
                failure = False
                all_vnfs_registered = True
                planinfo['vnfs-registered-with-nso'] = 'NOT COMPLETED'
                for device in service.device:
                    planinfo_devices[device.name]['registered-with-nso'] = 'NOT COMPLETED'
                    try:
                        if root.devices.device[device.name]:
                            self.log.info('Device Registered with NSO: '+device.name)
                            planinfo_devices[device.name]['registered-with-nso'] = 'COMPLETED'
                    except KeyError as e:
                        planinfo_devices[device.name]['registered-with-nso'] = 'COMPLETED'
                        pass        
                    except Exception as e:
                        failure = True
                        self.log.error(e)
                        self.log.error(traceback.format_exc())
                        self.addPlanFailure(planinfo, device.name, 'registered-with-nso')
                        self.addPlanFailure(planinfo, 'service', 'vnfs-registered-with-nso')
                if all_vnfs_registered and len(service.device) > 0 and not failure:
                    planinfo['vnfs-registered-with-nso'] = 'COMPLETED'

                # Do initial provisioning of each device
                # This should happen at NFVO device alive
                failure = False
                all_vnfs_provisioned = True
                planinfo['vnfs-initialized'] = 'NOT COMPLETED'
                with ncs.maapi.single_read_trans(tctx.uinfo.username, 'vnf-manager-provisioned-check',
                                                 db=ncs.RUNNING) as trans:
                    op_root = ncs.maagic.get_root(trans)
                    for device in service.device:
                        try:
                            planinfo_devices[device.name]['initialized'] = 'NOT COMPLETED'
                            if planinfo_devices[device.name]['api-available'] == 'COMPLETED':
                                # Check if device has been provisioned
                                dev = op_root.devices.device[device.name]
                                input = dev.config.cisco_ftd__ftd.actions.generic_call.get_input()
                                input.http_method = 'GET'
                                input.uri = '/policy/accesspolicies'
                                input.body = '{}'
                                output = dev.config.cisco_ftd__ftd.actions.generic_call(input)
                                if 'Failed' in output.result: 
                                    device.provision_ftd_device()
                                root.devices.device[device.name].authgroup = vnf_day1_authgroup
                                planinfo_devices[device.name]['initialized'] = 'COMPLETED'
                                self.log.info('Device Provisioned: '+device.name)
                            else:
                                all_vnfs_provisioned = False
                                self.log.info('Device NOT Provisioned (Device API not available): '+device.name)
                        except Exception as e:
                            failure = True
                            self.log.error(e)
                            self.log.error(traceback.format_exc())
                            self.addPlanFailure(planinfo, device.name, 'initialized')
                            self.addPlanFailure(planinfo, 'service', 'vnfs-initialized')
                if len(service.device) > 0 and not failure and all_vnfs_provisioned:
                    planinfo['vnfs-initialized'] = 'COMPLETED'

                failure = False
                all_vnfs_synced = True
                planinfo['vnfs-synchronized-with-nso'] = 'NOT COMPLETED'
                # Synchronization should happen after synchronization which was triggered by a successful provisioning
                for device in service.device:
                    planinfo_devices[device.name]['synchronized-with-nso'] = 'NOT COMPLETED'
                    planinfo_devices[device.name]['configurable'] = 'NOT COMPLETED'
                    try:
                        if planinfo_devices[device.name]['initialized'] == 'COMPLETED':
                            self.log.info('Syncing Device: ', device.name)
                            root.devices.device[device.name].sync_from()
                            self.log.info('Device Synced: ', device.name)
                            planinfo_devices[device.name]['synchronized-with-nso'] = 'COMPLETED'
                            planinfo_devices[device.name]['configurable'] = 'COMPLETED'
                            proplistdict['ProvisionedVMCount'] = str(int(proplistdict.get('ProvisionedVMCount', 0)) + 1)
                        else:
                            self.log.info('Device NOT synced: ', device.name)
                            all_vnfs_synced = False
                    except Exception as e:
                        self.log.error(e)
                        self.log.error(traceback.format_exc())
                        failure = True
                        self.addPlanFailure(planinfo, device.name, 'synchronized-with-nso')
                        self.addPlanFailure(planinfo, 'service', 'vnfs-synchronized-with-nso')
                if new_vm_count is not None and new_vm_count !=0 and not failure and all_vnfs_synced:
                    planinfo['vnfs-synchronized-with-nso'] = 'COMPLETED'
           
            # Configure Devices
            failure = False
            all_vnfs_configured = True
            planinfo['vnfs-configured'] = 'NOT COMPLETED'
            vars = ncs.template.Variables()
            vars.add('TENANT-NAME', service.tenant);
            vars.add('DEPLOYMENT-NAME', service.deployment_name);
            template = ncs.template.Template(site);
            for device in service.device:
                vars.add('DEVICE-NAME', device.name);
                applied_templates_count = 0
                stage_1_templates_applied = 0
                stage_2_templates_applied = 0
                if False or planinfo_devices[device.name]['configurable'] == 'COMPLETED':
                    for catalog_template in vnf_catalog.templates.template:
                        try:
                            if (self.managed and catalog_template.target.manager_type == 'device-configuration') or \
                             (not self.managed and catalog_template.target.device_type == 'configuration'):
                                self.log.info('Checking Template {}, {}, {}'.format(catalog_template.name,
                                              catalog_template.target.manager_type, catalog_template.stage))
                                if catalog_template.stage == '1':
                                    self.log.info('Applying Configuration {}: {}'.format(catalog_template.name, device.name))
                                    template.apply(catalog_template.name, vars)
                                    stage_1_templates_applied = stage_1_templates_applied + 1
                                    applied_templates_count = applied_templates_count + 1
                                    self.log.info('Configuration Stage-1 Applied: ', device.name)
                                if catalog_template.stage == '2' and \
                                  proplistdict.get(device.name+':configured-stage-1', 'False')  == 'True':
                                    self.log.info('Applying Configuration {}: {}'.format(catalog_template.name, device.name))
                                    template.apply(catalog_template.name, vars)
                                    stage_2_templates_applied = stage_2_templates_applied + 1
                                    applied_templates_count = applied_templates_count + 1
                                    self.log.info('Configuration Stage-1 Applied: ', device.name)
                        except Exception as e:
                            self.log.error(traceback.format_exc())
                            failure = True
                            service.status_message = e
                            self.addPlanFailure(planinfo, device.name, 'configured')
                            self.addPlanFailure(planinfo, 'service', 'vnfs-configured')
                else:
                    all_vnfs_configured = False
                stage_1_templates = len([stage_template for stage_template in vnf_catalog.templates.template \
                                                    if stage_template.stage == '1' \
                                                     and ((self.managed and stage_template.target.manager_type == 'device-configuration') \
                                                     or (not self.managed and stage_template.target.device_type == 'configuration'))])
                stage_2_templates = len([stage_template for stage_template in vnf_catalog.templates.template \
                                                    if stage_template.stage == '2' \
                                                     and ((self.managed and stage_template.target.manager_type == 'device-configuration') \
                                                     or (not self.managed and stage_template.target.device_type == 'configuration'))])
                config_template_count = stage_1_templates + stage_2_templates
                self.log.info('Templates applied: ', str(applied_templates_count), ', Config Template Count: ', str(config_template_count), 
                              ', stage1: ', str(stage_1_templates), ', stage2: ', str(stage_2_templates))
                if planinfo_devices[device.name]['configurable'] == 'COMPLETED':
                    if stage_1_templates_applied == stage_1_templates or stage_1_templates == 0:
                        planinfo_devices[device.name]['configured-stage-1'] = 'COMPLETED'
                        proplistdict[device.name+':configured-stage-1'] = 'True'
                    if stage_2_templates_applied == stage_2_templates or stage_2_templates == 0:
                        proplistdict[device.name+':configured-stage-2'] = 'True'
                        planinfo_devices[device.name]['configured'] = 'COMPLETED'
                    if applied_templates_count != config_template_count:
                        all_vnfs_configured = False
            if not failure and all_vnfs_configured and len(service.device) > 0 \
             and (self.managed or (not self.managed and planinfo['vnfs-synchronized-with-nso'] == 'COMPLETED')):
                planinfo['vnfs-configured'] = 'COMPLETED'

            if self.managed:
                failure = False
                all_vnfs_configurations_deployed = True
                planinfo['vnfs-configurations-deployed'] = 'NOT COMPLETED'
                for device in service.device:
                    try:
                        with ncs.maapi.single_read_trans(tctx.uinfo.username, 'itd',
                                                     db=ncs.RUNNING) as trans:
                            run_root = ncs.maagic.get_root(trans)
                            try:
                                if len(run_root.devices.device[service.manager.name].config.devices \
                                    .devicerecords[device.vm_name].routing.ipv4staticroutes) \
                                 > 0:
                                    self.log.info('Device Config Deployed to Manager: ', device.name)
                                else:
                                    self.log.info('Device Config NOT Deployed to Manager: ', device.name)
                                    all_vnfs_configurations_deployed = False
                            except KeyError as error:
                                self.log.info('Device Config NOT Deployed to Manager (Device not registered): ', device.name)
                                all_vnfs_configurations_deployed = False
                    except Exception as e:
                        failure = True
                        self.log.error(e)
                        self.log.error(traceback.format_exc())
                        self.addPlanFailure(planinfo, 'service', 'vnfs-configurations-deployed')
                    if new_vm_count is not None and new_vm_count !=0 and not failure and all_vnfs_configurations_deployed:
                        planinfo['vnfs-configurations-deployed'] = 'COMPLETED'

            if planinfo['load-balancing-configured'] == 'INITIALIZED' and \
              ((not self.managed and planinfo['vnfs-configured'] == 'COMPLETED') or \
               (self.managed and planinfo['vnfs-configurations-deployed'] == 'COMPLETED')):
                self.log.info('Configuring Load Balancing')
                for loadbalancer in service.scaling.load_balance:
                    self.log.info(loadbalancer)
                    if str(loadbalancer) != 'ftdv-ngfw:load-balancer':
                        try:
                            service.scaling.load_balance.__getitem__(loadbalancer).deploy()
                            service.scaling.load_balance.status == 'Enabled'
                            planinfo['load-balancing-configured'] = 'COMPLETED'
                            self.log.info("Load Balancing Configured")
                            break
                        except Exception as e:
                            self.log.error(e)
                            self.log.error(traceback.format_exc())
                            self.addPlanFailure(planinfo, 'service', 'load-balancing-configured')
                            service.scaling.load_balance.status == 'Failed'
                            service.status.message = "{} Plug-in failed to deploy".format(loadbalancer)
            elif planinfo['load-balancing-configured'] == 'DISABLED':
                self.log.info("Load Balancing Not Used")
                service.scaling.load_balance.status == 'Disabled'
            else:
                self.log.info("Load Balancing Not Configured")

            # Add scaling monitoring when VNFs are provisioned or anytime after Monitoring
            # is initially turned on
            if proplistdict.get('Monitored', 'False') == 'True' or \
              ((not self.managed and planinfo['vnfs-configured'] == 'COMPLETED') or \
               (self.managed and planinfo['vnfs-configurations-deployed'] == 'COMPLETED')):
                # Turn monitoring back on
                vars = ncs.template.Variables()
                vars.add('SITE-NAME', site.name);
                vars.add('DEPLOYMENT-TENANT', service.tenant);
                vars.add('DEPLOYMENT-NAME', service.deployment_name);
                vars.add('DEPLOY-PASSWORD', vnf_day0_password); # admin password to set when deploy
                vars.add('MONITOR-TYPE', 'load')
                vars.add('MONITORS-ENABLED', 'true');
                vars.add('MONITOR-USERNAME', vnf_day1_username);
                vars.add('IMAGE-NAME', root.nfv.vnfd[vnf_catalog.descriptor_name]
                                        .sw_image_desc[root.nfv.vnfd[vnf_catalog.descriptor_name]
                                                       .vdu[vnf_catalog.descriptor_vdu]
                                                       .sw_image_desc]
                                        .image);
                if self.managed:
                    vars.add('MANAGER-IP-ADDRESS', root.devices.device[service.manager.name].address);
                    vars.add('MONITOR-PASSWORD', vnf_day0_password);
                else: 
                    vars.add('MANAGER-IP-ADDRESS', '')
                    vars.add('MONITOR-PASSWORD', vnf_day1_password);
                # Set the context of the template to /vnf-manager
                template = ncs.template.Template(service._parent._parent._parent._parent)
                self.log.info('Applying template: vnf-deployment')
                template.apply('vnf-deployment', vars)
                proplistdict['Monitored'] = 'True'
                planinfo['scaling-monitoring-enabled'] = 'COMPLETED'
                self.log.info('VNF load monitoring Enabled')

            for device in service.device:
                if not self.managed:
#                    if planinfo_devices[device.name]['synchronized-with-nso'] != 'COMPLETED':
#                        self.applySyncDeviceKicker(root, self.log, site, service, device) 
                    if planinfo_devices[device.name]['registered-with-nso'] == 'COMPLETED' and \
                        planinfo_devices[device.name]['synchronized-with-nso'] != 'COMPLETED':
                        # Apply kicker to rerun service once a devices configuration shows up after synchronization
                        self.applyDeviceSyncedKicker(root, self.log, service.deployment_name, 
                                                     site.name, service.tenant, service.deployment_name,
                                                     site.elastic_services_controller, device.name)
                else:
                    if planinfo_devices[device.name]['deployed'] == 'COMPLETED' and \
                     planinfo_devices[device.name]['configurable'] != 'COMPLETED':
                        self.applyDeviceManagedKicker(root, self.log, site, service, device)
            if planinfo['vnfs-configured'] == 'COMPLETED' and \
               planinfo['vnfs-configurations-deployed'] != 'COMPLETED':
                self.applyConfigurationsDeployedKicker(root, self.log, site, service, device)
            
            # Apply kicker to monitor for nfv scaling and recovery events
            self.applyServiceKicker(root, self.log, service.deployment_name, site.name, service.tenant,
                                    service.deployment_name, nfv_deployment_name, 'unmanaged-vm-device')
        except Exception as e:
            self.log.error("Exception Here:")
            self.log.info(e)
            self.log.info(traceback.format_exc())
            service.status = 'Failure'
            proplistdict['Failure'] = 'True'
        finally:
            self.write_plan_data(service, planinfo)
            proplist = [(k,v) for k,v in proplistdict.iteritems()]
            self.log.info('==== Service Reactive-Redeploy Properties ====')
            od = collections.OrderedDict(sorted(proplistdict.items()))
            for k, v in od.iteritems(): self.log.info(k, ' ', v)
            for device in service.device:
                for network in device.networks.network:
                    if network.management:
                        self.log.info(device.name, ': ', network.ip_address, ' ', device.status)
            self.log.info('==============================================')
            self.log.info('Service status will be set to: ', service.status)
            self.log.info('Service message will be set to: ', service.status_message)
            return proplist

    def service_status_good(self, planinfo):
        self.log.debug('Checking service status with: '+str(planinfo))
        if len(planinfo['failure']) == 0:
            self.log.debug('Service Status GOOD: ', len(planinfo['failure']))
            return True
        else:
            self.log.debug('Service Status BAD: ', len(planinfo['failure']))
            return False

    def addPlanFailure(self, planinfo, component, step):
        fail = planinfo['failure'].get(component, list())
        fail.append(step)
        planinfo['failure'][component] = fail

    def write_plan_data(self, service, planinfo):
        self.log.info('Plan Data: ', planinfo)
        self_plan = PlanComponent(service, 'vnf-deployment_'+service.deployment_name, 'ncs:self')
        self_plan.append_state('ncs:init')
        self_plan.append_state('ftdv-ngfw:ip-addressing')
        self_plan.append_state('ftdv-ngfw:vnfs-deployed')
        if not self.managed:
            self_plan.append_state('ftdv-ngfw:vnfs-registered-with-nso')
        self_plan.append_state('ftdv-ngfw:vnfs-api-available')
        if self.managed:
            self_plan.append_state('ftdv-ngfw:vnfs-registered-with-manager')
            self_plan.append_state('ftdv-ngfw:vnfs-synchronized-with-manager')
        if not self.managed:
            self_plan.append_state('ftdv-ngfw:vnfs-initialized')
            self_plan.append_state('ftdv-ngfw:vnfs-synchronized-with-nso')
        self_plan.append_state('ftdv-ngfw:vnfs-configured')
        if self.managed:
            self_plan.append_state('ftdv-ngfw:vnfs-configurations-deployed')
        if planinfo.get('load-balancing-configured', '') != 'DISABLED':
            self_plan.append_state('ftdv-ngfw:load-balancing-configured')
        self_plan.append_state('ftdv-ngfw:scaling-monitoring-enabled')
        self_plan.append_state('ncs:ready')
        self_plan.set_reached('ncs:init')

        if planinfo['failure'].get('service', None) is not None:
            if 'init' in planinfo['failure']['service']:
                self_plan.set_failed('ncs:init')
                service.status = 'Failure'
                return

        service.status = 'Initializing'
        if planinfo.get('ip-addressing', '') == 'COMPLETED':
            self_plan.set_reached('ftdv-ngfw:ip-addressing')
            service.status = 'Deploying'
        if planinfo.get('vnfs-deployed', '') == 'COMPLETED':
            self_plan.set_reached('ftdv-ngfw:vnfs-deployed')
            service.status = 'Starting VNFs'
        if planinfo.get('vnfs-api-available', '') == 'COMPLETED':
              self_plan.set_reached('ftdv-ngfw:vnfs-api-available')
              service.status = 'Registering'
        if not self.managed:
            if planinfo.get('vnfs-registered-with-nso', '') == 'COMPLETED':
                self_plan.set_reached('ftdv-ngfw:vnfs-registered-with-nso')
                service.status = 'Provisioning'
        if self.managed:
            if planinfo.get('vnfs-registered-with-manager', '') == 'COMPLETED':
                self_plan.set_reached('ftdv-ngfw:vnfs-registered-with-manager')
                service.status = 'Provisioned'
            if planinfo.get('vnfs-synchronized-with-manager', '') == 'COMPLETED':
                self_plan.set_reached('ftdv-ngfw:vnfs-synchronized-with-manager')
                service.status = 'Configurable'
        if not self.managed:
            if planinfo.get('vnfs-initialized', '') == 'COMPLETED':
                self_plan.set_reached('ftdv-ngfw:vnfs-initialized')
                service.status = 'Synchronizing'
            if planinfo.get('vnfs-synchronized-with-nso', '') == 'COMPLETED':
                self_plan.set_reached('ftdv-ngfw:vnfs-synchronized-with-nso')
                service.status = 'Configurable'
        if planinfo.get('vnfs-configured', '') == 'COMPLETED':
            self_plan.set_reached('ftdv-ngfw:vnfs-configured')
            service.status = 'Configured'
        if self.managed:
            if planinfo.get('vnfs-configurations-deployed', '') == 'COMPLETED':
                self_plan.set_reached('ftdv-ngfw:vnfs-configurations-deployed')
        if planinfo.get('load-balancing-configured', '') == 'COMPLETED':
            self_plan.set_reached('ftdv-ngfw:load-balancing-configured')
        if planinfo.get('scaling-monitoring-enabled', '') == 'COMPLETED':
            self_plan.set_reached('ftdv-ngfw:scaling-monitoring-enabled')
            if planinfo['failure'].get('service', None) is None:
                self_plan.set_reached('ncs:ready')
                service.status = 'Operational'
        if planinfo['failure'].get('service', None) is not None:
            for failure in planinfo['failure']['service']:
                self.log.info('setting service failure ', 'ftdv-ngfw:'+failure)
                self_plan.set_failed('ftdv-ngfw:'+failure)
                service.status = 'Failure'

        for device in planinfo['devices']:
            self.log.info(("Creating plan for device: {}").format(device))
            device_states = planinfo['devices'][device]
            device_plan = PlanComponent(service, device, 'ftdv-ngfw:vnf')
            device_plan.append_state('ncs:init')
            device_plan.append_state('ftdv-ngfw:deployed')
            if not self.managed:
                device_plan.append_state('ftdv-ngfw:registered-with-nso')
            device_plan.append_state('ftdv-ngfw:api-available')
            if self.managed:
                device_plan.append_state('ftdv-ngfw:registered-with-manager')
                device_plan.append_state('ftdv-ngfw:synchronized-with-manager')
            if not self.managed:
                device_plan.append_state('ftdv-ngfw:initialized')
                device_plan.append_state('ftdv-ngfw:synchronized-with-nso')
            device_plan.append_state('ftdv-ngfw:configurable')
            device_plan.append_state('ftdv-ngfw:configured')
            device_plan.append_state('ncs:ready')
            device_plan.set_reached('ncs:init')

            service.device[device].status = 'Deploying'
            if device_states.get('deployed', '') == 'COMPLETED':
                device_plan.set_reached('ftdv-ngfw:deployed')
                service.device[device].status= 'Starting'
            if not self.managed:
                if device_states.get('registered-with-nso', '') == 'COMPLETED':
                    device_plan.set_reached('ftdv-ngfw:registered-with-nso')
            if device_states.get('api-available', '') == 'COMPLETED':
                device_plan.set_reached('ftdv-ngfw:api-available')
                if self.managed:
                    service.device[device].status = 'Registering'
                else:
                    service.device[device].status = 'Provisioning'
            if self.managed:
                if device_states.get('registered-with-manager', '') == 'COMPLETED':
                    device_plan.set_reached('ftdv-ngfw:registered-with-manager')
                    service.device[device].status = 'Registered'
                if device_states.get('synchronized-with-manager', '') == 'COMPLETED':
                    device_plan.set_reached('ftdv-ngfw:synchronized-with-manager')
                    service.device[device].status = 'Synchronized'
            if not self.managed:
                if device_states.get('initialized', '') == 'COMPLETED':
                    device_plan.set_reached('ftdv-ngfw:initialized')
                    service.device[device].status = 'Synchronizing'
                if device_states.get('synchronized-with-nso', '') == 'COMPLETED':
                    device_plan.set_reached('ftdv-ngfw:synchronized-with-nso')
                    service.device[device].status = 'Synchronized'
            if device_states.get('configurable', '') == 'COMPLETED':
                device_plan.set_reached('ftdv-ngfw:configurable')
                service.device[device].status = 'Configurable'
            if device_states.get('configured-stage-1', '') == 'COMPLETED':
                service.device[device].status = 'Configured-Stage-1'
            if device_states.get('configured', '') == 'COMPLETED':
                device_plan.set_reached('ftdv-ngfw:configured')
                service.device[device].status = 'Configured'
                if planinfo['failure'].get(device, None) is None:
                    device_plan.set_reached('ncs:ready')
                    service.device[device].status = 'Operational'

            if planinfo['failure'].get(device, None) is not None:
                for failure in planinfo['failure'][device]:
                    self.log.info('setting ',device,' failure ', 'ftdv-ngfw:'+failure)
                    device_plan.set_failed('ftdv-ngfw:'+failure)
                    service.device[device].status = 'Failure'

    def applyConfigurationsDeployedKicker(self, root, log, site, service, device):
        kick_monitor_node = "/vnf-manager/site[name='{}']/vnf-deployment[tenant='{}'][deployment-name='{}']".format(
                              site.name, service.tenant, service.deployment_name)
        trigger_expr = "status='ConfigurationsDeployed'"
        kick_node = ("/vnf-manager/site[name='{}']/vnf-deployment[tenant='{}'][deployment-name='{}']").format(
                      site.name, service.tenant, service.deployment_name)
        self.applyKicker(root, log, service.tenant+'-'+service.deployment_name, site.name, service.tenant, service.deployment_name,
                         'reactive-re-deploy', int(device.name[-1])+50, kick_monitor_node, kick_node, 'vnfManaged', trigger_expr)

    def applyDeviceManagedKicker(self, root, log, site, service, device):
        kick_monitor_node = "/vnf-manager/site[name='{}']/vnf-deployment[tenant='{}'][deployment-name='{}']".format(
                              site.name, service.tenant, service.deployment_name)
        trigger_expr = "status='Synchronized'"
        kick_node = ("/vnf-manager/site[name='{}']/vnf-deployment[tenant='{}'][deployment-name='{}']").format(
                      site.name, service.tenant, service.deployment_name)
        self.applyKicker(root, log, service.tenant+'-'+service.deployment_name, site.name, service.tenant, service.deployment_name,
                         'reactive-re-deploy', int(device.name[-1])+40, kick_monitor_node, kick_node, 'vnfManaged', trigger_expr)

    def applySyncDeviceKicker(self, root, log, site, service, device):
        kick_monitor_node = ("/vnf-manager/site[name='{}']/vnf-deployment[tenant='{}'][deployment-name='{}']/device[name='{}']").format(
                              site.name, service.tenant, service.deployment_name, device.name)
        trigger_expr = "status='Synchronizing'"
        kick_node = ("/vnf-manager/site[name='{}']/vnf-deployment[tenant='{}'][deployment-name='{}']/device[name='{}']").format(
                     site.name, service.tenant, service.deployment_name, device.name)
        self.applyKicker(root, log, service.tenant+'-'+service.deployment_name, site.name, service.tenant, service.deployment_name,
                         'sync-vnf-with-nso', int(device.name[-1])+20, kick_monitor_node, kick_node, 'vnfdeviceProvisioned', trigger_expr, device.name)

    def applyDeviceSyncedKicker(self, root, log, vnf_deployment_name, site_name, tenant, service_deployment_name,
                    esc_device_name, device_name):
        kick_monitor_node = ("/vnf-manager/site[name='{}']/vnf-deployment[tenant='{}'][deployment-name='{}']/device[name='{}']").format(
                              site_name, tenant, service_deployment_name, device_name)
        trigger_expr = "status='Synchronized'"
        kick_node = ("/vnf-manager/site[name='{}']/vnf-deployment[tenant='{}'][deployment-name='{}']").format(
                      site_name, tenant, service_deployment_name)
        self.applyKicker(root, log, vnf_deployment_name, site_name, tenant, service_deployment_name,
                         'reactive-re-deploy', int(device_name[-1])+30, kick_monitor_node, kick_node, 'vnfdeviceSynced', trigger_expr)

    def applyServiceKicker(self, root, log, vnf_deployment_name, site_name, tenant, service_deployment_name,
                           nfv_deployment_name, deployment_type):
        kick_monitor_node = ("/nfv/internal/netconf-deployment-result[id='{}']/vm-group").format(nfv_deployment_name) 
        kick_node = ("/vnf-manager/site[name='{}']/vnf-deployment[tenant='{}'][deployment-name='{}']").format(
                      site_name, tenant, service_deployment_name)
        self.applyKicker(root, log, vnf_deployment_name, site_name, tenant, service_deployment_name,
                         'reactive-re-deploy', 100, kick_monitor_node, kick_node, 'NFVODeploymentChange')

    def applyKicker(self, root, log, vnf_deployment_name, site_name, tenant, service_deployment_name, 
                    action_name, priority, kick_monitor_node, kick_node, monitor, trigger_expr=None, device_name=''):
        log.info('Creating Kicker Monitor on: ', action_name, ' ', kick_monitor_node, ' for ', kick_node, ' when ', trigger_expr)
        kicker = root.kickers.data_kicker.create('ftdv_ngfw-{}-{}-{}-{}-{}'.format(monitor, tenant, vnf_deployment_name, device_name, action_name))
        kicker.monitor = kick_monitor_node
        if trigger_expr is not None:
            kicker.trigger_expr = trigger_expr
        kicker.kick_node = kick_node
        kicker.action_name = action_name
        kicker.priority = priority
        kicker.trigger_type = 'enter'

    def provisionFTD(self, ip_address, username, current_password, new_password):
        self.log.info(" Device Provisining Started")
        URL = '/devices/default/action/provision'
        payload = { "acceptEULA": True,
                    "eulaText": "End User License Agreement\n\nEffective: May 22, 2017\n\nThis is an agreement between You and Cisco Systems, Inc. or its affiliates\n(\"Cisco\") and governs your Use of Cisco Software. \"You\" and \"Your\" means the\nindividual or legal entity licensing the Software under this EULA. \"Use\" or\n\"Using\" means to download, install, activate, access or otherwise use the\nSoftware. \"Software\" means the Cisco computer programs and any Upgrades made\navailable to You by an Approved Source and licensed to You by Cisco.\n\"Documentation\" is the Cisco user or technical manuals, training materials,\nspecifications or other documentation applicable to the Software and made\navailable to You by an Approved Source. \"Approved Source\" means (i) Cisco or\n(ii) the Cisco authorized reseller, distributor or systems integrator from whom\nyou acquired the Software. \"Entitlement\" means the license detail; including\nlicense metric, duration, and quantity provided in a product ID (PID) published\non Cisco's price list, claim certificate or right to use notification.\n\"Upgrades\" means all updates, upgrades, bug fixes, error corrections,\nenhancements and other modifications to the Software and backup copies thereof.\n\nThis agreement, any supplemental license terms and any specific product terms\nat www.cisco.com/go/softwareterms (collectively, the \"EULA\") govern Your Use of\nthe Software.\n\n1. Acceptance of Terms. By Using the Software, You agree to be bound by the\nterms of the EULA. If you are entering into this EULA on behalf of an entity,\nyou represent that you have authority to bind that entity. If you do not have\nsuch authority or you do not agree to the terms of the EULA, neither you nor\nthe entity may Use the Software and it may be returned to the Approved Source\nfor a refund within thirty (30) days of the date you acquired the Software or\nCisco product. Your right to return and refund applies only if you are the\noriginal end user licensee of the Software.\n\n2. License. Subject to payment of the applicable fees and compliance with this\nEULA, Cisco grants You a limited, non-exclusive and non-transferable license to\nUse object code versions of the Software and the Documentation solely for Your\ninternal operations and in accordance with the Entitlement and the\nDocumentation. Cisco licenses You the right to Use only the Software You\nacquire from an Approved Source. Unless contrary to applicable law, You are not\nlicensed to Use the Software on secondhand or refurbished Cisco equipment not\nauthorized by Cisco, or on Cisco equipment not purchased through an Approved\nSource. In the event that Cisco requires You to register as an end user, Your\nlicense is valid only if the registration is complete and accurate. The\nSoftware may contain open source software, subject to separate license terms\nmade available with the Cisco Software or Documentation.\n\nIf the Software is licensed for a specified term, Your license is valid solely\nfor the applicable term in the Entitlement. Your right to Use the Software\nbegins on the date the Software is made available for download or installation\nand continues until the end of the specified term, unless otherwise terminated\nin accordance with this Agreement.\n\n3. Evaluation License. If You license the Software or receive Cisco product(s)\nfor evaluation purposes or other limited, temporary use as authorized by Cisco\n(\"Evaluation Product\"), Your Use of the Evaluation Product is only permitted\nfor the period limited by the license key or otherwise stated by Cisco in\nwriting. If no evaluation period is identified by the license key or in\nwriting, then the evaluation license is valid for thirty (30) days from the\ndate the Software or Cisco product is made available to You. You will be\ninvoiced for the list price of the Evaluation Product if You fail to return or\nstop Using it by the end of the evaluation period. The Evaluation Product is\nlicensed \"AS-IS\" without support or warranty of any kind, expressed or implied.\nCisco does not assume any liability arising from any use of the Evaluation\nProduct. You may not publish any results of benchmark tests run on the\nEvaluation Product without first obtaining written approval from Cisco. You\nauthorize Cisco to use any feedback or ideas You provide Cisco in connection\nwith Your Use of the Evaluation Product.\n\n4. Ownership. Cisco or its licensors retain ownership of all intellectual\nproperty rights in and to the Software, including copies, improvements,\nenhancements, derivative works and modifications thereof. Your rights to Use\nthe Software are limited to those expressly granted by this EULA. No other\nrights with respect to the Software or any related intellectual property rights\nare granted or implied.\n\n5. Limitations and Restrictions. You will not and will not allow a third party\nto:\n\na. transfer, sublicense, or assign Your rights under this license to any other\nperson or entity (except as expressly provided in Section 12 below), unless\nexpressly authorized by Cisco in writing;\n\nb. modify, adapt or create derivative works of the Software or Documentation;\n\nc. reverse engineer, decompile, decrypt, disassemble or otherwise attempt to\nderive the source code for the Software, except as provided in Section 16\nbelow;\n\nd. make the functionality of the Software available to third parties, whether\nas an application service provider, or on a rental, service bureau, cloud\nservice, hosted service, or other similar basis unless expressly authorized by\nCisco in writing;\n\ne. Use Software that is licensed for a specific device, whether physical or\nvirtual, on another device, unless expressly authorized by Cisco in writing; or\n\nf. remove, modify, or conceal any product identification, copyright,\nproprietary, intellectual property notices or other marks on or within the\nSoftware.\n\n6. Third Party Use of Software. You may permit a third party to Use the\nSoftware licensed to You under this EULA if such Use is solely (i) on Your\nbehalf, (ii) for Your internal operations, and (iii) in compliance with this\nEULA. You agree that you are liable for any breach of this EULA by that third\nparty.\n\n7. Limited Warranty and Disclaimer.\n\na. Limited Warranty. Cisco warrants that the Software will substantially\nconform to the applicable Documentation for the longer of (i) ninety (90) days\nfollowing the date the Software is made available to You for your Use or (ii)\nas otherwise set forth at www.cisco.com/go/warranty. This warranty does not\napply if the Software, Cisco product or any other equipment upon which the\nSoftware is authorized to be used: (i) has been altered, except by Cisco or its\nauthorized representative, (ii) has not been installed, operated, repaired, or\nmaintained in accordance with instructions supplied by Cisco, (iii) has been\nsubjected to abnormal physical or electrical stress, abnormal environmental\nconditions, misuse, negligence, or accident; (iv) is licensed for beta,\nevaluation, testing or demonstration purposes or other circumstances for which\nthe Approved Source does not receive a payment of a purchase price or license\nfee; or (v) has not been provided by an Approved Source. Cisco will use\ncommercially reasonable efforts to deliver to You Software free from any\nviruses, programs, or programming devices designed to modify, delete, damage or\ndisable the Software or Your data.\n\nb. Exclusive Remedy. At Cisco's option and expense, Cisco shall repair,\nreplace, or cause the refund of the license fees paid for the non-conforming\nSoftware. This remedy is conditioned on You reporting the non-conformance in\nwriting to Your Approved Source within the warranty period. The Approved Source\nmay ask You to return the Software, the Cisco product, and/or Documentation as\na condition of this remedy. This Section is Your exclusive remedy under the\nwarranty.\n\nc. Disclaimer.\n\nExcept as expressly set forth above, Cisco and its licensors provide Software\n\"as is\" and expressly disclaim all warranties, conditions or other terms,\nwhether express, implied or statutory, including without limitation,\nwarranties, conditions or other terms regarding merchantability, fitness for a\nparticular purpose, design, condition, capacity, performance, title, and\nnon-infringement. Cisco does not warrant that the Software will operate\nuninterrupted or error-free or that all errors will be corrected. In addition,\nCisco does not warrant that the Software or any equipment, system or network on\nwhich the Software is used will be free of vulnerability to intrusion or\nattack.\n\n8. Limitations and Exclusions of Liability. In no event will Cisco or its\nlicensors be liable for the following, regardless of the theory of liability or\nwhether arising out of the use or inability to use the Software or otherwise,\neven if a party been advised of the possibility of such damages: (a) indirect,\nincidental, exemplary, special or consequential damages; (b) loss or corruption\nof data or interrupted or loss of business; or (c) loss of revenue, profits,\ngoodwill or anticipated sales or savings. All liability of Cisco, its\naffiliates, officers, directors, employees, agents, suppliers and licensors\ncollectively, to You, whether based in warranty, contract, tort (including\nnegligence), or otherwise, shall not exceed the license fees paid by You to any\nApproved Source for the Software that gave rise to the claim. This limitation\nof liability for Software is cumulative and not per incident. Nothing in this\nAgreement limits or excludes any liability that cannot be limited or excluded\nunder applicable law.\n\n9. Upgrades and Additional Copies of Software. Notwithstanding any other\nprovision of this EULA, You are not permitted to Use Upgrades unless You, at\nthe time of acquiring such Upgrade:\n\na. already hold a valid license to the original version of the Software, are in\ncompliance with such license, and have paid the applicable fee for the Upgrade;\nand\n\nb. limit Your Use of Upgrades or copies to Use on devices You own or lease; and\n\nc. unless otherwise provided in the Documentation, make and Use additional\ncopies solely for backup purposes, where backup is limited to archiving for\nrestoration purposes.\n\n10. Audit. During the license term for the Software and for a period of three\n(3) years after its expiration or termination, You will take reasonable steps\nto maintain complete and accurate records of Your use of the Software\nsufficient to verify compliance with this EULA. No more than once per twelve\n(12) month period, You will allow Cisco and its auditors the right to examine\nsuch records and any applicable books, systems (including Cisco product(s) or\nother equipment), and accounts, upon reasonable advanced notice, during Your\nnormal business hours. If the audit discloses underpayment of license fees, You\nwill pay such license fees plus the reasonable cost of the audit within thirty\n(30) days of receipt of written notice.\n\n11. Term and Termination. This EULA shall remain effective until terminated or\nuntil the expiration of the applicable license or subscription term. You may\nterminate the EULA at any time by ceasing use of or destroying all copies of\nSoftware. This EULA will immediately terminate if You breach its terms, or if\nYou fail to pay any portion of the applicable license fees and You fail to cure\nthat payment breach within thirty (30) days of notice. Upon termination of this\nEULA, You shall destroy all copies of Software in Your possession or control.\n\n12. Transferability. You may only transfer or assign these license rights to\nanother person or entity in compliance with the current Cisco\nRelicensing/Transfer Policy (www.cisco.com/c/en/us/products/\ncisco_software_transfer_relicensing_policy.html). Any attempted transfer or,\nassignment not in compliance with the foregoing shall be void and of no effect.\n\n13. US Government End Users. The Software and Documentation are \"commercial\nitems,\" as defined at Federal Acquisition Regulation (\"FAR\") (48 C.F.R.) 2.101,\nconsisting of \"commercial computer software\" and \"commercial computer software\ndocumentation\" as such terms are used in FAR 12.212. Consistent with FAR 12.211\n(Technical Data) and FAR 12.212 (Computer Software) and Defense Federal\nAcquisition Regulation Supplement (\"DFAR\") 227.7202-1 through 227.7202-4, and\nnotwithstanding any other FAR or other contractual clause to the contrary in\nany agreement into which this EULA may be incorporated, Government end users\nwill acquire the Software and Documentation with only those rights set forth in\nthis EULA. Any license provisions that are inconsistent with federal\nprocurement regulations are not enforceable against the U.S. Government.\n\n14. Export. Cisco Software, products, technology and services are subject to\nlocal and extraterritorial export control laws and regulations. You and Cisco\neach will comply with such laws and regulations governing use, export,\nre-export, and transfer of Software, products and technology and will obtain\nall required local and extraterritorial authorizations, permits or licenses.\nSpecific export information may be found at: tools.cisco.com/legal/export/pepd/\nSearch.do\n\n15. Survival. Sections 4, 5, the warranty limitation in 7(a), 7(b) 7(c), 8, 10,\n11, 13, 14, 15, 17 and 18 shall survive termination or expiration of this EULA.\n\n16. Interoperability. To the extent required by applicable law, Cisco shall\nprovide You with the interface information needed to achieve interoperability\nbetween the Software and another independently created program. Cisco will\nprovide this interface information at Your written request after you pay\nCisco's licensing fees (if any). You will keep this information in strict\nconfidence and strictly follow any applicable terms and conditions upon which\nCisco makes such information available.\n\n17. Governing Law, Jurisdiction and Venue.\n\nIf You acquired the Software in a country or territory listed below, as\ndetermined by reference to the address on the purchase order the Approved\nSource accepted or, in the case of an Evaluation Product, the address where\nProduct is shipped, this table identifies the law that governs the EULA\n(notwithstanding any conflict of laws provision) and the specific courts that\nhave exclusive jurisdiction over any claim arising under this EULA.\n\n\nCountry or Territory     | Governing Law           | Jurisdiction and Venue\n=========================|=========================|===========================\nUnited States, Latin     | State of California,    | Federal District Court,\nAmerica or the           | United States of        | Northern District of\nCaribbean                | America                 | California or Superior\n                         |                         | Court of Santa Clara\n                         |                         | County, California\n-------------------------|-------------------------|---------------------------\nCanada                   | Province of Ontario,    | Courts of the Province of\n                         | Canada                  | Ontario, Canada\n-------------------------|-------------------------|---------------------------\nEurope (excluding        | Laws of England         | English Courts\nItaly), Middle East,     |                         |\nAfrica, Asia or Oceania  |                         |\n(excluding Australia)    |                         |\n-------------------------|-------------------------|---------------------------\nJapan                    | Laws of Japan           | Tokyo District Court of\n                         |                         | Japan\n-------------------------|-------------------------|---------------------------\nAustralia                | Laws of the State of    | State and Federal Courts\n                         | New South Wales         | of New South Wales\n-------------------------|-------------------------|---------------------------\nItaly                    | Laws of Italy           | Court of Milan\n-------------------------|-------------------------|---------------------------\nChina                    | Laws of the People's    | Hong Kong International\n                         | Republic of China       | Arbitration Center\n-------------------------|-------------------------|---------------------------\nAll other countries or   | State of California     | State and Federal Courts\nterritories              |                         | of California\n-------------------------------------------------------------------------------\n\n\nThe parties specifically disclaim the application of the UN Convention on\nContracts for the International Sale of Goods. In addition, no person who is\nnot a party to the EULA shall be entitled to enforce or take the benefit of any\nof its terms under the Contracts (Rights of Third Parties) Act 1999. Regardless\nof the above governing law, either party may seek interim injunctive relief in\nany court of appropriate jurisdiction with respect to any alleged breach of\nsuch party's intellectual property or proprietary rights.\n\n18. Integration. If any portion of this EULA is found to be void or\nunenforceable, the remaining provisions of the EULA shall remain in full force\nand effect. Except as expressly stated or as expressly amended in a signed\nagreement, the EULA constitutes the entire agreement between the parties with\nrespect to the license of the Software and supersedes any conflicting or\nadditional terms contained in any purchase order or elsewhere, all of which\nterms are excluded. The parties agree that the English version of the EULA will\ngovern in the event of a conflict between it and any version translated into\nanother language.\n\n\nCisco and the Cisco logo are trademarks or registered trademarks of Cisco\nand/or its affiliates in the U.S. and other countries. To view a list of Cisco\ntrademarks, go to this URL: www.cisco.com/go/trademarks. Third-party trademarks\nmentioned are the property of their respective owners. The use of the word\npartner does not imply a partnership relationship between Cisco and any other\ncompany. (1110R)\n",
                    "currentPassword": "",
                    "newPassword": "",
                    "type": "initialprovision"
                  }
        payload["currentPassword"] = current_password
        payload["newPassword"] = new_password
        sendRequest(self.log, ip_address, URL, 'POST', payload, username, current_password)
        self.log.info(" Device Provisining Complete")

def getVNFPasswords(log, vnf_deployment):
    site = vnf_deployment._parent._parent
    vnf_manager = site._parent._parent
    root = vnf_manager._parent
    vnf_catalog = vnf_manager.vnf_catalog[vnf_deployment.catalog_vnf]
    day0_authgroup = root.devices.authgroups.group[vnf_catalog.day0_authgroup]
    day0_username = day0_authgroup.default_map.remote_name
    day0_password = _ncs.decrypt(day0_authgroup.default_map.remote_password)
    day1_authgroup = root.devices.authgroups.group[vnf_catalog.day1_authgroup]
    day1_username = day1_authgroup.default_map.remote_name
    day1_password = _ncs.decrypt(day1_authgroup.default_map.remote_password)
    auths = ((day0_authgroup.name, day0_username, day0_password), 
            (day1_authgroup.name, day1_username, day1_password))
    log.info('VNF auths: {}'.format(auths))
    return auths

def sendRequest(log, ip_address, url_suffix, device_type='ftd', version='latest', operation='GET', json_payload=None, username='admin', password=''):
    access_token = getAccessToken(log, ip_address, username, password)
    if device_type == 'ftd':
        URL = 'https://{}/api/{}/{}{}'.format(ip_address, device_type, version, url_suffix)
    elif device_type == 'fmc':
        URL = 'https://{}/fmc_config/{}{}'.format(ip_address, device_type, version, url_suffix)
    headers = {'Content-Type' : 'application/json', 'Accept' : 'application/json', 
            'Authorization': 'Bearer ' + access_token}
    if operation == 'GET':
        log.info('Sending GET: ', URL)
        response = requests.get(url=URL, headers=headers, verify=False)
    elif operation == 'POST':
        log.info('Sending POST: ', URL)
        response = requests.post(url=URL, headers=headers, verify=False, json=json_payload )
    elif operation == 'DELETE':
        log.info('Sending DELETE: ', URL)
        response = requests.delete(url=URL, headers=headers, verify=False)
    else:
        raise Exception('Unknown Operation: {}'.format(operation))

    log.info('Response Status: ', response.status_code)
    if response.status_code == requests.codes.ok \
        or (response.status_code == 204 and response.text == ''):
        return response
    else:
        log.error('Error Response: ', response.text)
        log.error('Request Payload: ', json_payload)
        raise Exception('Bad status code: {}'.format(response.status_code))

def getAccessToken(log, ip_address, username, password, device_type='ftd'):
    if device_type == 'ftd':
        URL = 'https://{}/api/fdm/latest/fdm/token'.format(ip_address)
        payload = {'grant_type': 'password','username': username,'password': password}
    elif device_type == 'fmc':
        URL = 'https://{}/fmc_platform/v1/auth/generatetoken'.format(ip_address)
        payload = ''
    else:
        raise Exception('Bad device type: {}'.format(device_type))
    
    headers = {'Content-Type' : 'application/json', 'Accept' : 'application/json'}
    login_wait_increment = 10
    login_wait_time = 15
    progressive_multiplier = 1
    timeout = 60
    while (True):
        response = requests.post(url=URL, headers=headers, verify=False, json=payload )
        if response.status_code == requests.codes.ok:
            if device_type == 'ftd':
                data = response.json()
                access_token = data['access_token']
                log.debug('AccessToken: ', access_token)
                return access_token
            else:
                access_token = response.headers['X-auth-access-token']
                log.debug('AccessToken: ', access_token)
                return access_token
        else:
            response_json = response.json()
            if response_json['message'].startswith('Too many failed attempts') and login_wait_time < timeout:
                log.info('Login failed, wait for it to reset {} seconds'.format(login_wait_time))
                login_wait_time = login_wait_time + (progressive_multiplier * login_wait_increment)
                progressive_multiplier = progressive_multiplier + 1 
                sleep(login_wait_time)
            else:
                log.error('Error Response:', response.text)
                raise Exception('Bad status code: {}'.format(response.status_code))

def commitDeviceChanges(log, ip_address, device_type='ftd', timeout=default_timeout):
    if device_type == 'ftd':
        URL = '/operational/deploy'
    elif device_type == 'fmc':
        URL = '/domain/deployment/deploymentrequests/{}'.format(device_uuid)
    
    response = sendRequest(log, ip_address, URL, device_type, 'v1', 'POST', password='Admin123')
    log.debug(response.text)
    data = response.json()
    commit_id = data['id']
    if device_type == 'ftd':
        URL = '/operational/deploy/{}'.format(commit_id)
    else:
        URL = '/domain/default/job/taskstatuses/{}'.format(commit_id)
    wait_time = 5
    wait_increment = 5
    progressive_multiplier = 1
    elapsed_time = 0
    while (True):
        response = sendRequest(log, ip_address, URL)
        data = response.json()
        if device_type == 'ftd':
            state = data['state']
        log.info('commit change state: {}'.format(state))
        if state == 'DEPLOYED':
            log.info('Deploy time: ', elapsed_time)
            break
        elif elapsed_time < timeout:
            log.info('Elapsed wait time: {}, wait {} seconds to check status of device commit'.format(timeout, wait_time))
            wait_time = wait_time + (progressive_multiplier * wait_increment)
            progressive_multiplier = progressive_multiplier + 1 
            sleep(wait_time)
            elapsed_time = elapsed_time + wait_time
        else:
            log.error('Commit device change wait time ({}) exceeded'.format(timeout))
            raise Exception('Commit device change wait time ({}) exceeded'.format(timeout))

def addDeviceUser(log, transaction, device, username, password):
    URL = '/object/users'
    payload = { "name": "",
                "identitySourceId": "e3e74c32-3c03-11e8-983b-95c21a1b6da9",
                "password": "",
                "type": "user",
                "userRole": "string",
                "userServiceTypes": [
                    "RA_VPN"
                ]
              }
    payload['name'] = username
    payload['password'] = password
    root = ncs.maagic.get_root(transaction)
    service = device._parent._parent
    catalog_vnf = root.vnf_manager.vnf_catalog[service.catalog_vnf]
    vnf_day1_authgroup = root.devices.authgroups.group[catalog_vnf.authgroup]
    vnf_day1_username = vnf_day1_authgroup.default_map.remote_name
    vnf_day1_password = _ncs.decrypt(vnf_day1_authgroup.default_map.remote_password)
    response = sendRequest(log, device.networks.network['Management'].ip_address, URL, 'POST', payload,
                           username=vnf_day1_username, password=vnf_day1_password)
    getDeviceData(log, device, trans)
    return response

def deleteDeviceUser(log, transaction, device, userid):
    URL = '/object/users/{}'.format(userid)
    root = ncs.maagic.get_root(transaction)
    service = device._parent._parent
    catalog_vnf = root.vnf_manager.vnf_catalog[service.catalog_vnf]
    vnf_day1_authgroup = root.devices.authgroups.group[catalog_vnf.authgroup]
    vnf_day1_username = vnf_day1_authgroup.default_map.remote_name
    vnf_day1_password = _ncs.decrypt(vnf_day1_authgroup.default_map.remote_password)
    response = sendRequest(log, device.networks.network['Management'].ip_address, URL, 'DELETE',
                           username=vnf_day1_username, password=vnf_day1_password)
    # commitDeviceChanges(log, device.networks.network['Management'].ip_address)
    getDeviceData(log, device, transaction)
    log.info('User delete complete')
    return response

def getDeviceData(log, device, trans):
    if device.state.port is not None:
        device.state.port.delete()
    if device.state.zone is not None:
        device.state.zone.delete()
    if device.state.user is not None:
        device.state.user.delete()

    catalog_vnf_name = device._parent._parent.catalog_vnf
    root = ncs.maagic.get_root(trans)
    authgroup_name = root.vnf_manager.vnf_catalog[catalog_vnf_name].authgroup
    vnf_authgroup = root.devices.authgroups.group[authgroup_name]
    vnf_username = vnf_authgroup.default_map.remote_name
    vnf_password = _ncs.decrypt(vnf_authgroup.default_map.remote_password)
    URL = '/object/tcpports?limit=0'
    response = sendRequest(log, device.networks.network['Management'].ip_address, URL, username=vnf_username, password=vnf_password)
    data = response.json()
    log.debug(data)
    for item in data['items']:
        log.debug(item['name'], ' ', item['id'])
        port = device.state.port.create(str(item['name']))
        port.id = item['id']
    URL = '/object/securityzones?limit=0'
    response = sendRequest(log, device.networks.network['Management'].ip_address, URL, username=vnf_username, password=vnf_password)
    data = response.json()
    log.debug(data)
    for item in data['items']:
        log.debug(item['name'], ' ', item['id'])
        zone = device.state.zone.create(str(item['name']))
        zone.id = item['id']
    URL = '/object/users'
    response = sendRequest(log, device.networks.network['Management'].ip_address, URL, username=vnf_username, password=vnf_password)
    data = response.json()
    log.debug(data)
    for item in data['items']:
        log.debug(item['name'], ' ', item['id'])
        user = device.state.user.create(str(item['name']))
        user.id = item['id']

class DeployManagerConfigurations(Action):
    @Action.action
    def cb_action(self, uinfo, name, kp, input, output):
        self.log.info('*************************************** action name: ', name)
        _ncs.dp.action_set_timeout(uinfo,600)
        result = 'Failed'      
        try:
            with ncs.maapi.single_write_trans(uinfo.username, 'vnf-manager',
                                             db=ncs.RUNNING) as trans:
                service_manager = ncs.maagic.get_node(trans, kp)
                service = service_manager._parent
                service_devices = service.device
                root = ncs.maagic.get_root(trans)
                manager = root.devices.device[service_manager.name]
                task_url = {}
                for device in service_devices:
                    URL = 'https://{}/api/fmc_platform/v1/auth/generatetoken'.format(manager.address)
                    self.log.info(URL)
                    headers = {'Content-Type' : 'application/json', 'Accept' : 'application/json'}
                    login_wait_increment = 10
                    login_wait_time = 15
                    progressive_multiplier = 1
                    timeout = 120
                    while (True):
                        response = requests.post(url=URL, headers=headers, verify=False, auth=('nsouser', 'cisco123'))
                        self.log.info('Response Code: {}:'.format(response.status_code))
                        if response.status_code == 204:
                            access_token = response.headers['X-auth-access-token']
                            self.log.info('AccessToken: ', access_token)
                            break
                        elif response.status_code == 401:
                            raise Exception('Bad credentials')
                        else:
                            response_json = response.json()
                            self.log.info(response_json)
                            if response_json['message'].startswith('Too many failed attempts') and login_wait_time < timeout:
                                self.log.info('Login failed, wait for it to reset {} seconds'.format(login_wait_time))
                                login_wait_time = login_wait_time + (progressive_multiplier * login_wait_increment)
                                progressive_multiplier = progressive_multiplier + 1 
                                sleep(login_wait_time)
                            else:
                                self.log.error('Error Response:', response.text)
                                raise Exception('Bad status code: {}'.format(response.status_code))
                   
                    URL = 'https://{}/api/fmc_config/v1/domain/default/deployment/deployabledevices?expanded=true'.format(manager.address)
                    self.log.info(URL)
                    headers = {'Content-Type' : 'application/json', 'Accept' : 'application/json',
                               'X-auth-access-token' : access_token}
                    response = requests.get(url=URL, headers=headers, verify=False)
                    self.log.info('Response Status: ', response.status_code)
                    data = response.json()
                    version = None
                    if response.status_code != 200:
                        raise Exception('Bad status code: {}'.format(response.status_code))
                    else:
                        data = response.json()
                        for item in data['items']:
                            if item['name'] == device.vm_name:
                                version = item['version']
                                deploy_device = [item['device']['id']]
                                break
                    if version is None:
                        result = 'No Deployment Needed'
                        return
                    deployment_json = {
                       "type": "DeploymentRequest",
                       "version": version,
                       "forceDeploy": False,
                       "ignoreWarning": True,
                       "deviceList": deploy_device }
                    URL = 'https://{}/api/fmc_config/v1/domain/default/deployment/deploymentrequests'.format(manager.address)
                    self.log.info(URL)
                    headers = {'Content-Type' : 'application/json', 'Accept' : 'application/json',
                               'X-auth-access-token' : access_token}
                    response = requests.post(url=URL, headers=headers, verify=False, json=deployment_json)
                    self.log.info('Response Status: ', response.status_code)
                    if response.status_code != 202:
                        self.log.error('Error Response: ', response.text)
                        raise Exception('Failed to deploy, Bad status code: {}'.format(response.status_code))
                    self.log.info(response.text)
                    data = response.json()
                    task_url[device.vm_name] = data['metadata']['task']['links']['self']
                wait_time = 5
                wait_increment = 5
                progressive_multiplier = 1
                elapsed_time = 0
                while (len(task_url) > 0):
                    for device in service_devices:
                        URL = task_url[device.vm_name]
                        self.log.info(URL)
                        headers = {'Content-Type' : 'application/json', 'Accept' : 'application/json',
                                  'X-auth-access-token' : access_token}
                        response = requests.get(url=URL, headers=headers, verify=False)
                        self.log.info('Response Status: ', response.status_code)
                        if response.status_code == 200:
                            response.json()['status'] == 'Deployed'
                            self.log.info('Deploy time ({}): {}'.format(device.vm_name, elapsed_time))
                            del task_url[device.vm_name]
                            if (len(task_url) == 0):
                                break
                    if elapsed_time < timeout:
                        self.log.info('Elapsed: {}, Max: {}, wait {} seconds to check status'.format(elapsed_time, timeout, wait_time))
                        wait_time = wait_time + (progressive_multiplier * wait_increment)
                        progressive_multiplier = progressive_multiplier + 1
                        sleep(wait_time)
                        elapsed_time = elapsed_time + wait_time
                    else:
                        self.log.error('Commit device change wait time ({}) exceeded'.format(timeout))
                        raise Exception('Commit device change wait time ({}) exceeded'.format(timeout))
                result = 'Success'
                service.status = 'ConfigurationsDeployed'
                trans.apply()
        except Exception as error:
            self.log.info(traceback.format_exc())
            result = 'Error Deploying Configrations on Manager: ' + str(error)
        finally:
            output.result = result

class ConfigureDevice(Action):
    @Action.action
    def cb_action(self, uinfo, name, kp, input, output):
        self.log.info('*************************************** action name: ', name)
        _ncs.dp.action_set_timeout(uinfo,600)
        try:
            with ncs.maapi.single_write_trans(uinfo.username, 'test',
                                             db=ncs.RUNNING) as trans:
                service_device = ncs.maagic.get_node(trans, kp)
                self.log.info('Configuring Device: '+service_device.name)
                service = service_device._parent._parent
                vnf_manager = service._parent._parent
                root = ncs.maagic.get_root(trans)
                vnf_catalog = root.vnf_manager.vnf_catalog[service.catalog_vnf]
                for template_name in vnf_catalog.templates.template:
                    vars = ncs.template.Variables()
                    vars.add('DEVICE-NAME', service_device.name);
                    vars.add('SERVICE-NAME', service.name);
                    template = ncs.template.Template(vnf_catalog)
                    if target.device-type or target.manager-manager:
                        if target.manger-type == "device-configuration" \
                         or arget.device-type == "configuration":
                            self.log.info('Applying template: ', template_name)
                            template.apply(template_name, vars)
                trans.apply()
                result = 'Configuration Applied;'
                self.log.info('Configuration Applied: '+service_device.name)
        except Exception as error:
            self.log.info(traceback.format_exc())
            result = 'Error Configuring Device: ' + str(error)
            return
        finally:
            output.result = result

class SyncManagerWithNSO(Action):
    @Action.action
    def cb_action(self, uinfo, name, kp, input, output):
        self.log.info('*************************************** action name: ', name)
        _ncs.dp.action_set_timeout(uinfo,600)
        try:
            with ncs.maapi.single_write_trans(uinfo.username, 'vnf-manager',
                                             db=ncs.OPERATIONAL) as trans:
                service_device_manager = ncs.maagic.get_node(trans, kp)
                service = service_device_manager._parent
                self.log.info('Syncing Device: '+service_device_manager.name)
                op_root = ncs.maagic.get_root(trans)
                device = op_root.devices.device[service_device_manager.name]
                sync_output = device.sync_from()
                result = str(sync_output.result)
                self.log.info('Sync Result: '+result)
        except Exception as error:
            self.log.info(traceback.format_exc())
            result = 'Error Syncing Device: ' + str(error)
            return
        finally:
            output.result = result
        try:
            with ncs.maapi.single_write_trans(uinfo.username, 'vnf-manager',
                                              db=ncs.RUNNING) as trans:
                manager_device = ncs.maagic.get_node(trans, kp)
                service = manager_device._parent
                self.log.info('Reporting Manager Synced: '+manager_device.name)
                service.status = 'Synchronized'
                trans.apply()
        except Exception as error:
            self.log.info(traceback.format_exc())
            result = 'Error Syncing Device: ' + str(error)
        finally:
            output.result = result


class ProvisionFTDDevice(Action):
    @Action.action
    def cb_action(self, uinfo, name, kp, input, output):
        self.log.info('*************************************** action name: ', name)
        result = "Device Provisioned Successful"
        try:
            with ncs.maapi.single_write_trans(uinfo.username, 'ftd',
                                             db=ncs.RUNNING) as trans:
                device = ncs.maagic.get_node(trans, kp)
                service = device._parent._parent
                root = ncs.maagic.get_root(trans)
                ((vnf_day0_authgroup, vnf_day0_username, vnf_day0_password), 
                 (vnf_day1_authgroup, vnf_day1_username, vnf_day1_password)) = getVNFPasswords(self.log, service)
                self.log.info('Provisioning Device: '+device.name)
                dev = root.devices.device[device.name]
                input = dev.config.cisco_ftd__ftd.actions.provision.get_input()
                input.acceptEULA = True
                input.currentPassword = vnf_day0_password
                input.newPassword = vnf_day1_password
                output = dev.config.cisco_ftd__ftd.actions.provision(input)
                dev.authgroup = vnf_day1_authgroup
                self.log.info('Device Provisioned: '+device.name)
                trans.apply()
                service.reactive_re_deploy()
        except Exception as error:
            self.log.info(traceback.format_exc())
            result = 'Error Provisioning Device: ' + str(error)
        finally:
            output.result = result

class SyncVNFWithNSO(Action):
    @Action.action
    def cb_action(self, uinfo, name, kp, input, output):
        self.log.info('*************************************** action name: ', name)
        result = "Device Synchronization Successful"
        try:
            with ncs.maapi.single_write_trans(uinfo.username, 'syncdevicewithNSO',
                                             db=ncs.RUNNING) as trans:
                service_device = ncs.maagic.get_node(trans, kp)
                self.log.info('Syncing Device: '+service_device.name)
                op_root = ncs.maagic.get_root(trans)
                device = op_root.devices.device[service_device.name]
                result = device.sync_from()
        except Exception as error:
            self.log.info(traceback.format_exc())
            result = 'Error Syncing Device: ' + str(error)
            return
        finally:
            output.result = result
        try:
            with ncs.maapi.single_write_trans(uinfo.username, 'test',
                                              db=ncs.OPERATIONAL) as trans:
                service_device = ncs.maagic.get_node(trans, kp)
                self.log.info('Reporting Device Synced: '+service_device.name)
                service_device.status = 'Synchronized'
                trans.apply()
        except Exception as error:
            self.log.info(traceback.format_exc())
            result = 'Error Syncing Device: ' + str(error)
        finally:
            output.result = result

class DeregisterVNFWithNSO(Action):
    @Action.action
    def cb_action(self, uinfo, name, kp, input, output):
        self.log.info('*************************************** action name: ', name)
        try:
            with ncs.maapi.single_write_trans(uinfo.username, uinfo.context,
                                              db=ncs.RUNNING) as trans:
                device = ncs.maagic.get_node(trans, kp)
                del root.devices.device[device.name]
                result = "Sucess"
        except Exception as error:
            self.log.info(traceback.format_exc())
            result = 'Error Deregistering Device: ' + str(error)
        finally:
            output.result = result

class RegisterVNFWithNSO(Action):
    @Action.action
    def cb_action(self, uinfo, name, kp, input, output):
        self.log.info('*************************************** action name: ', name)
        try:
            with ncs.maapi.single_write_trans(uinfo.username, uinfo.context,
                                              db=ncs.RUNNING) as trans:
                device = ncs.maagic.get_node(trans, kp)
                self.log.info('Registering Device: '+device.name)
                root = ncs.maagic.get_root(trans)
                service = device._parent._parent
                vnf_catalog = root.vnf_manager.vnf_catalog[service.catalog_vnf]
                ((vnf_day0_authgroup, vnf_day0_username, vnf_day0_password), 
                 (vnf_day1_authgroup, vnf_day1_username, vnf_day1_password)) = getVNFPasswords(self.log, service)
                vars = ncs.template.Variables()
                vars.add('DEVICE-NAME', device.name)
                for network in device.networks.network:
                    if network.management.exists():
                        vars.add('IP-ADDRESS', network.ip_address)
                        break
                vars.add('PORT', ftd_api_port);
                vars.add('AUTHGROUP', day0_authgroup);
                template = ncs.template.Template(device)
                for catalog_template in vnf_catalog.templates.template:
                    if catalog_template.target.device_type == 'registration':
                        self.log.info('Applying template: ', catalog_template_name)
                        template.apply(catalog_template.name, vars)
                        break;
                trans.apply()
                result = "Device Registration Successful"
                service.reactive_re_deploy()
        except Exception as error:
            self.log.info(traceback.format_exc())
            result = 'Error Registering Device: ' + str(error)
        finally:
            output.result = result

class DeleteDeviceUser(Action):
    @Action.action
    def cb_action(self, uinfo, name, kp, input, output):
        self.log.info('action name: ', name)
        try:
            with ncs.maapi.single_write_trans(uinfo.username, uinfo.context,
                                              db=ncs.RUNNING) as trans:
                device = ncs.maagic.get_node(trans, kp)
                if device.state.user[input.username] is None:
                    raise Exception('User {} not valid'.format(input.username))
                userid = device.state.user[input.username].id
                deleteDeviceUser(self.log, trans, device, userid)
                result = "User Deleted"
                trans.apply()
        except Exception as error:
            self.log.info(traceback.format_exc())
            result = 'Error Deleting User: ' + str(error)
        finally:
            output.result = result

class AddDeviceUser(Action):
    @Action.action
    def cb_action(self, uinfo, name, kp, input, output):
        self.log.info('action name: ', name)

        try:
            with ncs.maapi.single_write_trans(uinfo.username, uinfo.context,
                                              db=ncs.RUNNING) as trans:
                device = ncs.maagic.get_node(trans, kp)
                addDeviceUser(self.log, trans, device, input.username, input.password)
                result = "User Added"
                trans.apply()
        except Exception as error:
            self.log.info(traceback.format_exc())
            result = 'Error Adding User: ' + str(error)
        finally:
            output.result = result

class GetDeviceData(Action):
    @Action.action
    def cb_action(self, uinfo, name, kp, input, output):
        self.log.info('action name: ', name)

        maapi = ncs.maapi.Maapi()
        maapi.attach2(0, 0, uinfo.actx_thandle)
        trans = ncs.maapi.Transaction(maapi, uinfo.actx_thandle)
        device = ncs.maagic.get_node(trans, kp)
        getDeviceData(self.log, device, trans)
        output.result = "Ok"

class NGFWAdvancedService(Service):
    @Service.create
    def cb_create(self, tctx, root, service, proplist):
        self.log.info('Service create(service=', service._path, ')')
        proplistdict = dict(proplist)
        planinfo = {}
        try:
            # Deploy the VNF(s) using vnf-manager
            vars = ncs.template.Variables()
            template = ncs.template.Template(service)
            template.apply('vnf-manager-vnf-deployment', vars)
            # Check VNF-Manger service deployment status
            status = 'Unknown'
            with ncs.maapi.single_read_trans(tctx.uinfo.username, 'itd',
                                      db=ncs.OPERATIONAL) as trans:
                try:
                    op_root = ncs.maagic.get_root(trans)
                    deployment = op_root.vnf_manager.site[service.site].vnf_deployment[service.tenant, service.deployment_name]
                    status = deployment.status
                except KeyError:
                     # Service has just been called, have not committed NFVO information yet
                    self.log.info('Initial Service Call - wait for vnf-manager to report back')
                    pass
                self.log.info('VNF-Manager deployment status: ', status)
                if status == 'Failure':
                    planinfo['failure'] = 'vnfs-deployed'
                    return
                if status != 'Configurable':
                    return proplist
                planinfo['vnfs-deployed'] = 'COMPLETED'
                # Apply policies
                # TODO: This will be replaced with a template against the FTD NED when available
                for device in op_root.vnf_manager.site[service.site].vnf_deployment[service.tenant, service.deployment_name] \
                                .device:
                    self.log.info('Configuring device: ', device.name)
                    # Now apply the rules specified in the service by the user
                    for rule in service.access_rule:
                        zoneid = device.state.zone[rule.source_zone].id
                        portid = device.state.port[rule.source_port].id
                        url_suffix = '/policy/accesspolicies/c78e66bc-cb57-43fe-bcbf-96b79b3475b3/accessrules'
                        payload = {"name": rule.name,
                                   "sourceZones": [ {"id": zoneid,
                                                     "type": "securityzone"} ],
                                   "sourcePorts": [ {"id": portid,
                                                     "type": "tcpportobject"} ],
                                   "ruleAction": str(rule.action),
                                   "eventLogAction": "LOG_NONE",
                                   "type": "accessrule" }
                        try:
                            response = sendRequest(self.log, device.networks.network['Management'].ip_address, url_suffix, 'POST', payload)
                        except Exception as e:
                            if str(e) == 'Bad status code: 422':
                                self.log.info('Ignoring: ', e, ' for now as it is probably an error on applying the same rule twice')
                            else:
                                planinfo['failure'] = 'vnfs-deployed'
                                raise
                planinfo['vnfs-configured'] = 'COMPLETED'
        except Exception as e:
            self.log.error("Exception Here:")
            self.log.info(e)
            self.log.info(traceback.format_exc())
            raise
        finally:
            # Create a kicker to be alerted when the VNFs are deployed/undeployed
            kick_monitor_node = "/vnf-manager/site[name='{}']/vnf-deployment[tenant='{}'][deployment-name='{}']/status".format(
                                service.site, service.tenant, service.deployment_name)
            kick_node = "/firewall/ftdv-ngfw-advanced[site='{}'][tenant='{}'][deployment-name='{}']".format(
                                service.site, service.tenant, service.deployment_name)
            kick_expr = ". = 'Configurable' or . = 'Failure' or . = 'Starting VNFs'"

            self.log.info('Creating Kicker Monitor on: ', kick_monitor_node)
            self.log.info(' kicking node: ', kick_node)
            kicker = root.kickers.data_kicker.create('firewall-service-{}-{}-{}'.format(service.site, service.tenant, service.deployment_name))
            kicker.monitor = kick_monitor_node
            kicker.kick_node = kick_node
            # kicker.trigger_expr = kick_expr
            # kicker.trigger_type = 'enter'
            kicker.action_name = 'reactive-re-deploy'
            self.log.info(str(proplistdict))
            proplist = [(k,v) for k,v in proplistdict.iteritems()]
            self.log.info(str(proplist))
            self.write_plan_data(service, planinfo)
            return proplist

    def write_plan_data(self, service, planinfo):
        self_plan = PlanComponent(service, 'vnf-deployment', 'ncs:self')
        self_plan.append_state('ncs:init')
        self_plan.append_state('ftdv-ngfw:vnfs-deployed')
        self_plan.append_state('ftdv-ngfw:vnfs-configured')
        self_plan.append_state('ncs:ready')
        self_plan.set_reached('ncs:init')

        if planinfo.get('vnfs-deployed', '') == 'COMPLETED':
            self_plan.set_reached('ftdv-ngfw:vnfs-deployed')
        if planinfo.get('vnfs-configured', '') == 'COMPLETED':
            self_plan.set_reached('ftdv-ngfw:vnfs-configured')
            if planinfo.get('failure', None) is None:
                self_plan.set_reached('ncs:ready')

        if planinfo.get('failure', None) is not None:
            self.log.info('setting failure, ftdv-ngfw:'+planinfo['failure'])
            self_plan.set_failed('ftdv-ngfw:'+planinfo['failure'])


class NGFWBasicService(Service):

    @Service.create
    def cb_create(self, tctx, root, service, proplist):
        self.log.info('Service create(service=', service._path, ')')
        vnf_catalog = root.vnf_manager.vnf_catalog
        site = root.vnf_manager.site[service.site]
        management_network = site.management_network

        vars = ncs.template.Variables()
        vars.add('DEPLOYMENT-NAME', service.deployment_name);
        vars.add('DATACENTER-NAME', site.datacenter_name);
        vars.add('DATASTORE-NAME', site.datastore_name);
        vars.add('CLUSTER-NAME', site.cluster_name);
        vars.add('MANAGEMENT-NETWORK-NAME', management_network.name);
        vars.add('MANAGEMENT-NETWORK-IP-ADDRESS', service.ip_address);
        vars.add('MANAGEMENT-NETWORK-NETMASK', management_network.netmask);
        vars.add('MANAGEMENT-NETWORK-GATEWAY-IP-ADDRESS', management_network.gateway_ip_address);
        vars.add('DNS-IP-ADDRESS', site.dns_ip_address);
        vars.add('DEPLOY-PASSWORD', day0_admin_password); # admin password to set when deploy
        vars.add('IMAGE-NAME', root.nfv.vnfd[vnf_catalog[service.catalog_vnf].descriptor_name]
                                .vdu[vnf_catalog[service.catalog_vnf].descriptor_vdu]
                                .software_image_descriptor.image);
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
            URL = 'https://{}/api/fdm/latest/policy/accesspolicies/c78e66bc-cb57-43fe-bcbf-96b79b3475b3/accessrules'.format(service.ip_address)
            response = requests.get(url=URL, headers=headers, verify=False)
            data = response.json()
            found = False
            for item in data['items']:
                if item['name'] == rule.name:
                    found = True
                    self.log.info('Found')
            self.log.info('Got here')
            self.log.info('Deployment Exists')
            zoneid = service.state.zone[rule.source_zone].id
            portid = service.state.port[rule.source_port].id
            self.log.info('Deployment Exists ', rule.source_zone, ' ', rule.source_port)
            self.log.info('Deployment Exists ', zoneid, ' ', portid)
            URL = 'https://{}/api/fdm/latest/policy/accesspolicies/c78e66bc-cb57-43fe-bcbf-96b79b3475b3/accessrules'.format(service.ip_address)
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
                response = requests.post(url=URL, headers=headers, verify=False, json=payload )
            self.log.info('Got here 2')
            self.log.info(response.content)
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

    # @Service.post_modification
    # def cb_post_modification(self, tctx, op, kp, root, proplist):
    #     self.log.info('Service postmod(service=', kp, ' ', op, ')')
    #     try:
    #         with ncs.maapi.single_write_trans(uinfo.username, uinfo.context) as trans:
    #             service = ncs.maagic.get_node(trans, kp)
    #             device_name = service.device_name
    #             device = ncs.maagic.get_root().devices.device[device_name]
    #             inputs = service.check_bgp
    #             inputs.service_name = service.name
    #             result = service.check_bgp()
    #             addDeviceUser(self.log, x`, input.username, input.password)
    #             result = "User Added"
    #             service.status = "GOOD"
    #             trans.apply()
    #     except Exception as error:
    #         self.log.info(traceback.format_exc())
    #         result = 'Error Adding User: ' + str(error)
    #     finally:
    #         output.result = result


# ---------------------------------------------
# COMPONENT THREAD THAT WILL BE STARTED BY NCS.
# ---------------------------------------------
class Main(ncs.application.Application):
    def setup(self):
        # The application class sets up logging for us. It is accessible
        # through 'self.log' and is a ncs.log.Log instance.
        self.log.info('Main RUNNING')
        with ncs.maapi.Maapi() as m:
            m.install_crypto_keys()

        with ncs.maapi.single_write_trans('admin', 'vnf-manager',
                                          db=ncs.RUNNING) as trans:
            self.log.info('Creating Kicker Device Added Monitor')
            root = ncs.maagic.get_root(trans)
            kicker1 = root.kickers.data_kicker.create('ftdv_ngfw-DeviceAdded-sync-manager')
            kicker1.monitor = '/vnf-manager/site/vnf-deployment'
            kicker1.trigger_expr = "./manager and status='Provisioned'"
            kicker1.kick_node = './manager'
            kicker1.action_name = 'sync-manager-with-nso'
            kicker1.priority = 1
            kicker1.trigger_type = 'enter'

            self.log.info('Creating Device Configured-Stage-1 Monitor')
            kicker2 = root.kickers.data_kicker.create('ftdv_ngfw-DeviceConfigurable-configure-device')
            kicker2.monitor = '/vnf-manager/site/vnf-deployment/device'
            kicker2.trigger_expr = "status='Configured-Stage-1'"
            kicker2.kick_node = '..'
            kicker2.action_name = 'reactive-re-deploy'
            kicker2.priority = 1
            kicker2.trigger_type = 'enter'
            
            self.log.info('Creating Device Deployed Monitor')
            kicker3 = root.kickers.data_kicker.create('ftdv_ngfw-DeviceDeployed-register-device')
            kicker3.monitor = "/vnf-manager/site/vnf-deployment/device"
            kicker3.trigger_expr = "not(../manager/name) and status='Starting'"
            kicker3.kick_node = '.'
            kicker3.action_name = 'register-vnf-with-nso'
            kicker3.priority = 1
            kicker3.trigger_type = 'enter'
            
            self.log.info('Creating Device Removed Monitor')
            kicker4 = root.kickers.data_kicker.create('ftdv_ngfw-DeviceRemoved-register-device')
            kicker4.monitor = "/vnf-manager/site/vnf-deployment"
            kicker4.trigger_expr = "not(device)"
            kicker4.kick_node = '.'
            kicker4.action_name = 'deregister-vnf-with-nso'
            kicker4.priority = 1
            kicker4.trigger_type = 'enter'
            
            self.log.info('Creating Managed Device Configured Monitor')
            kicker5 = root.kickers.data_kicker.create('ftdv_ngfw-ManagedDeviceConfigured-deploy-configurations')
            kicker5.monitor = "/vnf-manager/site/vnf-deployment"
            kicker5.trigger_expr = "./manager and status='Configured'"
            kicker5.kick_node = './manager'
            kicker5.action_name = 'deploy-manager-configurations'
            kicker5.priority = 1
            kicker5.trigger_type = 'enter'
            trans.apply()

        # Service callbacks require a registration for a 'service point',
        # as specified in the corresponding data model.
        #
        self.register_service('ftdv-ngfw-servicepoint', NGFWBasicService)
        self.register_service('ftdv-ngfw-advanced-servicepoint', NGFWAdvancedService)
        self.register_service('ftdv-ngfw-scalable-servicepoint', ScalableService)
        self.register_action('ftdv-ngfw-registerVnfWithNso-action', RegisterVNFWithNSO)
        self.register_action('ftdv-ngfw-deregisterVnfWithNso-action', DeregisterVNFWithNSO)
        self.register_action('ftdv-ngfw-provisionFTDDevice-action', ProvisionFTDDevice)
        self.register_action('ftdv-ngfw-syncVnfWithNso-action', SyncVNFWithNSO)
        self.register_action('ftdv-ngfw-getDeviceData-action', GetDeviceData)
        self.register_action('ftdv-ngfw-addUser-action', AddDeviceUser)
        self.register_action('ftdv-ngfw-deleteUser-action', DeleteDeviceUser)
        self.register_action('ftdv-ngfw-syncManagerWithNso-action', SyncManagerWithNSO)
        self.register_action('ftdv-ngfw-configureDevice-action', ConfigureDevice)
        self.register_action('ftdv-ngfw-deployManagerConfigurations-action', DeployManagerConfigurations)
#        self.register_service('ftdv-ngfw-access-rule-servicepoint', AccessRuleService)

        # If we registered any callback(s) above, the Application class
        # took care of creating a daemon (related to the service/action point).

        # When this setup method is finished, all registrations are
        # considered done and the application is 'started'.

    def teardown(self):
        # When the application is finished (which would happen if NCS went
        # down, packages were reloaded or some error occurred) this teardown
        # method will be called.

#        with ncs.maapi.single_write_trans('admin', 'vnf-manager',
#                                          db=ncs.RUNNING) as trans:
#            root = ncs.maagic.get_root(trans)
#            self.log.info('Removing Kicker Device Added Monitor')
#            del root.kickers.data_kicker['ftdv_ngfw-DeviceAdded-sync-manager']
#            self.log.info('Removing Device Configured-Stage-1 Monitor')
#            del root.kickers.data_kicker['ftdv_ngfw-DeviceConfigurable-configure-device']
#            trans.apply()

        self.log.info('Main FINISHED')


