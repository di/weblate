# -*- coding: utf-8 -*-
#
# Copyright © 2012 - 2020 Michal Čihař <michal@cihar.com>
#
# This file is part of Weblate <https://weblate.org/>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
"""Version control system abstraction for Weblate needs."""


import hashlib
import logging
import os
import os.path
import subprocess
from distutils.version import LooseVersion

from dateutil import parser
from django.conf import settings
from django.core.cache import cache
from django.utils.functional import cached_property
from filelock import FileLock
from pkg_resources import Requirement, resource_filename
from sentry_sdk import add_breadcrumb

from weblate.trans.util import (
    add_configuration_error,
    delete_configuration_error,
    get_clean_env,
    path_separator,
)
from weblate.vcs.ssh import SSH_WRAPPER

LOGGER = logging.getLogger('weblate.vcs')


class RepositoryException(Exception):
    """Error while working with a repository."""

    def __init__(self, retcode, message):
        super().__init__(message)
        self.retcode = retcode

    def get_message(self):
        if self.retcode != 0:
            return '{0} ({1})'.format(self.args[0], self.retcode)
        return self.args[0]

    def __str__(self):
        return self.get_message()


class Repository:
    """Basic repository object."""

    _cmd = 'false'
    _cmd_last_revision = None
    _cmd_last_remote_revision = None
    _cmd_status = ['status']
    _cmd_list_changed_files = None

    name = None
    req_version = None
    default_branch = ''

    _is_supported = None
    _version = None

    @classmethod
    def get_identifier(cls):
        return cls.name.lower()

    def __init__(self, path, branch=None, component=None, local=False):
        self.path = path
        if branch is None:
            self.branch = self.default_branch
        else:
            self.branch = branch
        self.component = component
        self.last_output = ''
        self.lock = FileLock(self.path.rstrip('/').rstrip('\\') + '.lock', timeout=120)
        self.local = local
        if not local:
            # Create ssh wrapper for possible use
            SSH_WRAPPER.create()
            if not self.is_valid():
                self.init()

    @classmethod
    def add_breadcrumb(cls, message, **data):
        # Add breadcrumb only if settings are already loaded,
        # we do not want to force loading settings early
        if settings.configured and getattr(settings, 'SENTRY_DSN', None):
            add_breadcrumb(category='vcs', message=message, data=data, level='info')

    @classmethod
    def log(cls, message):
        return LOGGER.debug('%s: %s', cls._cmd, message)

    def ensure_config_updated(self):
        """Ensures the configuration is periodically checked."""
        cache_key = "sp-config-check-{}".format(self.component.pk)
        if cache.get(cache_key) is None:
            self.check_config()
            cache.set(cache_key, True, 86400)

    def check_config(self):
        """Check VCS configuration."""
        raise NotImplementedError()

    def is_valid(self):
        """Check whether this is a valid repository."""
        raise NotImplementedError()

    def init(self):
        """Initialize the repository."""
        raise NotImplementedError()

    def resolve_symlinks(self, path):
        """Resolve any symlinks in the path."""
        # Resolve symlinks first
        real_path = path_separator(os.path.realpath(os.path.join(self.path, path)))
        repository_path = path_separator(os.path.realpath(self.path))

        if not real_path.startswith(repository_path):
            raise ValueError('Too many symlinks or link outside tree')

        return real_path[len(repository_path) :].lstrip('/')

    @staticmethod
    def _getenv():
        """Generate environment for process execution."""
        return get_clean_env(
            {'GIT_SSH': SSH_WRAPPER.filename, 'GIT_TERMINAL_PROMPT': '0'}
        )

    @classmethod
    def _popen(
        cls, args, cwd=None, merge_err=True, fullcmd=False, raw=False, local=False
    ):
        """Execute the command using popen."""
        if args is None:
            raise RepositoryException(0, 'Not supported functionality')
        if not fullcmd:
            args = [cls._cmd] + list(args)
        text_cmd = ' '.join(args)
        process = subprocess.Popen(
            args,
            cwd=cwd,
            env={} if local else cls._getenv(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT if merge_err else subprocess.PIPE,
            stdin=subprocess.PIPE,
        )
        output, stderr = process.communicate()
        if not raw:
            output = output.decode()
        retcode = process.poll()
        cls.add_breadcrumb(
            text_cmd, retcode=retcode, output=output, stderr=stderr, cwd=cwd
        )
        cls.log('exec {0} [retcode={1}]'.format(text_cmd, retcode))
        if retcode:
            if stderr:
                output += stderr.decode()
            raise RepositoryException(retcode, output)
        return output

    def execute(self, args, needs_lock=True, fullcmd=False, merge_err=True):
        """Execute command and caches its output."""
        if needs_lock:
            if not self.lock.is_locked:
                raise RuntimeError('Repository operation without lock held!')
            if self.component:
                self.ensure_config_updated()
        is_status = args[0] == self._cmd_status[0]
        try:
            self.last_output = self._popen(
                args, self.path, fullcmd=fullcmd, local=self.local, merge_err=merge_err
            )
        except RepositoryException as error:
            if not is_status:
                self.log_status(error)
            raise
        return self.last_output

    def log_status(self, error):
        try:
            self.log('failure {}'.format(error))
            self.log(self.status())
        except RepositoryException:
            pass

    def clean_revision_cache(self):
        if 'last_revision' in self.__dict__:
            del self.__dict__['last_revision']
        if 'last_remote_revision' in self.__dict__:
            del self.__dict__['last_remote_revision']

    @cached_property
    def last_revision(self):
        """Return last local revision."""
        return self.get_last_revision()

    def get_last_revision(self):
        return self.execute(self._cmd_last_revision, needs_lock=False, merge_err=False)

    @cached_property
    def last_remote_revision(self):
        """Return last remote revision."""
        return self.execute(
            self._cmd_last_remote_revision, needs_lock=False, merge_err=False
        )

    @classmethod
    def _clone(cls, source, target, branch=None):
        """Clone repository."""
        raise NotImplementedError()

    @classmethod
    def clone(cls, source, target, branch=None, component=None):
        """Clone repository and return object for cloned repository."""
        SSH_WRAPPER.create()
        cls._clone(source, target, branch)
        return cls(target, branch, component)

    def update_remote(self):
        """Update remote repository."""
        raise NotImplementedError()

    def status(self):
        """Return status of the repository."""
        with self.lock:
            return self.execute(self._cmd_status)

    def push(self):
        """Push given branch to remote repository."""
        raise NotImplementedError()

    def unshallow(self):
        """Unshallow working copy."""
        return

    def reset(self):
        """Reset working copy to match remote branch."""
        raise NotImplementedError()

    def merge(self, abort=False, message=None):
        """Merge remote branch or reverts the merge."""
        raise NotImplementedError()

    def rebase(self, abort=False):
        """Rebase working copy on top of remote branch."""
        raise NotImplementedError()

    def needs_commit(self, *filenames):
        """Check whether repository needs commit."""
        raise NotImplementedError()

    def count_missing(self):
        """Count missing commits."""
        return len(
            self.log_revisions(self.ref_to_remote.format(self.get_remote_branch_name()))
        )

    def count_outgoing(self):
        """Count outgoing commits."""
        return len(
            self.log_revisions(
                self.ref_from_remote.format(self.get_remote_branch_name())
            )
        )

    def needs_merge(self):
        """Check whether repository needs merge with upstream.

        It is missing some revisions.
        """
        return self.count_missing() > 0

    def needs_push(self):
        """Check whether repository needs push to upstream.

        It has additional revisions.
        """
        return self.count_outgoing() > 0

    def _get_revision_info(self, revision):
        """Return dictionary with detailed revision information."""
        raise NotImplementedError()

    def get_revision_info(self, revision):
        """Return dictionary with detailed revision information."""
        key = 'rev-info-{}-{}'.format(self.get_identifier(), revision)
        result = cache.get(key)
        if not result:
            result = self._get_revision_info(revision)
            # Keep the cache for one day
            cache.set(key, result, 86400)

        # Parse timestamps into datetime objects
        for name, value in result.items():
            if 'date' in name:
                result[name] = parser.parse(value)

        return result

    @classmethod
    def is_supported(cls):
        """Check whether this VCS backend is supported."""
        if cls._is_supported is not None:
            return cls._is_supported
        try:
            version = cls.get_version()
        except (OSError, RepositoryException):
            cls._is_supported = False
            return False
        try:
            if cls.req_version is None or LooseVersion(version) >= LooseVersion(
                cls.req_version
            ):
                cls._is_supported = True
                delete_configuration_error(cls.name.lower())
                return True
        except Exception as error:
            add_configuration_error(
                cls.name.lower(),
                '{0} version check failed (version {1}, required {2}): {3}'.format(
                    cls.name, version, cls.req_version, error
                ),
            )
        else:
            add_configuration_error(
                cls.name.lower(),
                '{0} version is too old, please upgrade to {1}.'.format(
                    cls.name, cls.req_version
                ),
            )
        cls._is_supported = False
        return False

    @classmethod
    def get_version(cls):
        """Cached getting of version."""
        if cls._version is None:
            cls._version = cls._get_version()
        return cls._version

    @classmethod
    def _get_version(cls):
        """Return VCS program version."""
        return cls._popen(['--version'], merge_err=False)

    def set_committer(self, name, mail):
        """Configure commiter name."""
        raise NotImplementedError()

    def commit(self, message, author=None, timestamp=None, files=None):
        """Create new revision."""
        raise NotImplementedError()

    def remove(self, files, message, author=None):
        """Remove files and creates new revision."""
        raise NotImplementedError()

    @staticmethod
    def update_hash(objhash, filename, extra=None):
        with open(filename, 'rb') as handle:
            data = handle.read()
        if extra:
            objhash.update(extra.encode())
        objhash.update('blob {0}\0'.format(len(data)).encode('ascii'))
        objhash.update(data)

    def get_object_hash(self, path):
        """Return hash of object in the VCS.

        For files in a way compatible with Git (equivalent to git ls-tree HEAD), for
        dirs it behaves differently as we do not need to track some attributes (for
        example permissions).
        """
        real_path = os.path.join(self.path, self.resolve_symlinks(path))
        objhash = hashlib.sha1()

        if os.path.isdir(real_path):
            files = []
            for root, _unused, filenames in os.walk(real_path):
                for filename in filenames:
                    full_name = os.path.join(root, filename)
                    files.append((full_name, os.path.relpath(full_name, self.path)))
            for filename, name in sorted(files):
                self.update_hash(objhash, filename, name)
        else:
            self.update_hash(objhash, real_path)

        return objhash.hexdigest()

    def configure_remote(self, pull_url, push_url, branch):
        """Configure remote repository."""
        raise NotImplementedError()

    def configure_branch(self, branch):
        """Configure repository branch."""
        raise NotImplementedError()

    def describe(self):
        """Verbosely describes current revision."""
        raise NotImplementedError()

    def get_file(self, path, revision):
        """Return content of file at given revision."""
        raise NotImplementedError()

    @staticmethod
    def get_examples_paths():
        """Generator of possible paths for examples."""
        yield os.path.join(os.path.dirname(os.path.dirname(__file__)), 'examples')
        yield resource_filename(Requirement.parse('weblate'), 'examples')

    @classmethod
    def find_merge_driver(cls, name):
        for path in cls.get_examples_paths():
            result = os.path.join(path, name)
            if os.path.exists(result):
                return os.path.abspath(result)
        return None

    @classmethod
    def get_merge_driver(cls, file_format):
        merge_driver = None
        if file_format == 'po':
            merge_driver = cls.find_merge_driver('git-merge-gettext-po')
        if merge_driver is None or not os.path.exists(merge_driver):
            return None
        return merge_driver

    def cleanup(self):
        """Remove not tracked files from the repository."""
        raise NotImplementedError()

    def log_revisions(self, refspec):
        """Log revisions for given refspec.

        This is not universal as refspec is different per vcs.
        """
        raise NotImplementedError()

    def list_changed_files(self, refspec):
        """List changed files for given refspec.

        This is not universal as refspec is different per vcs.
        """
        lines = self.execute(
            self._cmd_list_changed_files + [refspec], needs_lock=False, merge_err=False
        ).splitlines()
        return self.parse_changed_files(lines)

    def parse_changed_files(self, lines):
        """Parses output with chanaged files."""
        raise NotImplementedError()

    def list_upstream_changed_files(self):
        """List files missing upstream."""
        return list(
            self.list_changed_files(
                self.ref_to_remote.format(self.get_remote_branch_name())
            )
        )

    def get_remote_branch_name(self):
        return 'origin/{0}'.format(self.branch)

    def list_remote_branches(self):
        return []
