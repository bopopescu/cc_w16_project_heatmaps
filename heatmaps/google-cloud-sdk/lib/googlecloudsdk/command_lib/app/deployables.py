# -*- coding: utf-8 -*- #
# Copyright 2017 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Utilities for deriving services and configs from paths.

Paths are typically given as positional params, like
`gcloud app deploy <path1> <path2>...`.
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import unicode_literals

import collections
import os

from googlecloudsdk.api_lib.app import deploy_command_util
from googlecloudsdk.api_lib.app import env
from googlecloudsdk.api_lib.app import yaml_parsing
from googlecloudsdk.command_lib.app import exceptions
from googlecloudsdk.core import log

_STANDARD_APP_YAML_URL = (
    'https://cloud.google.com/appengine/docs/standard/python/config/appref')
_FLEXIBLE_APP_YAML_URL = (
    'https://cloud.google.com/'
    'appengine/docs/flexible/python/configuring-your-app-with-app-yaml')
FINGERPRINTING_WARNING = (
    'As an alternative, create an app.yaml file yourself using the '
    'directions at {flex} (App Engine Flexible Environment) or {std} (App '
    'Engine Standard Environment) under the tab for your language.').format(
        flex=_FLEXIBLE_APP_YAML_URL, std=_STANDARD_APP_YAML_URL)


class Service(object):
  """Represents data around a deployable service.

  Attributes:
    descriptor: str, File path to the original deployment descriptor, which is
      either a `<service>.yaml` or an `appengine-web.xml`.
    source: str, Path to the original deployable artifact or directory, which
      is typically the original source directory, but could also be an artifact
      such as a fat JAR file.
    service_info: yaml_parsing.ServiceYamlInfo, Info parsed from the
      `<service>.yaml` file. Note that service_info.file may point to a
      file in a staged directory.
    upload_dir: str, Path to the source directory. If staging is required, this
      points to the staged directory.
    service_id: str, the service id.
    path: str, File path to the staged deployment `<service>.yaml` descriptor
      or to the original one, if no staging is used.
  """

  def __init__(self, descriptor, source, service_info, upload_dir):
    self.descriptor = descriptor
    self.source = source
    self.service_info = service_info
    self.upload_dir = upload_dir

  @property
  def service_id(self):
    return self.service_info.module

  @property
  def path(self):
    return self.service_info.file

  @classmethod
  def FromPath(cls, path, stager, path_matchers):
    """Return a Service from a path using staging if necessary.

    Args:
      path: str, Unsanitized absolute path, may point to a directory or a file
          of any type. There is no guarantee that it exists.
      stager: staging.Stager, stager that will be invoked if there is a runtime
          and environment match.
      path_matchers: List[Function], ordered list of functions on the form
          fn(path, stager), where fn returns a Service or None if no match.

    Returns:
      Service, if one can be derived, else None.
    """
    for matcher in path_matchers:
      service = matcher(path, stager)
      if service:
        return service
    return None


def ServiceYamlMatcher(path, stager):
  """Generate a Service from an <service>.yaml source path.

  This function is a path matcher that returns if and only if:
  - `path` points to either a `<service>.yaml` or `<app-dir>` where
    `<app-dir>/app.yaml` exists.
  - the yaml-file is a valid <service>.yaml file.

  If the runtime and environment match an entry in the stager, the service will
  be staged into a directory.

  Args:
    path: str, Unsanitized absolute path, may point to a directory or a file of
        any type. There is no guarantee that it exists.
    stager: staging.Stager, stager that will be invoked if there is a runtime
        and environment match.

  Raises:
    staging.StagingCommandFailedError, staging command failed.

  Returns:
    Service, fully populated with entries that respect a potentially
        staged deployable service, or None if the path does not match the
        pattern described.
  """
  descriptor = path if os.path.isfile(path) else os.path.join(path,
                                                              'app.yaml')
  _, ext = os.path.splitext(descriptor)
  if os.path.exists(descriptor) and ext in ['.yaml', '.yml']:
    app_dir = os.path.dirname(descriptor)
    service_info = yaml_parsing.ServiceYamlInfo.FromFile(descriptor)
    staging_dir = stager.Stage(descriptor, app_dir, service_info.runtime,
                               service_info.env)
    # If staging, stage, get stage_dir
    return Service(descriptor, app_dir, service_info, staging_dir or app_dir)
  return None


def AppengineWebMatcher(path, stager):
  """Generate a Service from an appengine-web.xml source path.

  This function is a path matcher that returns if and only if:
  - `path` points to either `.../WEB-INF/appengine-web.xml` or `<app-dir>` where
    `<app-dir>/WEB-INF/appengine-web.xml` exists.
  - the xml-file is a valid appengine-web.xml file according to the Java stager.

  The service will be staged according to the stager as a java-xml runtime,
  which is defined in staging.py.

  Args:
    path: str, Unsanitized absolute path, may point to a directory or a file of
        any type. There is no guarantee that it exists.
    stager: staging.Stager, stager that will be invoked if there is a runtime
        and environment match.

  Raises:
    staging.StagingCommandFailedError, staging command failed.

  Returns:
    Service, fully populated with entries that respect a staged deployable
        service, or None if the path does not match the pattern described.
  """
  suffix = os.path.join(os.sep, 'WEB-INF', 'appengine-web.xml')
  app_dir = path[:-len(suffix)] if path.endswith(suffix) else path
  descriptor = os.path.join(app_dir, 'WEB-INF', 'appengine-web.xml')
  if not os.path.isfile(descriptor):
    return None
  staging_dir = stager.Stage(descriptor, app_dir, 'java-xml', env.STANDARD)
  if not staging_dir:
    # After GA launch of appengine-web.xml support, this should never occur.
    return None
  yaml_path = os.path.join(staging_dir, 'app.yaml')
  service_info = yaml_parsing.ServiceYamlInfo.FromFile(yaml_path)
  return Service(descriptor, app_dir, service_info, staging_dir)


def UnidentifiedDirMatcher(path, stager):
  """Generate a Service from a potential app directory.

  This function is a path matcher that returns if and only if:
  - `path` points to an `<app-dir>` where the fingerprinter identifies a runtime
    and the user opts in to writing an `app.yaml` into `<app-dir>.

  If the runtime and environment match an entry in the stager, the service will
  be staged into a directory.

  Args:
    path: str, Unsanitized absolute path, may point to a directory or a file of
        any type. There is no guarantee that it exists.
    stager: staging.Stager, stager that will be invoked if there is a runtime
        and environment match.

  Raises:
    staging.StagingCommandFailedError, staging command failed.

  Returns:
    Service, fully populated with entries that respect a potentially
        staged deployable service, or None if the path does not fulfill the
        requirements described above.
  """
  if os.path.isdir(path):
    log.warning(
        'Automatic app detection is deprecated and will soon be removed. ' +
        FINGERPRINTING_WARNING)
    yaml = deploy_command_util.CreateAppYamlForAppDirectory(path)
    return ServiceYamlMatcher(yaml, stager)


def GetPathMatchers():
  """Get list of path matchers ordered by ascending precedence.

  Returns:
    List[Function], ordered list of functions on the form fn(path, stager),
    where fn returns a Service or None if no match.
  """
  return [
      ServiceYamlMatcher,
      AppengineWebMatcher,
      UnidentifiedDirMatcher]


class Services(object):
  """Collection of deployable services."""

  def __init__(self, services=None):
    """Instantiate a set of deployable services.

    Args:
      services: List[Service], optional list of services for quick
          initialization.

    Raises:
      DuplicateServiceError: Two or more services have the same service id.
    """
    self._services = collections.OrderedDict()
    if services:
      for d in services:
        self.Add(d)

  def Add(self, service):
    """Add a deployable service to the set.

    Args:
      service: Service, to add.

    Raises:
      DuplicateServiceError: Two or more services have the same service id.
    """
    existing = self._services.get(service.service_id)
    if existing:
      raise exceptions.DuplicateServiceError(existing.path, service.path,
                                             service.service_id)
    self._services[service.service_id] = service

  def GetAll(self):
    """Retreive the service info objects in the order they were added.

    Returns:
      List[Service], list of services.
    """
    return list(self._services.values())


class Configs(object):
  """Collection of config files."""

  def __init__(self):
    self._configs = collections.OrderedDict()

  def Add(self, config):
    """Add a ConfigYamlInfo to the set of configs.

    Args:
      config: ConfigYamlInfo, the config to add.

    Raises:
      exceptions.DuplicateConfigError, the config type is already in the set.
    """
    config_type = config.config
    existing = self._configs.get(config_type)
    if existing:
      raise exceptions.DuplicateConfigError(existing.file, config.file,
                                            config_type)
    self._configs[config_type] = config

  def GetAll(self):
    """Retreive the config file objects in the order they were added.

    Returns:
      List[ConfigYamlInfo], list of config file objects.
    """
    return list(self._configs.values())


def GetDeployables(args, stager, path_matchers):
  """Given a list of args, infer the deployable services and configs.

  Given a deploy command, e.g. `gcloud app deploy ./dir other/service.yaml
  cron.yaml WEB-INF/appengine-web.xml`, the deployables can be on multiple
  forms. This method pre-processes and infers yaml descriptors from the
  various formats accepted. The rules are as following:

  This function is a context manager, and should be used in conjunction with
  the `with` keyword.

  1. If `args` is an empty list, add the current directory to it.
  2. For each arg:
    - If arg refers to a config file, add it to the configs set.
    - Else match the arg against the path matchers. The first match will win.
      The match will be added to the services set. Matchers may run staging.

  Args:
    args: List[str], positional args as given on the command-line.
    stager: staging.Stager, stager that will be invoked on sources that have
        entries in the stager's registry.
    path_matchers: List[Function], list of functions on the form
        fn(path, stager) ordered by ascending precendence, where fn returns
        a Service or None if no match.

  Raises:
    FileNotFoundError: One or more argument does not point to an existing file
        or directory.
    UnknownSourceError: Could not infer a config or service from an arg.
    DuplicateConfigError: Two or more config files have the same type.
    DuplicateServiceError: Two or more services have the same service id.

  Returns:
    Tuple[List[Service], List[ConfigYamlInfo]], lists of deployable services
    and configs.
  """
  if not args:
    args = ['.']
  paths = [os.path.abspath(arg) for arg in args]
  configs = Configs()
  services = Services()
  for path in paths:
    if not os.path.exists(path):
      raise exceptions.FileNotFoundError(path)
    config = yaml_parsing.ConfigYamlInfo.FromFile(path)
    if config:
      configs.Add(config)
      continue
    service = Service.FromPath(path, stager, path_matchers)
    if service:
      services.Add(service)
      continue
    raise exceptions.UnknownSourceError(path)
  return services.GetAll(), configs.GetAll()

