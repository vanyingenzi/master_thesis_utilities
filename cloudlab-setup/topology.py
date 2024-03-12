"""A profile with three nodes. (Client - "Router" - Server ).

Instructions:
Not much for now.
"""
# Import the Portal object.
import geni.portal as portal
import geni.rspec.pg as pg
import geni.rspec.emulab as emulab

# Create a Request object to start building the RSpec.
request = portal.context.makeRequestRSpec()

def setup_router(client, server):
    router = request.RawPC("router")
    router.hardware_type = "xl170"
    # coordinator <-> server link 
    link_0 = request.Link(members = [router, server])
    link_1 = request.Link(members = [router, client])
    link_2 = request.Link(members = [router, client])

    for link in [link_0, link_1, link_2]:
        link.best_effort = True
        link.link_multiplexing = True
    return router

def setup_client_server_specs():
    server = request.RawPC("server")
    client = request.RawPC("client")

    for node in [client, server]:
        node.hardware_type = "xl170"
    return (client, server)


def specify_os_setup_scripts(nodes):
    for node in nodes:
        # Defalt OS is ubuntu 20.64 node.disk_image = "urn:publicid:IDN+emulab.net+image+emulab-ops//UBUNTU20-64-STD"
        node.addService(pg.Install(url="https://raw.githubusercontent.com/vanyingenzi/master_thesis_utilities/main/cloudlab-setup/install_scripts/setup.sh", path="/local"))
        node.addService(pg.Execute(shell="bash", command="/local/setup.sh"))

client, server = setup_client_server_specs()
router = setup_router(client, server)
portal.context.printRequestRSpec()