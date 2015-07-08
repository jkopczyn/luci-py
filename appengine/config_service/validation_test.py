#!/usr/bin/env python
# Copyright 2015 The Swarming Authors. All rights reserved.
# Use of this source code is governed by the Apache v2.0 license that can be
# found in the LICENSE file.

import base64
import logging

from test_env import future
import test_env
test_env.setup_test_env()

from google.appengine.ext import ndb

from test_support import test_case
import mock

from components import config
from components import net
from components.config import validation_context

from proto import project_config_pb2
from proto import service_config_pb2
import services
import storage
import validation


class ValidationTestCase(test_case.TestCase):
  def setUp(self):
    super(ValidationTestCase, self).setUp()
    self.services = []
    self.mock(services, 'get_services_async', lambda: future(self.services))

  def test_validate_project_registry(self):
    cfg = '''
      projects {
        id: "a"
        config_location {
          storage_type: GITILES
          url: "https://a.googlesource.com/project"
        }
      }
      projects {
        id: "b"
      }
      projects {
        id: "a"
        config_location {
          storage_type: GITILES
          url: "https://no-project.googlesource.com"
        }
      }
      projects {
        config_location {
          storage_type: GITILES
          url: "https://no-project.googlesource.com/bad_plus/+"
        }
      }
    '''
    result = validation.validate_config(
        config.self_config_set(), 'projects.cfg', cfg)

    self.assertEqual(
        [m.text for m in result.messages],
        [
          'Project b: config_location: storage_type is not set',
          'Project a: id is not unique',
          ('Project a: config_location: Invalid Gitiles repo url: '
           'https://no-project.googlesource.com'),
          'Project #4: id is not specified',
          ('Project #4: config_location: Invalid Gitiles repo url: '
           'https://no-project.googlesource.com/bad_plus/+'),
          'Projects are not sorted by id. First offending id: a',
        ]
    )

  def test_validate_acl_cfg(self):
    cfg = '''
      invalid_field: "admins"
    '''
    result = validation.validate_config(
        config.self_config_set(), 'acl.cfg', cfg)
    self.assertEqual(len(result.messages), 1)
    self.assertEqual(result.messages[0].severity, logging.ERROR)
    self.assertTrue(
        result.messages[0].text.startswith('Could not parse config'))

    cfg = '''
      project_access_group: "admins"
    '''
    result = validation.validate_config(
        config.self_config_set(), 'acl.cfg', cfg)
    self.assertEqual(len(result.messages), 0)

  def test_validate_services_registry(self):
    cfg = '''
      services {
        id: "a"
        access: "a@a.com"
        access: "user:a@a.com"
        access: "group:abc"
      }
      services {
        owners: "not an email"
        config_location {
          storage_type: GITILES
          url: "../some"
        }
        metadata_url: "not an url"
        access: "**&"
        access: "group:**&"
        access: "a:b"
      }
      services {
        id: "b"
        config_location {
          storage_type: GITILES
          url: "https://gitiles.host.com/project"
        }
      }
      services {
        id: "a-unsorted"
      }
    '''
    result = validation.validate_config(
        config.self_config_set(), 'services.cfg', cfg)

    self.assertEqual(
        [m.text for m in result.messages],
        [
          'Service #2: id is not specified',
          ('Service #2: config_location: '
           'storage_type must not be set if relative url is used'),
          'Service #2: invalid email: "not an email"',
          'Service #2: metadata_url: hostname not specified',
          'Service #2: metadata_url: scheme must be "https"',
          'Service #2: access #1: invalid email: "**&"',
          'Service #2: access #2: invalid group: **&',
          'Service #2: access #3: Identity has invalid format: b',
          'Services are not sorted by id. First offending id: a-unsorted',
        ]
    )


  def test_validate_service_dynamic_metadata_blob(self):
    def expect_errors(blob, expected_messages):
      ctx = config.validation.Context()
      validation.validate_service_dynamic_metadata_blob(blob, ctx)
      self.assertEqual(
          [m.text for m in ctx.result().messages], expected_messages)

    expect_errors([], ['Service dynamic metadata must be an object'])
    expect_errors({}, ['Expected format version 1.0, but found "None"'])
    expect_errors(
        {'version': '1.0', 'validation': 'bad'},
        ['validation: must be an object'])
    expect_errors(
        {
          'version': '1.0',
          'validation': {
            'patterns': 'bad',
          }
        },
        [
          'validation: url: not specified',
          'validation: patterns must be a list',
        ])
    expect_errors(
      {
        'version': '1.0',
        'validation': {
          'url': 'bad url',
          'patterns': [
            'bad',
            {
            },
            {
              'config_set': 'a:b',
              'path': '/foo',
            },
            {
              'config_set': 'regex:)(',
              'path': '../b',
            },
            {
              'config_set': 'projects/foo',
              'path': 'bar.cfg',
            },
          ]
        }
      },
      [
        'validation: url: hostname not specified',
        'validation: url: scheme must be "https"',
        'validation: pattern #1: must be an object',
        'validation: pattern #2: config_set: Pattern must be a string',
        'validation: pattern #2: path: Pattern must be a string',
        'validation: pattern #3: config_set: Invalid pattern kind: a',
        'validation: pattern #3: path: must not be absolute: /foo',
        'validation: pattern #4: config_set: unbalanced parenthesis',
        ('validation: pattern #4: path: '
         'must not contain ".." or "." components: ../b'),
      ]
    )

  def test_validate_schemas(self):
    cfg = '''
      schemas {
        name: "services/config:foo"
        url: "https://foo"
      }
      schemas {
        name: "projects:foo"
        url: "https://foo"
      }
      schemas {
        name: "projects/refs:foo"
        url: "https://foo"
      }
      # Invalid schemas.
      schemas {
      }
      schemas {
        name: "services/config:foo"
        url: "https://foo"
      }
      schemas {
        name: "no_colon"
        url: "http://foo"
      }
      schemas {
        name: "bad_prefix:foo"
        url: "https://foo"
      }
      schemas {
        name: "projects:foo/../a.cfg"
        url: "https://foo"
      }
    '''
    result = validation.validate_config(
        config.self_config_set(), 'schemas.cfg', cfg)

    self.assertEqual(
        [m.text for m in result.messages],
        [
          'Schema #4: name is not specified',
          'Schema #4: url: not specified',
          'Schema services/config:foo: duplicate schema name',
          'Schema no_colon: name must contain ":"',
          'Schema no_colon: url: scheme must be "https"',
          (
            'Schema bad_prefix:foo: left side of ":" must be a service config '
            'set, "projects" or "projects/refs"'),
          (
            'Schema projects:foo/../a.cfg: '
            'must not contain ".." or "." components: foo/../a.cfg'),
        ]
    )

  def test_validate_project_metadata(self):
    cfg = '''
      name: "Chromium"
      access: "group:all"
      access: "a@a.com"
    '''
    result = validation.validate_config('projects/x', 'project.cfg', cfg)

    self.assertEqual(len(result.messages), 0)

  def test_validate_refs(self):
    cfg = '''
      refs {
        name: "refs/heads/master"
      }
      # Invalid configs
      refs {

      }
      refs {
        name: "refs/heads/master"
        config_path: "non_default"
      }
      refs {
        name: "does_not_start_with_ref"
        config_path: "../bad/path"
      }
    '''
    result = validation.validate_config('projects/x', 'refs.cfg', cfg)

    self.assertEqual(
        [m.text for m in result.messages],
        [
          'Ref #2: name is not specified',
          'Ref #3: duplicate ref: refs/heads/master',
          'Ref #4: name does not start with "refs/": does_not_start_with_ref',
          'Ref #4: must not contain ".." or "." components: ../bad/path'
        ],
    )

  def test_validation_by_service_async(self):
    cfg = '# a config'
    cfg_b64 = base64.b64encode(cfg)

    self.services = [
      service_config_pb2.Service(id='a'),
      service_config_pb2.Service(id='b'),
      service_config_pb2.Service(id='c'),
    ]

    @ndb.tasklet
    def get_metadata_async(service_id):
      if service_id == 'a':
        raise ndb.Return(service_config_pb2.ServiceDynamicMetadata(
            validation=service_config_pb2.Validator(
                patterns=[service_config_pb2.ConfigPattern(
                    config_set='services/foo',
                    path='bar.cfg',
                )],
                url='https://bar.verifier',
            )
        ))
      if service_id == 'b':
        raise ndb.Return(service_config_pb2.ServiceDynamicMetadata(
            validation=service_config_pb2.Validator(
                patterns=[service_config_pb2.ConfigPattern(
                    config_set=r'regex:projects/[^/]+',
                    path=r'regex:.+\.cfg',
                )],
                url='https://bar2.verifier',
              )))
      if service_id == 'c':
        raise ndb.Return(service_config_pb2.ServiceDynamicMetadata(
            validation=service_config_pb2.Validator(
                patterns=[service_config_pb2.ConfigPattern(
                    config_set=r'regex:.+',
                    path=r'regex:.+',
                )],
                url='https://ultimate.verifier',
              )))
      return None
    self.mock(services, 'get_metadata_async', mock.Mock())
    services.get_metadata_async.side_effect = get_metadata_async

    @ndb.tasklet
    def json_request_async(url, **kwargs):
      raise ndb.Return({
        'messages': [{
          'text': 'OK from %s' % url,
          # default severity
        }],
      })

    self.mock(
        net, 'json_request_async', mock.Mock(side_effect=json_request_async))

    ############################################################################

    result = validation.validate_config('services/foo', 'bar.cfg', cfg)
    self.assertEqual(
        result.messages,
        [
          validation_context.Message(
              text='OK from https://bar.verifier', severity=logging.INFO),
          validation_context.Message(
              text='OK from https://ultimate.verifier', severity=logging.INFO)
        ])
    net.json_request_async.assert_any_call(
      'https://bar.verifier',
      method='POST',
      payload={
        'config_set': 'services/foo',
        'path': 'bar.cfg',
        'content': cfg_b64,
      },
      scope=net.EMAIL_SCOPE,
    )
    net.json_request_async.assert_any_call(
      'https://ultimate.verifier',
      method='POST',
      payload={
        'config_set': 'services/foo',
        'path': 'bar.cfg',
        'content': cfg_b64,
      },
      scope=net.EMAIL_SCOPE,
    )

    ############################################################################

    result = validation.validate_config('projects/foo', 'bar.cfg', cfg)
    self.assertEqual(
        result.messages,
        [
          validation_context.Message(
              text='OK from https://bar2.verifier', severity=logging.INFO),
          validation_context.Message(
              text='OK from https://ultimate.verifier', severity=logging.INFO)
        ])
    net.json_request_async.assert_any_call(
      'https://bar2.verifier',
      method='POST',
      payload={
        'config_set': 'projects/foo',
        'path': 'bar.cfg',
        'content': cfg_b64,
      },
      scope=net.EMAIL_SCOPE,
    )
    net.json_request_async.assert_any_call(
      'https://ultimate.verifier',
      method='POST',
      payload={
        'config_set': 'projects/foo',
        'path': 'bar.cfg',
        'content': cfg_b64,
      },
      scope=net.EMAIL_SCOPE,
    )

    ############################################################################
    # Error found

    net.json_request_async.side_effect = None
    net.json_request_async.return_value = ndb.Future()
    net.json_request_async.return_value.set_result({
      'messages': [{
        'text': 'error',
        'severity': 'ERROR'
      }]
    })

    result = validation.validate_config('projects/baz/refs/x', 'qux.cfg', cfg)
    self.assertEqual(
        result.messages,
        [
          validation_context.Message(text='error', severity=logging.ERROR)
        ])

    ############################################################################
    # Less-expected responses

    res = {
      'messages': [
        {'severity': 'invalid severity'},
        {},
        []
      ]
    }
    net.json_request_async.return_value = ndb.Future()
    net.json_request_async.return_value.set_result(res)

    result = validation.validate_config('projects/baz/refs/x', 'qux.cfg', cfg)
    self.assertEqual(
        result.messages,
        [
          validation_context.Message(
              severity=logging.CRITICAL,
              text=(
                  'Error during external validation: invalid response: '
                  'unexpected message severity: invalid severity\n'
                  'url: https://ultimate.verifier\n'
                  'config_set: projects/baz/refs/x\n'
                  'path: qux.cfg\n'
                  'response: %r' % res),
              ),
          validation_context.Message(severity=logging.INFO, text=''),
          validation_context.Message(
              severity=logging.CRITICAL,
              text=(
                  'Error during external validation: invalid response: '
                  'message is not a dict: []\n'
                  'url: https://ultimate.verifier\n'
                  'config_set: projects/baz/refs/x\n'
                  'path: qux.cfg\n'
                  'response: %r' % res),
              ),
        ],
    )


if __name__ == '__main__':
  test_env.main()