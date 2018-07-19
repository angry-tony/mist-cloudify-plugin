import os
import glob
import json
import time
import string
import random

from cloudify import ctx
from cloudify.exceptions import NonRecoverableError

from plugin.constants import STORAGE, STORAGE2


# TODO Merge with LocalStorageOld, once scale_down workflow is polished.
class LocalStorage(object):

    def __init__(self, storage=None):
        self.storage = (storage or STORAGE2)

    def get_node_instance(self, instance_id):
        with open(os.path.join(self.storage, instance_id)) as fobj:
            instance = json.loads(fobj.read())
        return instance

    def clone_node_instance(self, instance_id):
        instance = self.get_node_instance(instance_id)
        host_id = '%s_%s' % (instance['name'], random_string(5))
        instance['id'] = host_id
        instance['host_id'] = host_id
        with open(os.path.join(self.storage, host_id), 'w') as fobj:
            fobj.write(json.dumps(instance))
        return instance


# TODO Deprecated
class LocalStorageOld(object):
    """
    LocalStorage gives full access to a node instance's properties by reading
    the instance object directly from file

    This class is meant to be called as such:

        node_instance = LocalStorage.get('kube_master')

    where `kube_master` is the actual node_instance as defined in the
    respective blueprint. In order to access the runtime properties simply call:

        node_instance.runtime_properties

    which will return the dict of all of the instance's runtime properties
    """
    def __init__(self, node):
        """
        Searches in local-storage for the file that corresponds to the node
        instance provided
        """
        instance_file = self.fetch_instance_file(node)
        with open(instance_file, 'r') as _instance:
            instance_from_file = _instance.read()

        self.instance_from_file = json.loads(instance_from_file)

    @classmethod
    def get(cls, node):
        """
        A class method to initiate the LocalStorage with the specified node
        """
        node = cls(node)
        return node

    @property
    def runtime_properties(self):
        """
        Returns the node instance's runtime properties
        """
        return self.instance_from_file['runtime_properties']

    def fetch_instance_file(self, node):
        """
        Tries to discover the path of local-storage in order to fetch the
        required node instance
        """
        local_storage = os.path.join('/tmp/templates',
                                     'kubernetes-blueprint',
                                     STORAGE % node)
        local_storage = glob.glob(local_storage)
        if local_storage:
            node_file = local_storage[0]
        # TODO: Well, this is weird, but the local-storage exists on a different
        # path in case a user executes `cfy local` directly from his terminal
        else:
            if not os.path.exists(os.path.join('..', STORAGE % node)):
                raise Exception('Failed to locate local-storage')
            node_file = os.path.join('..', STORAGE % node)
        return node_file


def wait_for_event(job_id, job_kwargs, timeout=1800):
    """Wait for an event to take place.

    This method enters a loop, while waiting for a specific event to
    take place.

    This method polls the workflow's logs over the mist.io API in order
    to decide whether the specified event has finally occurred.

    Parameters:

        job_id:     the UUID of the job that includes the desired event
        job_kwargs: a dict of key-value pairs that must match the event
        timeout:    seconds to wait for, before raising an exception

    This method will either exit with an exit code 0, if the desired
    event occurs within a given timeframe, otherwise it will raise a
    non-recoverable error.

    """
    ctx.logger.info('Waiting for event %s with kwargs=%s', job_id, job_kwargs)

    # FIXME Imported here due to circular dependency issues.
    from plugin.connection import MistConnectionClient
    conn = MistConnectionClient()

    # Mark the beginning of the polling period.
    started_at = time.time()
    timeout_at = started_at + timeout

    # Wait for newly indexed events to become available/searchable.
    for _ in range(30):
        try:
            conn.client.get_job(job_id)
        except Exception as exc:
            ctx.logger.debug('Failed to get logs of %s: %r', job_id, exc)
            time.sleep(1)
        else:
            break
    else:
        raise

    # Poll for the event with the specified key-values pairs.
    while True:
        for log in conn.client.get_job(job_id).get('logs', []):
            if all([log.get(k) == v for k, v in job_kwargs.iteritems()]):
                if log.get('error'):
                    msg = log.get('stdout', '') + log.get('extra_output', '')
                    msg = msg or log['error']
                    ctx.logger.error(msg)
                    raise NonRecoverableError('Error in event %s' % job_id)
                return log

        if time.time() > timeout_at:
            raise NonRecoverableError('Time threshold exceeded!')

        time.sleep(10)


def generate_name(stack, role):
    """Generate a random name for a newly provisioned machine"""
    return '%s-%s-%s' % (stack.lower(), role, random_string().lower())


def random_string(length=4):
    """Generate a random alphanumeric string. Default length is set to 4"""
    _chars = string.letters + string.digits
    return ''.join(random.choice(_chars) for _ in range(length))


def get_job_id():
    """
    Read the Stack's original job ID from file in order to create nested logs
    """
    try:
        with open('/tmp/cloudify-mist-plugin-job', 'r') as jf:
            job_id = jf.read()
    except IOError as err:
        ctx.logger.debug(err)
        job_id = ''
    return job_id


def get_stack_name():
    """
    Read the Stack's name from file. If not found, generate one at random
    """
    try:
        with open('/tmp/cloudify-mist-plugin-stack', 'r') as sf:
            stack_name = sf.read()
        stack_name = stack_name.replace(' ', '-')
    except IOError as err:
        ctx.logger.debug(err)
        stack_name = 'stack-%s' % ctx.deployment.id  # TODO in runtime_properties
        with open('/tmp/cloudify-mist-plugin-stack', 'w') as sf:
            sf.write(stack_name)
    return stack_name


def is_resource_external(properties=None):
    """Return True if resource is external

    A resource is considered external if it already exists, so it won't be
    created. The check is performed against the node's properties. Another
    properties dict may be optionally passed to this method, which will be
    used instead of `ctx.node.properties`.

    """
    return (properties or ctx.node.properties).get('use_external_resource')


def get_external_resource_id(properties=None):
    """Return the id of an external resource

    A resource is considered external if it already exists, so it won't be
    created. The check is performed against the node's properties. Another
    properties dict may be optionally passed to this method, which will be
    used instead of `ctx.node.properties`.

    If the node instance of the current execution thread isn't external,
    then an exception will be thrown. Otherwise, it is expected that the
    resource's id can be found under the `resource_id` key.

    """
    properties = properties or ctx.node.properties
    if not is_resource_external(properties):
        raise NonRecoverableError('use_external_resource is False')
    if not properties.get('resource_id'):
        raise NonRecoverableError('use_external_resource is True, but '
                                  'resource_id is missing')
    return properties['resource_id']
