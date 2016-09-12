# Copyright 2016 The LUCI Authors. All rights reserved.
# Use of this source code is governed under the Apache License, Version 2.0
# that can be found in the LICENSE file.

import collections
import logging
import threading
import time

from utils import auth_server

import file_reader


class AuthSystemError(Exception):
  """Fatal errors raised by AuthSystem class."""


# Parsed value of JSON at path specified by --auth-params-file task_runner arg.
AuthParams = collections.namedtuple('AuthParams', [
  # Dict with HTTP headers to use when calling Swarming backend (specifically).
  # They identify the bot to the Swarming backend. Ultimately generated by
  # 'get_authentication_headers' in bot_config.py.
  'swarming_http_headers',

  # Indicates the service account the task runs as. One of:
  #   - 'none' if the task shouldn't use any authentication at all.
  #   - 'bot' if the task should use bot's own service account.
  #   - <email> if the task is using service acccount via delegation token.
  'task_service_account',
])


def prepare_auth_params_json(bot, manifest):
  """Returns a dict to put into JSON file passed to task_runner.

  This JSON file contains various tokens and configuration parameters that allow
  task_runner to make HTTP calls authenticated by bot's own credentials.

  The file is managed by bot_main.py (main Swarming bot process) and consumed by
  task_runner.py.

  It lives it the task work directory.

  Args:
    bot: instance of bot.Bot.
    manifest: dict with the task manifest, as generated by the backend in /poll.
  """
  return {
    'swarming_http_headers': bot.remote.get_authentication_headers(),
    'task_service_account': manifest.get('service_account') or 'none',
  }


def process_auth_params_json(val):
  """Takes a dict loaded from auth params JSON file and validates it.

  Args:
    val: decoded JSON value read from auth params JSON file.

  Returns:
    AuthParams tuple.

  Raises:
    ValueError if val has invalid format.
  """
  if not isinstance(val, dict):
    raise ValueError('Expecting dict, got %r' % (val,))

  headers = val.get('swarming_http_headers') or {}
  if not isinstance(headers, dict):
    raise ValueError(
        'Expecting "swarming_http_headers" to be dict, got %r' % (headers,))

  # The headers must be ASCII for sure, so don't bother with picking the
  # correct unicode encoding, default would work. If not, it'll raise
  # UnicodeEncodeError, which is subclass of ValueError.
  headers = {str(k): str(v) for k, v in headers.iteritems()}

  acc = val.get('task_service_account') or 'none'
  if not isinstance(acc, basestring):
    raise ValueError(
        'Expecting "task_service_account" to be a string, got %r' % (acc,))

  return AuthParams(headers, str(acc))


class AuthSystem(object):
  """Authentication subsystem used by task_runner.

  Contains two threads:
    * One thread periodically rereads the file with bots own authentication
      information (auth_params_file). This file is generated by bot_main.
    * Another thread hosts local HTTP server that servers authentication tokens
      to local processes. This is enabled only if the task is running in a
      context of some service account (as specified by 'service_account_token'
      parameter supplied when the task was created).

  The local HTTP server exposes /rpc/LuciLocalAuthService.GetOAuthToken
  endpoint that the processes running inside Swarming tasks can use to request
  an OAuth access token associated with the task.

  They can discover the port to connect to by looking at LUCI_CONTEXT
  environment variable. This variables is only set if the task is running in
  the context of some service account.

  TODO(vadimsh): Actually implement LUCI_CONTEXT env var.
  """

  def __init__(self, auth_params_file):
    self._auth_params_file = auth_params_file
    self._auth_params_reader = None
    self._local_server = None
    self._local_auth_context = None
    self._lock = threading.Lock()

  def __enter__(self):
    self.start()
    return self

  def __exit__(self, _exc_type, _exc_val, _exc_tb):
    self.stop()
    return False

  def start(self):
    """Grabs initial bot auth headers and starts all auth related threads.

    Raises:
      AuthSystemError on fatal errors.
    """
    assert not self._auth_params_reader, 'already running'
    try:
      # Read headers more often than bot_main writes them (which is 60 sec), to
      # reduce maximum possible latency between header updates and reads. Use
      # interval that isn't a divisor of 60 to avoid reads and writes happening
      # at the same moment in time.
      reader = file_reader.FileReaderThread(
          self._auth_params_file, interval_sec=53)
      reader.start()
    except file_reader.FatalReadError as e:
      raise AuthSystemError('Cannot start FileReaderThread: %s' % e)

    # Initial validation.
    try:
      params = process_auth_params_json(reader.last_value)
    except ValueError as e:
      reader.stop()
      raise AuthSystemError('Cannot parse bot_auth_params.json: %s' % e)

    # If using task auth, launch local HTTP server that serves tokens (let OS
    # assign the port).
    server = None
    local_auth_context = None
    if params.task_service_account != 'none':
      try:
        server = auth_server.LocalAuthServer()
        local_auth_context = server.start(token_provider=self)
      except Exception as exc:
        reader.stop() # cleanup
        raise AuthSystemError('Failed to start local auth server - %s' % exc)

    # Good to go.
    with self._lock:
      self._auth_params_reader = reader
      self._local_server = server
      self._local_auth_context = local_auth_context

  def stop(self):
    """Shuts down all the threads if they are running."""
    with self._lock:
      reader, self._auth_params_reader = self._auth_params_reader, None
      server, self._local_server = self._local_server, None
      self._local_auth_context = None
    if server:
      server.stop()
    if reader:
      reader.stop()

  @property
  def bot_headers(self):
    """A dict with HTTP headers that contain bots own credentials.

    Such headers can be sent to Swarming server's /bot/* API. Must be used only
    after 'start' and before 'stop'.

    Raises:
      AuthSystemError if auth_params_file is suddenly no longer valid.
    """
    with self._lock:
      assert self._auth_params_reader, '"start" was not called'
      raw_val = self._auth_params_reader.last_value
    try:
      val = process_auth_params_json(raw_val)
      return val.swarming_http_headers
    except ValueError as e:
      raise AuthSystemError('Cannot parse bot_auth_params.json: %s' % e)

  @property
  def local_auth_context(self):
    """A dict with parameters of local auth server to put into LUCI_CONTEXT.

    None if the task is not using service accounts.

    Format:
    {
      'rpc_port': <int with port number>,
      'secret': <str with a random string to send with RPCs>,
    }
    """
    with self._lock:
      return self._local_auth_context

  def generate_token(self, scopes):
    """Generates a new access token with given scopes.

    Called by LocalAuthServer from some internal thread whenever new token is
    needed. See TokenProvider for more details.

    Returns:
      AccessToken.

    Raises:
      RPCError, TokenError, AuthSystemError.
    """
    # Grab AuthParams supplied by the main bot process.
    with self._lock:
      if not self._auth_params_reader:
        raise auth_server.RPCError(503, 'Stopped already.')
      val = self._auth_params_reader.last_value
    params = process_auth_params_json(val)

    logging.info(
        'Getting the token for "%s", scopes %s', params.task_service_account,
        scopes)

    # This shouldn't happen, since the local HTTP server isn't actually
    # running in this case. But handle anyway, for completeness.
    if params.task_service_account == 'none':
      raise auth_server.TokenError(1, 'The task is not using service accounts')

    # This works only for bots that use OAuth for authentication (e.g. GCE
    # bots). It will raise TokenError if the bot is not using OAuth.
    if params.task_service_account == 'bot':
      return self._grab_bot_oauth_token(params)

    # Using some custom service account.
    # TODO(vadimsh): Implement. This would involve sending a request to Swarming
    # backend to generate the token.
    raise auth_server.TokenError(3, 'Not implemented yet')

  def _grab_bot_oauth_token(self, auth_params):
    # Piggyback on the bot own credentials for now. This works only for bots
    # that use OAuth for authentication (e.g. GCE bots). Also it totally ignores
    # scopes or expiration time. It relies on bot_main to keep the bot OAuth
    # token sufficiently fresh. See remote_client.AUTH_HEADERS_EXPIRATION_SEC.
    bot_auth_hdr = auth_params.swarming_http_headers.get('Authorization') or ''
    if not bot_auth_hdr.startswith('Bearer '):
      raise auth_server.TokenError(2, 'The bot is not using OAuth', fatal=True)
    tok = bot_auth_hdr[len('Bearer '):]

    # bot_main guarantees swarming_http_headers are usable for at least 6 min.
    # (see AUTH_HEADERS_EXPIRATION_SEC). task_runner grabs these headers from
    # bot_main asynchronously with some delay. To account for that delay make
    # expiration time shorter (4 min instead of 6 min).
    #
    # TODO(vadimsh): The real token expiration time can be passed from
    # bot_main.py via --auth-params-file mechanism (same way as
    # 'swarming_http_headers' are passed).
    #
    # TODO(vadimsh): For GCE bots specifically we can pass a list of OAuth
    # scopes granted to the GCE token and verify it contains all the requested
    # scopes.
    return auth_server.AccessToken(tok, int(time.time()) + 4*60)
