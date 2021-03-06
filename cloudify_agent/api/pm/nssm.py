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

import os

from cloudify.exceptions import CommandExecutionException

from cloudify_agent import VIRTUALENV
from cloudify_agent.api import defaults
from cloudify_agent.api import exceptions
from cloudify_agent.api import utils
from cloudify_agent.api.pm.base import Daemon
from cloudify_agent.included_plugins import included_plugins


class NonSuckingServiceManagerDaemon(Daemon):

    """
    Implementation for the nssm windows service management.
    Based on the nssm service management. see https://nssm.cc/

    Following are all possible custom key-word arguments
    (in addition to the ones available in the base daemon)

    ``startup_policy``

        Specifies the start type for the service.
        possible values are:

            boot - A device driver that is loaded by the boot loader.
            system - A device driver that is started during kernel
                     initialization
            auto - A service that automatically starts each time the
                   computer is restarted and runs even if no one logs on to
                   the computer.
            demand - A service that must be manually started. This is the
                    default value if start= is not specified.
            disabled - A service that cannot be started. To start a disabled
                       service, change the start type to some other value.

    ``failure_reset_timeout``

        Specifies the length of the period (in seconds) with no failures
        after which the failure count should be reset to 0.

    ``failure_restart_delay``

        Specifies delay time (in milliseconds) for the restart action.
    """

    PROCESS_MANAGEMENT = 'nssm'

    RUNNING_STATES = ['SERVICE_RUNNING', 'SERVICE_STOP_PENDING']

    def __init__(self, logger=None, **params):
        super(NonSuckingServiceManagerDaemon, self).__init__(
            logger=logger, **params)

        self.config_path = os.path.join(
            self.workdir,
            '{0}.conf.bat'.format(self.name))
        self.nssm_path = utils.get_absolute_resource_path(
            os.path.join('pm', 'nssm', 'nssm.exe'))
        self.startup_policy = params.get('startup_policy', 'auto')
        self.failure_reset_timeout = params.get('failure_reset_timeout', 60)
        self.failure_restart_delay = params.get('failure_restart_delay', 5000)

    def configure(self):

        env_string = self._create_env_string()

        # creating the installation script
        self._logger.debug('Rendering configuration script from template')
        utils.render_template_to_file(
            template_path='pm/nssm/nssm.conf.template',
            file_path=self.config_path,
            queue=self.queue,
            nssm_path=self.nssm_path,
            log_level=self.log_level,
            log_file=self.log_file,
            pid_file=self.pid_file,
            workdir=self.workdir,
            user=self.user,
            manager_ip=self.manager_ip,
            manager_port=self.manager_port,
            broker_url=self.broker_url,
            min_workers=self.min_workers,
            max_workers=self.max_workers,
            includes=','.join(self.includes),
            virtualenv_path=VIRTUALENV,
            name=self.name,
            storage_dir=utils.internal.get_storage_directory(self.user),
            custom_environment=env_string,
            startup_policy=self.startup_policy,
            failure_reset_timeout=self.failure_reset_timeout,
            failure_restart_delay=self.failure_restart_delay
        )

        self._logger.debug('Rendered configuration script: {0}'.format(
            self.config_path))

        # run the configuration script
        self._logger.info('Running configuration script')
        self._runner.run(self.config_path)
        self._logger.debug('Successfully executed configuration script')

        # register plugins
        for plugin in included_plugins:
            self.register(plugin)

    def apply_includes(self):
        output = self._runner.run(
            '{0} get {1} AppParameters'.format(
                self.nssm_path, self.name)).std_out

        # apparently nssm output is encoded in utf16.
        # encode to ascii to be able to parse this
        app_parameters = output.decode('utf16').encode('utf-8').rstrip()

        current_includes = app_parameters.split('--include=')[1].split()[0]

        new_app_parameters = app_parameters.replace(
            current_includes,
            ','.join(self.includes))

        self._runner.run('{0} set {1} AppParameters {2}'
                         .format(self.nssm_path, self.name,
                                 new_app_parameters))

    def delete(self, force=defaults.DAEMON_FORCE_DELETE):
        self._logger.debug('Retrieving daemon stats')
        stats = utils.get_agent_stats(self.name, self._celery)
        if stats:
            if not force:
                raise exceptions.DaemonStillRunningException(self.name)
            self.stop()

        self._logger.info('Removing {0} service'.format(
            self.name))
        self._runner.run('{0} remove {1} confirm'.format(
            self.nssm_path,
            self.name))

        self._logger.debug('Deleting {0}'.format(self.config_path))
        if os.path.exists(self.config_path):
            os.remove(self.config_path)

    def start_command(self):
        if not os.path.isfile(self.config_path):
            raise exceptions.DaemonNotConfiguredError(self.name)
        return 'sc start {0}'.format(self.name)

    def stop_command(self):
        return 'sc stop {0}'.format(self.name)

    def status(self):
        try:
            command = '{0} status {1}'.format(self.nssm_path, self.name)
            response = self._runner.run(command)
            # apparently nssm output is encoded in utf16.
            # encode to ascii to be able to parse this
            state = response.std_out.decode('utf16').encode(
                'utf-8').rstrip()
            self._logger.info(state)
            if state in self.RUNNING_STATES:
                return True
            else:
                return False
        except CommandExecutionException as e:
            self._logger.debug(str(e))
            return False

    def _create_env_string(self):
        env_string = ''
        if self.extra_env_path and os.path.exists(self.extra_env_path):
            with open(self.extra_env_path) as f:
                content = f.read()
            for line in content.splitlines():
                if line.startswith('set'):
                    parts = line.split(' ')[1].split('=')
                    key = parts[0]
                    value = parts[1]
                    env_string = '{0} {1}={2}'.format(env_string, key, value)
        return env_string.rstrip()
