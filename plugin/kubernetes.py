from cloudify import ctx
from cloudify.decorators import operation
from cloudify.exceptions import NonRecoverableError, RecoverableError
from mistclient import MistClient
from time import sleep
import connection
import constants
import keypair
import os
import string
import uuid
import random
import pkg_resources, os
import requests


resource_package = __name__  ## Could be any module/package name.
resource_path = os.path.join('scripts', 'worker.sh')
install_worker_script = pkg_resources.resource_string(resource_package, resource_path)
resource_path = os.path.join('scripts', 'master.sh')
install_master_script = pkg_resources.resource_string(resource_package, resource_path)
resource_path = os.path.join('scripts', 'coreos_master.sh')
install_coreos_master_script = pkg_resources.resource_string(resource_package, resource_path)
resource_path = os.path.join('scripts', 'coreos_worker.sh')
install_coreos_worker_script = pkg_resources.resource_string(resource_package, resource_path)

from cloudify.decorators import workflow

from cloudify.workflows import ctx as workctx

@workflow
def scale_cluster_up(**kwargs):

    master = workctx.get_node("master")
    mist_client = connection.MistConnectionClient(properties=master.properties)
    client = mist_client.client
    cloud = mist_client.cloud
    master_machine = mist_client.machine
    master_ip = master_machine.info["private_ips"][0]

    if kwargs['use_external_resource']:
        machine = mist_client.other_machine(kwargs)
    machine_name = kwargs["name"]
    machines = cloud.machines(search=machine_name)
    if len(machines):
        for m in machines:
            if m.info["state"] in ["running", "stopped"]:
                raise NonRecoverableError(
                    "Machine with name {0} exists".format(machine_name))

    key = ""
    if kwargs.get("key_name"):
        key = client.keys(search=kwargs["key_name"])
        if len(key):
            key = key[0]
        else:
            raise NonRecoverableError("key not found")
    else:
        raise NonRecoverableError("key not found")
    # print 'Key:', key
    if kwargs.get("networks"):
        networks = kwargs["networks"]
    else:
        networks = []
    quantity = kwargs.get("delta")
    job_id = cloud.create_machine(async=True, name=machine_name,
                                  key=key, image_id=kwargs["image_id"],
                                  location_id=kwargs["location_id"],
                                  size_id=kwargs["size_id"], quantity=quantity,
                                  networks=networks)
    job_id = job_id.json()["job_id"]
    job = client.get_job(job_id)
    timer = 0
    while True:
        if job["summary"]["probe"]["success"]:
            break
        if job["summary"]["create"]["error"] or job["summary"]["probe"]["error"]:
            workctx.logger.error('Error on machine creation:{0}'.format(job))
            raise NonRecoverableError("Not able to create machine")
        sleep(10)
        job = client.get_job(job_id)
        timer += 1
        if timer >= 360:   # timeout 1hour
            raise NonRecoverableError("Timeout.Not able to create machine.")

    kub_type = "worker"

    if not kwargs["coreos"]:
        script = """#!/bin/sh
command_exists() {
command -v "$@" > /dev/null 2>&1
}
if command_exists curl; then
curl -sSL https://get.docker.com/ | sh
elif command_exists wget; then
wget -qO- https://get.docker.com/ | sh
fi
"""
        response = client.add_script(
            name="install_docker" + kub_type + uuid.uuid1().hex, script=script,
            location_type="inline", exec_type="executable",
        )
        script_id = response['script_id']
        machine_id = kwargs['machine_id']
        cloud_id = kwargs['cloud_id']
        job_id = client.run_script(script_id=script_id, cloud_id=cloud_id,
                                   machine_id=machine_id,
                                   script_params="",
                                   su=False)
        workctx.logger.info("Docker installation started")
        job_id = job_id["job_id"]
        job = client.get_job(job_id)
        while True:
            if job["error"]:
                raise NonRecoverableError("Not able to install docker")
            if job["finished_at"]:
                break
            sleep(10)
            job = client.get_job(job_id)
        workctx.logger.info(job["logs"][2]['stdout'])
        workctx.logger.info(job["logs"][2]['extra_output'])
        workctx.logger.info("Docker installation script succeeded")
    if kwargs["coreos"]:
        install_script = install_coreos_worker_script
    else:
        install_script = install_worker_script
    response = client.add_script(
        name="install_kubernetes_worker" + uuid.uuid1().hex,
        script=install_script,
        location_type="inline", exec_type="executable",
    )
    for m in xrange(quantity):
        kwargs["name"] = machine_name + "-" + str(m + 1)
        kwargs["machine_id"] = ""
        machine = mist_client.other_machine(kwargs)
        kwargs["machine_id"] = machine.info["id"]
        workctx.logger.info('Machine created')

        script_id = response['script_id']
        machine_id = kwargs['machine_id']
        cloud_id = kwargs['cloud_id']
        script_params = "-m '{0}'".format(master_ip)
        job_id = client.run_script(script_id=script_id, cloud_id=cloud_id,
                                   machine_id=machine_id,
                                   script_params=script_params,
                                   su=True)
        workctx.logger.info("Kubernetes worker installation started")
        job_id = job_id["job_id"]
        job = client.get_job(job_id)
        while True:
            if job["error"]:
                raise NonRecoverableError("Not able to install kubernetes worker")
            if job["finished_at"]:
                break
            sleep(10)
            job = client.get_job(job_id)
        workctx.logger.info(job["logs"][2]['stdout'])
        workctx.logger.info(job["logs"][2]['extra_output'])
        workctx.logger.info("Kubernetes worker {0} installation script succeeded".format(kwargs["name"]))
    workctx.logger.info("Upscaling kubernetes cluster succeeded")


@workflow
def scale_cluster_down(**kwargs):
    master = workctx.get_node("master")
    mist_client = connection.MistConnectionClient(properties=master.properties)
    client = mist_client.client
    cloud = mist_client.cloud
    master_machine = mist_client.machine
    master_ip = master_machine.info["public_ips"][0]

    worker_name = kwargs.get("name")
    machines = cloud.machines(search=worker_name)
    delta = kwargs.get("delta")
    counter = 0
    for m in machines:
        if not m.info["state"] in ("stopped", "running"):
            continue
        counter += 1
        worker_priv_ip = m.info["private_ips"][0]
        m.destroy()
        requests.delete("http://%s:8080/api/v1/nodes/%s" % (master_ip, worker_priv_ip))
        if counter == delta:
            break
    workctx.logger.info("Downscaling kubernetes cluster succeeded")

@operation
def create(**_):
    mist_client = connection.MistConnectionClient()
    client = mist_client.client
    cloud = mist_client.cloud
    if ctx.node.properties['use_external_resource']:
        machine = mist_client.machine
        ctx.instance.runtime_properties['ip'] = public_ip = machine.info["public_ips"][0]
        ctx.instance.runtime_properties["master_ip"] = machine.info["private_ips"][0]
        ctx.instance.runtime_properties['networks'] = {
            "default": public_ip}
        ctx.instance.runtime_properties['machine_id'] = machine_id = machine.info["id"]
        ctx.instance.runtime_properties['cloud_id'] = cloud.id
        ctx.instance.runtime_properties['mist_type'] = "machine"

        ctx.logger.info('External machine attached to ctx')
        return
    machine_name = ctx.node.properties['parameters']["name"]
    machines = cloud.machines(
        search=machine_name)
    if len(machines):
        for m in machines:
            if m.info["state"] in ["running", "stopped"]:
                raise NonRecoverableError(
                    "Machine with name {0} exists".format(machine_name))

    key = ""
    if ctx.node.properties['parameters'].get("key_name"):
        key = client.keys(search=ctx.node.properties['parameters']["key_name"])
        if len(key):
            key = key[0]
        else:
            raise NonRecoverableError("key not found")
    else:
        raise NonRecoverableError("key not found")
    # print 'Key:', key
    if ctx.node.properties['parameters'].get("networks"):
        networks = ctx.node.properties['parameters']["networks"]
        ctx.instance.runtime_properties["networks"] = networks
    else:
        networks = []
    ctx.logger.info('Networks are: {0}'.format(networks))

    job_id = cloud.create_machine(async=True, name=machine_name, fire_and_forget=True,
                                  key=key, image_id=ctx.node.properties['parameters']["image_id"],
                                  location_id=ctx.node.properties['parameters']["location_id"],
                                  size_id=ctx.node.properties['parameters']["size_id"],
                                  networks=networks,
                                  associate_floating_ip=True)
    job_id = job_id.json()["job_id"]

    job = client.get_job(job_id)
    timer = 0
    while True:

        if job["summary"]["probe"]["success"]:
            break
        if job["summary"]["create"]["error"] or job["summary"]["probe"]["error"]:
            ctx.logger.error('Error on machine creation:{0}'.format(job))
            raise NonRecoverableError("Not able to create machine")
        sleep(10)
        try:
            job = client.get_job(job_id)
        except Exception as exc:
            pass
        timer += 1
        if timer >= 360:   # timeout 1hour
            raise NonRecoverableError("Timeout.Not able to create machine.")

    # print job["summary"]
    machine = mist_client.machine
    ctx.instance.runtime_properties['machine_id'] = machine.info["id"]
    ctx.instance.runtime_properties['cloud_id'] = cloud.id
    ctx.instance.runtime_properties['mist_type'] = "machine"
    if len(machine.info["public_ips"]):
        ctx.instance.runtime_properties['ip'] = machine.info["public_ips"][0]
        ctx.instance.runtime_properties['networks'] = {
            "default": machine.info["public_ips"][0]}
    if len(machine.info["private_ips"]):
        ctx.instance.runtime_properties['priv_ip'] = machine.info["private_ips"][0]
    ctx.logger.info('Machine created')


@operation
def start(**_):
    try:
        connection.MistConnectionClient().machine.start()
        ctx.logger.info('Machine started')
    except:
        ctx.logger.info("Failed to start machine")
    if ctx.node.properties.get("monitoring"):
        connection.MistConnectionClient().machine.enable_monitoring()
        ctx.logger.info('Monitoring enabled')


@operation
def install_kubernetes(**kwargs):
    client = connection.MistConnectionClient().client
    machine = connection.MistConnectionClient().machine
    if kwargs.get("master"):
        ctx.instance.runtime_properties["master_ip"] = machine.info["private_ips"][0]
        kub_type = "master"
        if ctx.node.properties["parameters"]["coreos"]:
            install_script = install_coreos_master_script
        else:
            install_script = install_master_script
    else:
        ctx.instance.runtime_properties["master_ip"] = ctx.instance.relationships[0]._target.instance.runtime_properties["master_ip"]
        if ctx.node.properties["parameters"]["coreos"]:
            install_script = install_coreos_worker_script
        else:
            install_script = install_worker_script
        kub_type = "worker"

    if ctx.node.properties["configured"]:
        return
    if not ctx.node.properties["parameters"]["coreos"]:
        script = """#!/bin/sh
command_exists() {
command -v "$@" > /dev/null 2>&1
}
if command_exists curl; then
curl -sSL https://get.docker.com/ | sh
elif command_exists wget; then
wget -qO- https://get.docker.com/ | sh
fi
"""
        response = client.add_script(
            name="install_docker" + kub_type + uuid.uuid1().hex, script=script,
            location_type="inline", exec_type="executable",
        )
        script_id = response['script_id']
        machine_id = ctx.instance.runtime_properties['machine_id']
        cloud_id = ctx.node.properties['parameters']['cloud_id']
        job_id = client.run_script(script_id=script_id, cloud_id=cloud_id,
                                   machine_id=machine_id,
                                   script_params="",
                                   su=False)
        ctx.logger.info("Docker installation started")
        job_id = job_id["job_id"]
        job = client.get_job(job_id)
        while True:
            if job["error"]:
                raise NonRecoverableError("Not able to install docker")
            if job["finished_at"]:
                break
            sleep(10)
            job = client.get_job(job_id)
        ctx.logger.info(job["logs"][2]['stdout'])
        ctx.logger.info(job["logs"][2]['extra_output'])
        ctx.logger.info("Docker installation script succeeded")
    response = client.add_script(
        name="install_kubernetes_" + kub_type + uuid.uuid1().hex,
        script=install_script,
        location_type="inline", exec_type="executable",
    )
    script_id = response['script_id']
    machine_id = ctx.instance.runtime_properties['machine_id']
    cloud_id = ctx.node.properties['parameters']['cloud_id']
    if kub_type == "master":
        script_params = ""
    else:
        script_params = "-m '{0}'".format(ctx.instance.runtime_properties["master_ip"])
    job_id = client.run_script(script_id=script_id, cloud_id=cloud_id,
                               machine_id=machine_id,
                               script_params=script_params,
                               su=True)
    ctx.logger.info("Kubernetes {0} installation started".format(kub_type))
    job_id = job_id["job_id"]
    job = client.get_job(job_id)
    while True:
        if job["error"]:
            raise NonRecoverableError("Not able to install kubernetes {0}".format(kub_type))
        if job["finished_at"]:
            break
        sleep(10)
        job = client.get_job(job_id)
    ctx.logger.info(job["logs"][2]['stdout'])
    ctx.logger.info(job["logs"][2]['extra_output'])
    ctx.logger.info("Kubernetes {0} installation script succeeded".format(kub_type))


@operation
def stop(**_):
    connection.MistConnectionClient().machine.stop()
    ctx.logger.info('Machine stopped')


@operation
def delete(**_):
    connection.MistConnectionClient().machine.destroy()
    ctx.logger.info('Machine destroyed')


@operation
def run_script(**kwargs):
    client = connection.MistConnectionClient().client
    script = kwargs.get('script', '')
    name = kwargs.get("name", '')
    scripts = client.get_scripts()
    machine = connection.MistConnectionClient().machine
    if kwargs.get("script_id", ''):
        script_id = kwargs["script_id"]
        job_id = client.run_script(script_id=script_id,
                                   cloud_id=ctx.node.properties['parameters'][
                                       'cloud_id'],
                                   machine_id=ctx.instance.runtime_properties[
                                       'machine_id'],
                                   script_params=kwargs.get("params", ""),
                                   su=kwargs.get("su"), env=kwargs.get("env")
                                   )
    else:
        entrypoint = kwargs.get("entrypoint")
        if kwargs.get("exec_type", ''):
            exec_type = kwargs["exec_type"]
        else:
            exec_type = "executable"

        if kwargs.get("location_type", ""):
            location_type = kwargs["location_type"]
        else:
            if (script.startswith('http://github.com') or script.startswith('https://github.com')):
                location_type = 'github'
            elif (script.startswith('http://') or script.startswith('https://')):
                location_type = 'url'
            elif kwargs.get("tmp_prefix"):
                script = kwargs["tmp_prefix"] + script
                if os.path.exists():
                    if not name:
                        name = script.split("/").pop()
                    location_type = 'inline'
                    with open(script, "r") as scriptfile:
                        script = scriptfile.read()
            elif os.path.exists(script):
                if not name:
                    name = script.split("/").pop()
                location_type = 'inline'
                with open(script, "r") as scriptfile:
                    script = scriptfile.read()
            elif script.startswith("#!"):
                location_type = 'inline'
            else:
                raise NonRecoverableError("Script not found {0}".format(script))
        if not name:
            uid = ''.join(
                random.choice(string.ascii_uppercase + string.digits) for _ in range(4))
            name = ctx.node.properties["parameters"]["name"] + uid

        for s in scripts:
            if s['name'] == name:
                raise NonRecoverableError("Script with name {0} exists. Rename the script \
                                            or use external resource.".format(name))
        response = client.add_script(
            name=name, script=script, location_type=location_type,
            exec_type=exec_type, entrypoint=entrypoint
        )
        script_id = response['script_id']
        machine_id = ctx.instance.runtime_properties['machine_id']
        cloud_id = ctx.node.properties['parameters']['cloud_id']
        job_id = client.run_script(script_id=script_id, cloud_id=cloud_id,
                                   machine_id=machine_id,
                                   script_params=kwargs.get("params", ""),
                                   su=kwargs.get("su"), env=kwargs.get("env"))
    ctx.logger.info("Script with name {0} started".format(name))
    job_id = job_id["job_id"]
    job = client.get_job(job_id)
    while True:
        if job["error"]:
            raise NonRecoverableError("Not able to run script {0}".format(name))
        if job["finished_at"]:
            break
        sleep(10)
        job = client.get_job(job_id)
    ctx.logger.info(job["logs"][2]['stdout'])
    ctx.logger.info(job["logs"][2]['extra_output'])
    ctx.logger.info("Script with name {0} succeeded".format(name))


# @operation
# def install_master(**kwargs):
#     client = connection.MistConnectionClient().client
#     machine = connection.MistConnectionClient().machine
#     if len(machine.info["private_ips"]) and not ctx.node.properties["multicloud"]:
#         ctx.instance.runtime_properties['master_ip'] = machine.info["private_ips"][0]
#     else:
#         ctx.instance.runtime_properties['master_ip'] = machine.info["public_ips"][0]
#     env = kwargs.get("env")
#     if not isinstance(env, dict):
#         env = {}
#     env.update({"MASTER_IP": ctx.instance.runtime_properties['master_ip']})
#     operation(run_script({"script": "scripts/install_docker.sh"}))
#     operation(run_script({
#         "script": "https://github.com/kubernetes/kubernetes",
#         "entry_point": "docs/getting-started-guides/docker-multinode/master.sh",
#         "su": True,
#         "env": env
#     }))


# @operation
# def install_worker(**kwargs):
#     client = connection.MistConnectionClient().client
#     machine = connection.MistConnectionClient().machine
#     env = kwargs.get("env")
#     if not isinstance(env, dict):
#         env = {}
#     env.update({"MASTER_IP": ctx.instance.runtime_properties['master_ip']})
#     run_script({"script": "scripts/install_docker.sh"})
#     run_script({
#         "script": "https://github.com/kubernetes/kubernetes",
#         "entry_point": "docs/getting-started-guides/docker-multinode/worker.sh",
#         "su": True,
#         "env": env
#     })



@operation
def creation_validation(**_):
    """ This checks that all user supplied info is valid """
    ctx.logger.info('Checking validity of info')
    mist_client = connection.MistConnectionClient()
    try:
        client = mist_client.client
    except:
        raise NonRecoverableError('Credentials failed')

    for property_key in constants.INSTANCE_REQUIRED_PROPERTIES:
        if property_key not in ctx.node.properties:
            raise NonRecoverableError(
                '{0} is a required input. Unable to create.'.format(key))
    cloud = client.clouds(id=ctx.node.properties['cloud_id'])
    if not len(cloud):
        raise NonRecoverableError(
            '{0} cloud was not found.'.format(ctx.node.properties['cloud_id']))
    image = ""
    for im in cloud[0].images:
        if im[id] == ctx.node.properties['image_id']:
            image = im
            break
    if not image:
        raise NonRecoverableError(
            'image_id {0} not found.'.format(ctx.node.properties['image_id']))
    size = ""
    for si in cloud[0].sizes:
        if si[id] == ctx.node.properties['size_id']:
            size = si
            break
    if not size:
        raise NonRecoverableError(
            'size_id {0} not found.'.format(ctx.node.properties['size_id']))
    location = ""
    for lo in cloud[0].locations:
        if lo[id] == ctx.node.properties['location_id']:
            location = lo
            break
    if not location:
        raise NonRecoverableError(
            'location_id {0} not found.'.format(ctx.node.properties['location_id']))

    machines = cloud[0].machines(search=ctx.node.properties["name"])
    if ctx.node.properties['use_external_resource'] and not len(machines):
        raise NonRecoverableError(
            'machine {0} not found.'.format(ctx.node.properties["name"]))
    if not ctx.node.properties['use_external_resource'] and len(machines):
        raise NonRecoverableError(
            'machine {0} exists.'.format(ctx.node.properties["name"]))
    if ctx.node.properties['use_external_resource'] and len(machines):
        if machines[0].info["state"] == "running":
            pass
        elif machines[0].info["state"] == "stopped":
            machines[0].start()
            delay = 0
            while True:
                sleep(10)
                cloud[0].update_machines()
                if cloud[0].machines(search=ctx.node.properties["name"])[0].info["state"] == "running":
                    break
                elif delay == 5:
                    raise NonRecoverableError(
                        'machine {0} in stopped state.'.format(ctx.node.properties["name"]))
                delay += 1
        else:
            raise NonRecoverableError(
                'machine {0} error state.'.format(ctx.node.properties["name"]))

