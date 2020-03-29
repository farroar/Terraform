#!/usr/bin/python3
'''
##############################################################################
This is a collection of functions that initializes a storage account in Azure
to facilitate Terraform state storage.

Also added is a function to run VNET peerings

Nathan Farrar - 27-Mar-2020
##############################################################################
'''

import string
import random
import os
import time


from azure.common.credentials import ServicePrincipalCredentials
from azure.mgmt.storage import StorageManagementClient
from azure.mgmt.storage.models import StorageAccountCreateParameters, Sku, SkuName, Kind
from azure.mgmt.keyvault import KeyVaultManagementClient
from azure.mgmt.resource import ResourceManagementClient
from azure.mgmt.network import NetworkManagementClient
from azure.identity import ClientSecretCredential
from azure.keyvault.secrets import SecretClient
from azure.storage.blob import BlockBlobService
from msrestazure.azure_exceptions import CloudError

def random_generator(size=6, chars=string.ascii_lowercase+string.digits):
    return ''.join(random.choice(chars) for x in range(size))

def azure_state_setup(vnet, ops_subscription):
    
    keyvault_name = vnet.orgName+'-tfstate-kv'
    storage_prefix = vnet.orgName+'tfstate'
    storage_account_name = storage_prefix+random_generator()
    resource_group = vnet.orgName+'-tfstate-rg'

    CREDENTIALS = ServicePrincipalCredentials(
        client_id = os.environ['ARM_CLIENT_ID'],
        object_id = os.environ['ARM_OBJECT_ID'],
        secret = os.environ['ARM_CLIENT_SECRET'],
        tenant = os.environ['ARM_TENANT_ID']
    )

    #setup clients for resource creation 
    resource_client = ResourceManagementClient(CREDENTIALS, ops_subscription)
    storage_client = StorageManagementClient(CREDENTIALS, ops_subscription)
    keyvault_client = KeyVaultManagementClient(CREDENTIALS, ops_subscription)
    secret_credentials = ClientSecretCredential(
        os.environ['ARM_TENANT_ID'],
        os.environ['ARM_CLIENT_ID'],
        os.environ['ARM_CLIENT_SECRET']
    )
    vault_uri = 'https://'+keyvault_name+'.vault.azure.net/'

    # MIGHT need to add KeyVault as a valid provider for these credentials
    # If so, this operation has to be done only once for each credentials
    resource_client.providers.register('Microsoft.KeyVault')

    #create resource group for state storage
    if not resource_client.resource_groups.check_existence(resource_group):
        resource_group_params = {'location': vnet.location}
        resource_client.resource_groups.create_or_update(resource_group, resource_group_params)

    #check if keyvault already exists, if not create... if does bypass
    #first find out if there are more than 0 vaults in the subscription. Haven't found a better way to do this.
    i = 0
    keyvault_exists = False
    vaults = keyvault_client.vaults.list()
    for item in vaults:
        i += 1
    if i > 0:
        #need to re-pull the vaults in order to iterate over them again    
        vaults = keyvault_client.vaults.list()
        for item in vaults:
            if item.name == keyvault_name:
                print(f'*** The keyvault {keyvault_name} already exists, bypassing creation ***')
                keyvault_exists = True

    if not keyvault_exists:
        #create key vault
        print(f'*** The keyvault {keyvault_name} doesn\'t exist, creating ***')
        vault = keyvault_client.vaults.create_or_update(
            resource_group,
            keyvault_name,
            {
                'location': vnet.location,
                'properties': {
                    'sku': {
                        'name': 'standard'
                    },
                    'tenant_id': os.environ['ARM_TENANT_ID'],
                    'access_policies': [{
                        'tenant_id': os.environ['ARM_TENANT_ID'],
                        'object_id': os.environ['ARM_OBJECT_ID'],
                        'permissions': {
                            'keys': ['all'],
                            'secrets': ['all']
                        }
                    }]
                }
            }
        )

    #create storage account for terraform state, check if already exists then if doesn't exist check for availability.
    #create if doesn't already exist and is avilable, if not avilable... stdout error
    storage_accounts = storage_client.storage_accounts.list()
    storage_account_exists = False
    i = 0
    for item in storage_accounts:
        i += 1
    #if there are storage accounts in subscription, check to see if one has 'tfstate' in it
    if i > 0:
        storage_accounts = storage_client.storage_accounts.list()
        for item in storage_accounts:
            if 'tfstate' in item.name:
                #storage account already exists, need to pull keys and place them in the keyvault
                print('*** The tfstate storage account exists, bypassing storage build ***')
                #since we are using the random function, we need to pass the actual vaule of the account name
                storage_account_name = item.name
                storage_account_exists = True

    if not storage_account_exists:
        #storage account doesn't already exist, now check if storage account name is available before building
        print(f'Checking if storage account name {storage_account_name} is available')
        stg_name_availability = storage_client.storage_accounts.check_name_availability(storage_account_name).name_available

        if stg_name_availability:
            print(f'The storage account name {storage_account_name} is available, building account.')
            storage_async_operation = storage_client.storage_accounts.create(
                resource_group,
                storage_account_name,
                StorageAccountCreateParameters(
                    sku=Sku(name=SkuName.standard_ragrs),
                    kind=Kind.storage,
                    location=vnet.location
                )
            )
            storage_account = storage_async_operation.result()

            #after storage account is setup, need to create the container
            storage_client.blob_containers.create(resource_group, storage_account_name, 'tfstate')
            print(f'The tfstate container added to the storage account')

            #grab current keys from storage account, place into dictionary called 'keys'
            storage_keys = storage_client.storage_accounts.list_keys(resource_group, storage_account_name)
            keys = {v.key_name: v.value for v in storage_keys.keys}

        else:
            #storage account isn't available, figure out what to do here.
            print(f'The storage account {storage_account_name} is not available.')
            ############ ADD ERROR CONTROL #################
            return
    else:
        #storage account already in place
        #grab current keys from storage account, place into dictionary called 'keys'
        storage_keys = storage_client.storage_accounts.list_keys(resource_group, storage_account_name)
        keys = {v.key_name: v.value for v in storage_keys.keys}

        #make sure contianer is there
        print('Confirming container for tfstate')
        blob_service = BlockBlobService(account_name=storage_account_name, account_key=keys['key1'])
        container_list = blob_service.list_containers()
        containers = []
        for c in container_list:
            containers.append(c.name)
        if 'tfstate' not in containers:
            print('tfstate container doesn\'t exist, creating container.')
            storage_client.blob_containers.create(resource_group, storage_account_name, 'tfstate')
        else:
            print('tfstate container already exists.')
    
    #at this point we should have a keyvault and storage account ready with keys ready
    #place them in the keyvault and then return the keys
    secret_client = SecretClient(vault_url=vault_uri, credential=secret_credentials)
    secret_client.set_secret('tfstate1', keys['key1'])
    secret_client.set_secret('tfstate2', keys['key2'])

    #return the storage keys and storage account name
    return keys['key1'],keys['key2'], storage_account_name

def azure_peering(sub1, rg1, vnet1, sub2, rg2, vnet2):

    CREDENTIALS = ServicePrincipalCredentials(
        client_id = os.environ['ARM_CLIENT_ID'],
        secret = os.environ['ARM_CLIENT_SECRET'],
        tenant = os.environ['ARM_TENANT_ID']
    )
    network_client_1 = NetworkManagementClient(CREDENTIALS,sub1)
    network_client_2 = NetworkManagementClient(CREDENTIALS,sub2)

    vnet1_obj = network_client_1.virtual_networks.get(rg1, vnet1)
    vnet2_obj = network_client_2.virtual_networks.get(rg2, vnet2)

    try:
        async_vnet_peering = network_client_1.virtual_network_peerings.create_or_update(
            rg1,
            vnet1,
            "hubpeering",
            {
                "remote_virtual_network": {
                    "id": vnet2_obj.id
                },
                'allow_virtual_network_access': True,
                'allow_forwarded_traffic': True,
                'remote_address_space': {
                    'address_prefixes': vnet2_obj.address_space.address_prefixes
                }
            }
        ) 
        while not async_vnet_peering.done():
            time.sleep(2)
            print(f'Peering: {async_vnet_peering.status()}')
        print(' ********** peering completed **********')
    except CloudError as ex:
        print(str(ex))
