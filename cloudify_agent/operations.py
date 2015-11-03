#########
# Copyright (c) 2015 GigaSpaces Technologies Ltd. All rights reserved
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
#  * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  * See the License for the specific language governing permissions and
#  * limitations under the License.

import tempfile
import time
import threading
import ssl
import sys
import os

from contextlib import contextmanager

import celery

from cloudify import ctx
from cloudify.exceptions import NonRecoverableError

from cloudify.utils import get_manager_file_server_url
from cloudify.decorators import operation

from cloudify_agent.api.plugins.installer import PluginInstaller
from cloudify_agent.api.factory import DaemonFactory
from cloudify_agent.api import defaults
from cloudify_agent.api import exceptions
from cloudify_agent.api import utils
from cloudify_agent.app import app

from cloudify_agent.installer.config import configuration

##########################################################################
# this array is used for creating the initial includes file of the agent
# it should contain tasks that are inside the cloudify-agent project.
##########################################################################
CLOUDIFY_AGENT_BUILT_IN_TASK_MODULES = [
    'cloudify.plugins.workflows',
    'cloudify_agent.operations',
    'cloudify_agent.installer.operations',

    # maintain backwards compatibility with version < 3.3
    'worker_installer.tasks',
    'windows_agent_installer.tasks',
    'plugin_installer.tasks',
    'windows_plugin_installer.tasks'
]

_VERSION = '3.3'


def _install_plugins(plugins):
    installer = PluginInstaller(logger=ctx.logger)
    for plugin in plugins:
        ctx.logger.info('Installing plugin: {0}'.format(plugin['name']))
        try:
            package_name = installer.install(plugin,
                                             blueprint_id=ctx.blueprint.id)
        except exceptions.PluginInstallationError as e:
            # preserve traceback
            tpe, value, tb = sys.exc_info()
            raise NonRecoverableError, NonRecoverableError(str(e)), tb
        daemon = _load_daemon(logger=ctx.logger)
        daemon.register(package_name)
        _save_daemon(daemon)


@operation
def install_plugins(plugins, **_):
    _install_plugins(plugins)


@operation
def restart(new_name=None, delay_period=5, **_):

    cloudify_agent = ctx.instance.runtime_properties['cloudify_agent']
    if new_name is None:
        new_name = utils.internal.generate_new_agent_name(
            cloudify_agent.get('name', 'agent'))

    # update agent name in runtime properties so that the workflow will
    # what the name of the worker handling tasks to this instance.
    # the update cannot be done by setting a nested property directly
    # because they are not recognized as 'dirty'
    cloudify_agent['name'] = new_name
    ctx.instance.runtime_properties['cloudify_agent'] = cloudify_agent

    # must update instance here because the process may shutdown before
    # the decorator has a chance to do it.
    ctx.instance.update()

    daemon = _load_daemon(logger=ctx.logger)

    # make the current master stop listening to the current queue
    # to avoid a situation where we have two masters listening on the
    # same queue.
    app.control.cancel_consumer(
        queue=daemon.queue,
        destination=['celery@{0}'.format(daemon.name)]
    )

    # clone the current daemon to preserve all the attributes
    attributes = utils.internal.daemon_to_dict(daemon)

    # give the new daemon the new name
    attributes['name'] = new_name

    # remove the log file and pid file so that new ones will be created
    # for the new agent
    del attributes['log_file']
    del attributes['pid_file']

    # Get the broker credentials for the daemon
    attributes.update(ctx.bootstrap_context.broker_config())

    new_daemon = DaemonFactory().new(logger=ctx.logger, **attributes)

    # create the new daemon
    new_daemon.create()
    _save_daemon(new_daemon)

    # configure the new daemon
    new_daemon.configure()
    new_daemon.start()

    # start a thread that will kill the current master.
    # this is done in a thread so that the current task will not result in
    # a failure
    thread = threading.Thread(target=shutdown_current_master,
                              args=[delay_period, ctx.logger])
    thread.daemon = True
    thread.start()


@operation
def stop(delay_period=5, **_):
    thread = threading.Thread(target=shutdown_current_master,
                              args=[delay_period, ctx.logger])
    thread.daemon = True
    thread.start()


def shutdown_current_master(delay_period, logger):
    if delay_period > 0:
        time.sleep(delay_period)
    daemon = _load_daemon(logger=logger)
    daemon.before_self_stop()
    daemon.stop()


def _load_daemon(logger):
    factory = DaemonFactory(
        username=utils.internal.get_daemon_user(),
        storage=utils.internal.get_daemon_storage_dir())
    return factory.load(utils.internal.get_daemon_name(), logger=logger)


def _save_daemon(daemon):
    factory = DaemonFactory(
        username=utils.internal.get_daemon_user(),
        storage=utils.internal.get_daemon_storage_dir())
    factory.save(daemon)


def create_new_agent_dict(old_agent):
    new_agent = {}
    new_agent['name'] = utils.internal.generate_new_agent_name(
        old_agent['name'])
    new_agent['remote_execution'] = True
    fields_to_copy = ['windows', 'ip', 'basedir', 'user']
    for field in fields_to_copy:
        if field in old_agent:
            new_agent[field] = old_agent[field]
    configuration.reinstallation_attributes(new_agent)
    new_agent['manager_file_server_url'] = get_manager_file_server_url()
    # Assuming that if there is no version info in the agent then
    # this agent was installed by current manager.
    new_agent['old_agent_version'] = old_agent.get('version', _VERSION)
    return new_agent


@contextmanager
def _celery_client(ctx, agent):
    # We retrieve broker url from old agent in order to support
    # cases when old agent is not connected to current rabbit server.
    if 'broker_config' in agent:
        broker_config = agent['broker_config']
    else:
        broker_config = ctx.bootstrap_context.broker_config()
    broker_url = utils.internal.get_broker_url(broker_config)
    celery_client = celery.Celery(broker=broker_url, backend=broker_url)
    config = {}
    if agent['old_agent_version'] != '3.2':
        config['CELERY_TASK_RESULT_EXPIRES'] = \
            defaults.CELERY_TASK_RESULT_EXPIRES
    _, cert_path = tempfile.mkstemp()
    try:
        if broker_config.get('broker_ssl_enabled'):
            with open(cert_path, 'w') as cert_file:
                cert_file.write(broker_config.get('broker_ssl_cert', ''))
            broker_ssl = {
                'ca_certs': cert_path,
                'cert_reqs': ssl.CERT_REQUIRED
            }
        else:
            broker_ssl = False
        config['BROKER_USE_SSL'] = broker_ssl
        celery_client.conf.update(**config)
        yield celery_client
    finally:
        os.remove(cert_path)


def create_agent_from_old_agent(install_agent_timeout=300):
    if 'cloudify_agent' not in ctx.instance.runtime_properties:
        raise NonRecoverableError(
            'cloudify_agent key not available in runtime_properties')
    old_agent = ctx.instance.runtime_properties['cloudify_agent']
    new_agent = create_new_agent_dict(old_agent)
    with _celery_client(ctx, old_agent) as celery_client:
        script_format = '{0}/cloudify/install_agent.py'
        script_url = script_format.format(get_manager_file_server_url())
        result = celery_client.send_task(
            'script_runner.tasks.run',
            args=[script_url],
            kwargs={'cloudify_agent': new_agent},
            queue=old_agent['queue']
        )
        returned_agent = result.get(timeout=install_agent_timeout)
    # Make sure that new celery agent was started:
    agent_status = app.control.inspect(destination=[
        'celery@{0}'.format(returned_agent['name'])])
    if not agent_status.active():
        raise NonRecoverableError('Could not start agent.')
    # Setting old_cloudify_agent in order to uninstall it later.
    old_agents = ctx.instance.runtime_properties.get('old_cloudify_agent', [])
    old_agents.append(old_agent)
    ctx.instance.runtime_properties['old_cloudify_agent'] = old_agents
    returned_agent.pop('old_agent_version', None)
    ctx.instance.runtime_properties['cloudify_agent'] = returned_agent


@operation
def create_agent_amqp(install_agent_timeout, **_):
    create_agent_from_old_agent(install_agent_timeout)
