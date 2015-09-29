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

import logging
import platform
import time
import socket
import subprocess
import os
import filecmp
import shutil
import tarfile
import tempfile

import cloudify_agent

from contextlib import contextmanager

from celery import Celery

from agent_packager import packager

from cloudify.exceptions import NonRecoverableError
from cloudify.utils import LocalCommandRunner
from cloudify.utils import setup_logger

from cloudify_agent import VIRTUALENV
from cloudify_agent.api import defaults, utils
from cloudify_agent.tests import resources, BaseTest


BUILT_IN_TASKS = [
    'cloudify.plugins.workflows.scale',
    'cloudify.plugins.workflows.auto_heal_reinstall_node_subgraph',
    'cloudify.plugins.workflows.uninstall',
    'cloudify.plugins.workflows.execute_operation',
    'cloudify.plugins.workflows.install',
    'script_runner.tasks.execute_workflow',
    'script_runner.tasks.run',
    'diamond_agent.tasks.install',
    'diamond_agent.tasks.uninstall',
    'diamond_agent.tasks.start',
    'diamond_agent.tasks.stop',
    'diamond_agent.tasks.add_collectors',
    'diamond_agent.tasks.del_collectors',
    'cloudify_agent.operations.create_agent_amqp',
    'cloudify_agent.operations.delete_old_agents_amqp',
    'cloudify_agent.operations.install_plugins',
    'cloudify_agent.operations.restart',
    'cloudify_agent.operations.stop',
    'cloudify_agent.installer.operations.create',
    'cloudify_agent.installer.operations.configure',
    'cloudify_agent.installer.operations.start',
    'cloudify_agent.installer.operations.stop',
    'cloudify_agent.installer.operations.delete',
    'cloudify_agent.installer.operations.restart',
    'worker_installer.tasks.install',
    'worker_installer.tasks.start',
    'worker_installer.tasks.stop',
    'worker_installer.tasks.restart',
    'worker_installer.tasks.uninstall',
    'windows_agent_installer.tasks.install',
    'windows_agent_installer.tasks.start',
    'windows_agent_installer.tasks.stop',
    'windows_agent_installer.tasks.restart',
    'windows_agent_installer.tasks.uninstall',
    'plugin_installer.tasks.install',
    'windows_plugin_installer.tasks.install'
]


logger = setup_logger('cloudify_agent.tests.utils')


@contextmanager
def env(key, value):
    os.environ[key] = value
    yield
    del os.environ[key]


def create_plugin_tar(plugin_dir_name, target_directory):

    """
    Create a tar file from the plugin.

    :param plugin_dir_name: the plugin directory name, relative to the
    resources package.
    :type plugin_dir_name: str
    :param target_directory: the directory to create the tar in
    :type target_directory: str

    :return: the name of the create tar, note that this is will just return
    the base name, not the full path to the tar.
    :rtype: str
    """

    plugin_source_path = resources.get_resource(os.path.join(
        'plugins', plugin_dir_name))

    plugin_tar_file_name = '{0}.tar'.format(plugin_dir_name)
    target_tar_file_path = os.path.join(target_directory,
                                        plugin_tar_file_name)

    plugin_tar_file = tarfile.TarFile(target_tar_file_path, 'w')
    plugin_tar_file.add(plugin_source_path, plugin_dir_name)
    plugin_tar_file.close()
    return plugin_tar_file_name


def get_source_uri():
    return os.path.dirname(os.path.dirname(cloudify_agent.__file__))


def get_requirements_uri():
    return os.path.join(get_source_uri(), 'dev-requirements.txt')


# This should be integrated into packager
# For now, this is the best place
def create_windows_installer(config, logger):
    runner = LocalCommandRunner()
    wheelhouse = resources.get_resource('winpackage/source/wheels')

    pip_cmd = 'pip wheel --wheel-dir {wheel_dir} --requirement {req_file}'.\
        format(wheel_dir=wheelhouse, req_file=config['requirements_file'])

    logger.info('Building wheels into: {0}'.format(wheelhouse))
    runner.run(pip_cmd)

    pip_cmd = 'pip wheel --find-links {wheel_dir} --wheel-dir {wheel_dir} ' \
              '{repo_url}'.format(wheel_dir=wheelhouse,
                                  repo_url=config['cloudify_agent_module'])
    runner.run(pip_cmd)

    iscc_cmd = 'C:\\Program Files (x86)\\Inno Setup 5\\iscc.exe {0}'\
        .format(resources.get_resource(
            os.path.join('winpackage', 'create.iss')))
    os.environ['VERSION'] = '0'
    os.environ['iscc_output'] = os.getcwd()
    runner.run(iscc_cmd)


def create_agent_package(directory, config, package_logger=None):
    if package_logger is None:
        package_logger = logger
    package_logger.info('Changing directory into {0}'.format(directory))
    original = os.getcwd()
    try:
        package_logger.info('Creating Agent Package')
        os.chdir(directory)
        if platform.system() == 'Linux':
            packager.create(config=config,
                            config_file=None,
                            force=False,
                            verbose=False)
            distname, _, distid = platform.dist()
            return '{0}-{1}-agent.tar.gz'.format(distname, distid)
        elif platform.system() == 'Windows':
            create_windows_installer(config, logger)
            return 'cloudify_agent_0.exe'
        else:
            raise NonRecoverableError('Platform not supported: {0}'
                                      .format(platform.system()))
    finally:
        os.chdir(original)


class BaseDaemonLiveTestCase(BaseTest):

    def setUp(self):
        super(BaseDaemonLiveTestCase, self).setUp()
        self.celery = Celery(broker='amqp://',
                             backend='amqp://')
        self.celery.conf.update(
            CELERY_TASK_RESULT_EXPIRES=defaults.CELERY_TASK_RESULT_EXPIRES)
        self.runner = LocalCommandRunner(logger=self.logger)
        self.daemons = []

    def tearDown(self):
        super(BaseDaemonLiveTestCase, self).tearDown()
        if os.name == 'nt':
            # with windows we need to stop and remove the service
            nssm_path = utils.get_absolute_resource_path(
                os.path.join('pm', 'nssm', 'nssm.exe'))
            for daemon in self.daemons:
                self.runner.run('sc stop {0}'.format(daemon.name),
                                exit_on_failure=False)
                self.runner.run('{0} remove {1} confirm'
                                .format(nssm_path, daemon.name),
                                exit_on_failure=False)
        else:
            self.runner.run("pkill -9 -f 'celery'", exit_on_failure=False)

    def assert_registered_tasks(self, name, additional_tasks=None):
        if not additional_tasks:
            additional_tasks = set()
        destination = 'celery@{0}'.format(name)
        c_inspect = self.celery.control.inspect(destination=[destination])
        registered = c_inspect.registered() or {}

        def include(task):
            return 'celery' not in task

        daemon_tasks = set(filter(include, set(registered[destination])))
        expected_tasks = set(BUILT_IN_TASKS)
        expected_tasks.update(additional_tasks)
        self.assertEqual(expected_tasks, daemon_tasks)

    def assert_daemon_alive(self, name):
        stats = utils.get_agent_stats(name, self.celery)
        self.assertTrue(stats is not None)

    def assert_daemon_dead(self, name):
        stats = utils.get_agent_stats(name, self.celery)
        self.assertTrue(stats is None)

    def wait_for_daemon_alive(self, name, timeout=10):
        deadline = time.time() + timeout

        while time.time() < deadline:
            stats = utils.get_agent_stats(name, self.celery)
            if stats:
                return
            self.logger.info('Waiting for daemon {0} to start...'
                             .format(name))
            time.sleep(5)
        raise RuntimeError('Failed waiting for daemon {0} to start. Waited '
                           'for {1} seconds'.format(name, timeout))

    def wait_for_daemon_dead(self, name, timeout=10):
        deadline = time.time() + timeout

        while time.time() < deadline:
            stats = utils.get_agent_stats(name, self.celery)
            if not stats:
                return
            self.logger.info('Waiting for daemon {0} to stop...'
                             .format(name))
            time.sleep(1)
        raise RuntimeError('Failed waiting for daemon {0} to stop. Waited '
                           'for {1} seconds'.format(name, timeout))


class AgentPackageTest(BaseDaemonLiveTestCase):

    @classmethod
    def setUpClass(cls):
        cls._resources_dir = tempfile.mkdtemp(
            prefix='file-server-resource-base')
        cls._fs = FileServer(
            root_path=cls._resources_dir)
        cls._fs.start()
        if 'AGENT_PACKAGE_URL' in os.environ:
            cls.package_url = os.environ['AGENT_PACKAGE_URL']
        else:
            config = {
                'cloudify_agent_module': get_source_uri(),
                'requirements_file': get_requirements_uri()
            }
            package_name = create_agent_package(cls._resources_dir, config)
            cls.package_url = 'http://localhost:{0}/{1}'.format(
                cls._fs.port, package_name)

    @classmethod
    def tearDownClass(cls):
        cls._fs.stop()
        shutil.rmtree(cls._resources_dir)


def are_dir_trees_equal(dir1, dir2):

    """
    Compare two directories recursively. Files in each directory are
    assumed to be equal if their names and contents are equal.

    :param dir1: First directory path
    :type dir1: str
    :param dir2: Second directory path
    :type dir2: str

    :return: True if the directory trees are the same and
             there were no errors while accessing the directories or files,
             False otherwise.
    :rtype: bool
   """

    # compare file lists in both dirs. If found different lists
    # or "funny" files (failed to compare) - return false
    dirs_cmp = filecmp.dircmp(dir1, dir2)
    if len(dirs_cmp.left_only) > 0 or len(dirs_cmp.right_only) > 0 or \
            len(dirs_cmp.funny_files) > 0:
        return False

    # compare the common files between dir1 and dir2
    (match, mismatch, errors) = filecmp.cmpfiles(
        dir1, dir2, dirs_cmp.common_files, shallow=False)
    if len(mismatch) > 0 or len(errors) > 0:
        return False

    # continue to compare sub-directories, recursively
    for common_dir in dirs_cmp.common_dirs:
        new_dir1 = os.path.join(dir1, common_dir)
        new_dir2 = os.path.join(dir2, common_dir)
        if not are_dir_trees_equal(new_dir1, new_dir2):
            return False

    return True


class FileServer(object):

    def __init__(self, root_path=None, port=5555):
        self.port = port
        self.root_path = root_path or os.path.dirname(resources.__file__)
        self.process = None
        self.logger = setup_logger('cloudify_agent.tests.utils.FileServer',
                                   logger_level=logging.DEBUG)
        self.runner = LocalCommandRunner(self.logger)

    def start(self, timeout=5):
        if os.name == 'nt':
            serve_path = os.path.join(VIRTUALENV, 'Scripts', 'serve')
        else:
            serve_path = os.path.join(VIRTUALENV, 'bin', 'serve')

        self.process = subprocess.Popen(
            [serve_path, '-p', str(self.port), self.root_path],
            stdin=open(os.devnull, 'w'),
            stdout=None,
            stderr=None)

        end_time = time.time() + timeout

        while end_time > time.time():
            if self.is_alive():
                logger.info('File server is up and serving from {0} ({1})'
                            .format(self.root_path, self.process.pid))
                return
            logger.info('File server is not responding. waiting 10ms')
            time.sleep(0.1)
        raise RuntimeError('FileServer failed to start')

    def stop(self, timeout=15):
        if self.process is None:
            return

        end_time = time.time() + timeout

        if os.name == 'nt':
            self.runner.run('taskkill /F /T /PID {0}'.format(self.process.pid),
                            stdout_pipe=False, stderr_pipe=False,
                            exit_on_failure=False)
        else:
            self.runner.run('kill -9 {0}'.format(self.process.pid))

        while end_time > time.time():
            if not self.is_alive():
                logger.info('File server has shutdown')
                return
            logger.info('File server is still running. waiting 10ms')
            time.sleep(0.1)
        raise RuntimeError('FileServer failed to stop')

    def is_alive(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.connect(('localhost', self.port))
            s.close()
            return True
        except socket.error:
            return False
